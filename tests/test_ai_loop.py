"""Director ↔ Gemini loop with a scripted provider (VERIFIED MOCK, not a live API test).
Checks: model tool calls go through the policy gateway, approvals still apply,
history/function responses are threaded correctly, provider errors are honest."""
import pytest

from jarvis.providers.gemini import ProviderError
from jarvis.tasks import S

from conftest import wait_done, wait_for


@pytest.fixture
def fake(core):
    from conftest import ScriptedGemini, install

    def go(turns):
        g = ScriptedGemini(core.config, turns)
        install(core, gemini=g)
        return g
    return go


async def test_model_tool_call_runs_through_gateway(core, fake):
    g = fake([[("system_info", {})], ["Свободно много места."]])
    tid = await core.director.submit("проверь, всё ли в порядке с компьютером")
    t = await wait_done(core, tid)
    assert t["state"] == S.COMPLETED
    assert [s["tool_id"] for s in t["steps"]] == ["agent_chat", "system_info"]
    assert t["steps"][1]["verification"]["status"] == "verified"
    # second model call received the function response
    last = g.calls[1][-1]
    assert last.role == "user" and last.parts[0].function_response.name == "system_info"
    assert last.parts[0].function_response.response["status"] == "ok"
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert msg["text"] == "Свободно много места." and msg["meta"]["cloud"] is True


async def test_model_cannot_skip_approval(core, fake, home):
    fake([[("create_note", {"text": "от модели"})], ["Готово."]])
    tid = await core.director.submit("сохрани мысль про окна")
    await wait_for(lambda: core.approvals.pending())
    assert not (home / "out").exists()
    core.director.decide(core.approvals.pending()[0]["id"], False)
    t = await wait_done(core, tid)
    assert [x for x in t["steps"] if x["tool_id"] == "create_note"][0]["state"] == "denied"
    assert not (home / "out").exists()


async def test_model_cannot_call_ui_only_or_unknown_tools(core, fake):
    g = fake([[("flora_send", {"draft_id": "x"}), ("shell_exec", {"cmd": "rm -rf ~"})], ["Не могу."]])
    tid = await core.director.submit("сделай что-нибудь полезное с почтой")
    t = await wait_done(core, tid)
    states = {s["tool_id"]: s["state"] for s in t["steps"]}
    assert states.get("flora_send") in (None, "invalid", "denied")   # not model-callable: never runs
    assert "shell_exec" not in states
    assert not core.director.flora.mail.sent_all()


async def test_send_request_is_never_executed_without_mail_and_approval(core, fake):
    g = fake([])
    tid = await core.director.submit("отправь всё")
    t = await wait_done(core, tid)
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert "Ничего не отправлено" in msg or "модуль связи" in msg
    assert not g.calls and not core.director.flora.mail.sent_all()


async def test_provider_error_is_reported_not_faked(core, fake):
    fake([ProviderError("quota", "Исчерпана квота Gemini")])
    tid = await core.director.submit("расскажи что-нибудь интересное")
    t = await wait_done(core, tid)
    assert t["state"] == S.FAILED
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert "квота" in msg["text"]


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
    fake([[("system_info", {})], [("system_info", {})], [("system_info", {})]])
    tid = await core.director.submit("зацикленный запрос")
    t = await wait_done(core, tid)
    assert len(t["steps"]) == 1 or len(t["steps"]) == 2
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert "лимит шагов" in msg["text"]
