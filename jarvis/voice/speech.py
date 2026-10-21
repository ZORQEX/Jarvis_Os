"""Streaming speech output: text deltas → sentence buffer → TTS → ordered audio chunks.

Text generation and audio are decoupled: as soon as a natural speech segment is
ready it is synthesized and sent to the UI while the model keeps writing.
Every voice turn records real timings (voice_turns table)."""
from __future__ import annotations

import asyncio
import base64
import random
import re
import time

from ..db import Database, new_id
from ..events import EventBus
from .tts import ACKS, clean_for_speech

HARD = re.compile(r"[.!?…;:](?=\s)|\n+")
SOFT = re.compile(r",(?=\s)")


class SentenceBuffer:
    """Accumulates streamed text and emits natural speech segments.
    The first segment is emitted early (at a comma once ~25 chars are ready) so
    audio starts quickly; later segments prefer sentence ends."""

    def __init__(self, first_min: int = 25, soft_min: int = 80, max_len: int = 220):
        self.buf = ""
        self.first_min, self.soft_min, self.max_len = first_min, soft_min, max_len
        self.emitted = 0

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            seg = self._cut()
            if seg is None:
                break
            seg = clean_for_speech(seg)
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
        rest, self.buf = clean_for_speech(self.buf), ""
        return [rest] if re.search(r"\w", rest) else []


class SpeechStream:
    def __init__(self, mgr: "SpeechManager", task_id: str, turn_id: str | None, max_chars: int = 1200):
        self.mgr, self.task_id, self.turn_id = mgr, task_id, turn_id
        self.max_chars, self.spoken, self.truncated = max_chars, 0, False
        self.buffer = SentenceBuffer()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.seq = 0
        self.cancelled = False
        self.got_delta = False
        self.finished = False
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
        if self.truncated:
            self.queue.put_nowait("Остальное — на экране.")
        self.queue.put_nowait(None)

    def cancel(self) -> None:
        self.cancelled = True
        self.worker.cancel()

    async def _work(self) -> None:
        try:
            while True:
                text = await self.queue.get()
                if text is None:
                    break
                self.mgr.mark(self.turn_id, "tts_start")
                if self.seq == 0:
                    self.mgr.bus.publish("speech.synth", {}, self.task_id)    # real state: synthesizing speech
                try:
                    audio = await self.mgr.engine.synth(text)
                except Exception as e:   # noqa: BLE001 — speech failure must never hide the text answer
                    self.mgr.bus.publish("speech.error", {"error": str(e)[:200]}, self.task_id)
                    continue
                if self.cancelled or not audio:
                    continue
                self.mgr.mark(self.turn_id, "first_audio_sent")
                self.seq += 1
                self.mgr.bus.publish("speech.audio", {"seq": self.seq, "text": text, "mime": "audio/wav",
                                                      "audio_b64": base64.b64encode(audio).decode()}, self.task_id)
            self.mgr.bus.publish("speech.end", {"segments": self.seq}, self.task_id)
            self.mgr.mark(self.turn_id, "speech_done")
        except asyncio.CancelledError:
            self.mgr.bus.publish("speech.cancelled", {}, self.task_id)
        finally:
            self.mgr.streams.pop(self.task_id, None)


TURN_FIELDS = ("speech_start", "speech_end", "stt_start", "stt_end", "request_start", "ttft", "first_sentence",
               "tts_start", "first_audio_sent", "first_audio_played", "answer_done", "speech_done")


class SpeechManager:
    def __init__(self, db: Database, bus: EventBus, engine):
        self.db, self.bus, self.engine = db, bus, engine
        self.streams: dict[str, SpeechStream] = {}
        bus.listeners.append(self._on_event)

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

            def ms(k):
                return round((d[k] - base) * 1000) if d.get(k) and base else None
            d["ms"] = {k: ms(k) for k in TURN_FIELDS if k not in ("speech_start", "speech_end")}
            d["ms"]["speech_duration"] = round((d["speech_end"] - d["speech_start"]) * 1000) \
                if d["speech_start"] and d["speech_end"] else None
            out.append(d)
        return out

    # ---------------------------------------------------------------- streams
    def start(self, task_id: str, turn_id: str | None, max_chars: int = 1200) -> SpeechStream:
        s = self.streams.get(task_id)
        if s is None:
            s = SpeechStream(self, task_id, turn_id, max_chars)
            self.streams[task_id] = s
        return s

    def ack(self, task_id: str) -> None:
        s = self.streams.get(task_id)
        if s and not s.got_delta:
            s.say(random.choice(ACKS))

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
                n += 1
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
