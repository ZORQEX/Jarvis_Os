"""New abilities: system programs (password in the system dialog), browser control, publishing sites."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from jarvis.tools.base import CmdResult, ToolContext, ToolInputError


def _ctx(core):
    return ToolContext(core.config, "t_test")


# ---------------------------------------------------------------- system programs
def _fake_pacman(installed, repo):
    def run(*args, timeout=15):
        op, name = args[0], args[1]
        ok = name in repo if op == "-Si" else name in installed
        return subprocess.CompletedProcess(args, 0 if ok else 1, "", "")
    return run


def test_system_install_checks_names_repos_and_skips_installed(core, monkeypatch):
    from jarvis.tools import install
    monkeypatch.setattr(install, "_pacman", _fake_pacman({"git"}, {"git", "nodejs", "postgresql"}))
    monkeypatch.setattr(install.shutil, "which", lambda n: "/usr/bin/" + n)
    t = core.registry.tools["system_install"]
    assert t.spec.risk.value == "high" and t.spec.model_callable
    a = t.validate({"packages": ["git", "NodeJS", "postgresql", "nodejs"]}, _ctx(core))
    assert a["packages"] == ["nodejs", "postgresql"]                     # already installed skipped, deduplicated
    for bad in (["rm -rf /"], ["yay-only-pkg"], ["git"]):
        with pytest.raises(ToolInputError):
            t.validate({"packages": bad}, _ctx(core))


async def test_system_install_asks_the_system_for_the_password_and_checks(core, monkeypatch):
    from jarvis.tools import install
    calls = []

    async def fake_run(argv, timeout=10.0, **k):
        calls.append(argv)
        if argv[0] == "pkexec":
            return CmdResult(argv, 0, "installed", "", 1.0)
        return CmdResult(argv, 0, "nodejs 26", "", 1.0)
    monkeypatch.setattr(install, "run_cmd", fake_run)
    t = core.registry.tools["system_install"]
    ctx = SimpleNamespace(progress=lambda *a: None)
    r = await t.run({"packages": ["nodejs"]}, ctx)
    assert calls[0] == ["pkexec", "pacman", "-S", "--needed", "--noconfirm", "nodejs"]   # never sudo, never a password
    v = await t.verify({"packages": ["nodejs"]}, r, ctx)
    assert v.status == "verified"

    async def cancelled(argv, timeout=10.0, **k):
        return CmdResult(argv, 126, "", "", 1.0)
    monkeypatch.setattr(install, "run_cmd", cancelled)
    with pytest.raises(ToolInputError, match="Пароль не введён"):
        await t.run({"packages": ["nodejs"]}, ctx)


# ---------------------------------------------------------------- browser
def test_browser_only_when_the_task_allows_it():
    from jarvis.providers.claude_code import PLAYWRIGHT_MCP, build_argv
    plain = build_argv("x", "change", allow={"install"}, workdir="/w")
    assert "--mcp-config" not in plain and "mcp__browser" not in plain
    b = build_argv("x", "change", allow={"browser"}, workdir="/w")
    cfg = json.loads(b[b.index("--mcp-config") + 1])
    args = cfg["mcpServers"]["browser"]["args"]
    assert PLAYWRIGHT_MCP in args and "--isolated" in args and "--headless" in args     # no user logins/cookies
    assert "mcp__browser" in b and "--strict-mcp-config" in b and "Bash(sudo:*)" in b


def test_computer_task_accepts_browser_permission(core, home):
    core.config.code_workspaces = [str(home / "Projects")]
    (home / "Projects").mkdir()
    t = core.registry.tools["computer_task"]
    a = t.validate({"instruction": "Зайди на сайт и сохрани цены", "new_folder": "prices", "allow": ["browser"]},
                   _ctx(core))
    assert a["permissions"] == ["browser"] and "управление браузером" in t.describe(a)


# ---------------------------------------------------------------- publishing
def _site(home, extra=None):
    d = home / "Projects" / "cafe"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>Кафе</title>")
    for name in extra or []:
        (d / name).write_text("x")
    return d


def test_publish_refuses_secrets_and_non_sites(core, home):
    core.config.code_workspaces = [str(home / "Projects")]
    t = core.registry.tools["site_publish"]
    d = _site(home, [".env"])
    with pytest.raises(ToolInputError, match="ключи или пароли"):
        t.validate({"folder": str(d)}, _ctx(core))
    (d / ".env").unlink()
    (d / "index.html").unlink()
    with pytest.raises(ToolInputError, match="index.html"):
        t.validate({"folder": str(d)}, _ctx(core))


async def test_publish_pushes_with_gh_and_returns_the_public_link(core, home, monkeypatch):
    import jarvis.publish as pub
    core.config.code_workspaces = [str(home / "Projects")]
    d = _site(home)
    t = core.registry.tools["site_publish"]
    monkeypatch.setattr(pub.shutil, "which", lambda n: "/usr/bin/" + n)
    a = t.validate({"folder": str(d), "name": "Cafe Lumiere"}, _ctx(core))
    assert a["name"] == "cafe-lumiere" and t.spec.side_effects == "external" and t.spec.risk.value == "high"
    calls = []

    async def fake_run(argv, timeout=10.0, **k):
        calls.append(argv)
        if argv[:2] == ["gh", "api"] and argv[2] == "user":
            return CmdResult(argv, 0, "tim-dev 42", "", 1)
        if "remote" in argv and "get-url" in argv:
            ok = any(c[:3] == ["gh", "repo", "create"] for c in calls)
            return CmdResult(argv, 0 if ok else 2, "https://github.com/tim-dev/cafe-lumiere.git" if ok else "", "", 1)
        return CmdResult(argv, 0, "", "", 1)
    monkeypatch.setattr(pub, "run_cmd", fake_run)
    r = await t.run(a, SimpleNamespace(progress=lambda *x: None))
    assert r.data["url"] == "https://tim-dev.github.io/cafe-lumiere/"
    flat = [" ".join(c) for c in calls]
    assert any("gh repo create cafe-lumiere --public" in c for c in flat)
    assert any("push -u origin main" in c and "credential.helper=!gh auth git-credential" in c for c in flat)
    assert any("repos/tim-dev/cafe-lumiere/pages" in c for c in flat)
    assert "*.pem" in (d / ".gitignore").read_text()                 # secrets never committed later either


async def test_publish_without_github_says_how_to_connect(core, home, monkeypatch):
    import jarvis.publish as pub
    core.config.code_workspaces = [str(home / "Projects")]
    d = _site(home)
    t = core.registry.tools["site_publish"]

    async def no_login(argv, timeout=10.0, **k):
        return CmdResult(argv, 1, "", "not logged in", 1)
    monkeypatch.setattr(pub, "run_cmd", no_login)
    monkeypatch.setattr(pub.shutil, "which", lambda n: "/usr/bin/" + n)
    with pytest.raises(ToolInputError, match="Подключить GitHub"):
        await t.run({"folder": str(d), "name": "cafe"}, SimpleNamespace(progress=lambda *x: None))


def test_new_abilities_are_offered_to_the_conversation(core):
    names = {t.spec.id for t in core.registry.model_tools()}
    assert {"computer_task", "system_install", "site_publish"} <= names


# ---------------------------------------------------------------- quick file actions and read-only terminal
def _home_core(core, home, monkeypatch):
    import pathlib
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    core.config.claude_home_wide = True


async def test_quick_file_tools_create_write_list_and_trash(core, home, monkeypatch):
    _home_core(core, home, monkeypatch)
    reg = core.registry.tools
    ctx = _ctx(core)
    a = reg["folder_create"].validate({"path": "~/Desktop/JARVIS_TEST"}, ctx)
    assert a["path"] == str(home / "Desktop" / "JARVIS_TEST")
    r = await reg["folder_create"].run(a, ctx)
    assert (home / "Desktop" / "JARVIS_TEST").is_dir() and (await reg["folder_create"].verify(a, r, ctx)).status == "verified"
    w = reg["file_write"].validate({"path": "~/Desktop/JARVIS_TEST/test.txt", "text": "JARVIS SYSTEM TEST"}, ctx)
    r = await reg["file_write"].run(w, ctx)
    assert (home / "Desktop/JARVIS_TEST/test.txt").read_text() == "JARVIS SYSTEM TEST"
    assert (await reg["file_write"].verify(w, r, ctx)).status == "verified"
    with pytest.raises(ToolInputError, match="уже есть"):           # never silently replaces a file
        reg["file_write"].validate({"path": "~/Desktop/JARVIS_TEST/test.txt", "text": "x"}, ctx)
    l = reg["folder_list"].validate({"path": "~/Desktop/JARVIS_TEST"}, ctx)
    r = await reg["folder_list"].run(l, ctx)
    assert [e["name"] for e in r.data["entries"]] == ["test.txt"]
    assert reg["file_trash"].spec.risk.value == "high"               # deleting always asks
    for bad in ("~/.ssh/x", "/etc/passwd"):
        with pytest.raises(ToolInputError):
            reg["file_write"].validate({"path": bad, "text": "x"}, ctx)


async def test_terminal_is_read_only(core, home, monkeypatch):
    _home_core(core, home, monkeypatch)
    t = core.registry.tools["shell_read"]
    ctx = _ctx(core)
    (home / "a").mkdir()
    (home / "a" / "f.txt").write_text("x")
    a = t.validate({"command": "ls -la ~/a"}, ctx)
    r = await t.run(a, ctx)
    assert r.data["exit_code"] == 0 and "f.txt" in r.data["stdout"]
    for bad in ("rm -rf ~/a", "ls | sh", "find ~ -delete", "cat ~/.ssh/id_rsa", "git push", "env", "bash -c ls",
                "ls > out.txt", "sudo ls"):
        with pytest.raises(ToolInputError):
            t.validate({"command": bad}, ctx)


# ---------------------------------------------------------------- scheduled actions
async def test_scheduled_action_runs_at_its_time_like_a_typed_command(core):
    from conftest import wait_for
    reg = core.registry.tools
    ctx = _ctx(core)
    a = reg["task_schedule"].validate({"command": "который час", "minutes": 0.1}, ctx)
    r = await reg["task_schedule"].run(a, ctx)
    rid = r.data["id"]
    assert core.scheduler.get(rid)["command"] == "который час"
    core.scheduler.reschedule(rid, __import__("time").time() - 1)          # its time has come
    core.scheduler.start()
    try:
        await wait_for(lambda: core.scheduler.get(rid)["status"] == "fired", timeout=5)
        await wait_for(lambda: any(m["meta"].get("source") == "scheduled" for m in
                                   core.director.messages(core.director.conversation_id())), timeout=5)
        msgs = core.director.messages(core.director.conversation_id())
        assert msgs[-1]["role"] == "assistant" and "Сейчас" in msgs[-1]["text"]     # really executed
    finally:
        await core.scheduler.stop()


async def test_cancel_and_move_the_latest_scheduled_action(core):
    import time as _t
    reg = core.registry.tools
    ctx = _ctx(core)
    r = await reg["task_schedule"].run(reg["task_schedule"].validate(
        {"command": "создай файл cancel_test.txt", "minutes": 10}, ctx), ctx)
    c = reg["scheduled_change"].validate({"op": "cancel"}, ctx)          # «отмени эту задачу» = the latest
    assert c["id"] == r.data["id"]
    out = await reg["scheduled_change"].run(c, ctx)
    assert (await reg["scheduled_change"].verify(c, out, ctx)).status == "verified"
    assert core.scheduler.get(r.data["id"])["status"] == "cancelled"
    r2 = await reg["task_schedule"].run(reg["task_schedule"].validate(
        {"command": "создай файл edit_test.txt", "minutes": 20}, ctx), ctx)
    m = reg["scheduled_change"].validate({"op": "move", "minutes": 5}, ctx)
    out = await reg["scheduled_change"].run(m, ctx)
    due = core.scheduler.get(r2.data["id"])["due_at"]
    assert abs(due - (_t.time() + 300)) < 5 and (await reg["scheduled_change"].verify(m, out, ctx)).status == "verified"


async def test_an_action_missed_while_jarvis_was_off_is_reported_not_run(core):
    import time as _t
    from conftest import wait_for
    rid = core.scheduler.add("создай файл x.txt", _t.time() - 3600, None, command="создай файл x.txt")["id"]
    core.scheduler.start()
    try:
        await wait_for(lambda: core.scheduler.get(rid)["status"] == "missed", timeout=5)
        await wait_for(lambda: "Пропущено действие" in core.director.messages(core.director.conversation_id())[-1]["text"])
        assert not any(t["request"] == "создай файл x.txt" for t in core.tasks.recent(20))
    finally:
        await core.scheduler.stop()
