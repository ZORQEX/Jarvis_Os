"""Which language a piece of text is in — local, instant, no model. Used to pick the voice for each sentence (the Russian
voice cannot read English: it spelled «Done. I've sent…» as «днме. и уе ремс…», measured 2026-10-24), the language of
JARVIS's own short phrases («Секунду» / «One moment»), and to tell the speech recogniser's output apart.

Languages JARVIS speaks and hears on this computer: ru (Silero), en/es/fr/it/pt (Kokoro), de/uk (Silero)."""
from __future__ import annotations

import re

SPOKEN = ("ru", "en", "uk", "de", "es", "fr", "it", "pt")
NAMES = {"ru": "русский", "en": "английский", "uk": "украинский", "de": "немецкий", "es": "испанский",
         "fr": "французский", "it": "итальянский", "pt": "португальский"}

CYR = re.compile(r"[а-яёіїєґ]", re.I)
LAT = re.compile(r"[a-zà-ÿœß]", re.I)
UK_ONLY = re.compile(r"[іїєґ]", re.I)
RU_ONLY = re.compile(r"[ыэъё]", re.I)
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.U)

# Frequent short words that identify a language (and occur in short commands).
STOP = {
    "en": set("the a an is are am was be to of and or in on at for with you your i me my it this that what how "
              "where when who why do does did can could will would please open send turn find tell show me hi hello "
              "yes no not thanks thank".split()),
    "de": set("der die das und ist ich nicht ein eine zu mit den dem sie es auf für bitte wie was wo wer ja nein "
              "danke mir mein öffne schick zeig".split()),
    "es": set("el la los las y es en de que un una por para con no sí hola gracias qué cómo dónde por favor abre "
              "envía muéstrame mi yo tú".split()),
    "fr": set("le la les et est je tu vous il elle un une des de du que qui pas oui non merci bonjour comment où "
              "quoi ouvre envoie montre mon ma".split()),
    "it": set("il lo la gli le e è di che un una per con non sì ciao grazie come dove cosa apri invia mostra mio "
              "io tu".split()),
    "pt": set("o a os as e é de que um uma por para com não sim olá obrigado obrigada como onde abre envia mostra "
              "meu eu você".split()),
}
# Cyrillic text without і/ї/є/ґ («Зараз дванадцята година.»): words that are Ukrainian and not Russian decide; «так» /
# «добре» occur in Russian too («так и есть», «ну так»), so they only back up a clear Ukrainian word.
# Not «друга» / «доброго»: Russian has them too («для друга», «доброго утра» were read with the Ukrainian voice).
UK_WORDS = set("зараз година години годину годин хвилина хвилин хвилини дякую дякуємо привіт добрий "
               "дванадцята одинадцята десята дев'ята восьма сьома шоста п'ята четверта третя перша "
               "що це як вже дуже щоб тепер немає зробив зробила відкрив гаразд звісно будь-ласка".split())
UK_WEAK = {"так", "добре", "ні"}
RU_WORDS = set("сейчас это что как уже очень чтобы теперь нет да ну час часа часов минута минут минуты спасибо "
               "пожалуйста привет хорошо сделал сделала открыл конечно ладно и он она они".split())
MARKS = {"de": re.compile(r"[äöüß]", re.I), "es": re.compile(r"[ñ¿¡]", re.I), "fr": re.compile(r"[çœêèàùâîôû]", re.I),
         "pt": re.compile(r"[ãõ]", re.I)}


def script(text: str) -> str:
    """'cyr', 'lat' or '' — which alphabet most letters of the text use."""
    c, l = len(CYR.findall(text or "")), len(LAT.findall(text or ""))
    if not c and not l:
        return ""
    return "cyr" if c >= l else "lat"


def latin_share(text: str) -> float:
    c, l = len(CYR.findall(text or "")), len(LAT.findall(text or ""))
    return l / (c + l) if c + l else 0.0


def detect(text: str, default: str = "ru") -> str:
    """The language of a sentence or a short phrase: ru / uk / en / de / es / fr / it / pt."""
    t = text or ""
    sc = script(t)
    if not sc:
        return default
    if sc == "cyr":
        if UK_ONLY.search(t):
            return "uk"
        if RU_ONLY.search(t):
            return "ru"
        words = [w.casefold().replace("’", "'") for w in WORD.findall(t)]
        uk = sum(1 for w in words if w in UK_WORDS) + (1 if "будь ласка" in " ".join(words) else 0)
        ru = sum(1 for w in words if w in RU_WORDS)
        if uk and uk + 0.5 * sum(1 for w in words if w in UK_WEAK) > ru:
            return "uk"
        if ru > uk:
            return "ru"
        return default if default in ("ru", "uk") else "ru"
    words = [w.casefold() for w in WORD.findall(t) if LAT.search(w)]
    scores = {k: sum(1 for w in words if w in s) for k, s in STOP.items()}
    for k, rx in MARKS.items():
        if rx.search(t):
            scores[k] = scores.get(k, 0) + 2
    best = max(scores, key=lambda k: (scores[k], k == "en"))
    return best if scores[best] > 0 else ("en" if default in ("ru", "uk") else default if default in STOP else "en")


SENT = re.compile(r"(?<=[.!?…:])\s+|\n+")          # «Переведено: Hello, how are you?» — two languages


def runs(text: str, default: str = "ru") -> list[tuple[str, str]]:
    """Split a text into (language, piece) runs, sentence by sentence, joining neighbours of the same language.
    A Russian sentence with a brand name («Открыл YouTube») stays Russian: only a sentence written mostly in Latin
    letters switches the voice."""
    out: list[tuple[str, str]] = []
    for s in SENT.split(text or ""):
        s = s.strip()
        if not re.search(r"\w", s):
            continue
        lat_words = sum(1 for w in WORD.findall(s) if LAT.search(w))
        cyr_words = sum(1 for w in WORD.findall(s) if CYR.search(w))
        if cyr_words and lat_words <= 3 * cyr_words:
            lang = detect(re.sub(r"[A-Za-z]+", " ", s), default)    # «Открыл YouTube Music»: Russian with a brand
        elif script(s) == "lat" and (lat_words >= 2 or default not in ("ru", "uk") or len(s) > 12):
            lang = detect(s, default if default not in ("ru", "uk") else "en")
        elif script(s) == "lat":
            lang = default                   # «OK.» / «WhatsApp.» alone: say it in the conversation's language
        else:
            lang = detect(s, default) if script(s) else default
        if out and out[-1][0] == lang:
            out[-1] = (lang, out[-1][1] + " " + s)
        else:
            out.append((lang, s))
    return out
