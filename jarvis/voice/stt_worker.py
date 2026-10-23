"""Local speech recognition worker — runs in its own virtualenv (~/.local/share/jarvis/stt), never imported by JARVIS.

Two models, both on the CPU through onnx-asr/onnxruntime, nothing leaves the computer:
- GigaAM v3 e2e RNN-T (Sber, MIT), int8 — the best Russian recogniser; writes punctuation, digits, Latin brand names.
  ~0.3–0.5 s for a command, ~520 MB RAM.
- Parakeet TDT 0.6B v3 (NVIDIA, CC-BY-4.0), int8 — 25 European languages (English, Ukrainian, German, Spanish, French,
  Italian, Portuguese…), detects the language itself. ~0.9 s for a command, loaded in the background after start.

How a phrase is recognised (measured 2026-10-24 on 38 spoken commands): GigaAM first. Russian speech gives Russian
words — done, no extra cost. English speech gives Latin text («The Wather in London…») or made-up Cyrillic words
(«Хавуарью», «Вот изерту-Морроу»); then Parakeet hears the phrase again («How are you?», «What is the weather
tomorrow»). If Parakeet also writes Cyrillic, the phrase was Russian after all and GigaAM's text is kept.

Protocol (one JSON object per line):
  stdin  {"id": "...", "path": "/tmp/x.wav", "mode": "auto"|"ru"}   16 kHz mono 16-bit WAV, deleted by JARVIS afterwards
         {"id": "...", "vocab": ["телеграм", "тимур", ...]}       extra known Russian words (names, brands)
  stdout {"id": "...", "text": "...", "model": "gigaam"|"parakeet", "ms": 123}   or {"id": "...", "error": "..."}
"""
import json
import os
import sys
import threading
import time
import wave

os.environ.setdefault("HF_HUB_OFFLINE", "1")          # the models are already downloaded: never call the network

import numpy as np  # noqa: E402
import onnx_asr  # noqa: E402


def _load_lexicon_module():
    """lexicon.py next to this file, loaded by path: the folder is not put on sys.path, so nothing else in it can shadow
    a library module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("jarvis_lexicon", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                                "lexicon.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_lex = _load_lexicon_module()
Lexicon, prefer_second = _lex.Lexicon, _lex.prefer_second

MODEL = os.environ.get("JARVIS_STT_MODEL", "gigaam-v3-e2e-rnnt")
MULTI = os.environ.get("JARVIS_STT_MULTI", "nemo-parakeet-tdt-0.6b-v3")
LEXICON = os.environ.get("JARVIS_STT_LEXICON", "")



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


def load(name: str):
    # int8: same transcripts as the full model on our test set, a third of the memory, slightly faster (measured).
    try:
        return onnx_asr.load_model(name, quantization="int8")
    except Exception:   # noqa: BLE001
        return onnx_asr.load_model(name)


def warm(model) -> None:
    # onnxruntime prepares buffers per input size (measured: first phrases 2.1/1.4 s with a 1 s warm-up, 0.5/0.4 s
    # after this one).
    rng = np.random.default_rng(0)
    for sec in (1, 3, 6):
        model.recognize(rng.normal(0, 0.05, 16000 * sec).astype(np.float32))


class Multi:
    """Parakeet, loaded in the background so start-up is not slower; a phrase that needs it waits until it is ready."""

    def __init__(self, name: str):
        self.name, self.model, self.error = name, None, None
        self.ready = threading.Event()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self) -> None:
        try:
            m = load(self.name)
            warm(m)
            self.model = m
        except Exception as e:   # noqa: BLE001 — not downloaded: Russian only
            self.error = f"{e.__class__.__name__}: {str(e)[:120]}"
        self.ready.set()

    def recognize(self, audio):
        if not self.ready.wait(120) or self.model is None:
            return None
        return self.model.recognize(audio)


def main() -> None:
    model = load(MODEL)
    warm(model)
    lex = Lexicon(LEXICON)
    multi = Multi(MULTI) if MULTI and MULTI != "none" else None
    print(json.dumps({"ready": True, "model": MODEL, "multi": bool(multi), "lexicon": len(lex.words)}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            if "vocab" in req:
                lex.add(req.get("vocab") or [])
                print(json.dumps({"id": rid, "ok": True, "lexicon": len(lex.words)}), flush=True)
                continue
            t0 = time.perf_counter()
            audio = load_wav(req["path"])
            text = (model.recognize(audio) if len(audio) > 1600 else "") or ""
            used = "gigaam"
            if multi and req.get("mode", "auto") != "ru" and text.strip() and not lex.russian(text):
                other = (multi.recognize(audio) or "").strip()
                if prefer_second(other):
                    text, used = other, "parakeet"
            print(json.dumps({"id": rid, "text": text.strip(), "model": used,
                              "ms": round((time.perf_counter() - t0) * 1000)}, ensure_ascii=False), flush=True)
        except Exception as e:   # noqa: BLE001
            print(json.dumps({"id": rid, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}), flush=True)


if __name__ == "__main__":
    main()
