"""Live provider checks. Opt-in (costs real usage): JARVIS_LIVE=1 uv run pytest tests/test_live.py -q"""
import os
import re
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


# ---------------------------------------------------------------- Gemini (needs the key in GNOME Keyring)
from pathlib import Path  # noqa: E402

from jarvis import secrets as _secrets  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.providers.gemini import GeminiProvider  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def gem():
    return GeminiProvider(Config())


@live
async def test_gemini_writes_a_business_letter(gem):
    r = await gem.generate(StepRequest(Cap.WRITING, "Напиши короткое вежливое письмо поставщику мрамора с просьбой "
                                                    "прислать прайс-лист. 3–4 предложения."))
    assert len(r.text) > 80 and re.search(r"прайс", r.text, re.I), r.text


@live
async def test_gemini_web_research_returns_sources(gem):
    r = await gem.generate(StepRequest(Cap.WEB_RESEARCH, "Найди в интернете, кто разработал Omarchy Linux. "
                                                         "Ответь одним предложением."))
    assert r.text, r
    if r.meta.get("grounding") is False:          # Google Search quota exhausted for this key: honest fallback
        assert "Квота поиска" in r.meta["note"] and not r.sources
    else:
        assert r.sources and all(s["url"].startswith("http") for s in r.sources), (r.text, r.sources)


@live
async def test_gemini_vision_reads_an_image(gem):
    img = (ROOT / "docs/screenshots/headless-flora-05-annotated.png").read_bytes()
    r = await gem.generate(StepRequest(Cap.VISION, "Какой текст написан красным на этом плане? Перепиши дословно.",
                                       image=img))
    assert "2100" in r.text and "2000" in r.text, r.text


@live
async def test_gemini_falls_back_to_next_model(gem):
    gem.config.model_main = "gemini-model-that-does-not-exist"
    gem._init_models()
    r = await gem.generate(StepRequest(Cap.GENERAL, "Ответь одним словом: да"))
    assert r.text and r.model != "gemini-model-that-does-not-exist", r.model


@live
async def test_director_research_then_letter_on_real_gemini(core, monkeypatch):
    monkeypatch.setattr(_secrets, "get", lambda a: _REAL_GET(a))
    core.director.agency.mgr.providers["gemini"].reset()
    tid = await core.director.submit("Найди в интернете, что такое Hyprland, и напиши об этом короткое сообщение "
                                     "коллеге на русском")
    from conftest import wait_done
    t = await wait_done(core, tid, timeout=180)
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert t["state"] == "completed", msg["text"]
    assert msg["meta"]["providers"] == ["gemini"]
    assert "Источники" in msg["text"] or "Поиск в интернете сейчас недоступен" in msg["text"], msg["text"]


_REAL_GET = _secrets.get
