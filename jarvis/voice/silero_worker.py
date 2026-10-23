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
from num2words import num2words

SR = 24000
LATIN = {
    "jarvis": "джарвис", "telegram": "телеграм", "whatsapp": "вотсап", "whats app": "вотсап", "gemini": "джемини",
    "claude": "клод", "code": "код", "google": "гугл", "gmail": "джимейл", "email": "имейл", "e-mail": "имейл",
    "api": "эй пи ай", "ai": "эй ай", "ok": "окей", "wi-fi": "вай фай", "wifi": "вай фай", "youtube": "ютуб",
    "omarchy": "омарчи", "linux": "линукс", "hyprland": "хайпрленд", "spacex": "спейс икс", "tesla": "тесла",
    "iphone": "айфон", "android": "андроид", "windows": "виндоус", "chrome": "хром", "pdf": "пэ дэ эф",
    "excel": "эксель", "word": "ворд", "zoom": "зум", "instagram": "инстаграм", "x": "икс", "id": "ай ди",
    "music": "мьюзик", "facebook": "фейсбук", "spotify": "спотифай", "github": "гитхаб", "apple": "эпл", "app": "эпп",
    "store": "стор", "play": "плей", "macbook": "макбук", "microsoft": "майкрософт", "skype": "скайп", "steam": "стим",
    "discord": "дискорд", "twitch": "твич", "tiktok": "тикток", "reddit": "реддит", "amazon": "амазон",
    "anthropic": "антропик", "nvidia": "энвидиа",
    "firefox": "файрфокс", "safari": "сафари", "python": "пайтон", "javascript": "джаваскрипт", "docker": "докер",
    "bluetooth": "блютуз", "online": "онлайн", "offline": "офлайн", "slack": "слак", "notion": "ноушн",
    "figma": "фигма", "photoshop": "фотошоп", "powerpoint": "пауэрпоинт", "outlook": "аутлук", "dropbox": "дропбокс",
    "netflix": "нетфликс", "kokoro": "кокоро", "silero": "силеро", "linkedin": "линкедин", "pinterest": "пинтерест",
    "the": "зе", "and": "энд", "new": "нью", "pro": "про", "max": "макс", "mini": "мини", "web": "веб",
}
# English spelling → Russian sounds, longest first. The old letter table was shifted by two letters («o» → «н»):
# «Done» came out as «днме» (measured 2026-10-24).
TRANSLIT = [("tion", "шн"), ("sion", "жн"), ("ough", "оу"), ("igh", "ай"), ("sch", "ш"), ("sh", "ш"), ("ch", "ч"),
            ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("th", "т"), ("ph", "ф"), ("wh", "у"), ("ck", "к"), ("qu", "кв"),
            ("ee", "и"), ("ea", "и"), ("oo", "у"), ("ou", "ау"), ("ow", "оу"), ("ay", "эй"), ("ai", "эй"),
            ("ey", "ей"), ("oy", "ой"), ("ya", "я"), ("yo", "йо"), ("yu", "ю"), ("ye", "е"), ("ce", "се"),
            ("ci", "си"), ("cy", "си"), ("x", "кс"), ("j", "дж"), ("w", "у")]
LETTERS = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "k": "к",
           "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
           "v": "в", "y": "и", "z": "з"}
MODELS = {"ru": "v5_2_ru.pt", "de": "v3_de.pt", "uk": "v4_ua.pt"}
NUM_LANG = {"ru": "ru", "de": "de", "uk": "uk"}


def latin_word(w: str) -> str:
    lw = w.lower()
    if lw in LATIN:
        return LATIN[lw]
    if len(w) <= 4 and w.isupper():                       # abbreviations: letter by letter
        names = {"a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч", "i": "ай",
                 "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "оу", "p": "пи", "q": "кью", "r": "ар",
                 "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл ю", "x": "икс", "y": "уай", "z": "зед"}
        return " ".join(names.get(c, "") for c in lw)
    out = lw[:-1] if len(lw) > 3 and lw.endswith("e") and lw[-2] not in "aeiouy" else lw    # silent final «e»
    for a, b in TRANSLIT:
        out = out.replace(a, b)
    return "".join(LETTERS.get(c, c) for c in out)


def number(m: re.Match, lang: str = "ru") -> str:
    s = re.sub(r"\s", "", m.group(0))
    nl = NUM_LANG.get(lang, "ru")
    try:
        if "," in s or "." in s:
            whole, frac = re.split(r"[.,]", s, maxsplit=1)
            sep = {"ru": " целых ", "uk": " цілих ", "de": " Komma "}[nl]
            return num2words(int(whole), lang=nl) + sep + num2words(int(frac), lang=nl)
        return num2words(int(s), lang=nl)
    except (ValueError, OverflowError, NotImplementedError):
        return s


SYMBOLS = {"ru": {"%": " процентов", "₽": " рублей", "$": " долларов", "€": " евро", "№": "номер ", "+": " плюс ",
                  "=": " равно ", "&": " и "},
           "uk": {"%": " відсотків", "₽": " рублів", "$": " доларів", "€": " євро", "№": "номер ", "+": " плюс ",
                  "=": " дорівнює ", "&": " і "},
           "de": {"%": " Prozent", "€": " Euro", "$": " Dollar", "+": " plus ", "=": " gleich ", "&": " und "}}


def normalize(text: str, lang: str = "ru") -> str:
    t = text
    for a, b in SYMBOLS.get(lang, SYMBOLS["ru"]).items():
        t = t.replace(a, b)
    t = re.sub(r"\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?", lambda m: number(m, lang), t)
    if lang in ("ru", "uk"):
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
