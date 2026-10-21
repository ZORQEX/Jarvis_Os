"""Silero TTS worker — runs in its own virtualenv (~/.local/share/jarvis/silero), never imported by JARVIS.

Protocol (one JSON object per line):
  stdin  {"id": "...", "text": "...", "speaker": "xenia", "rate": 1.0}
  stdout {"id": "...", "path": "/tmp/....wav", "ms": 123}   or   {"id": "...", "error": "..."}
Silero reads only Cyrillic words, so numbers, «%» and Latin words are spelled out here first.
Model licence: Silero (snakers4/silero-models) — non-commercial use; see the model card.
"""
import json
import os
import re
import sys
import tempfile
import time
import wave

import torch
from num2words import num2words

SR = 24000
LATIN = {
    "jarvis": "джарвис", "telegram": "телеграм", "whatsapp": "вотсап", "whats app": "вотсап", "gemini": "джемини",
    "claude": "клод", "code": "код", "google": "гугл", "gmail": "джимейл", "email": "имейл", "e-mail": "имейл",
    "api": "эй пи ай", "ai": "эй ай", "ok": "окей", "wi-fi": "вай фай", "wifi": "вай фай", "youtube": "ютуб",
    "omarchy": "омарчи", "linux": "линукс", "hyprland": "хайпрленд", "spacex": "спейс икс", "tesla": "тесла",
    "iphone": "айфон", "android": "андроид", "windows": "виндоус", "chrome": "хром", "pdf": "пэ дэ эф",
    "excel": "эксель", "word": "ворд", "zoom": "зум", "instagram": "инстаграм", "x": "икс", "id": "ай ди",
}
TRANSLIT = [("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"),
            ("ee", "и"), ("oo", "у"), ("ph", "ф"), ("th", "т"), ("ck", "к"), ("qu", "кв")]
LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz", "абкдефгхиджклмнопкрстуввксиз"))


def latin_word(w: str) -> str:
    lw = w.lower()
    if lw in LATIN:
        return LATIN[lw]
    if len(w) <= 4 and w.isupper():                       # abbreviations: letter by letter
        names = {"a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч", "i": "ай",
                 "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "оу", "p": "пи", "q": "кью", "r": "ар",
                 "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл ю", "x": "икс", "y": "уай", "z": "зед"}
        return " ".join(names.get(c, "") for c in lw)
    out = lw
    for a, b in TRANSLIT:
        out = out.replace(a, b)
    return "".join(LETTERS.get(c, c) for c in out)


def number(m: re.Match) -> str:
    s = m.group(0).replace(" ", "").replace(" ", "").replace(" ", "")
    try:
        if "," in s or "." in s:
            whole, frac = re.split(r"[.,]", s, 1)
            return num2words(int(whole), lang="ru") + " целых " + num2words(int(frac), lang="ru")
        return num2words(int(s), lang="ru")
    except (ValueError, OverflowError):
        return s


def normalize(text: str) -> str:
    t = text.replace("%", " процентов").replace("₽", " рублей").replace("$", " долларов").replace("€", " евро")
    t = t.replace("№", "номер ").replace("+", " плюс ").replace("=", " равно ").replace("&", " и ")
    t = re.sub(r"\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?", number, t)
    t = re.sub(r"[A-Za-z][A-Za-z'\-]*", lambda m: latin_word(m.group(0)), t)
    t = re.sub(r"[^\w\s.,!?…:;—–\-«»\"()]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


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


def main() -> None:
    torch.set_num_threads(max(1, min(6, os.cpu_count() or 4)))
    model_path = sys.argv[1]
    model = torch.package.PackageImporter(model_path).load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    outdir = tempfile.mkdtemp(prefix="jarvis-silero-")
    print(json.dumps({"ready": True, "speakers": list(getattr(model, "speakers", []))}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            t0 = time.perf_counter()
            text = normalize(req.get("text", ""))
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
                    audio = model.apply_tts(text=part, speaker=req.get("speaker") or "xenia", sample_rate=SR,
                                            put_accent=True, put_yo=True)
                pieces.append(audio)
            wavdata = torch.cat(pieces).clamp(-1, 1).mul(32767).to(torch.int16).numpy().tobytes()
            path = os.path.join(outdir, f"{rid}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(wavdata)
            print(json.dumps({"id": rid, "path": path, "ms": round((time.perf_counter() - t0) * 1000)}), flush=True)
        except Exception as e:   # noqa: BLE001
            print(json.dumps({"id": rid, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}), flush=True)


if __name__ == "__main__":
    main()
