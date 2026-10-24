"""Silero TTS worker — runs in its own virtualenv (~/.local/share/jarvis/silero), never imported by JARVIS.

Languages: Russian (v5_2_ru, loaded at start), German (v3_de) and Ukrainian (v4_ua), loaded on their first phrase.

Protocol (one JSON object per line):
  stdin  {"id": "...", "text": "...", "speaker": "xenia", "rate": 1.0, "lang": "ru"}
  stdout {"id": "...", "wav_b64": "...", "ms": 123}   or   {"id": "...", "error": "..."}
The audio goes back inside the line itself: no temporary files to leak.
The Russian and Ukrainian models read only Cyrillic, so numbers, «%» and Latin words (brand names inside a Russian
sentence) are spelled out here first; whole foreign sentences go to another voice (jarvis/voice/lang.py).
Model licence: Silero (snakers4/silero-models) — non-commercial use; see the model card.
"""
import base64
import io
import json
import os
import re
import sys
import time
import wave

import torch

SR = 24000


def _load_textnorm():
    """textnorm.py next to this file, loaded by path (the folder is not put on sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("jarvis_textnorm", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                                 "textnorm.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


normalize = _load_textnorm().normalize
MODELS = {"ru": "v5_2_ru.pt", "de": "v3_de.pt", "uk": "v4_ua.pt"}


def chunks(text: str, limit: int = 800) -> list[str]:
    parts, cur = [], ""
    for s in re.split(r"(?<=[.!?…])\s+", text):
        if len(cur) + len(s) > limit and cur:
            parts.append(cur)
            cur = ""
        cur = (cur + " " + s).strip()
    if cur:
        parts.append(cur)
    return parts


def load(path: str):
    model = torch.package.PackageImporter(path).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    return model


def main() -> None:
    torch.set_num_threads(max(1, min(6, os.cpu_count() or 4)))
    model_path = sys.argv[1]
    models = {"ru": load(model_path)}
    print(json.dumps({"ready": True, "speakers": list(getattr(models["ru"], "speakers", []))}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            t0 = time.perf_counter()
            lang = req.get("lang") if req.get("lang") in MODELS else "ru"
            if lang not in models:
                path = os.path.join(os.path.dirname(model_path), MODELS[lang])
                if not os.path.exists(path):
                    raise FileNotFoundError(f"нет голоса для языка {lang}")
                models[lang] = load(path)
            model = models[lang]
            text = normalize(req.get("text", ""), lang)
            if not re.search(r"\w", text):
                raise ValueError("пустой текст")
            rate = float(req.get("rate") or 1.0)
            pieces = []
            for part in chunks(text):
                if abs(rate - 1.0) > 0.02:
                    pct = int(round(rate * 100))
                    audio = model.apply_tts(ssml_text=f'<speak><prosody rate="{pct}%">{part}</prosody></speak>',
                                            speaker=req.get("speaker") or "xenia", sample_rate=SR)
                else:
                    extra = {"put_accent": True, "put_yo": True} if lang == "ru" else {}
                    audio = model.apply_tts(text=part, speaker=req.get("speaker") or "xenia", sample_rate=SR, **extra)
                pieces.append(audio)
            wavdata = torch.cat(pieces).clamp(-1, 1).mul(32767).to(torch.int16).numpy().tobytes()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(wavdata)
            print(json.dumps({"id": rid, "wav_b64": base64.b64encode(buf.getvalue()).decode(),
                              "ms": round((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as e:   # noqa: BLE001
            print(json.dumps({"id": rid, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}), flush=True)


if __name__ == "__main__":
    main()
