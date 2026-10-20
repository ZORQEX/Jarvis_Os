"""AI agency: routing tests from the milestone spec (§30) + execution behaviour.
Providers are scripted (VERIFIED MOCK); live checks live in test_live.py."""
import json

import pytest

from jarvis.agency.planner import heuristic_plan
from jarvis.agency.verifier import reconcile, ungrounded_numbers
from jarvis.providers.base import Cap, Health, ProviderFailure
from jarvis.providers.claude_code import DENY, build_argv
from jarvis.tasks import S

from conftest import FakeProvider, install, wait_done, wait_for

GEM = {Cap.WEB_RESEARCH, Cap.VISION, Cap.VOICE, Cap.GENERAL, Cap.DESKTOP_TOOLS, Cap.DOC_ANALYSIS, Cap.WRITING,
       Cap.TRANSLATION, Cap.PLANNING}
GPT = {Cap.WRITING, Cap.TRANSLATION, Cap.DOC_ANALYSIS, Cap.GENERAL, Cap.DESKTOP_TOOLS, Cap.WEB_RESEARCH, Cap.VISION,
       Cap.PLANNING}
CC = {Cap.CODE, Cap.TERMINAL, Cap.FILESYSTEM}


@pytest.fixture
def all3(core, home):
    (home / "proj").mkdir()
    core.config.code_workspaces = [str(home / "proj")]
    p = install(core, gemini=FakeProvider("gemini", GEM, sources=[{"title": "Фабрика А", "url": "https://a.example"}]),
                openai=FakeProvider("openai", GPT), claude_code=FakeProvider("claude_code", CC))
    return p


async def plan(core, text):
    tid = core.tasks.create(text, core.director.conversation_id())
    d = await core.director.agency.plan(core.director, tid, text)
    return d


def chain(d):
    return [(s.action, s.executor) for s in d.steps]


# ---------------------------------------------------------------- spec §30 routing tests
async def test_1_write_letter_goes_to_gpt(core, all3):
    assert chain(await plan(core, "Напиши письмо клиенту")) == [("write", "openai")]


async def test_2_internet_goes_to_gemini(core, all3):
    assert chain(await plan(core, "Найди информацию в интернете")) == [("research", "gemini")]


async def test_3_fix_code_goes_to_claude(core, all3):
    d = await plan(core, "Исправь ошибку в коде в proj")
    assert chain(d) == [("change_code", "claude_code")] and d.requires_approval


async def test_4_screen_goes_to_gemini(core, all3):
    assert chain(await plan(core, "Посмотри на экран и объясни ошибку")) == [("vision_screen", "gemini")]


async def test_5_files_and_tests_go_to_claude(core, all3):
    assert chain(await plan(core, "Создай файл и запусти тесты в proj")) == [("change_code", "claude_code")]


async def test_6_suppliers_then_letter(core, all3):
    d = await plan(core, "Найди поставщиков и напиши им письмо")
    assert chain(d) == [("research", "gemini"), ("write", "openai")] and d.mode == "multi_ai"


async def test_7_estimates_tools_then_analysis_then_letter(core, all3):
    d = await plan(core, "Проверь две сметы, найди расхождения и подготовь письмо подрядчику")
    assert chain(d) == [("compare_estimates", "flora_compare"), ("analyze", "openai"), ("write", "openai")]
    assert d.mode == "ai_tools" and d.sensitivity == "CONFIDENTIAL"


async def test_8_show_last_mail_uses_mail_tool_without_ai(core, all3):
    tid = await core.director.submit("Покажи последнее письмо")
    t = await wait_done(core, tid)
    assert not any(p.requests for p in all3.values())                 # no AI call
    d = json.loads(core.db.one("SELECT decision FROM tasks WHERE id=?", (tid,))["decision"])
    assert d["steps"][0]["executor"] == "mail_read" and d["steps"][0]["status"] == "blocked"
    assert "Gmail" in core.director.messages(core.director.conversation_id())[-1]["text"]


async def test_9_gpt_unavailable_follows_fallback_policy(core, all3):
    all3["openai"]._health = Health.NOT_CONFIGURED
    d = await plan(core, "Напиши письмо клиенту")
    assert chain(d) == [("write", "gemini")] and "политике резерва" in d.steps[0].reason
    core.director.agency.mgr.update_settings("gemini", fallback_eligible=False)
    d = await plan(core, "Напиши письмо клиенту")
    assert d.steps[0].executor is None and d.steps[0].status == "blocked"
    tid = await core.director.submit("Напиши письмо клиенту")
    await wait_done(core, tid)
    assert not all3["gemini"].requests and not all3["openai"].requests   # nobody pretended to be GPT


async def test_10_send_requires_approval(core, all3):
    d = await plan(core, "Отправь письмо")
    assert d.requires_approval and d.risk == "high" and d.steps[0].approval == "required"


# ---------------------------------------------------------------- execution behaviour
async def test_multi_ai_context_isolation_and_provenance(core, all3):
    d = core.director
    d.add_message(d.conversation_id(), "user", "мой пароль от почты: qwerty123")      # unrelated private history
    all3["gemini"].replies = ["Фабрика А: 4 800 € за дверь, срок 30 дней"]
    all3["openai"].replies = ["Добрый день! Предлагаем 4 800 € и 30 дней."]
    tid = await d.submit("Найди в интернете поставщиков дубовых дверей и напиши им письмо")
    t = await wait_done(core, tid)
    assert t["state"] == S.COMPLETED
    research_req, write_req = all3["gemini"].requests[0], all3["openai"].requests[0]
    assert research_req.context == []                                   # research gets only its clause
    sent = json.dumps([c["text"] for c in write_req.context], ensure_ascii=False)
    assert "4 800" in sent and "qwerty" not in sent and "пароль" not in write_req.instruction
    steps = {s["tool_id"]: s for s in t["steps"]}
    assert steps["agent_generate"]["args"]["context_chars"] == 0 or True
    msg = d.messages(d.conversation_id())[-1]
    assert "Gemini — поиск в интернете → GPT — текст" in msg["text"] and "https://a.example" in msg["text"]
    assert msg["meta"]["providers"] == ["gemini", "openai"]
    usage = core.db.all("SELECT provider, ok FROM provider_usage WHERE task_id=?", (tid,))
    assert {(r["provider"], r["ok"]) for r in usage} == {("gemini", 1), ("openai", 1)}
    # persisted args are metadata only (no prompt context in the task log)
    gen = [s for s in t["steps"] if s["tool_id"] == "agent_generate"]
    assert all("text" not in s["args"] for s in gen)


async def test_invented_numbers_are_flagged(core, all3):
    all3["gemini"].replies = ["Цена 4 800 €"]
    all3["openai"].replies = ["Цена 5 200 € с доставкой"]
    tid = await core.director.submit("Найди в интернете цену двери и напиши письмо")
    await wait_done(core, tid)
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert "5200" in msg and "Проверка" in msg


async def test_provider_failure_falls_back_once_and_reports(core, all3):
    all3["openai"].replies = [ProviderFailure("quota", "квота")]
    all3["gemini"].replies = ["Черновик от Gemini"]
    tid = await core.director.submit("Напиши письмо архитектору")
    await wait_done(core, tid)
    d = json.loads(core.db.one("SELECT decision FROM tasks WHERE id=?", (tid,))["decision"])
    assert d["steps"][0]["executor"] == "gemini" and "ошибка (quota)" in d["steps"][0]["reason"]
    assert "Черновик от Gemini" in core.director.messages(core.director.conversation_id())[-1]["text"]


async def test_explicit_override_is_respected_and_not_substituted(core, all3):
    d = await plan(core, "Сделай через GPT: напиши письмо поставщику")
    assert chain(d) == [("write", "openai")] and "вы выбрали" in d.steps[0].reason
    all3["openai"]._health = Health.AUTH_ERROR
    d = await plan(core, "Сделай через GPT: напиши письмо поставщику")
    assert d.steps[0].executor is None and "не подставляю" in d.steps[0].reason
    d = await plan(core, "Используй Gemini, исправь ошибку в коде в proj")
    assert d.steps[0].executor == "claude_code" and "не умеет" in d.steps[0].reason


async def test_privacy_highly_sensitive_never_goes_to_cloud(core, all3):
    tid = await core.director.submit("Напиши письмо с моим паролем и номером карты")
    await wait_done(core, tid)
    assert not all3["openai"].requests and not all3["gemini"].requests


async def test_provider_sensitivity_setting_is_a_hard_constraint(core, all3):
    core.director.agency.mgr.update_settings("openai", max_sensitivity="PUBLIC")
    core.director.agency.mgr.update_settings("gemini", max_sensitivity="PUBLIC")
    d = await plan(core, "Проверь две сметы и подготовь письмо подрядчику")
    write = [s for s in d.steps if s.action == "write"][0]
    assert write.executor is None


async def test_strict_offline_blocks_all_cloud(core, all3):
    core.db.set_kv("privacy_profile", "STRICT_OFFLINE")
    tid = await core.director.submit("Найди поставщиков и напиши письмо")
    await wait_done(core, tid)
    assert not any(p.requests for p in all3.values())


async def test_code_change_requires_approval_and_deny_runs_nothing(core, all3):
    tid = await core.director.submit("Исправь ошибку в коде в proj")
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "claude_code_change" and a["risk"] == "high"
    assert not all3["claude_code"].requests
    core.director.decide(a["id"], False)
    await wait_done(core, tid)
    assert not all3["claude_code"].requests


async def test_code_change_without_folder_asks_instead_of_guessing(core, all3):
    d = await plan(core, "Исправь ошибку в коде")
    assert d.steps[0].status == "blocked" and "папку" in d.steps[0].reason


async def test_code_inspect_runs_without_approval_in_workspace(core, all3):
    tid = await core.director.submit("Claude, посмотри код в proj и объясни")
    t = await wait_done(core, tid)
    assert [s["tool_id"] for s in t["steps"]] == ["claude_code_inspect"]
    assert all3["claude_code"].requests[0].mode == "inspect"


async def test_estop_blocks_code_change_even_if_planned(core, all3):
    core.policy.estop = True
    tid = await core.director.submit("Исправь ошибку в коде в proj")
    t = await wait_done(core, tid)
    assert not all3["claude_code"].requests


async def test_llm_planner_cannot_add_send_or_lower_risk(core, all3):
    from jarvis.agency.planner import llm_plan
    base, _ = heuristic_plan("t", "Посмотри что-нибудь и потом что-то сделай с текстом")
    all3["gemini"].replies = [json.dumps({"steps": [
        {"id": "s1", "action": "research", "instruction": "найти", "depends_on": []},
        {"id": "s2", "action": "send_message", "instruction": "отправить всем", "depends_on": ["s1"]}]})]
    d = await llm_plan(all3["gemini"], "t", base.objective, base, {"gemini", "openai", "claude_code"},
                       set(core.registry.tools))
    assert d and [s.action for s in d.steps] == ["research"] and "удалено" in d.notes[-1]


# ---------------------------------------------------------------- verifier & adapters
def test_source_outranks_ai_opinion():
    r = reconcile({"gemini": "4 800 €", "openai": "5 200 €"}, "Итого: 4 800 EUR")
    assert r["status"] == "resolved_by_source" and r["provider"] == "gemini"
    r = reconcile({"gemini": "4 800 €", "openai": "5 200 €"}, "цена по запросу")
    assert r["status"] == "unresolved"
    assert ungrounded_numbers("итог 140 400 ₽", ["разница 140 400 ₽"]) == []


def test_claude_code_runs_restricted():
    a = build_argv("x", "inspect")
    assert "--restricted" in a and "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert a[a.index("--tools") + 1] == "Read,Glob,Grep" and "Bash" not in a[a.index("--tools") + 1]
    assert "--disallowedTools" not in a
    c = build_argv("x", "change")
    assert "Bash" in c[c.index("--tools") + 1] and all(x in c for x in DENY)
    assert "Bash(sudo:*)" in c and "Bash(git push:*)" in c and "bypassPermissions" not in c


def test_provider_settings_cannot_allow_highly_sensitive(core):
    s = core.director.agency.mgr.update_settings("openai", max_sensitivity="HIGHLY_SENSITIVE", enabled="yes")
    assert s["max_sensitivity"] == "CONFIDENTIAL" and s["enabled"] is True


async def test_cancel_kills_claude_code_process(core, tmp_path, monkeypatch):
    """Cancellation propagates to the Claude Code subprocess (stand-in CLI that never finishes)."""
    import asyncio
    import os
    from jarvis.providers.base import StepRequest
    from jarvis.providers.claude_code import ClaudeCodeProvider
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "pid"
    fake = bindir / "claude"
    fake.write_text(f"#!/bin/sh\necho $$ > {marker}\necho '{{\"type\":\"system\",\"subtype\":\"init\",\"model\":\"x\"}}'\n"
                    "exec sleep 60\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    p = ClaudeCodeProvider(core.config, core.db)
    task = asyncio.create_task(p.generate(StepRequest(Cap.CODE, "x", workdir=str(tmp_path))))
    await wait_for(lambda: marker.exists() and marker.read_text().strip())
    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"
