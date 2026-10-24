"""Supertonic 3 TTS worker — runs in its own virtualenv (~/.local/share/jarvis/supertonic), never imported by JARVIS.

One voice for every language JARVIS speaks (31 in the model: ru, en, uk, de, es, fr, it, pt, pl, …), ~99M parameters,
ONNX on the CPU. Chosen on 2026-10-24 after measuring on this laptop (i5-1235U) against the voices JARVIS had:
naturalness (UTMOS, 8 test phrases in 6 languages) 3.95 for the male voice M4 vs 2.98 for Silero + Kokoro (Russian 3.94
vs 2.98, Ukrainian 4.01 vs 1.82, English 4.49 vs 4.12); the recogniser hears its Russian, English, German and Spanish
back word for word; ~0.35 s of compute per second of speech. Model: OpenRAIL-M (Supertone), code MIT.

Protocol (one JSON object per line):
  stdin  {"id": "...", "text": "...", "voice": "M4", "lang": "ru", "rate": 1.0}
  stdout {"id": "...", "wav_b64": "...", "ms": 123}   or   {"id": "...", "error": "..."}
"""
import base64
import importlib.util
import io
import json
import os
import re
import sys
import time
import wave

import numpy as np
import onnxruntime as ort

THREADS = int(os.environ.get("JARVIS_TTS_THREADS", "2"))
LANGS = {"ru", "en", "uk", "de", "es", "fr", "it", "pt", "pl", "cs", "nl", "sv", "tr", "ko", "ja", "ar", "bg", "da",
         "el", "et", "fi", "hi", "hr", "hu", "id", "lt", "lv", "ro", "sk", "sl", "vi"}


class _Session(ort.InferenceSession):
    """Two threads that sleep when idle. More threads were slower on this laptop's mixed P/E cores, and spinning
    threads cost 4–6× the CPU for the same speed (measured: 36 vs 164 CPU-seconds for the same phrases)."""

    def __init__(self, path, sess_options=None, **kw):
        so = sess_options or ort.SessionOptions()
        so.intra_op_num_threads = THREADS
        so.inter_op_num_threads = 1
        try:
            so.get_session_config_entry("session.intra_op.allow_spinning")
        except Exception:   # noqa: BLE001 — not set yet (the loader shares one options object between sessions)
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        super().__init__(path, sess_options=so, **kw)


ort.InferenceSession = _Session            # the Supertonic loader builds its sessions through this name

from supertonic import TTS  # noqa: E402


def _textnorm():
    spec = importlib.util.spec_from_file_location(
        "jarvis_textnorm", os.path.join(os.path.dirname(os.path.abspath(__file__)), "textnorm.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


normalize = _textnorm().normalize


def wav_bytes(samples: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(np.asarray(samples).reshape(-1), -1, 1) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def main() -> None:
    tts = TTS(model="supertonic-3", model_dir=sys.argv[1], auto_download=False)
    styles = {}

    def style(name: str):
        name = name if name in tts.voice_style_names else "M4"
        if name not in styles:
            styles[name] = tts.get_voice_style(name)
        return styles[name]

    for lang, text in (("ru", "Готово."), ("en", "Done.")):           # warm-up: the first answer is not slower
        tts.synthesize(text, voice_style=style("M4"), lang=lang)
    print(json.dumps({"ready": True, "voices": tts.voice_style_names, "sr": tts.sample_rate}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            t0 = time.perf_counter()
            lang = req.get("lang") if req.get("lang") in LANGS else "na"
            text = normalize(re.sub(r"\s+", " ", str(req.get("text", ""))).strip(), lang if lang != "na" else "en",
                             translit=False)
            if not re.search(r"\w", text):
                raise ValueError("empty text")
            speed = 1.05 * max(0.6, min(1.6, float(req.get("rate") or 1.0)))
            wav, _ = tts.synthesize(text, voice_style=style(str(req.get("voice") or "M4")), lang=lang, speed=speed)
            print(json.dumps({"id": rid, "wav_b64": base64.b64encode(wav_bytes(wav, tts.sample_rate)).decode(),
                              "ms": round((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as e:   # noqa: BLE001
            print(json.dumps({"id": rid, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}), flush=True)


if __name__ == "__main__":
    main()
