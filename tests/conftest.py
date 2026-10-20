import asyncio
import os
from pathlib import Path

import pytest

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
    c.screenshot_dir = str(home / "shots")
    c.approval_ttl_s = 5
    return c


@pytest.fixture
def core(home, cfg, monkeypatch):
    # Keep tests independent of a real keyring entry.
    from jarvis import secrets
    monkeypatch.setattr(secrets, "get", lambda account: None)
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
