import asyncio
import os
import socket
from pathlib import Path

import pytest

from jarvis.app import build
from jarvis.config import Config, Paths


@pytest.fixture(autouse=True)
def _no_real_voice_cache(tmp_path, monkeypatch):
    """Tests must never write into the user's ~/.local/share/jarvis (voice cache, quota notes)."""
    from jarvis.voice.tts import GeminiVoiceTTS
    monkeypatch.setattr(GeminiVoiceTTS, "CACHE_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(GeminiVoiceTTS, "blocked", {})


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
    c.screenshot_dir = str(home / "shots")
    c.approval_ttl_s = 5
    with socket.socket() as sock:          # never collide with a running JARVIS
        sock.bind(("127.0.0.1", 0))
        c.port = sock.getsockname()[1]
    return c


@pytest.fixture
def core(home, cfg, monkeypatch):
    # Keep tests independent of a real keyring entry.
    from jarvis import scheduler, secrets
    monkeypatch.setattr(secrets, "get", lambda account: None)

    async def no_desktop(*a, **k):        # tests must never show real notifications
        from jarvis.tools.base import CmdResult
        return CmdResult(list(a[0]) if a else [], 0, "", "", 0.0)
    monkeypatch.setattr(scheduler, "run_cmd", no_desktop)
    c = build(Paths.detect(), cfg)
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
from google.genai import types as gtypes  # noqa: E402

from jarvis.providers.base import AIProvider, Cap, GenResult, Health, ProviderFailure  # noqa: E402
from jarvis.providers.gemini import GeminiProvider  # noqa: E402


class FakeProvider(AIProvider):
    """Scripted provider: replies is a list of str | Exception consumed by generate()."""

    def __init__(self, pid, caps, replies=None, health=Health.CONNECTED, cloud=True, sources=None):
        self.id = pid
        self.display_name = {"gemini": "Gemini", "claude_code": "Claude Code"}.get(pid, pid)
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


class ScriptedGemini(GeminiProvider):
    """Real Gemini adapter logic (tool loop) with a scripted stream()."""

    def __init__(self, config, turns):
        super().__init__(config)
        self.turns, self.calls = list(turns), []
        self.status = "ready"

    def configured(self):
        return True

    def health(self):
        return Health.CONNECTED

    async def stream(self, model, contents, tools, **kw):
        self.calls.append([c.model_copy(deep=True) for c in contents])
        self.last_system, self.last_model = kw.get("system"), model
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        parts = []
        for item in turn:
            if isinstance(item, str):
                parts.append(gtypes.Part.from_text(text=item))
                yield {"type": "text", "text": item}
            else:
                fc = gtypes.FunctionCall(name=item[0], args=item[1])
                parts.append(gtypes.Part(function_call=fc))
                yield {"type": "call", "call": fc}
        yield {"type": "done", "content": gtypes.Content(role="model", parts=parts),
               "usage": {"total_token_count": 42}, "first_token_ms": 300, "total_ms": 900}


def install(core, **providers):
    for pid, p in providers.items():
        core.director.agency.mgr.providers[pid] = p
        if pid == "gemini":
            core.director.gemini = p
    return providers
