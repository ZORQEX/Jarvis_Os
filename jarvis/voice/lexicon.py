"""Is a recognised phrase Russian? Decides whether the second (multilingual) recogniser should hear it again.
Pure Python, no imports from JARVIS: the speech worker loads it from its own virtualenv (stt_worker.py)."""
import os
import re

CYR = re.compile(r"[а-яё]", re.I)
LAT = re.compile(r"[a-z]", re.I)
UK = re.compile(r"[іїєґ]", re.I)
WORDS = re.compile(r"[а-яё]+", re.I)
LATIN_WORDS = re.compile(r"[a-z]+", re.I)


class Lexicon:
    def __init__(self, path: str):
        self.words: set[str] = set()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    w = line.split(" ", 1)[0].strip().casefold().replace("ё", "е")
                    if w:
                        self.words.add(w)

    def add(self, words) -> None:
        for w in words:
            w = str(w).casefold().replace("ё", "е").strip()
            if w and len(w) < 40:
                self.words.add(w)

    def russian(self, text: str) -> bool:
        """Does GigaAM's text read as Russian? Mostly Cyrillic and most words are real Russian words."""
        ws = [w.casefold().replace("ё", "е") for w in WORDS.findall(text)]
        ws = [w for w in ws if len(w) > 1] or ws
        latin = LATIN_WORDS.findall(text)
        if not ws:
            return not latin                             # only Latin: another language; nothing: silence / noise
        if len(latin) > 2 * len(ws):
            return False                                 # «ну, send a message to my mom»
        if not self.words:
            return True                                  # no word list: trust GigaAM (Russian-only behaviour)
        # «Открой YouTube»: the Russian words are real words — Russian with a brand name, nothing to re-hear.
        # GigaAM writes brands capitalised («WhatsApp», «Space X»); a lower-case Latin word is foreign speech it could
        # not spell («Ола Кумостас, oyatesol» was Spanish).
        known = sum(1 for w in ws if w in self.words)
        return known / (len(ws) + sum(1 for w in latin if w.islower())) >= 0.5


def prefer_second(other: str) -> bool:
    """Parakeet wrote Latin (or Ukrainian): another language — use it. Parakeet wrote Russian: the phrase was Russian
    after all, keep GigaAM's text (it is better at Russian)."""
    return bool(other) and (len(LAT.findall(other)) > len(CYR.findall(other)) or bool(UK.search(other)))
