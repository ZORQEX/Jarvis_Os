"""«Возможности» (jarvis/hub, from morph-rq/jarvis-os): OpenClaw and Hermes behind JARVIS, the capability catalog, the
model endpoint Hermes thinks with, the agents' search bridge, the coding-agent hooks (Coucou idea).
VERIFIED LOCAL against stand-ins (tests/hub_fakes.py): never the real agents, the person's Claude or keyring."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jarvis import router, secrets
from jarvis.hub import backends as bk
from jarvis.hub import coding as cm
from jarvis.hub.hermes import Hermes, parse_frontmatter
from jarvis.hub.llm import ClaudeModel, parse_turn, transcript
from jarvis.hub.openclaw import GatewayClient, OpenClaw, _schedule
from jarvis.hub.registry import Capability, Registry
from jarvis.hub.select import plan
from jarvis.tasks import S
from jarvis.tools.base import Risk, Tool, ToolResult, ToolSpec, Verification

from conftest import wait_for
from hub_fakes import FakeGateway, FakeHermes

ROOT = Path(__file__).resolve().parents[1]
TOKEN, HKEY = "gw-token-0123456789abcdef0123", "hermes-key-0123456789abcdef"


@pytest.fixture
def vault(monkeypatch):
    """The keyring, in memory (tests never write the real one)."""
    box: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get", lambda a: box.get(a))
    monkeypatch.setattr(secrets, "store", lambda a, v, label: box.__setitem__(a, v))
    monkeypatch.setattr(secrets, "clear", lambda a: box.pop(a, None))
    return box


# ---------------------------------------------------------------- the two clients

async def test_openclaw_client_reads_the_gateway_and_runs_an_agent_turn():
    gw = await FakeGateway(TOKEN).start()
    c = GatewayClient(gw.url, token=TOKEN)
    oc = OpenClaw(c)
    c.start()
    try:
        assert await c.wait_ready(5) and c.server["version"] == "2026.10.6"
        assert [s["name"] for s in await oc.skills() if s["eligible"]] == ["github", "weather"]
        jobs = await oc.automations()
        assert jobs[0]["system"] and not jobs[0]["enabled"] and jobs[1]["schedule"] == "по будням в 09:00"
        r = await oc.run_agent("Какого цвета небо?", timeout=5)
        assert r == {"status": "ok", "text": "Голубое.", "run_id": r["run_id"], "error": ""} and not oc.runs
    finally:
        await c.close()
        await gw.stop()


async def test_a_wrong_token_is_not_retried():
    gw = await FakeGateway(TOKEN).start()
    c = GatewayClient(gw.url, token="wrong")
    c.start()
    try:
        await wait_for(lambda: c.state == "auth_failed", 5)
        n = sum(1 for m, _ in gw.requests if m == "connect")
        await asyncio.sleep(1.5)
        assert sum(1 for m, _ in gw.requests if m == "connect") == n == 1     # the person must fix it; no hammering
    finally:
        await c.close()
        await gw.stop()


async def test_hermes_runs_and_every_risky_step_is_asked():
    h_srv = await FakeHermes(HKEY).start()
    h = Hermes(h_srv.url, key=HKEY)
    try:
        assert (await h.status())["healthy"]
        assert (await Hermes(h_srv.url, key="bad").status())["state"] == "auth_failed"
        h_srv.ask_approval = True
        asked = []

        async def deny(ev):
            asked.append(ev["command"])
            return False
        r = await h.delegate("убери временные файлы", approve=deny, timeout=5)
        assert asked == ["rm /tmp/x"] and list(h_srv.answers.values()) == ["deny"] and r["ok"]
        assert "запрещ" in r["output"] and r["tools"] == ["read_file"] and not h.runs
    finally:
        await h.close()
        await h_srv.stop()


# ---------------------------------------------------------------- the model Hermes thinks with

def test_conversation_and_answer_shapes():
    system, convo = transcript([
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": [{"type": "text", "text": "Прочитай x.txt"}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "read_file",
                                                                                        "arguments": "{\"path\": \"x\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "42"}])
    assert system == "You are Hermes." and '<tool_call id="c1" name="read_file">' in convo and 'role="tool"' in convo
    assert parse_turn('```json\n{"content": "", "actions": [{"name": "a", "arguments": {}}]}\n```')["actions"][0]["name"] == "a"
    assert parse_turn("Просто ответ") == {"content": "Просто ответ", "actions": []}


async def test_model_turns_become_tool_calls_and_streams():
    seen = {}

    async def scripted(system, prompt, model, schema):
        seen.update(system=system, model=model, schema=schema)
        return {"content": "", "actions": [{"name": "read_file", "arguments": {"path": "/tmp/x"}},
                                            {"name": "rm_rf", "arguments": {}}], "usage": {"input_tokens": 7}}

    class A:
        def write(self, *a, **k):
            pass
    from jarvis.config import Config
    m = ClaudeModel(Config(), A(), runner=scripted)
    body = {"model": "gpt-whatever", "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
    r = await m.complete(body)
    calls = r["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["read_file"]          # an action it was not offered is dropped
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "/tmp/x"} and r["choices"][0]["finish_reason"] == "tool_calls"
    assert seen["model"] == "claude-sonnet-5-5" and "read_file" in seen["system"]     # only Claude, whatever is asked
    chunks = ClaudeModel.chunks(r, include_usage=True)
    assert chunks[-1] == "data: [DONE]\n\n" and '"tool_calls"' in "".join(chunks) and '"usage"' in chunks[-2]


# ---------------------------------------------------------------- inside the core

@pytest.fixture
async def hub(core, vault, monkeypatch, tmp_path, request):
    """The core's hub against the stand-ins: agents «installed», never started as real processes."""
    gw, hm = FakeGateway(TOKEN), FakeHermes(HKEY)
    await gw.start()
    await hm.start()
    caps = core.director.capabilities
    caps.cfg.openclaw_enabled = caps.cfg.hermes_enabled = True
    caps.cfg.openclaw_on_demand = request.param if hasattr(request, "param") else False
    caps.cfg.openclaw_port, caps.cfg.hermes_port = gw.port, hm.port
    caps.store.store("hub-openclaw-token", TOKEN, "t")
    caps.store.store("hub-hermes-key", HKEY, "t")
    monkeypatch.setattr(bk.Layout, "installed", lambda self, name: True)
    monkeypatch.setattr(bk, "setup", lambda *a, **k: [])
    monkeypatch.setattr(bk.Child, "start", lambda self: None)
    monkeypatch.setattr(bk, "agent_pids", lambda name, layout: [])
    from jarvis.hub.tools import tools as hub_tools
    for t in hub_tools(caps):
        core.registry.tools[t.spec.id] = t
    await caps.start()
    if not caps.cfg.openclaw_on_demand:
        await caps.backends.openclaw.c.wait_ready(5)
    await caps.registry.refresh("openclaw", "hermes")
    caps.gw, caps.hm = gw, hm
    yield caps
    await caps.stop()
    await gw.stop()
    await hm.stop()


async def run_with_approvals(core, tool, args, approve=True, expect=1, timeout=8):
    tid = core.tasks.create(f"тест {tool}", None, route="test")
    core.tasks.transition(tid, S.RUNNING)
    job = asyncio.ensure_future(core.gateway.execute(tid, tool, args, "тест"))
    seen = []
    t0 = time.monotonic()
    while len(seen) < expect and time.monotonic() - t0 < timeout:
        for a in core.approvals.pending():
            if a["id"] not in [s["id"] for s in seen]:
                seen.append(a)
                core.approvals.decide(a["id"], approve)       # the task is not the director's: no resume path
        await asyncio.sleep(0.02)
    return await asyncio.wait_for(job, timeout), seen


async def test_one_catalog_over_jarvis_and_both_agents(hub):
    reg = hub.registry
    assert reg.get("openclaw:skill:github").usable
    assert reg.get("openclaw:skill:weather").duplicate_of == "jarvis:tool:weather"     # JARVIS's own weather wins
    assert not reg.get("openclaw:skill:pdf-editor").usable                             # missing qpdf
    assert plan(reg, "github issues")["choice"]["id"] == "openclaw:skill:github"
    assert plan(reg, "какая погода")["choice"]["id"] == "jarvis:tool:weather"           # Russian intent words
    assert plan(reg, "pdf editor")["action"] == "enable"                                # there, but missing qpdf
    snap = await hub.op({"action": "snapshot"})
    assert snap["backends"]["openclaw"]["state"] == "connected" and snap["backends"]["hermes"]["healthy"]
    assert snap["automations"][1]["schedule"] == "по будням в 09:00"
    rows = await hub.op({"action": "automation_pause", "target": "hb"})
    assert rows["ok"] is False and "служебная" in rows["message"]                     # OpenClaw's own job stays


async def test_an_upstream_skill_runs_only_after_a_yes(hub, core):
    out, asked = await run_with_approvals(core, "capability_use", {"id": "openclaw:skill:github", "request": "мои issues"})
    assert [a["risk"] for a in asked] == ["high"] and out.status == "ok"
    assert out.result.data["text"] == "Голубое." and out.result.summary == "OpenClaw: Голубое." and out.verification.status == "unverified"
    assert any(m == "agent" and "github" in p["message"] for m, p in hub.gw.requests)
    out, asked = await run_with_approvals(core, "capability_use", {"id": "openclaw:skill:github", "request": "ещё"},
                                          approve=False)
    assert out.status == "denied" and sum(1 for m, _ in hub.gw.requests if m == "agent") == 1


async def test_hermes_steps_become_jarvis_approval_cards(hub, core):
    hub.hm.ask_approval = True
    out, asked = await run_with_approvals(core, "specialist", {"task": "убери временные файлы"}, expect=2)
    assert [a["tool_id"] for a in asked] == ["specialist", "hermes_step"]
    assert asked[1]["risk"] == "high" and "rm /tmp/x" in asked[1]["summary"]["action"]
    assert out.status == "ok" and list(hub.hm.answers.values()) == ["once"] and out.verification.status == "verified"


async def test_the_agents_search_through_jarvis_fast_search(hub, core):
    class FakeSearch(Tool):
        spec = ToolSpec(id="web_search", title="Поиск в интернете", description="search", risk=Risk.LOW,
                        params={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
                        side_effects="none")

        async def run(self, args, ctx):
            return ToolResult({"sources": [{"n": 1, "url": "https://python.org"}]}, "Найдено источников: 1")

        async def verify(self, args, result, ctx):
            return Verification("verified", "1")
    core.registry.tools["web_search"] = FakeSearch()
    out = await hub.research_call("web_search", {"query": "python latest"})
    assert out["status"] == "ok" and out["data"]["sources"][0]["url"] == "https://python.org"
    t = core.tasks.recent(5)[0]
    assert t["route"] == "agent_research" and t["state"] == "completed"           # visible, through the Gateway
    assert (await hub.research_call("rm", {}))["status"] == "invalid"            # nothing but search


async def test_emergency_stop_reaches_the_agents(hub, core):
    hub.gw.runs_delay = 5
    orig = hub.gw._agent

    async def slow(p):
        r = await orig(p)
        hub.gw.runs[r["runId"]]["delay"] = 5
        return r
    hub.gw.methods["agent"] = slow
    job = asyncio.ensure_future(hub.backends.openclaw.run_agent("долго", timeout=10))
    await wait_for(lambda: hub.backends.openclaw.runs, 3)
    await core.director.estop()
    assert any(m == "chat.abort" for m, _ in hub.gw.requests)
    assert (await asyncio.wait_for(job, 8))["status"] == "aborted"
    assert hub.llm.blocked()                                                       # Hermes's next model turn is refused
    core.director.policy.estop = False
    stopped = await hub.backends.stop_all()
    assert stopped == {"openclaw_runs": 0, "hermes_runs": 0} and hub.backends.lockdown
    assert (await hub.invoke("openclaw:skill:github", "x"))["ok"] is False


async def test_model_endpoint_and_search_bridge_have_their_own_keys(core, vault):
    from jarvis.server import Server

    async def scripted(system, prompt, model, schema):
        return {"content": "Привет", "actions": [], "usage": {}}
    srv = Server(core)
    core.director.capabilities.llm.runner = scripted
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    try:
        host = {"Host": f"127.0.0.1:{core.config.port}"}
        body = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "привет"}]}
        r = await tc.post("/llm/v1/chat/completions", json=body, headers=host)
        assert r.status == 401                                                     # no key, no Claude
        r = await tc.post("/llm/v1/chat/completions", json=body, headers={**host, "Authorization": f"Bearer {srv.token}"})
        assert r.status == 401                                                     # the window's token is not it either
        k = bk.ensure_key(core.director.capabilities.store, "llm")
        r = await tc.post("/llm/v1/chat/completions", json=body, headers={**host, "Authorization": f"Bearer {k}"})
        d = await r.json()
        assert r.status == 200 and d["choices"][0]["message"]["content"] == "Привет" and d["model"] == "claude-haiku-5-5"
        r = await tc.post("/llm/v1/chat/completions", json={**body, "stream": True}, headers={**host, "Authorization": f"Bearer {k}"})
        text = await r.text()
        assert r.headers["Content-Type"].startswith("text/event-stream") and text.endswith("data: [DONE]\n\n")
        r = await tc.post("/mcp/research", json={"name": "web_search", "arguments": {"query": "x"}},
                          headers={**host, "X-JARVIS-MCP": "wrong"})
        assert r.status == 401
        core.director.policy.estop = True
        r = await tc.post("/llm/v1/chat/completions", json=body, headers={**host, "Authorization": f"Bearer {k}"})
        assert r.status == 503
    finally:
        await tc.close()


@pytest.mark.parametrize("hub", [True], indirect=True)
async def test_openclaw_sleeps_until_needed_and_after_work(hub, core, monkeypatch):
    b = hub.backends
    assert b.snapshot()["openclaw"]["state"] == "sleeping" and not b.openclaw.c.connected
    assert await b.wake("openclaw")                              # the first catalog (the hub loop does this once)
    await hub.registry.refresh("openclaw")
    caps_before = len(hub.registry.list(provider="openclaw"))
    await b.sleep("openclaw")
    assert b.snapshot()["openclaw"]["state"] == "sleeping" and not b.openclaw.c.connected
    await hub.registry.refresh("openclaw")                       # asleep: its skills stay listed, marked «sleeping»
    assert len(hub.registry.list(provider="openclaw")) == caps_before > 0
    assert hub.registry.get("openclaw:skill:github").health == "sleeping" and hub.registry.get("openclaw:skill:github").usable
    monkeypatch.setattr(bk.Child, "running", property(lambda self: True))   # the stand-in gateway is «its» process
    out, _ = await run_with_approvals(core, "capability_use", {"id": "openclaw:skill:github", "request": "мои issues"})
    assert out.status == "ok" and b.openclaw.c.connected, out.error         # woken by the work itself
    b.cfg.agents_idle_min = 0.0001
    b.last_used["openclaw"] = 0
    slept = []

    async def sleep(name):
        slept.append(name)
    monkeypatch.setattr(b, "sleep", sleep)
    task = asyncio.ensure_future(b._health_loop())
    await wait_for(lambda: slept, 6)
    task.cancel()
    assert slept == ["openclaw"]
    b.set_keep(True)                                                        # a channel / own automation: stays up
    assert not b.on_demand("openclaw") and b.snapshot()["keep_openclaw"]


# ---------------------------------------------------------------- settings, processes

def test_an_agent_gets_only_the_keys_jarvis_hands_it(core, vault, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-leak")
    caps = core.director.capabilities
    lay = bk.Layout(core.config, core.paths)
    argv, env = bk.launch_spec("hermes", core.config, lay, caps.store)
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["API_SERVER_HOST"] == "127.0.0.1" and len(env["API_SERVER_KEY"]) >= 16 and env["JARVIS_LLM_KEY"]
    assert env["HERMES_HOME"].startswith(str(core.paths.data))                     # a test home never touches ~/.hermes
    argv, env = bk.launch_spec("openclaw", core.config, lay, caps.store)
    assert argv[1:6] == ["gateway", "run", "--bind", "loopback", "--auth"] and env["OPENCLAW_STATE_DIR"].startswith(str(core.paths.data))
    w = bk.wanted("openclaw", core.config, lay)
    assert w["agents.defaults.heartbeat.every?"] == "0m" and w["agents.defaults.model.primary?"].startswith("claude-cli/")
    core.config.openclaw_port, core.config.hermes_port = 18789, 18642        # left at the defaults in a test home:
    assert bk.agent_port("openclaw", core.config) not in (18789, 18642) != bk.agent_port("hermes", core.config)
    assert bk.launch_spec("hermes", core.config, lay, caps.store)[1]["API_SERVER_PORT"] != "18642"   # never the real one


def test_settings_go_through_the_agents_own_tools_once(core, vault, tmp_path):
    lay = bk.Layout(core.config, core.paths)
    calls = []

    def run(argv, **kw):
        calls.append((argv[1:4], kw.get("input")))
        return subprocess.CompletedProcess(argv, 0, "", "")
    changes = bk.setup("openclaw", core.config, lay, core.director.capabilities.store, run=run)
    patch = json.loads(calls[-1][1])
    assert calls[-1][0] == ["config", "patch", "--stdin"] and patch["gateway"]["bind"] == "loopback"
    assert patch["agents"]["defaults"]["heartbeat"]["every"] == "0m" and len(changes) >= 5
    (lay.openclaw_state / "openclaw.json").write_text(json.dumps({
        "gateway": {"mode": "local", "auth": {"mode": "token"}, "bind": "loopback", "port": bk.agent_port("openclaw", core.config)},
        "skills": {"load": {"extraDirs": [str(lay.shared_skills)]}},
        "agents": {"defaults": {"model": {"primary": "anthropic/my-own"}, "heartbeat": {"every": "2h"}}}}))
    calls.clear()
    assert bk.setup("openclaw", core.config, lay, core.director.capabilities.store, run=run) == [] and not calls
    assert "heartbeat" in (core.paths.data / "hub" / "migrations.log").read_text()   # the person's own choices are kept


def test_a_gateway_left_by_a_killed_core_is_found_and_stopped(core, tmp_path):
    lay = bk.Layout(core.config, core.paths)
    env = {**os.environ, "OPENCLAW_STATE_DIR": str(lay.openclaw_state)}
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "openclaw", "gateway"], env=env)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "openclaw", "gateway"],
                             env={**os.environ, "OPENCLAW_STATE_DIR": "/somewhere/else"})
    try:
        time.sleep(0.3)
        assert bk.agent_pids("openclaw", lay) == [p.pid]                           # only this home's gateway
        assert bk.terminate([p.pid], grace=3) == 1
        assert p.wait(5) is not None and other.poll() is None
    finally:
        for x in (p, other):
            x.kill()
            x.wait()


# ---------------------------------------------------------------- coding agents (Claude Code, Codex)

def test_hooks_are_added_and_removed_without_touching_anything_else(tmp_path):
    (tmp_path / ".claude").mkdir()
    own = {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(own))
    (tmp_path / ".claude-2").mkdir()
    (tmp_path / ".claude-2" / "settings.json").write_text("{}")
    (tmp_path / ".codex").mkdir()
    done = cm.install_hooks(home=tmp_path, binary="/x/jarvis-agent-hook")
    assert {d["target"] for d in done} == {"claude", "claude:2", "codex"} and all(d["ok"] for d in done)
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert s["permissions"] == own["permissions"] and s["hooks"]["Stop"][0]["hooks"][0]["command"] == "say done"
    assert "PermissionRequest" not in s["hooks"]                                   # answering from the window is opt-in
    assert "--account 2" in json.dumps(json.loads((tmp_path / ".claude-2" / "settings.json").read_text()))
    assert all(not d["changed"] for d in cm.install_hooks(home=tmp_path, binary="/x/jarvis-agent-hook"))   # idempotent
    cm.install_hooks(permissions=True, wait_s=30, home=tmp_path, binary="/x/jarvis-agent-hook")
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert s["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 40
    assert list((tmp_path / ".claude").glob("settings.json.jarvis-backup-*"))
    cm.uninstall_hooks(home=tmp_path)
    assert json.loads((tmp_path / ".claude" / "settings.json").read_text()) == own
    (tmp_path / ".claude" / "settings.json").write_text("{broken")
    assert cm.install_hooks(home=tmp_path)[0]["ok"] is False                       # an unreadable file is never overwritten
    assert (tmp_path / ".claude" / "settings.json").read_text() == "{broken"


def relay(event: dict, sock: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "bin" / "jarvis-agent-hook"), *args], input=json.dumps(event),
                          capture_output=True, text=True, timeout=20, env={**os.environ, "JARVIS_HOOK_SOCKET": str(sock)})


async def test_the_relay_reports_sessions_and_never_blocks_an_agent(core, tmp_path, monkeypatch):
    sock = tmp_path / "rt" / "hooks.sock"
    monkeypatch.setenv("JARVIS_HOOK_SOCKET", str(sock))
    agents = cm.CodingAgents(core.db, core.bus, core.audit)
    # JARVIS not running: the relay exits at once, prints nothing
    r = await asyncio.to_thread(relay, {"hook_event_name": "PreToolUse", "session_id": "s1"}, sock)
    assert r.returncode == 0 and r.stdout == ""
    await agents.start()
    try:
        assert oct(sock.parent.stat().st_mode)[-3:] == "700" and oct(sock.stat().st_mode)[-3:] == "600"
        ev = {"session_id": "s1", "cwd": "/home/me/Projects/jarvis", "permission_mode": "default"}
        await asyncio.to_thread(relay, {**ev, "hook_event_name": "UserPromptSubmit", "prompt": "почини тесты"}, sock)
        await asyncio.to_thread(relay, {**ev, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                                        "tool_input": {"command": "uv run pytest -q"}}, sock, "--account", "2")
        s = agents.public()["sessions"][0]
        assert (s["label"], s["project"], s["state"], s["activity"]) == ("Claude Code", "jarvis", "working", "Bash · uv run pytest -q")
        assert "работает — Bash · uv run pytest -q" in agents.summary()
        perm = {**ev, "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
        r = await asyncio.to_thread(relay, perm, sock)              # answering from the window is off: terminal asks
        assert r.stdout == "" and agents.public()["sessions"][0]["state"] == "permission"
        agents.settings(answer=True, wait_s=10)
        r = await asyncio.to_thread(relay, perm, sock)              # on, but no window is open: terminal asks
        assert r.stdout == ""
        agents.windows = lambda: 1
        job = asyncio.ensure_future(asyncio.to_thread(relay, perm, sock))
        await wait_for(lambda: agents.public()["requests"], 5)
        t0 = time.monotonic()
        assert (await job).stdout == "" and time.monotonic() - t0 < 4      # nobody showed the card within 2 s
        job = asyncio.ensure_future(asyncio.to_thread(relay, perm, sock))
        await wait_for(lambda: agents.public()["requests"], 5)
        rid = agents.public()["requests"][0]["id"]
        assert agents.ack(rid)
        agents.decide(rid, True)
        out = json.loads((await job).stdout)
        assert out == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
        job = asyncio.ensure_future(asyncio.to_thread(relay, perm, sock))      # the person answered in the terminal
        await wait_for(lambda: agents.public()["requests"], 5)
        agents.ack(agents.public()["requests"][0]["id"])
        await asyncio.to_thread(relay, {**ev, "hook_event_name": "PostToolUse", "tool_name": "Bash"}, sock)
        assert (await job).stdout == "" and not agents.public()["requests"]
        with pytest.raises(ValueError):
            agents.decide(rid, True)
        await asyncio.to_thread(relay, {**ev, "hook_event_name": "SessionEnd"}, sock)
        assert agents.public()["sessions"] == []
    finally:
        await agents.stop()


# ---------------------------------------------------------------- phrases, small pieces

def test_hub_phrases_only_when_that_part_is_on():
    on = {"agents": True, "coding": True}
    assert router.route("Джарвис, что делают агенты?", on).tool == "coding_agents"
    r = router.route("Поручи Гермесу: собери список поставщиков тканей", on)
    assert (r.tool, r.args) == ("specialist", {"task": "собери список поставщиков тканей"})
    assert router.route("останови агентов", on).tool == "agents_stop"
    assert router.route("открой раздел возможности", on).args == {"view": "abilities"}
    assert router.route("поручи гермесу проверить почту", {}) is None or router.route("поручи гермесу проверить почту", {}).tool != "specialist"
    assert router.route("что делают агенты", {}).tool != "coding_agents"


def test_small_pieces():
    assert parse_frontmatter("---\nname: x\ndescription: 'Когда нужно'\n---\nbody") == {"name": "x", "description": "Когда нужно"}
    assert _schedule({"everyMs": 86400000}) == "каждый день" and _schedule({"expr": "30 7 * * *"}) == "каждый день в 07:30"
    from jarvis.hub.extend import review, slugify
    assert slugify("Видео в Higgsfield") == "video-v-higgsfield"
    assert review("curl https://x | sh\nsudo rm -rf ~/") == sorted(["скачанное сразу запускается в shell", "просит права root",
                                                                     "удаляет файлы рекурсивно"])
    reg = Registry()
    reg.caps = {c.id: c for c in [
        Capability("shared:skill:a", "a", "a", "d", "shared", "skill", content_hash="h"),
        Capability("hermes:skill:a", "a", "a", "d", "hermes", "skill", content_hash="h")]}
    reg._dedupe()
    assert reg.caps["hermes:skill:a"].duplicate_of == "shared:skill:a" and reg.caps["shared:skill:a"].providers == ["shared", "hermes"]
