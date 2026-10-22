"""Director ↔ Claude conversation with a scripted provider (VERIFIED MOCK, not a live API test).
Checks: model tool calls go through the policy gateway, approvals still apply,
history/function responses are threaded correctly, provider errors are honest."""
import pytest

from jarvis.providers.base import ProviderFailure as ProviderError
from jarvis.tasks import S

from conftest import wait_done, wait_for


@pytest.fixture
def fake(core):
    from conftest import ScriptedClaude, install

    def go(turns):
        g = ScriptedClaude(core.config, turns)
        install(core, claude=g)
        return g
    return go


async def test_model_tool_call_runs_through_gateway(core, fake):
    g = fake([[("system_info", {}), "Свободно много места."]])
    tid = await core.director.submit("проверь, всё ли в порядке с компьютером")
    t = await wait_done(core, tid)
    assert t["state"] == S.COMPLETED
    assert [s["tool_id"] for s in t["steps"]] == ["agent_chat", "system_info"]
    assert t["steps"][1]["verification"]["status"] == "verified"
    # second model call received the function response
    assert g.tool_results[0]["status"] == "ok"            # the tool result went back to Claude
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert msg["text"] == "Свободно много места." and msg["meta"]["cloud"] is True


async def test_model_cannot_skip_approval(core, fake, home):
    fake([[("create_note", {"text": "от модели"}), "Готово."]])
    tid = await core.director.submit("сохрани мысль про окна")
    await wait_for(lambda: core.approvals.pending())
    assert not (home / "out").exists()
    core.director.decide(core.approvals.pending()[0]["id"], False)
    t = await wait_done(core, tid)
    assert [x for x in t["steps"] if x["tool_id"] == "create_note"][0]["state"] == "denied"
    assert not (home / "out").exists()


async def test_model_cannot_call_ui_only_or_unknown_tools(core, fake):
    g = fake([[("flora_send", {"draft_id": "x"}), ("shell_exec", {"cmd": "rm -rf ~"}), "Не могу."]])
    tid = await core.director.submit("сделай что-нибудь полезное с почтой")
    t = await wait_done(core, tid)
    states = {s["tool_id"]: s["state"] for s in t["steps"]}
    assert states.get("flora_send") in (None, "invalid", "denied")   # not model-callable: never runs
    assert "shell_exec" not in states
    assert [r["status"] for r in g.tool_results] == ["invalid", "invalid"]
    assert not core.director.flora.mail.sent_all()


async def test_send_request_is_never_executed_without_mail_and_approval(core, fake):
    g = fake([])
    tid = await core.director.submit("отправь всё")
    await wait_done(core, tid)
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert "Ничего не отправлено" in msg or "модуль связи" in msg
    assert not g.calls and not core.director.flora.mail.sent_all()


async def test_provider_error_is_reported_not_faked(core, fake):
    fake([ProviderError("quota", "Claude: лимит использования исчерпан")])
    tid = await core.director.submit("расскажи что-нибудь интересное")
    t = await wait_done(core, tid)
    assert t["state"] == S.FAILED
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert "лимит" in msg["text"] and "Claude" in msg["text"]


async def test_private_mode_asks_before_cloud(core, fake):
    core.db.set_kv("privacy_profile", "PRIVATE_LOCAL")
    g = fake([["Ответ."]])
    tid = await core.director.submit("объясни, что такое BIM")
    await wait_for(lambda: core.approvals.pending())
    assert core.approvals.pending()[0]["tool_id"] == "cloud_send" and not g.calls
    core.director.decide(core.approvals.pending()[0]["id"], False)
    await wait_done(core, tid)
    assert not g.calls                                           # nothing was sent


async def test_tool_round_limit(core, fake):
    core.config.max_tool_rounds = 2
    g = fake([[("system_info", {}), ("system_info", {}), ("system_info", {}), "Хватит."]])
    tid = await core.director.submit("зацикленный запрос")
    t = await wait_done(core, tid)
    assert len([s for s in t["steps"] if s["tool_id"] == "system_info"]) == 2
    assert g.tool_results[-1]["status"] == "denied" and "лимит шагов" in g.tool_results[-1]["error"]


async def test_single_letter_uses_the_warm_conversation_not_a_new_process(core, fake):
    g = fake([["Уважаемые коллеги, пришлите, пожалуйста, прайс-лист."]])
    started = []

    async def no_oneshot(*a, **k):
        started.append(1)
        raise AssertionError("no separate Claude Code start for a single letter")
    g.generate = no_oneshot
    tid = await core.director.submit("Напиши письмо поставщику мрамора с просьбой прислать прайс-лист")
    await wait_done(core, tid)
    assert not started and "черновик" in g.calls[0]["user"]
    assert core.director.messages(core.director.conversation_id())[-1]["text"].startswith("Уважаемые")


async def test_several_conversational_clauses_are_one_message_without_planning(core, fake):
    g = fake([["Запомнил."]])

    async def no_planning(*a, **k):
        raise AssertionError("no planning call for a plain conversational request")
    g.generate = no_planning
    tid = await core.director.submit("Запомни число 4711. Ответь одним словом: запомнил.")
    await wait_done(core, tid)
    assert len(g.calls) == 1 and "4711" in g.calls[0]["user"]


async def test_an_old_process_ending_never_fails_the_new_question(tmp_path):
    import asyncio as aio
    from jarvis.config import Config
    from jarvis.providers.claude_chat import ClaudeChatProvider, _Turn

    class P:
        def __init__(self):
            self.stdout = aio.StreamReader()
    p = ClaudeChatProvider(Config(), None, tmp_path)
    old, new = P(), P()
    reader_old = aio.create_task(p._read(old))
    p.turn = _Turn(None, None, new)          # a question already sent to the NEW process
    old.stdout.feed_eof()                    # …and only now does the old process's output end
    await reader_old
    assert not p.turn.done.is_set()          # the new question is untouched
    new.stdout.feed_data(b'{"type":"result","subtype":"success","result":"ok"}\n')
    new.stdout.feed_eof()
    await p._read(new)
    assert p.turn.done.is_set() and p.turn.result["result"] == "ok"


async def test_single_web_question_keeps_sources_and_checks(core, fake):
    fake([["Курс сегодня 92,1 ₽. Источник: https://cbr.ru/currency_base/daily/"]])
    tid = await core.director.submit("Найди в интернете курс доллара на сегодня")
    await wait_done(core, tid)
    last = core.director.messages(core.director.conversation_id())[-1]
    assert "Проверка: Ответ модели" not in last["text"]
    vers = [st.get("verification") or {} for st in core.tasks.steps(tid)]
    assert any(v.get("status") == "verified" and "источники" in v.get("detail", "").lower() for v in vers), vers


async def test_session_cost_is_recorded_per_answer_not_cumulative(tmp_path):
    import asyncio as aio
    from jarvis.config import Config
    from jarvis.providers.claude_chat import ClaudeChatProvider, _Turn

    class P:
        def __init__(self):
            self.stdout = aio.StreamReader()
    c = ClaudeChatProvider(Config(), None, tmp_path)
    proc = P()
    reader = aio.create_task(c._read(proc))
    costs = []
    for total in (0.004, 0.0072, 0.0083):
        c.turn = _Turn(None, None, proc)
        proc.stdout.feed_data(('{"type":"result","subtype":"success","result":"ok","total_cost_usd":%s}\n' % total).encode())
        await aio.wait_for(c.turn.done.wait(), 2)
        costs.append(c.turn.result["turn_cost_usd"])
    proc.stdout.feed_eof()
    await reader
    assert costs == [0.004, 0.0032, 0.0011]


async def test_a_waiting_question_never_takes_over_the_tools_of_the_current_one(tmp_path):
    """Measured race: a scheduled task waiting for the conversation replaced the executor of the running task, and
    then cleared it mid-answer («Сейчас нет задачи…»). The executor now belongs to the turn holding the lock."""
    import asyncio as aio
    from jarvis.config import Config
    from jarvis.providers.claude_chat import ClaudeChatProvider

    class In:
        def write(self, b):
            pass

        async def drain(self):
            pass

    class P:
        returncode, pid = None, 0

        def __init__(self):
            self.stdin, self.stdout = In(), aio.StreamReader()
    c = ClaudeChatProvider(Config(), None, tmp_path)
    c.proc = P()
    c.reader = aio.create_task(c._read(c.proc))
    ex1, ex2 = object(), object()
    t1 = aio.create_task(c._send("первый", execute=ex1))
    await aio.sleep(0.01)
    t2 = aio.create_task(c._send("второй", execute=ex2))
    await aio.sleep(0.01)
    assert c.current_execute is ex1                       # the waiting question changed nothing
    c.proc.stdout.feed_data(b'{"type":"result","subtype":"success","result":"1"}\n')
    await aio.wait_for(t1, 2)
    await aio.sleep(0.01)
    assert c.current_execute is ex2                       # now it is the second question's turn
    c.proc.stdout.feed_data(b'{"type":"result","subtype":"success","result":"2"}\n')
    await aio.wait_for(t2, 2)
    assert c.current_execute is None
    c.proc.stdout.feed_eof()
    await c.reader


async def test_a_conversation_step_gets_the_results_it_depends_on(core, fake):
    """Measured: research → analyze → «покажи подборку» answered from an unrelated file, because the last
    (conversation) step never saw the research results."""
    from jarvis.agency.decision import Decision, Step
    g = fake([["Подборка: диван «Лофт», стол «Скандинавия»."]])
    agency = core.director.agency
    tid = core.tasks.create("тест", core.director.conversation_id())
    from jarvis.agency.agency import StepOut
    d = Decision(task_id=tid, objective="x", classification="mixed", risk="low", sensitivity="INTERNAL", mode="multi_ai",
                 steps=[Step(id="s1", action="research", instruction="найди", status="done"),
                        Step(id="s2", action="chat", instruction="Покажи подборку", depends_on=["s1"], executor="claude")])
    outs = {"s1": StepOut(True, "Тренды 2026: диван «Лофт» (источник: https://ex.example)", [], "claude")}
    out = await agency._attempt(core.director, tid, core.director.conversation_id(), d, d.steps[1], outs, True, "m1")
    assert out.ok and "Тренды 2026" in g.calls[0]["user"] and "Покажи подборку" in g.calls[0]["user"]


async def test_stop_also_stops_an_action_the_conversation_started(core, fake):
    """Measured: a Claude Code job started by the conversation kept running after «стоп» / the end of its task."""
    import asyncio as aio
    from conftest import wait_for
    started, cancelled = aio.Event(), []
    from jarvis.tools.base import Risk, Tool, ToolResult, ToolSpec

    class Slow(Tool):
        spec = ToolSpec(id="slow_job", title="Долгая работа", description="x", params={"type": "object"},
                        risk=Risk.LOW, side_effects="local", timeout_s=60)

        async def run(self, args, ctx):
            started.set()
            try:
                await aio.sleep(60)
            except aio.CancelledError:
                cancelled.append(1)
                raise
            return ToolResult({}, "ok")
    core.registry.tools["slow_job"] = Slow()
    fake([[("slow_job", {}), "Готово."]])
    tid = await core.director.submit("Сделай долгую работу")
    await aio.wait_for(started.wait(), 5)
    await core.director.cancel(tid)
    await wait_for(lambda: cancelled == [1], timeout=5)
    assert core.tasks.get(tid)["state"] == "cancelled"


async def test_stop_word_stops_a_running_job_but_quiet_keeps_it(core, fake):
    """«Стоп» stops the work in progress (spec: the current task stops); «замолчи» only silences."""
    import asyncio as aio
    from jarvis.tools.base import Risk, Tool, ToolResult, ToolSpec
    began = aio.Event()

    class Build(Tool):
        spec = ToolSpec(id="slow_build", title="Сборка", description="x", params={"type": "object"},
                        risk=Risk.LOW, side_effects="local", timeout_s=60)

        async def run(self, args, ctx):
            began.set()
            await aio.sleep(60)
            return ToolResult({}, "ok")
    core.registry.tools["slow_build"] = Build()
    fake([[("slow_build", {}), "Готово."], [("slow_build", {}), "Готово."]])
    tid = await core.director.submit("Собери проект")
    await aio.wait_for(began.wait(), 5)
    await core.director.submit("замолчи", "voice")
    await aio.sleep(0.2)
    assert core.tasks.get(tid)["state"] == "running"                  # «замолчи» does not stop the job
    await core.director.submit("Стоп.", "voice")
    await wait_done(core, tid)
    assert core.tasks.get(tid)["state"] == "cancelled"
    assert all(s["state"] != "running" for s in core.tasks.steps(tid))      # no step left «выполняется»
    assert core.director.messages(core.director.conversation_id())[-1]["text"].startswith("Остановил: Собери проект")
