"""Live provider checks. Opt-in (costs real usage): JARVIS_LIVE=1 uv run pytest tests/test_live.py -q"""
import os
import subprocess

import pytest

from jarvis.providers.base import Cap, Health, StepRequest
from jarvis.providers.claude_code import ClaudeCodeProvider

live = pytest.mark.skipif(os.environ.get("JARVIS_LIVE") != "1", reason="live checks are opt-in (JARVIS_LIVE=1)")


@live
async def test_claude_code_live_inspect(core, tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1 / 0\n")
    p = ClaudeCodeProvider(core.config, core.db)
    d = await p.health_check(live=True)
    assert p.health() == Health.CONNECTED, d
    r = await p.generate(StepRequest(Cap.CODE, "Прочитай a.py и одной фразой назови ошибку.", workdir=str(tmp_path)))
    assert r.text and r.model and r.cost_usd is not None
    assert all(t.split(" ")[0] in ("Read", "Glob", "Grep") for t in r.meta["tools_used"])
