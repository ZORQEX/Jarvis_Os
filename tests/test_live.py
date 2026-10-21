"""Live provider checks. Opt-in (costs real usage): JARVIS_LIVE=1 uv run pytest tests/test_live.py -q"""
import os
import re
import subprocess

import pytest

from jarvis.providers.base import Cap, Health, StepRequest
from jarvis.providers.claude_code import ClaudeCodeProvider

live = pytest.mark.skipif(os.environ.get("JARVIS_LIVE") != "1", reason="live checks are opt-in (JARVIS_LIVE=1)")


@live
async def test_claude_code_live_inspect(core, tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def f():\n    return 1 / 0\n")
    monkeypatch.setattr(ClaudeCodeProvider, "configured", lambda self: True)     # the real CLI, on purpose
    p = ClaudeCodeProvider(core.config, core.db)
    d = await p.health_check(live=True)
    assert p.health() == Health.CONNECTED, d
    r = await p.generate(StepRequest(Cap.CODE, "Прочитай a.py и одной фразой назови ошибку.", workdir=str(tmp_path)))
    assert r.text and r.model and r.cost_usd is not None
    assert all(t.split(" ")[0] in ("Read", "Glob", "Grep") for t in r.meta["tools_used"])


# ---------------------------------------------------------------- Claude (the user's Claude Code sign-in)
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from jarvis.config import Config  # noqa: E402
from jarvis.providers.claude_chat import ClaudeChatProvider  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def claude():
    p = ClaudeChatProvider(Config(), None, Path(tempfile.mkdtemp(prefix="jarvis-live-claude-")))
    yield p
    await p.stop()


@live
async def test_claude_conversation_keeps_context_and_streams(claude):
    deltas = []

    async def no_tools(name, args):
        return {"status": "denied", "error": "no tools in this test"}
    r1 = await claude.run_tool_loop("", [], "Кто такой Илон Маск? Одним предложением.", [], no_tools, deltas.append)
    r2 = await claude.run_tool_loop("", [], "А сколько ему лет? Коротко.", [], no_tools)
    assert "Маск" in r1.text and deltas and r1.first_token_ms
    assert re.search(r"\d{2}", r2.text), r2.text                    # understood «ему» from the conversation


@live
async def test_claude_writes_a_business_letter(claude):
    r = await claude.generate(StepRequest(Cap.WRITING, "Напиши короткое вежливое письмо поставщику мрамора с просьбой "
                                                       "прислать прайс-лист. 3–4 предложения."))
    assert len(r.text) > 80 and re.search(r"прайс", r.text, re.I), r.text


@live
async def test_claude_web_research_returns_sources(claude):
    r = await claude.generate(StepRequest(Cap.WEB_RESEARCH, "Найди в интернете, кто разработал Omarchy Linux. "
                                                            "Ответь одним предложением."))
    assert r.text and r.meta["grounding"] and r.sources, (r.text, r.meta)


@live
async def test_claude_reads_an_image(claude):
    img = (ROOT / "docs/screenshots/headless-flora-05-annotated.png").read_bytes()
    r = await claude.generate(StepRequest(Cap.VISION, "Что изображено? Одним предложением.", image=img))
    assert len(r.text) > 20, r.text


@live
async def test_claude_reads_a_message_command_into_json(claude):
    from jarvis.comms.agent import SCHEMA, system_prompt
    import datetime as dt
    d = await claude.structured(system_prompt(dt.datetime.now()), "Отправь маме в вотсап завтра в 10 утра: привет", SCHEMA)
    assert d["action"] == "send" and d["recipients"][0]["platform"] == "whatsapp" and d["when"].endswith("10:00"), d
