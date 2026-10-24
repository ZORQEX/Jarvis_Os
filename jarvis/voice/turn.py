"""Has the person finished speaking? Smart Turn v3.2 (Pipecat / Daily, BSD-2-Clause): a Whisper-tiny based 8 MB model
that hears the intonation at the end of a phrase — «Отправь маме в…» trailing off vs. a finished «…который час?».
93.5 % accurate on Russian in its authors' benchmark, ~10–60 ms on a CPU. Pure numpy + onnxruntime: the speech worker
loads this file by path (no import from JARVIS, no `transformers`); the features match Whisper's extractor.

Input: up to the last 8 s of 16 kHz mono audio. Output: probability that the turn is complete."""
from __future__ import annotations

import numpy as np

SR = 16000
N_FFT, HOP, N_MELS, SECONDS = 400, 160, 80, 8
N_SAMPLES = SR * SECONDS


def _hz_to_mel(f):
    f = np.asarray(f, dtype=np.float64)
    mel = f / (200.0 / 3)
    log_step = np.log(6.4) / 27.0
    return np.where(f >= 1000.0, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / log_step, mel)


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    log_step = np.log(6.4) / 27.0
    return np.where(m >= 15.0, 1000.0 * np.exp(log_step * (m - 15.0)), m * (200.0 / 3))


def mel_filters() -> np.ndarray:
    """(201, 80) Slaney-scale, Slaney-normalised triangular filters — the ones Whisper uses."""
    fft_freqs = np.linspace(0, SR / 2, 1 + N_FFT // 2)
    pts = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(SR / 2), N_MELS + 2))
    diff = np.diff(pts)
    slopes = pts[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / diff[:-1]
    up = slopes[:, 2:] / diff[1:]
    fb = np.maximum(0, np.minimum(down, up))
    fb *= 2.0 / (pts[2:N_MELS + 2] - pts[:N_MELS])
    return fb.astype(np.float32)


_FILTERS = mel_filters()
_WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)


def features(audio: np.ndarray) -> np.ndarray:
    """Whisper log-mel features (80 × 800) of the last 8 s, zero-padded at the start, normalised like the original."""
    a = np.asarray(audio, dtype=np.float32).reshape(-1)[-N_SAMPLES:]
    a = np.concatenate([np.zeros(N_SAMPLES - a.size, dtype=np.float32), a])
    # Normalised over the whole 8 s, padding included — the way the model was fed (checked: normalising only the
    # speech, or padding at the end, makes it call almost every phrase complete).
    a = (a - a.mean()) / np.sqrt(a.var() + 1e-7)
    padded = np.pad(a, N_FFT // 2, mode="reflect")
    n = 1 + (padded.size - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(padded[idx] * _WINDOW, axis=1)) ** 2          # (frames, 201)
    mel = spec[:-1] @ _FILTERS                                               # drop the last frame, like Whisper
    log = np.log10(np.maximum(mel, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    # (80, 800), contiguous: onnxruntime reads a transposed view's memory in the wrong order (measured: 0.51 vs 0.11)
    return np.ascontiguousarray(((log + 4.0) / 4.0).T, dtype=np.float32)


def trim_tail(audio: np.ndarray, keep: float = 0.2) -> np.ndarray:
    """Leave 0.2 s after the last speech. The model was trained to answer ~0.2 s into a pause; with the 0.6–1.3 s of
    silence JARVIS's phrases end with it calls almost every cut-off phrase finished (measured: «Готово. Я открыл папку
    с лабораторными и нашёл…» 0.008 with 0.2 s of silence, 0.42 with 1.3 s)."""
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    n = a.size // 320
    if n < 3:
        return a
    rms = np.sqrt((a[: n * 320].reshape(n, 320) ** 2).mean(axis=1))
    voiced = np.nonzero(rms > max(0.004, rms.max() * 0.08))[0]
    if not voiced.size:
        return a
    return a[: min(a.size, (voiced[-1] + 1) * 320 + int(SR * keep))]


class TurnModel:
    def __init__(self, path: str, threads: int = 1):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.session = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])

    def complete(self, audio: np.ndarray) -> float:
        out = self.session.run(None, {"input_features": np.ascontiguousarray(features(trim_tail(audio))[None])})[0]
        p = float(np.asarray(out).reshape(-1)[0])
        return p if 0.0 <= p <= 1.0 else float(1 / (1 + np.exp(-p)))         # a probability (or a logit)
