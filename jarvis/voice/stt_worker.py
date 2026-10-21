"""Local speech recognition worker — runs in its own virtualenv (~/.local/share/jarvis/stt), never imported by JARVIS.

Model: GigaAM v3 e2e RNN-T (Sber, MIT licence), int8 build, via onnx-asr/onnxruntime on the CPU. It writes
punctuation, digits and Latin brand names itself. Measured on this laptop: ~0.3–0.5 s for a command, ~520 MB RAM.

Protocol (one JSON object per line):
  stdin  {"id": "...", "path": "/tmp/x.wav"}          16 kHz mono 16-bit WAV, deleted by JARVIS afterwards
  stdout {"id": "...", "text": "...", "ms": 123}      or {"id": "...", "error": "..."}
"""
import json
import os
import sys
import time
import wave

os.environ.setdefault("HF_HUB_OFFLINE", "1")          # the model is already downloaded: never call the network

import numpy as np  # noqa: E402
import onnx_asr  # noqa: E402

MODEL = os.environ.get("JARVIS_STT_MODEL", "gigaam-v3-e2e-rnnt")


def load_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("нужен WAV 16 бит, моно")
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    if rate != 16000:                                    # simple linear resample (the window always sends 16 kHz)
        n = int(len(a) * 16000 / rate)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


def main() -> None:
    # int8 first: same transcripts as the full model on our 20-command test set, 523 MB RAM instead of 1417 MB,
    # slightly faster (measured). The full model is used only if the int8 files are not downloaded.
    quant = "int8"
    try:
        model = onnx_asr.load_model(MODEL, quantization="int8")
    except Exception:   # noqa: BLE001
        model, quant = onnx_asr.load_model(MODEL), "fp32"
    # Warm up on several lengths: onnxruntime prepares buffers per input size (measured: first phrases 2.1/1.4 s with
    # a 1 s silence warm-up, 0.5/0.4 s after this one; costs ~4 s once, in the background at JARVIS start).
    rng = np.random.default_rng(0)
    for sec in (1, 3, 6):
        model.recognize(rng.normal(0, 0.05, 16000 * sec).astype(np.float32))
    print(json.dumps({"ready": True, "model": MODEL, "quantization": quant}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            t0 = time.perf_counter()
            audio = load_wav(req["path"])
            text = model.recognize(audio) if len(audio) > 1600 else ""
            print(json.dumps({"id": rid, "text": (text or "").strip(), "ms": round((time.perf_counter() - t0) * 1000)},
                             ensure_ascii=False), flush=True)
        except Exception as e:   # noqa: BLE001
            print(json.dumps({"id": rid, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}), flush=True)


if __name__ == "__main__":
    main()
