"""«Полный доступ»: what it opens, what still asks, what stays closed."""
from pathlib import Path

import pytest

from jarvis.comms.base import Contact
from jarvis.tools.base import ToolInputError, resolve_in_scopes

from conftest import wait_done
from test_comms import FakeMessenger, Intents, PEOPLE, pending_send


@pytest.fixture
def hub(core):
    h = core.comms
    h.providers = {pid: FakeMessenger(pid, PEOPLE[pid]) for pid in ("telegram", "whatsapp", "email")}
    return h


def test_secrets_are_closed_at_any_level(core):
    core.director.set_access("full")
    home = Path.home()
    for p in ("~/.ssh/id_ed25519", "~/.config/chromium/Default/Cookies", "~/.local/share/keyrings/login.keyring",
              "~/Projects/x/.env"):
        with pytest.raises(ToolInputError):
            resolve_in_scopes(p, [home])


def test_full_access_widens_scopes_and_is_reversible(core):
    d = core.director
    assert "~" not in core.config.read_scopes and d.core_state()["access"] == "standard"
    d.set_access("full")
    assert "~" in core.config.read_scopes and "~" in core.config.code_workspaces
    assert d.core_state()["access"] == "full" and core.db.get_kv("access_level") == "full"
    d.set_access("standard")
    assert "~" not in core.config.read_scopes and not core.policy.full_access


def test_full_access_skips_moderate_questions_but_not_high(core):
    from jarvis.tools.base import Risk, ToolSpec
    mod = ToolSpec("x", "x", "x", {}, Risk.MODERATE, "local")
    ext = ToolSpec("y", "y", "y", {}, Risk.MODERATE, "external")
    high = ToolSpec("z", "z", "z", {}, Risk.HIGH, "local")
    core.director.set_access("full")
    assert core.policy.evaluate(mod, {}).action == "allow"
    assert core.policy.evaluate(ext, {}).action == "ask"       # anything leaving the computer still asks
    assert core.policy.evaluate(high, {}).action == "ask"
    core.director.set_access("standard")
    assert core.policy.evaluate(mod, {}).action == "ask"


async def test_full_access_estop_still_blocks(core):
    from jarvis.tools.base import Risk, ToolSpec
    core.director.set_access("full")
    await core.director.estop()
    assert core.policy.evaluate(ToolSpec("x", "x", "x", {}, Risk.MODERATE, "local"), {}).action == "deny"


async def test_message_to_self_needs_no_question_but_others_do(core, hub):
    core.director.set_access("full")
    hub.providers["telegram"].people = PEOPLE["telegram"]
    hub.providers["telegram"].search = _self_search(hub.providers["telegram"])
    core.director.comms.local_first = False
    core.director.comms.ai = Intents(
        {"action": "send", "recipients": [{"name": "себе", "platform": "telegram"}], "message": "тест"},
        {"action": "send", "recipients": [{"name": "Иван Петров", "platform": "telegram"}], "message": "Привет"})
    t = await core.director.submit("Отправь себе в Telegram: тест")
    await wait_done(core, t)
    assert hub.providers["telegram"].sent[0][:2] == ("self", "тест")
    assert hub.outbox.list()[0]["auth"] == "full:self"
    t = await core.director.submit("Отправь Ивану Петрову в Telegram: Привет")
    a = await pending_send(core)                      # another person: still asks
    core.director.decide(a["id"], False)
    await wait_done(core, t)


async def test_voice_answers_any_confirmation_except_durable_permissions(core):
    for level in ("full", "standard"):
        core.director.set_access(level)
        assert core.director.voice_approvable("claude_code_change") and core.director.voice_approvable("create_note")
        assert core.director.voice_approvable("message_send")
        assert not core.director.voice_approvable("trust_rule_add")    # durable permission changes: click only


async def _note_waiting(core, text="запиши заметку: купить молоко"):
    from conftest import wait_for
    tid = await core.director.submit(text)
    await wait_for(lambda: any(a["task_id"] == tid for a in core.director.approvals.pending()))
    return tid


async def test_spoken_razreshayu_approves_the_waiting_action(core):
    from conftest import wait_for
    core.director.set_access("standard")
    tid = await _note_waiting(core)
    assert await core.director.submit("Разрешаю.", "voice") == tid
    await wait_for(lambda: core.tasks.get(tid)["state"] == "completed")
    assert not [t for t in core.tasks.recent(10) if t["request"].startswith("Разрешаю")]   # not a new request


async def test_spoken_ne_razreshayu_denies(core):
    from conftest import wait_for
    tid = await _note_waiting(core)
    await core.director.submit("Нет, не разрешаю", "voice")
    await wait_for(lambda: core.tasks.get(tid)["state"] in ("completed", "cancelled", "failed"))
    assert not any(a["task_id"] == tid for a in core.director.approvals.pending())
    assert core.db.one("SELECT status FROM approvals WHERE task_id=?", (tid,))["status"] == "denied"


async def test_two_waiting_actions_are_never_picked_at_random(core):
    t1 = await _note_waiting(core, "запиши заметку: первая")
    t2 = await _note_waiting(core, "запиши заметку: вторая")
    assert await core.director.submit("да", "voice") == ""
    assert {a["task_id"] for a in core.director.approvals.pending()} == {t1, t2}
    assert "несколько действий" in core.director.messages(core.director.conversation_id())[-1]["text"]


async def test_bearer_api_cannot_answer_confirmations(core):
    """The local API (scripts, `jarvis say`) can never pretend to be the window and say «да»."""
    from aiohttp.test_utils import TestClient, TestServer
    from jarvis.server import Server
    tid = await _note_waiting(core)
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    try:
        r = await tc.post("/api/submit", json={"text": "да", "source": "voice"},
                          headers={"Host": f"127.0.0.1:{core.config.port}", "Authorization": f"Bearer {srv.token}"})
        assert r.status == 200
        assert any(a["task_id"] == tid for a in core.director.approvals.pending())      # still waiting
    finally:
        await tc.close()


def _self_search(p):
    orig = p.search

    async def search(name):
        if name == "себе":
            return [Contact("telegram", "self", "Избранное (вы сами)", "Saved Messages", "test")]
        return await orig(name)
    return search
