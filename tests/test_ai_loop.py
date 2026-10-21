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
    t = await wait_done(core, tid)
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
