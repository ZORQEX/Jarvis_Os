"""Claude Code as JARVIS's agent: failures in plain words, the fallback model, what Claude is doing (progress), and a
process that hangs (VERIFIED MOCK: scripted provider and a fake CLI process, never the real Claude)."""
import asyncio
import json
import time

import pytest

from jarvis.config import Config
from jarvis.providers import claude_chat as cc
from jarvis.providers.claude_chat import ClaudeChatProvider, STALLED, STOPPED, trouble, _Turn
from jarvis.tasks import S
from jarvis.voice.speech import SpeechManager

from conftest import ScriptedClaude, install, wait_done, wait_for


@pytest.fixture
def almaty(monkeypatch):
    """The CLI prints reset times in the user's zone (seen live: «resets 4am (Asia/Almaty)»)."""
    monkeypatch.setenv("TZ", "Asia/Almaty")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def at(hh: int, mm: int = 0) -> float:
    """2026-10-25 hh:mm in this computer's zone."""
    return time.mktime((2026, 10, 25, hh, mm, 0, 0, 0, -1))


# ---------------------------------------------------------------- failures in plain words
def test_session_limit_says_when_it_comes_back(almaty):
    t = trouble("You've hit your session limit · resets 4am (Asia/Almaty)", now=at(1, 55))
    assert t.kind == "quota" and t.until == at(4)
    assert t.text.startswith("Лимит Claude исчерпан — снова заработает в 4:00.") and "работают и без него" in t.text
    assert t.speech == "Лимит Claude закончился, он вернётся в 4 утра. Простые команды выполняю и без него."
    assert "You've" not in t.text + t.speech


def test_limit_with_minutes_and_after_midnight(almaty):
    t = trouble("Claude AI usage limit reached. You've hit your limit · resets 4:20am (Asia/Almaty)", now=at(23, 10))
    assert t.until == at(4, 20) + 24 * 3600
    assert "в 4:20" in t.text and t.speech.startswith("Лимит Claude закончился, он вернётся в 4 20 утра")


def test_weekly_limit_names_the_day(almaty):
    t = trouble("You've hit your weekly limit · resets Oct 30, 9pm (Asia/Almaty)", now=at(12))
    assert t.kind == "quota" and t.text.startswith("Недельный лимит Claude исчерпан — снова заработает 30 октября в 21:00")
    assert "30 октября в 9 вечера" in t.speech


def test_other_failures_in_plain_words():
    over = trouble('API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}')
    assert over.kind == "server" and "перегружен" in over.text and "529" not in over.text
    auth = trouble("Invalid API key · Please run /login")
    assert auth.kind == "auth" and "войти заново" in auth.text and "/login" in auth.text
    net = trouble("Request timed out")
    assert net.kind == "network" and "интернет" in net.speech
    assert trouble(STALLED).text.startswith("Claude перестал отвечать — я остановил его, со следующей просьбой")
    assert "запустится заново" in trouble(STOPPED).text
    model = trouble("There's an issue with the selected model (claude-sonnet-0-0). It may not exist or you may not have "
                    "access to it. Run --model to pick a different model.")         # seen live, fallback off
    assert model.kind == "model" and "(claude-sonnet-0-0) сейчас недоступна" in model.text and "There's" not in model.text
    assert trouble("Odd failure.").text == "Claude не смог ответить: Odd failure."
    odd = trouble("Something unexpected: code 7")
    assert odd.text == "Claude не смог ответить: Something unexpected: code 7." and "экране" in odd.speech


def test_limit_is_remembered_until_it_resets(tmp_path, almaty):
    p = ClaudeChatProvider(Config(), None, tmp_path)
    p.configured = lambda: True
    p.status = "ready"
    e = p._failure("You've hit your session limit · resets 4am (Asia/Almaty)")
    assert e.kind == "quota" and e.speech and e.raw.startswith("You've hit")
    assert p.health().value == "RATE_LIMITED" and p.describe()["limited_until"] == p.limited_until
    p.limited_until = time.time() - 1                       # 4:00 has passed
    assert p.health().value == "CONNECTED" and p.last_error is None


# ---------------------------------------------------------------- the fallback model
def test_fallback_models_are_the_other_configured_ones(tmp_path):
    p = ClaudeChatProvider(Config(), None, tmp_path)
    assert p._fallbacks("claude-sonnet-5-5") == "claude-haiku-5-5"
    assert p._fallbacks("claude-haiku-5-5") == "claude-sonnet-5-5"
    assert p._fallbacks("claude-opus-5-5") == "claude-haiku-5-5,claude-sonnet-5-5"
    p.config.claude_fallback = False
    assert p._fallbacks("claude-sonnet-5-5") == ""


async def test_conversation_and_one_off_steps_start_with_a_fallback_model(tmp_path, monkeypatch):
    seen = []

    class Proc:
        pid, returncode = 0, None

        def __init__(self):
            self.stdin, self.stdout, self.stderr = None, asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_data(b'{"type":"result","subtype":"success","result":"ok","structured_output":{"a":1}}\n')
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

        async def communicate(self):
            return await self.stdout.read(), b""

    async def spawn(*argv, **kw):
        seen.append(list(argv))
        return Proc()
    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", spawn)
    p = ClaudeChatProvider(Config(), None, tmp_path)
    p.configured = lambda: True
    await p._start()
    p.reader.cancel()
    from jarvis.providers.base import Cap, StepRequest
    await p.generate(StepRequest(Cap.WRITING, "Напиши слово"))
    await p.structured("sys", "user", {"type": "object"})
    for argv in seen:
        main = argv[argv.index("--model") + 1]
        fb = argv[argv.index("--fallback-model") + 1]
        assert fb and main not in fb.split(","), argv


# ---------------------------------------------------------------- what Claude is doing, in plain words
def test_progress_lines_never_show_raw_tool_calls():
    titles = {"app_open": "Открыть приложение"}
    assert cc.progress_line("WebSearch", {"query": "погода в Алматы"}) == "Ищу в интернете: погода в Алматы"
    assert cc.progress_line("WebFetch", {"url": "https://www.example.com/a/b"}) == "Читаю страницу example.com"
    assert cc.progress_line("app_open", {"name": "telegram"}, titles) == "Открыть приложение"
    assert cc.progress_line("mcp__jarvis__secret_thing", {"cmd": "rm -rf"}, titles) == "Выполняю действие"


async def test_cli_events_reach_progress_and_the_fallback_model_is_reported(tmp_path):
    class P:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
    p = ClaudeChatProvider(Config(), None, tmp_path)
    proc, tools, status = P(), [], []
    p.turn = _Turn(None, lambda n, a: tools.append((n, a)), proc, status.append)
    reader = asyncio.create_task(p._read(proc))
    for ev in ({"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 10, "retry_delay_ms": 4000,
                "error_status": 529},
               {"type": "system", "subtype": "model_fallback", "original_model": "claude-sonnet-5-5",
                "fallback_model": "claude-haiku-5-5"},
               {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch",
                                                             "input": {"query": "курс тенге"}}]}},
               {"type": "result", "subtype": "success", "result": "ok"}):
        proc.stdout.feed_data((json.dumps(ev) + "\n").encode())
    proc.stdout.feed_eof()
    await reader
    assert [s["subtype"] for s in status] == ["api_retry", "model_fallback"]
    assert tools == [("WebSearch", {"query": "курс тенге"})]
    assert p.turn.model == "claude-haiku-5-5" and p.turn.quiet_until > time.monotonic() + 60


async def test_the_task_shows_what_claude_is_doing(core):
    q = core.bus.subscribe()
    install(core, claude=ScriptedClaude(core.config, [[{"progress": ("Ищу в интернете: погода", "tool", {})},
                                                        "Сейчас +12 °C."]]))
    tid = await core.director.submit("Расскажи, почему небо голубое")
    await wait_done(core, tid)
    got = []
    while not q.empty():
        ev = q.get_nowait()
        if ev["kind"] == "step.progress":
            got.append(ev)
    chat = [st for st in core.tasks.steps(tid) if st["tool_id"] == "agent_chat"]
    assert got and got[0]["message"] == "Ищу в интернете: погода" and got[0]["step_id"] == chat[0]["id"]


# ---------------------------------------------------------------- a failed answer, on screen and spoken
class FakeTTS:
    def __init__(self):
        self.calls = []

    async def synth(self, text):
        self.calls.append(text)
        return b"RIFF" + text.encode()

    async def prewarm(self):
        return None

    async def stop(self):
        return None


async def test_a_claude_limit_is_said_plainly_by_voice(core, tmp_path, almaty):
    tts = FakeTTS()
    core.director.speech = SpeechManager(core.db, core.bus, tts)
    real = ClaudeChatProvider(core.config, None, tmp_path)
    limit = real._failure("You've hit your session limit · resets 4am (Asia/Almaty)")
    install(core, claude=ScriptedClaude(core.config, [limit]))
    tid = await core.director.submit("Расскажи что-нибудь интересное", "voice")
    t = await wait_done(core, tid)
    assert t["state"] == S.FAILED
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert msg["text"].startswith("⚠ Лимит Claude исчерпан — снова заработает") and "Шаг" not in msg["text"]
    assert "You've" not in msg["text"]
    await wait_for(lambda: any("Лимит Claude закончился" in c for c in tts.calls))
    assert not any("Не удалось получить ответ" in c for c in tts.calls)
    journal = core.audit.path.read_text("utf-8")                # the CLI's own words stay in the journal only
    assert '"ai.failed"' in journal and "session limit" in journal


async def test_while_the_limit_lasts_every_request_hears_the_same_plain_words(core, tmp_path, almaty):
    """Seen live 2026-10-26: after the first «Лимит Claude исчерпан…» the next requests got «Не могу выполнить это
    сейчас: нет доступного исполнителя для «беседа» (claude: Claude недоступен (RATE_LIMITED))»."""
    tts = FakeTTS()
    core.director.speech = SpeechManager(core.db, core.bus, tts)
    real = ClaudeChatProvider(core.config, None, tmp_path)
    real.configured = lambda: True
    real._failure("You've hit your session limit · resets 4am (Asia/Almaty)")
    install(core, claude=real)
    tid = await core.director.submit("Расскажи что-нибудь интересное", "voice")
    t = await wait_done(core, tid)
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert t["state"] == S.FAILED and msg.startswith("⚠ Лимит Claude исчерпан — снова заработает")
    assert "исполнителя" not in msg and "RATE_LIMITED" not in msg
    await wait_for(lambda: any("Лимит Claude закончился" in c for c in tts.calls))


# ---------------------------------------------------------------- a hung process
class _In:
    def write(self, b):
        pass

    async def drain(self):
        pass


class _Proc:
    returncode, pid = None, 0

    def __init__(self):
        self.stdin, self.stdout = _In(), asyncio.StreamReader()


def _quick(tmp_path):
    p = ClaudeChatProvider(Config(), None, tmp_path)
    p.TICK_S, p.STALL_S = 0.02, 0.15
    p.proc = _Proc()
    p.reader = asyncio.create_task(p._read(p.proc))
    stopped = []

    async def stop():
        stopped.append(True)            # (never os.killpg on the fake pid 0)
        p.proc = None
    p._stop = stop
    return p, stopped


async def test_a_silent_process_is_ended_not_waited_for_forever(tmp_path):
    p, stopped = _quick(tmp_path)
    turn = await asyncio.wait_for(p._send("привет"), 3)
    assert turn.result == {"is_error": True, "result": STALLED} and stopped
    assert not p.lock.locked()
    p.reader.cancel()


async def test_a_long_jarvis_tool_or_an_api_retry_is_not_a_stall(tmp_path):
    p, stopped = _quick(tmp_path)
    p.inflight = 1                       # a Claude Code job / the person's «да» — may take an hour
    task = asyncio.create_task(p._send("сделай сайт"))
    await asyncio.sleep(0.4)
    assert not task.done() and not stopped
    p.inflight = 0
    p.turn.heard = time.monotonic()
    p.proc.stdout.feed_data(b'{"type":"system","subtype":"api_retry","attempt":3,"max_retries":10,"retry_delay_ms":300}\n')
    await asyncio.sleep(0.35)            # silence after the retry notice is expected, not a hang
    assert not task.done() and not stopped
    p.proc.stdout.feed_data('{"type":"result","subtype":"success","result":"Готово."}\n'.encode())
    turn = await asyncio.wait_for(task, 2)
    assert turn.result["result"] == "Готово." and not stopped
    p.reader.cancel()


# ---------------------------------------------------------------- a quick command that is only part of the request
@pytest.mark.parametrize("phrase", [
    "Джарвис, узнай, сколько свободного места на диске, и запиши это в заметку.",     # seen live: the note was dropped
    "сделай скриншот и опиши, что на экране", "сделай погромче и включи музыку",
    "покажи процессы, а потом закрой хром", "открой папку загрузки и найди там pdf",
])
def test_a_second_action_sends_the_whole_request_to_claude(phrase):
    from jarvis import router as rt
    assert rt.route(phrase, {}) is None


@pytest.mark.parametrize("phrase,tool", [
    ("сколько свободного места на диске", "disk_usage"), ("сделай скриншот", "screenshot"),
    ("почисти диск и корзину", "disk_cleanup"), ("какая погода в Алматы", "weather"),
])
def test_one_quick_command_stays_local(phrase, tool):
    from jarvis import router as rt
    r = rt.route(phrase, {})
    assert r and r.tool == tool


async def test_claude_does_both_parts_with_jarvis_tools(core):
    g = ScriptedClaude(core.config, [[("disk_usage", {}), ("create_note", {"text": "Свободно 29 ГБ"}), "Записал."]])
    install(core, claude=g)
    tid = await core.director.submit("узнай, сколько свободного места на диске, и запиши это в заметку")
    await wait_for(lambda: core.approvals.pending())         # the note still needs the person's «да»
    core.director.decide(core.approvals.pending()[0]["id"], True)
    t = await wait_done(core, tid)
    assert len(g.calls) == 1 and t["state"] == S.COMPLETED
    assert [s["tool_id"] for s in t["steps"]] == ["agent_chat", "disk_usage", "create_note"]
    assert all(s["state"] == "succeeded" for s in t["steps"])


@pytest.mark.parametrize("phrase,action", [
    ("Ответь одним словом: какого цвета небо днём?", "chat"),     # seen live: answered + «черновик я не готовил»
    ("ответь коротко, что такое API", "chat"), ("Ответь мне честно, ты робот?", "chat"),
    ("ответь Ивану, что документы готовы", "write"), ("ответь на письмо поставщика", "write"),
])
def test_answer_briefly_is_a_question_not_a_draft(phrase, action):
    from jarvis.agency.planner import classify_clause
    assert classify_clause(phrase, None)[0] == action
