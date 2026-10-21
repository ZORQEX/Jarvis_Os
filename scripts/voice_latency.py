"""Real voice turns through the server path (local STT → Director → Claude → streamed local speech).
Usage: uv run python scripts/voice_latency.py <tmp_dir> ["фраза" ...]  — prints per-stage timings from the end of speech."""
import asyncio, base64, os, subprocess, sys, tempfile, time
os.environ["JARVIS_HOME"] = tempfile.mkdtemp(prefix="jarvis-e2e-")
from jarvis.app import build
from jarvis.server import Server
from jarvis.voice.tts import PiperTTS
OUT = sys.argv[1]
PHRASES = sys.argv[2:] or ["Джарвис, сколько будет двадцать пять процентов от восьмисот?", "Что такое искусственный интеллект?",
           "Кто такой Илон Маск?", "А сколько ему лет?", "Расскажи подробно, как работает майнинг биткоина."]

async def main():
    core = build(); srv = Server(core); await core.claude.warm()
    await core.director.speech.engine.prewarm()
    mk = PiperTTS(); await mk.start()
    q = core.bus.subscribe()
    for i, ph in enumerate(PHRASES):
        raw = await mk.synth(ph)
        open(f"{OUT}/u{i}.wav", "wb").write(raw)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f"{OUT}/u{i}.wav", "-ar", "16000", "-ac", "1", f"{OUT}/u{i}_16k.wav"], check=True)
        audio = open(f"{OUT}/u{i}_16k.wav", "rb").read()
        while not q.empty(): q.get_nowait()
        end = time.time(); dur = len(audio) / 32000
        r = await srv._voice_turn({"audio_b64": base64.b64encode(audio).decode(), "mime": "audio/wav", "speech_start": end - dur, "speech_end": end})
        tid = r.get("task_id")
        deadline = time.time() + 120
        while time.time() < deadline:
            try: ev = await asyncio.wait_for(q.get(), 2)
            except asyncio.TimeoutError: continue
            if ev["kind"] == "speech.end" and ev.get("task_id") == tid: break
        t = core.director.speech.turns(1)[0]; ms = t["ms"]
        msg = core.director.messages(core.director.conversation_id())[-1]["text"]
        f = lambda k: f"{ms[k]/1000:5.2f}" if ms.get(k) is not None else "   — "
        print(f"[{t['route']:7}] {r['text'][:52]!r}")
        print(f"   STT {f('stt_end')} | first token {f('ttft')} | first sentence {f('first_sentence')} | FIRST AUDIO {f('first_audio_sent')} | answer done {f('answer_done')} | speech done {f('speech_done')}  [{t['stt_model']}]")
        print(f"   -> {msg[:150]!r}")
    await mk.stop(); await core.director.speech.engine.stop()
asyncio.run(main())
