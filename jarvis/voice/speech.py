"""Streaming speech output: text deltas → sentence buffer → TTS → ordered audio chunks.

Text generation and audio are decoupled: as soon as a natural speech segment is
ready it is synthesized and sent to the UI while the model keeps writing.
Every voice turn records real timings (voice_turns table)."""
from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import wave

from ..db import Database, new_id
from ..events import EventBus
from .tts import ACKS_BY_LANG, clean_for_speech, phrase

HARD = re.compile(r"[.!?…;:](?=\s)|\n+")
SOFT = re.compile(r",(?=\s)")
PLAY_TAIL_S = 0.6        # after a piece ends: the browser's output latency and the room's echo
IDLE_YIELD_S = 2.0       # a stream waiting this long for more text (a tool runs, approval) lets another task speak


def wav_seconds(audio: bytes, text: str = "") -> float:
    """How long a piece of speech plays (from its WAV header; a rough estimate from the text otherwise)."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:   # noqa: BLE001 — not a WAV (tests, cloud voices)
        return max(0.4, len(text or "") * 0.065)


_W = re.compile(r"\w+")


def _words(s: str) -> list[str]:
    return _W.findall((s or "").casefold().replace("ё", "е"))


def _said(w: str, said: set[str]) -> bool:
    if w in said:
        return True
    # «Джарви» for «Джарвис», «Колледже» for «Колледж»: one word is the start of the other (5+ letters)
    return len(w) >= 5 and any(len(x) >= 5 and (x.startswith(w) or w.startswith(x)) for x in said)


def strip_echo(heard: str, said_texts: list[str]) -> str:
    """Remove from a transcript the sentences that are JARVIS's own voice heard through the speakers.
    `said_texts` is what JARVIS's voice said while the phrase was being recorded. Measured 2026-10-24: the companion
    heard the main window's voice sample «Привет! Я Джарвис. Так я буду звучать…» and took it for a command."""
    said = set(_words(" ".join(said_texts)))
    if not said or not (heard or "").strip():
        return heard
    sents = [x for x in re.split(r"(?<=[.!?…])\s+", heard.strip()) if x]
    info = [(x, ws, sum(1 for w in ws if _said(w, said))) for x in sents for ws in [_words(x)]]
    allw = sum(len(ws) for _, ws, _ in info)
    if allw >= 2 and all(h == len(ws) for _, ws, h in info):
        return ""                                    # every word of it was just said by JARVIS
    echo = [len(ws) >= 3 and h / len(ws) >= 0.7 for _, ws, h in info]
    changed = True
    while changed:               # «Привет.» / «Я Джарвис.» between echoed sentences: echo too, if every word was said
        changed = False
        for i, (_, ws, h) in enumerate(info):
            if not echo[i] and ws and h == len(ws) and ((i and echo[i - 1]) or (i + 1 < len(info) and echo[i + 1])):
                echo[i] = changed = True
    return " ".join(x for (x, *_), e in zip(info, echo) if not e).strip()


class SentenceBuffer:
    """Accumulates streamed text and emits natural speech segments.
    The first segment is emitted early (at a comma once ~25 chars are ready) so
    audio starts quickly; later segments prefer sentence ends."""

    def __init__(self, first_min: int = 25, soft_min: int = 80, max_len: int = 220, lang: str = "ru"):
        self.buf, self.lang = "", lang
        self.first_min, self.soft_min, self.max_len = first_min, soft_min, max_len
        self.emitted = 0

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            seg = self._cut()
            if seg is None:
                break
            seg = clean_for_speech(seg, self.lang)
            if re.search(r"\w", seg):
                out.append(seg)
                self.emitted += 1
        return out

    def _cut(self) -> str | None:
        b = self.buf
        m = HARD.search(b)
        if m and m.end() >= 2:
            seg, self.buf = b[:m.end()], b[m.end():].lstrip()
            return seg
        soft_min = self.first_min if self.emitted == 0 else self.soft_min
        for sm in SOFT.finditer(b):
            if sm.end() >= soft_min:
                seg, self.buf = b[:sm.end()], b[sm.end():].lstrip()
                return seg
        if len(b) > self.max_len:
            cut = b.rfind(" ", 0, self.max_len)
            cut = cut if cut > 40 else self.max_len
            seg, self.buf = b[:cut], b[cut:].lstrip()
            return seg
        return None

    def flush(self) -> list[str]:
        rest, self.buf = clean_for_speech(self.buf, self.lang), ""
        return [rest] if re.search(r"\w", rest) else []


class SpeechStream:
    def __init__(self, mgr: "SpeechManager", task_id: str, turn_id: str | None, max_chars: int = 1200,
                 lang: str = "ru"):
        self.mgr, self.task_id, self.turn_id, self.lang = mgr, task_id, turn_id, lang
        self.max_chars, self.spoken, self.truncated = max_chars, 0, False
        self.buffer = SentenceBuffer(lang=lang)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.seq = 0
        self.cancelled = False
        self.got_delta = False
        self.finished = False
        self.nseg, self.batch = 0, []
        self.fallback_told = False
        self.idle_since = time.time()      # waiting for text (the answer is still being written / a tool runs)
        self.worker = asyncio.create_task(self._work(), name=f"speech-{task_id}")

    def say(self, text: str) -> None:
        if not self.cancelled:
            self.queue.put_nowait(text)

    def feed(self, text: str) -> None:
        if self.cancelled:
            return
        if not self.got_delta:
            self.got_delta = True
            self.mgr.mark(self.turn_id, "ttft")
        for seg in self.buffer.feed(text):
            self._queue(seg)

    def _queue(self, seg: str) -> None:
        if self.truncated:
            return
        if self.spoken and self.spoken + len(seg) > self.max_chars:
            self.truncated = True          # long answers: speak the start, the rest stays on screen
            return
        self.spoken += len(seg)
        self.mgr.mark(self.turn_id, "first_sentence")
        if self.nseg and getattr(self.mgr.engine, "prefers_batch", False):
            self.batch.append(seg)         # cloud voice: everything after the first sentence goes as one request
            return
        self.nseg += 1
        self.queue.put_nowait(seg)

    def finish(self, final_text: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        if not self.got_delta and final_text:
            self.mgr.mark(self.turn_id, "ttft")
            self.feed(final_text)
        for seg in self.buffer.flush():
            self._queue(seg)
        if self.batch:
            self.queue.put_nowait(" ".join(self.batch))
            self.batch = []
        if self.truncated:
            self.queue.put_nowait("The rest is on the screen." if self.lang == "en" else "Остальное — на экране.")
        self.queue.put_nowait(None)

    def cancel(self) -> None:
        self.cancelled = True
        self.worker.cancel()

    async def _work(self) -> None:
        try:
            while True:
                if self.queue.empty():
                    self.idle_since = time.time()
                text = await self.queue.get()
                self.idle_since = 0.0
                if text is None:
                    break
                await self.mgr.take_floor(self)      # one voice at a time, never over the person
                self.mgr.mark(self.turn_id, "tts_start")
                if self.seq == 0:
                    self.mgr.bus.publish("speech.synth", {}, self.task_id)    # real state: synthesizing speech
                eng = self.mgr.engine
                kw = {"lang": self.lang} if getattr(eng, "multilingual", False) else {}
                try:
                    if hasattr(eng, "synth_stream"):          # engines that stream: play pieces as they arrive
                        async for audio in eng.synth_stream(text, **kw):
                            self._emit(text, audio)
                    else:
                        self._emit(text, await eng.synth(text, **kw))
                except Exception as e:   # noqa: BLE001 — speech failure must never hide the text answer
                    self.mgr.bus.publish("speech.error", {"error": str(e)[:200]}, self.task_id)
                    continue
                fell = getattr(eng, "fell_back", None)
                if fell is None:
                    fell = bool(getattr(eng, "silero", None)) and getattr(eng, "last_engine", "") == "piper"
                if fell and not self.fallback_told:
                    self.fallback_told = True      # the chosen voice did not speak: say so, never hide it
                    self.mgr.bus.publish("speech.fallback", {"voice": getattr(eng, "voice_id", ""),
                                                             "reason": getattr(eng, "last_fallback", "") or ""},
                                         self.task_id)
            self.mgr.bus.publish("speech.end", {"segments": self.seq}, self.task_id)
            self.mgr.mark(self.turn_id, "speech_done")
        except asyncio.CancelledError:
            self.mgr.bus.publish("speech.cancelled", {}, self.task_id)
        finally:
            if self.mgr.streams.get(self.task_id) is self:
                self.mgr.streams.pop(self.task_id, None)
            if self.mgr.floor is self:
                self.mgr.floor = None

    def _emit(self, text: str, audio: bytes) -> None:
        if self.cancelled or not audio:
            return
        self.mgr.mark(self.turn_id, "first_audio_sent")
        self.mgr.played(self.task_id, text, audio)
        self.seq += 1
        self.mgr.bus.publish("speech.audio", {"seq": self.seq, "text": text, "mime": "audio/wav",
                                              "audio_b64": base64.b64encode(audio).decode()}, self.task_id)


TURN_FIELDS = ("speech_start", "speech_end", "stt_start", "stt_end", "request_start", "ttft", "first_sentence",
               "tts_start", "first_audio_sent", "first_audio_played", "answer_done", "speech_done")


class SpeechManager:
    def __init__(self, db: Database, bus: EventBus, engine):
        self.db, self.bus, self.engine = db, bus, engine
        self.streams: dict[str, SpeechStream] = {}
        self.lang = "ru"                 # the language the person spoke last (JARVIS's own phrases follow it)
        # What the speakers play and when: pieces are sent faster than they play and the window plays them one after
        # another, so each piece sounds right after the previous one. Used to know which answer is being heard now
        # («стоп» stops that one, not the other tasks) and for the echo check (only what sounded during the phrase).
        self.play_end = 0.0
        self.playlog: list[tuple[float, float, str, str]] = []      # (start, end, task id, text)
        self.floor: SpeechStream | None = None      # the stream whose voice is going out now
        self.hold_until = 0.0                        # the person is talking: JARVIS waits before saying more
        self.silenced: tuple[float, list[str]] = (0.0, [])   # what a barge-in just cut off
        bus.listeners.append(self._on_event)

    # ---------------------------------------------------------------- what is being heard
    def played(self, task_id: str | None, text: str, audio: bytes = b"", seconds: float | None = None) -> None:
        dur = seconds if seconds is not None else wav_seconds(audio, text)
        now_ = time.time()
        start = max(now_, self.play_end)
        self.play_end = start + dur
        self.playlog = [x for x in self.playlog if now_ - x[1] < 60][-200:] + [(start, start + dur, task_id or "", text)]

    def spoken(self, text: str) -> None:
        self.played("", text)

    def audible(self, t0: float | None = None, t1: float | None = None) -> list[str]:
        """What JARVIS's voice said between t0 and t1 (epoch seconds); without times, in the last 30 s."""
        if t0 is None or t1 is None:
            t1 = time.time()
            t0 = t1 - 30
        return [x for a, b, _, x in self.playlog if a <= t1 + 0.3 and b + PLAY_TAIL_S >= t0]

    def audible_tasks(self) -> list[str]:
        """Tasks whose answer is being heard now or is queued to be heard."""
        now_ = time.time()
        out = [tid for a, b, tid, _ in self.playlog if tid and b > now_]
        f = self.floor                    # its next piece is being synthesised right now
        if f and not f.cancelled and not f.worker.done() and not f.idle_since:
            out.append(f.task_id)
        return list(dict.fromkeys(out))

    def is_echo(self, heard: str, t0: float | None = None, t1: float | None = None) -> bool:
        return bool((heard or "").strip()) and not strip_echo(heard, self.audible(t0, t1))

    def strip_echo(self, heard: str, t0: float | None = None, t1: float | None = None) -> str:
        said = self.audible(t0, t1)
        return strip_echo(heard, said) if said else heard

    def hush(self, tasks: list[str] | None = None) -> None:
        """The windows stopped playing (all of it, or these tasks): nothing more of it will sound."""
        now_ = time.time()
        if tasks is None:
            self.playlog = [(a, min(b, now_), t, x) for a, b, t, x in self.playlog if a <= now_]
        else:
            self.playlog = [(a, min(b, now_) if t in tasks else b, t, x) for a, b, t, x in self.playlog
                            if not (t in tasks and a > now_)]
        self.play_end = max([now_] + [b for _, b, _, _ in self.playlog])

    async def take_floor(self, s: "SpeechStream") -> None:
        """Answers of several tasks are spoken one after another, not mixed sentence by sentence; and JARVIS does not
        start talking while the person is talking."""
        while True:
            f = self.floor
            busy = (f is not None and f is not s and not f.cancelled and not f.worker.done()
                    and not (f.idle_since and time.time() - f.idle_since > IDLE_YIELD_S))
            if not busy and time.time() >= self.hold_until:
                self.floor = s
                return
            await asyncio.sleep(0.05)

    # ---------------------------------------------------------------- telemetry
    def new_turn(self, speech_start: float | None = None, speech_end: float | None = None) -> str:
        tid = new_id("v_")
        self.db.execute("INSERT INTO voice_turns(id, created_at, speech_start, speech_end, tts_engine) VALUES(?,?,?,?,?)",
                        (tid, time.time(), speech_start, speech_end, getattr(self.engine, "name", "?")))
        return tid

    def mark(self, turn_id: str | None, field: str, value: float | None = None, only_first: bool = True) -> None:
        if not turn_id or field not in TURN_FIELDS:
            return
        cond = f" AND {field} IS NULL" if only_first else ""
        self.db.execute(f"UPDATE voice_turns SET {field}=? WHERE id=?{cond}", (value or time.time(), turn_id))

    def annotate(self, turn_id: str | None, **kw) -> None:
        if not turn_id:
            return
        allowed = {k: v for k, v in kw.items() if k in ("task_id", "transcript", "route", "stt_model", "llm_model", "error")}
        if allowed:
            cols = ",".join(f"{k}=?" for k in allowed)
            self.db.execute(f"UPDATE voice_turns SET {cols} WHERE id=?", (*allowed.values(), turn_id))

    def turns(self, limit: int = 20) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM voice_turns ORDER BY created_at DESC LIMIT ?", (limit,)):
            d = dict(r)
            base = d["speech_end"] or d["stt_start"] or d["created_at"]
            d["ms"] = {k: (round((d[k] - base) * 1000) if d.get(k) and base else None)
                       for k in TURN_FIELDS if k not in ("speech_start", "speech_end")}
            d["ms"]["speech_duration"] = round((d["speech_end"] - d["speech_start"]) * 1000) \
                if d["speech_start"] and d["speech_end"] else None
            out.append(d)
        return out

    # ---------------------------------------------------------------- streams
    def start(self, task_id: str, turn_id: str | None, max_chars: int = 1200, lang: str | None = None) -> SpeechStream:
        s = self.streams.get(task_id)
        if s is None:
            s = SpeechStream(self, task_id, turn_id, max_chars, lang or self.lang)
            self.streams[task_id] = s
        return s

    def ack(self, task_id: str) -> None:
        s = self.streams.get(task_id)
        if s and not s.got_delta and not s.spoken:
            s.say(phrase(ACKS_BY_LANG, s.lang))

    def speak_now(self, task_id: str, text: str, turn_id: str | None = None) -> None:
        """Speak a phrase now and keep the stream open (the final answer is spoken after it)."""
        self.start(task_id, turn_id).say(text)

    def say(self, task_id: str, text: str, turn_id: str | None = None) -> None:
        s = self.start(task_id, turn_id)
        s.say(text)
        s.finish()

    def cancel(self, task_id: str | None = None) -> int:
        ids = [task_id] if task_id else list(self.streams)
        n = 0
        for t in ids:
            s = self.streams.pop(t, None)
            if s:
                s.cancel()
                if self.floor is s:
                    self.floor = None
                n += 1
        self.hush(None if task_id is None else ids)
        return n

    def _on_event(self, kind: str, ev: dict | None = None) -> None:
        if ev is None:
            return
        tid = ev.get("task_id")
        s = self.streams.get(tid) if tid else None
        if not s:
            return
        if kind == "assistant.delta":
            s.feed(ev.get("text", ""))
        elif kind == "message" and ev["message"]["role"] == "assistant":
            m = ev["message"]
            self.mark(s.turn_id, "answer_done")
            text = m["text"]
            if (m.get("meta") or {}).get("error_kind") or text.startswith("⚠"):
                text = "Не удалось получить ответ. Попробуйте ещё раз чуть позже." if not s.got_delta else None
            # Long text answers keep their formatting on screen; speech gets the start plus a hint.
            s.finish(text)
