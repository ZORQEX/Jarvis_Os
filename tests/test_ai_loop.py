"""Director ↔ Gemini loop with a scripted provider (VERIFIED MOCK, not a live API test).
Checks: model tool calls go through the policy gateway, approvals still apply,
history/function responses are threaded correctly, provider errors are honest."""
import pytest
from google.genai import types

from jarvis.providers.gemini import ProviderError
from jarvis.tasks import S

from conftest import wait_done, wait_for


class ScriptedGemini:
    """Replays a fixed list of turns; records what the director sent."""

    def __init__(self, real, turns):
        self.real, self.turns, self.calls = real, list(turns), []
        self.status = "ready"

    def configured(self):
        return True

    def pick_model(self, role):
        return "gemini-3.8-flash"

    def build_tools(self, tools):
        return self.real.build_tools(tools)

    def describe(self):
        return {"configured": True, "status": "ready", "models": []}

    async def stream(self, model, contents, tools, **kw):
        self.calls.append([c.model_copy(deep=True) for c in contents])
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        parts = []
        for item in turn:
            if isinstance(item, str):
                parts.append(types.Part.from_text(text=item))
                yield {"type": "text", "text": item}
            else:
                name, args = item
                fc = types.FunctionCall(name=name, args=args)
                parts.append(types.Part(function_call=fc))
                yield {"type": "call", "call": fc}
        yield {"type": "done", "content": types.Content(role="model", parts=parts), "usage": {"total_token_count": 42},
               "first_token_ms": 300, "total_ms": 900}


@pytest.fixture
def fake(core):
    def install(turns):
        g = ScriptedGemini(core.gemini, turns)
        core.director.gemini = g
        return g
    return install


async def test_model_tool_call_runs_through_gateway(core, fake):
    g = fake([[("system_info", {})], ["Свободно много места."]])
    tid = await core.director.submit("проверь, всё ли в порядке с компьютером")
    t = await wait_done(core, tid)
    assert t["state"] == S.COMPLETED
    assert [s["tool_id"] for s in t["steps"]] == ["system_info"]
    assert t["steps"][0]["verification"]["status"] == "verified"
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
    assert t["steps"][0]["state"] == "denied"
    assert not (home / "out").exists()


async def test_model_cannot_call_ui_only_or_unknown_tools(core, fake):
    fake([[("flora_send", {"draft_id": "x"}), ("shell_exec", {"cmd": "rm -rf ~"})], ["Не могу."]])
    tid = await core.director.submit("отправь всё")
    t = await wait_done(core, tid)
    states = {s["tool_id"]: s["state"] for s in t["steps"]}
    assert states.get("flora_send") in ("invalid", "denied")   # unknown draft → invalid; never sent
    assert "shell_exec" not in states                           # unknown tool is refused before a step exists
    assert not core.director.flora.mail.sent_all()


async def test_provider_error_is_reported_not_faked(core, fake):
    fake([ProviderError("quota", "Исчерпана квота Gemini")])
    tid = await core.director.submit("напиши стихотворение")
    t = await wait_done(core, tid)
    assert t["state"] == S.FAILED
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert "квота" in msg["text"] and msg["meta"]["error_kind"] == "quota"


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
