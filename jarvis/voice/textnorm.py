"""Text → words a voice can read: numbers in words, symbols in words, and (for the Silero voices, which only know
Cyrillic) English words in Russian letters. Pure Python + num2words; the voice workers load this file by path from
their own virtualenvs, so it imports nothing from JARVIS."""
import re

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
NUM_LANG = {"ru": "ru", "de": "de", "uk": "uk", "en": "en", "es": "es", "fr": "fr", "it": "it", "pt": "pt"}
DECIMAL = {"ru": " целых ", "uk": " цілих ", "de": " Komma ", "en": " point ", "es": " coma ", "fr": " virgule ",
           "it": " virgola ", "pt": " vírgula "}


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
    from num2words import num2words
    s = re.sub(r"\s", "", m.group(0))
    nl = NUM_LANG.get(lang, "ru")
    try:
        if "," in s or "." in s:
            whole, frac = re.split(r"[.,]", s, maxsplit=1)
            return num2words(int(whole), lang=nl) + DECIMAL[nl] + num2words(int(frac), lang=nl)
        return num2words(int(s), lang=nl)
    except (ValueError, OverflowError, NotImplementedError, ImportError):
        return s


SYMBOLS = {"ru": {"%": " процентов", "₽": " рублей", "$": " долларов", "€": " евро", "№": "номер ", "+": " плюс ",
                  "=": " равно ", "&": " и "},
           "uk": {"%": " відсотків", "₽": " рублів", "$": " доларів", "€": " євро", "№": "номер ", "+": " плюс ",
                  "=": " дорівнює ", "&": " і "},
           "de": {"%": " Prozent", "€": " Euro", "$": " Dollar", "+": " plus ", "=": " gleich ", "&": " und "},
           "en": {"%": " percent", "€": " euros", "$": " dollars", "₽": " rubles", "+": " plus ", "=": " equals ",
                  "&": " and "},
           "es": {"%": " por ciento", "€": " euros", "$": " dólares", "+": " más ", "=": " igual a ", "&": " y "},
           "fr": {"%": " pour cent", "€": " euros", "$": " dollars", "+": " plus ", "=": " égale ", "&": " et "},
           "it": {"%": " per cento", "€": " euro", "$": " dollari", "+": " più ", "=": " uguale ", "&": " e "},
           "pt": {"%": " por cento", "€": " euros", "$": " dólares", "+": " mais ", "=": " igual a ", "&": " e "}}


def normalize(text: str, lang: str = "ru", translit: bool = True) -> str:
    """`translit`: write English words in Russian letters inside Russian / Ukrainian text (Silero needs it; Supertonic
    reads «YouTube» or «WhatsApp» in a Russian sentence correctly by itself — checked by recognising it back)."""
    t = text
    for a, b in SYMBOLS.get(lang, SYMBOLS["ru"]).items():
        t = t.replace(a, b)
    t = re.sub(r"\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?", lambda m: number(m, lang), t)
    if translit and lang in ("ru", "uk"):
        t = re.sub(r"[A-Za-z][A-Za-z'\-]*", lambda m: latin_word(m.group(0)), t)
    t = re.sub(r"[^\w\s.,!?…:;—–\-«»\"()]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
