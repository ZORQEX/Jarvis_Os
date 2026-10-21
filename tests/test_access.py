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


async def test_voice_yes_answers_any_confirmation_in_full_access(core):
    core.director.set_access("full")
    assert core.director.voice_approvable("claude_code_change")
    assert not core.director.voice_approvable("trust_rule_add")    # durable permission changes: click only
    core.director.set_access("standard")
    assert not core.director.voice_approvable("claude_code_change")
    assert core.director.voice_approvable("message_send")


def _self_search(p):
    orig = p.search

    async def search(name):
        if name == "себе":
            return [Contact("telegram", "self", "Избранное (вы сами)", "Saved Messages", "test")]
        return await orig(name)
    return search
