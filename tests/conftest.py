import asyncio
import socket
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jarvis.app import build
from jarvis.config import Config, Paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    (tmp_path / "files").mkdir()
    return tmp_path


@pytest.fixture
def cfg(home):
    c = Config()
    c.read_scopes = [str(home / "files")]
    c.write_dir = str(home / "out")
    c.skills_dir = str(home / "skills")
    c.screenshot_dir = str(home / "shots")
    c.approval_ttl_s = 5
    c.fashion_enabled = False            # the real orders app starts only in its own tests (test_fashion.py)
    c.research_enabled = False           # the real SearXNG neither: test_research.py uses a fake one
    c.openclaw_enabled = c.hermes_enabled = False   # the real agents neither: test_hub.py uses fakes
    c.coding_agents_enabled = False      # no hook socket unless a test turns it on
    with socket.socket() as sock:          # never collide with a running JARVIS
        sock.bind(("127.0.0.1", 0))
        c.port = sock.getsockname()[1]
    return c


@pytest.fixture
def core(home, cfg, monkeypatch):
    # Keep tests independent of a real keyring entry.
    from jarvis import scheduler, secrets
    monkeypatch.setattr(secrets, "get", lambda account: None)
    # The real voice and recognition models (~1 GB, seconds to load) are exercised by their own tests only.
    from jarvis.voice.stt import LocalSTT
    from jarvis.voice.tts import KokoroTTS, PiperTTS, SileroTTS, SupertonicTTS
    for cls in (LocalSTT, SileroTTS, PiperTTS, KokoroTTS, SupertonicTTS):
        monkeypatch.setattr(cls, "available", lambda self: False)
    # Never the real Claude in tests (it would use the user's subscription): providers look «not installed» unless a
    # test installs a scripted one. Live checks are in test_live.py (JARVIS_LIVE=1).
    from jarvis.providers.claude_chat import ClaudeChatProvider
    from jarvis.providers.claude_code import ClaudeCodeProvider
    monkeypatch.setattr(ClaudeChatProvider, "configured", lambda self: False)
    monkeypatch.setattr(ClaudeCodeProvider, "configured", lambda self: False)

    async def no_desktop(*a, **k):        # tests must never show real notifications
        from jarvis.tools.base import CmdResult
        return CmdResult(list(a[0]) if a else [], 0, "", "", 0.0)
    monkeypatch.setattr(scheduler, "run_cmd", no_desktop)
    c = build(Paths.detect(), cfg)
    wt = c.director.gw.registry.get("window_show")     # tests never open a real JARVIS window on the desktop
    wt.opener, wt.wait_s = (lambda: None), 0.2
    yield c
    c.db.close()


async def wait_done(core, tid, timeout=5.0):
    t0 = asyncio.get_running_loop().time()
    while tid in core.director.rt:
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise TimeoutError(tid)
        await asyncio.sleep(0.01)
    return core.tasks.get(tid)


async def wait_for(pred, timeout=5.0):
    t0 = asyncio.get_running_loop().time()
    while not pred():
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise TimeoutError("condition")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- scripted providers (VERIFIED MOCK only)
import tempfile  # noqa: E402

from jarvis.providers.base import AIProvider, Cap, GenResult, Health  # noqa: E402
from jarvis.providers.claude_chat import ClaudeChatProvider  # noqa: E402


class FakeProvider(AIProvider):
    """Scripted provider: replies is a list of str | Exception consumed by generate()."""

    def __init__(self, pid, caps, replies=None, health=Health.CONNECTED, cloud=True, sources=None):
        self.id = pid
        self.display_name = {"claude": "Claude", "claude_code": "Claude Code"}.get(pid, pid)
        self.role_ru = "test"
        self.capabilities = set(caps)
        self.cloud = cloud
        self._health = health
        self.replies = list(replies or [])
        self.requests = []
        self.sources = sources or []

    def configured(self):
        return self._health != Health.NOT_CONFIGURED

    def health(self):
        return self._health

    async def health_check(self, live=False):
        return {}

    def model_for(self, cap):
        return f"{self.id}-model"

    async def generate(self, req, on_delta=None, on_progress=None):
        self.requests.append(req)
        r = self.replies.pop(0) if self.replies else f"[{self.id}] ответ"
        if isinstance(r, Exception):
            raise r
        if on_delta:
            on_delta(r)
        return GenResult(r, self.id, self.model_for(req.capability),
                         self.sources if req.capability == Cap.WEB_RESEARCH else [], {"input_tokens": 10,
                                                                                      "output_tokens": 20},
                         None, 5, 10, {"tools_used": ["Read x.py"], "turns": 1})

    def describe(self):
        return {"configured": self.configured()}


_TMP = tempfile.TemporaryDirectory(prefix="jarvis-test-claude-")     # one folder per test run, removed at exit


class ScriptedClaude(ClaudeChatProvider):
    """The Claude conversation provider with a scripted turn instead of the Claude Code process.
    A turn is a list of str (streamed text) and (tool_name, args) tuples — tool calls run through JARVIS's Gateway
    exactly as MCP calls from the real Claude would (VERIFIED MOCK, not a live API test)."""

    def __init__(self, config, turns):
        super().__init__(config, None, Path(_TMP.name) / f"claude-{id(self)}")
        self.turns, self.calls, self.tool_results = list(turns), [], []
        self.status = "ready"

    def configured(self):
        return True

    def health(self):
        return Health.CONNECTED

    async def warm(self):
        return None

    async def run_tool_loop(self, system, history, user_text, tools, execute, on_delta=None, max_rounds=6,
                            estop=lambda: False, checkpoint=None, thinking=None, role="main", conversation=None,
                            progress=None):
        self.calls.append({"system": system, "history": list(history), "user": user_text, "role": role})
        self.last_system, self.last_role = system, role
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        text = ""
        self.current_execute, self.turn_calls = execute, 0
        for item in turn:
            if isinstance(item, str):
                text += item
                if on_delta:
                    on_delta(item)
            elif isinstance(item, dict):       # {"progress": (text, kind, info)}: what the real CLI's events report
                if progress:
                    progress(*item["progress"])
            else:              # the same path a real MCP call takes: allowed-tool check, step limit, then the Gateway
                self.tool_results.append(await self.mcp_call(item[0], item[1]))
        self.current_execute = None
        return GenResult(text, "claude", "claude-test", [], {"input_tokens": 42}, None, 300, 900,
                         {"rounds": sum(1 for i in turn if not isinstance(i, str))})


def install(core, **providers):
    for pid, p in providers.items():
        core.director.agency.mgr.providers[pid] = p
        if pid == "claude":
            core.director.ai = core.claude = p
            if getattr(p, "registry", "absent") is None:
                p.registry = core.registry
    return providers


# ---------------------------------------------------------------- the window's WebSocket (desktop companion tests)
@pytest.fixture
async def ws_client(core):
    from jarvis.server import Server
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    host = {"Host": f"127.0.0.1:{core.config.port}"}
    await tc.get(f"/?t={srv.token}", headers=host)
    ws = await tc.ws_connect("/ws", headers={**host, "Origin": f"http://127.0.0.1:{core.config.port}"})
    await ws.receive()
    tc.srv, tc.ws, tc.hdr = srv, ws, host
    yield tc
    await ws.close()
    await tc.close()
