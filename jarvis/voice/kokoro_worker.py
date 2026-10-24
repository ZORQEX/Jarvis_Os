"""Kokoro TTS worker — runs in its own virtualenv (~/.local/share/jarvis/kokoro), never imported by JARVIS.

Kokoro-82M (Apache-2.0) via kokoro-onnx, int8 model, on the CPU: English (British and American voices), Spanish,
French, Italian, Portuguese. Measured on this laptop: ~1.2 s for a 3.3 s English sentence; a round trip through the
speech recogniser gives the same words back («Done, I've sent the message to your mom on WhatsApp.»).

Protocol (one JSON object per line):
  stdin  {"id": "...", "text": "...", "voice": "bm_george", "lang": "en-gb", "rate": 1.0}
  stdout {"id": "...", "wav_b64": "...", "ms": 123}   or   {"id": "...", "error": "..."}
"""
import base64
import io
import json
import os
import re
import sys
import time
import wave

import numpy as np
from kokoro_onnx import Kokoro

SR = 24000


def main() -> None:
    home = sys.argv[1]
    import onnxruntime as ort
    so = ort.SessionOptions()                 # threads that sleep when idle (spinning ones cost 4× the CPU here)
    so.intra_op_num_threads = int(os.environ.get("JARVIS_TTS_THREADS", "4"))
    so.inter_op_num_threads = 1
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")
    sess = ort.InferenceSession(os.path.join(home, "models", "kokoro-v1.0.int8.onnx"), sess_options=so,
                                providers=["CPUExecutionProvider"])
    model = Kokoro.from_session(sess, os.path.join(home, "models", "voices-v1.0.bin"))
    model.create("Ready.", voice="bm_george", lang="en-gb")            # warm-up: the first phrase is not slower
    print(json.dumps({"ready": True, "voices": len(model.get_voices())}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            t0 = time.perf_counter()
            text = re.sub(r"\s+", " ", str(req.get("text", ""))).strip()
            if not re.search(r"\w", text):
                raise ValueError("empty text")
            samples, sr = model.create(text, voice=req.get("voice") or "bm_george", lang=req.get("lang") or "en-gb",
                                       speed=max(0.6, min(1.6, float(req.get("rate") or 1.0))))
            pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes(pcm)
            print(json.dumps({"id": rid, "wav_b64": base64.b64encode(buf.getvalue()).decode(),
                              "ms": round((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as e:   # noqa: BLE001
            print(json.dumps({"id": rid, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}), flush=True)


if __name__ == "__main__":
    main()
