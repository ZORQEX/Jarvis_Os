"""AI agency with exactly two specialists, both through Claude Code: «claude» (conversation, web, texts, documents,
screenshots, JARVIS tools) and «claude_code» (computer & engineering in a project folder). Providers are scripted here (VERIFIED MOCK); see test_live.py."""
import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from jarvis.agency.planner import heuristic_plan
from jarvis.agency.verifier import reconcile, ungrounded_numbers
from jarvis.providers.base import Cap, GenResult, Health, ProviderFailure
from jarvis.providers.claude_code import DENY, GATED, build_argv
from jarvis.tasks import S

from conftest import FakeProvider, install, wait_done, wait_for

GEM = {Cap.WEB_RESEARCH, Cap.VISION, Cap.VOICE, Cap.GENERAL, Cap.DESKTOP_TOOLS, Cap.DOC_ANALYSIS, Cap.WRITING,
       Cap.TRANSLATION, Cap.PLANNING}
CC = {Cap.CODE, Cap.TERMINAL, Cap.FILESYSTEM}
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def both(core, home):
    (home / "proj").mkdir()
    core.config.code_workspaces = [str(home / "proj")]
    return install(core, claude=FakeProvider("claude", GEM, sources=[{"title": "Фабрика А", "url": "https://a.example"}]),
                   claude_code=FakeProvider("claude_code", CC))


async def plan(core, text):
    tid = core.tasks.create(text, core.director.conversation_id())
    return await core.director.agency.plan(core.director, tid, text)


def chain(d):
    return [(s.action, s.executor) for s in d.steps]


# ---------------------------------------------------------------- the registry has exactly two specialists
def test_registry_is_claude_only(core):
    assert set(core.director.agency.mgr.providers) == {"claude_code", "claude"}
    assert {m.provider for m in core.director.agency.mgr.models} == {"claude_code", "claude"}


def test_no_claude_code_left_in_the_application():
    hits = []
    for base in ("jarvis", "web"):
        for p in (ROOT / base).rglob("*"):
            if p.suffix in (".py", ".js", ".html", ".css") and p.is_file():
                for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                    if re.search(r"gemini|google\.genai|GEMINI_API_KEY", line, re.I) and not re.search(
                            r"REMOVED_PROVIDER|removed_note|\"gemini\": \"джемини\"|r\"гемини", line):
                        hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert not hits, hits
    assert "google-genai" not in (ROOT / "pyproject.toml").read_text().lower()


def test_no_openai_code_left_in_the_application():
    hits = []
    for base in ("jarvis", "web"):
        for p in (ROOT / base).rglob("*"):
            if p.suffix in (".py", ".js", ".html", ".css") and p.is_file():
                for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                    if re.search(r"openai|OPENAI_API_KEY|chatgpt", line, re.I) and "REMOVED_PROVIDER" not in line:
                        hits.append(f"{p.relative_to(ROOT)}:{i}")
    assert not hits, hits
    assert "openai" not in (ROOT / "pyproject.toml").read_text().lower()


# ---------------------------------------------------------------- AI work → Claude
@pytest.mark.parametrize("text,action", [
    ("Напиши письмо клиенту", "write"),
    ("Напиши письмо поставщику", "write"),
    ("Сделай это сообщение официальным и вежливым", "write"),
    ("Напиши пост для Instagram про новую коллекцию", "write"),
    ("Придумай рекламный текст для студии", "write"),
    ("Сделай краткое резюме", "write"),
    ("Переведи этот текст на английский", "translate"),
    ("Проанализируй этот документ", "analyze"),
    ("Что такое IFC?", "chat"),
    ("Найди в интернете поставщиков мебели", "research"),
])
async def test_general_ai_work_goes_to_claude(core, both, text, action):
    assert chain(await plan(core, text)) == [(action, "claude")]


async def test_screen_goes_to_claude(core, both):
    assert chain(await plan(core, "Посмотри на экран и скажи, что происходит")) == [("vision_screen", "claude")]


# ---------------------------------------------------------------- computer & engineering → Claude Code
@pytest.mark.parametrize("text,action", [
    ("Исправь ошибку в проекте proj", "change_code"),
    ("Исправь ошибку в коде в proj", "change_code"),
    ("Проверь почему приложение не запускается в proj", "inspect_code"),
    ("Посмотри проект proj и исправь ошибку", "change_code"),
    ("Создай файл и запусти тесты в proj", "change_code"),
    ("Установи зависимость requests в proj", "change_code"),
    ("Покажи последние коммиты git в proj", "inspect_code"),
    ("Создай файл заметки.txt в папке proj", "files"),
])
async def test_computer_work_goes_to_claude_code(core, both, text, action):
    d = await plan(core, text)
    assert chain(d) == [(action, "claude_code")]


async def test_install_is_an_explicit_permission(core, both):
    d = await plan(core, "Установи зависимость requests в proj")
    assert d.steps[0].args["permissions"] == ["install"] and d.requires_approval
    d = await plan(core, "Исправь ошибку в коде в proj")
    assert d.steps[0].args["permissions"] == []


async def test_open_terminal_is_a_local_tool_not_ai(core, both):
    tid = await core.director.submit("Открой терминал")
    t = await wait_done(core, tid)
    assert [s["tool_id"] for s in t["steps"]] == ["app_open"]
    assert not both["claude"].requests and not both["claude_code"].requests


async def test_writing_error_is_not_code(core, both):
    assert chain(await plan(core, "Исправь ошибки в письме архитектору")) == [("write", "claude")]


# ---------------------------------------------------------------- multi-step Claude → Claude Code
async def test_research_then_save_to_file(core, both, home):
    d = await plan(core, "Найди в интернете информацию о компании и сохрани результаты в отдельный файл")
    assert chain(d) == [("research", "claude"), ("files", "claude_code")]
    assert d.mode == "multi_ai" and d.requires_approval
    assert d.steps[1].depends_on == ["s1"]


async def test_research_suppliers_and_download_prices(core, both):
    d = await plan(core, "Найди поставщиков, скачай их прайсы и сохрани их в папку proj")
    assert [a for a, _ in chain(d)][0] == "research" and chain(d)[-1] == ("files", "claude_code")
    assert "download" in d.steps[-1].args["permissions"]


async def test_research_analysis_writing_all_claude(core, both):
    d = await plan(core, "Найди поставщиков в интернете, сравни их и подготовь им письмо")
    assert chain(d) == [("research", "claude"), ("analyze", "claude"), ("write", "claude")]
    assert d.mode == "single_ai"


async def test_research_then_file_executes_with_context_and_approval(core, both, home):
    both["claude"].replies = ["Компания А: основана в 2001, 120 сотрудников"]
    both["claude_code"].replies = ["Сохранил в компания.md"]
    core.config.write_dir = str(home / "out")
    tid = await core.director.submit("Найди в интернете информацию о компании и сохрани результаты в отдельный файл")
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "claude_code_change" and str(home / "out") in a["args"]["workdir"]
    core.director.decide(a["id"], True)
    t = await wait_done(core, tid)
    req = both["claude_code"].requests[0]
    assert req.mode == "change" and "2001" in req.context[0]["text"]          # research result handed over
    assert [s["tool_id"] for s in t["steps"]] == ["agent_generate", "claude_code_change"]
    ch = [s for s in t["steps"] if s["tool_id"] == "claude_code_change"][0]
    assert ch["result"]["data"]["git"]["backup"]                               # non-git folder backed up first


# ---------------------------------------------------------------- no GPT, no silent substitution
async def test_claude_unavailable_is_reported_never_substituted(core, both):
    both["claude"]._health = Health.NOT_CONFIGURED
    tid = await core.director.submit("Напиши письмо поставщику")
    t = await wait_done(core, tid)
    assert not both["claude_code"].requests
    assert t["state"] == S.FAILED
    assert "Claude Code" in core.director.messages(core.director.conversation_id())[-1]["text"]


async def test_claude_code_is_not_a_replacement_for_web_or_vision(core, both):
    both["claude"]._health = Health.AUTH_ERROR
    d = await plan(core, "Найди в интернете поставщиков мрамора")
    assert d.steps[0].executor is None and d.steps[0].status == "blocked"


async def test_gpt_mention_routes_to_claude_with_note(core, both):
    d = await plan(core, "Сделай через GPT: напиши письмо поставщику")
    assert chain(d) == [("write", "claude")] and any("GPT" in n for n in d.notes)


async def test_explicit_override_between_the_two(core, both):
    d = await plan(core, "Через Claude Code: напиши письмо поставщику")
    assert d.steps[0].executor == "claude" and "не умеет" in d.steps[0].reason
    both["claude"]._health = Health.AUTH_ERROR
    d = await plan(core, "Через Claude: напиши письмо поставщику")
    assert d.steps[0].executor is None and "не подставляю" in d.steps[0].reason


async def test_claude_mention_is_a_note_not_a_provider(core, both):
    d = await plan(core, "Используй Gemini, исправь ошибку в коде в proj")
    assert d.steps[0].executor == "claude_code" and any("Gemini" in n for n in d.notes)


# ---------------------------------------------------------------- tools first, privacy, approvals
async def test_show_last_mail_uses_mail_tool_without_ai(core, both):
    tid = await core.director.submit("Покажи последнее письмо")
    await wait_done(core, tid)
    assert not any(p.requests for p in both.values())
    d = json.loads(core.db.one("SELECT decision FROM tasks WHERE id=?", (tid,))["decision"])
    assert d["steps"][0]["executor"] == "mail_read" and d["steps"][0]["status"] == "blocked"


async def test_estimates_tools_then_claude(core, both):
    d = await plan(core, "Проверь две сметы, найди расхождения и подготовь письмо подрядчику")
    assert chain(d) == [("compare_estimates", "flora_compare"), ("analyze", "claude"), ("write", "claude")]


async def test_named_document_is_read_locally_first(core, both, home):
    f = home / "files" / "отчёт.txt"
    f.write_text("Итог проекта: 140 400 ₽")
    both["claude"].replies = ["Главное: итог 140 400 ₽"]
    tid = await core.director.submit(f"Проанализируй документ {f}")
    t = await wait_done(core, tid)
    assert [s["tool_id"] for s in t["steps"]] == ["read_file", "agent_generate"]
    assert "140 400" in both["claude"].requests[0].context[0]["text"]


async def test_send_requires_approval(core, both):
    d = await plan(core, "Отправь письмо")
    assert d.requires_approval and d.risk == "high" and d.steps[0].approval == "required"


async def test_privacy_highly_sensitive_never_goes_to_cloud(core, both):
    tid = await core.director.submit("Напиши письмо с моим паролем и номером карты")
    await wait_done(core, tid)
    assert not both["claude"].requests


async def test_strict_offline_blocks_all_cloud(core, both):
    core.db.set_kv("privacy_profile", "STRICT_OFFLINE")
    tid = await core.director.submit("Найди поставщиков и напиши письмо")
    await wait_done(core, tid)
    assert not any(p.requests for p in both.values())


async def test_code_change_requires_approval_and_deny_runs_nothing(core, both):
    tid = await core.director.submit("Исправь ошибку в коде в proj")
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "claude_code_change" and a["risk"] == "high"
    core.director.decide(a["id"], False)
    await wait_done(core, tid)
    assert not both["claude_code"].requests


async def test_code_change_without_folder_asks_instead_of_guessing(core, both):
    d = await plan(core, "Исправь ошибку в коде")
    assert d.steps[0].status == "blocked" and "папку" in d.steps[0].reason


async def test_claude_cannot_work_outside_allowed_folders(core, both):
    tid = core.tasks.create("x", None)
    core.tasks.transition(tid, S.RUNNING)
    out = await core.gateway.execute(tid, "claude_code_change", {"instruction": "x", "workdir": "/etc"}, "t")
    assert out.status == "invalid"
    out = await core.gateway.execute(tid, "claude_code_change", {"instruction": "x", "workdir": str(Path.home() / "proj"),
                                                                 "permissions": ["root"]}, "t")
    assert out.status == "invalid"


async def test_estop_blocks_code_change(core, both):
    core.policy.estop = True
    tid = await core.director.submit("Исправь ошибку в коде в proj")
    await wait_done(core, tid)
    assert not both["claude_code"].requests


# ---------------------------------------------------------------- execution, provenance, verification
async def test_chain_context_isolation_and_provenance(core, both):
    d = core.director
    d.add_message(d.conversation_id(), "user", "мой пароль от почты: qwerty123")
    both["claude"].replies = ["Фабрика А: 4 800 € за дверь, срок 30 дней", "Добрый день! Предлагаем 4 800 € и 30 дней."]
    tid = await d.submit("Найди в интернете поставщиков дубовых дверей и напиши им письмо")
    t = await wait_done(core, tid)
    assert t["state"] == S.COMPLETED
    research_req, write_req = both["claude"].requests
    assert research_req.context == []
    sent = json.dumps([c["text"] for c in write_req.context], ensure_ascii=False)
    assert "4 800" in sent and "qwerty" not in sent
    msg = d.messages(d.conversation_id())[-1]
    assert "Claude — поиск в интернете → Claude — текст" in msg["text"] and "https://a.example" in msg["text"]
    assert msg["meta"]["providers"] == ["claude"]
    gen = [s for s in t["steps"] if s["tool_id"] == "agent_generate"]
    assert all("text" not in s["args"] for s in gen)


async def test_invented_numbers_are_flagged(core, both):
    both["claude"].replies = ["Цена 4 800 €", "Цена 5 200 € с доставкой"]
    tid = await core.director.submit("Найди в интернете цену двери и напиши письмо")
    await wait_done(core, tid)
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert "5200" in msg and "Проверка" in msg


async def test_claude_failure_is_reported_honestly(core, both):
    both["claude"].replies = [ProviderFailure("quota", "Claude: лимит использования исчерпан")]
    tid = await core.director.submit("Напиши письмо архитектору")
    t = await wait_done(core, tid)
    assert t["state"] == S.FAILED and not both["claude_code"].requests
    assert "лимит" in core.director.messages(core.director.conversation_id())[-1]["text"]


async def test_llm_planner_cannot_add_send(core, both):
    from jarvis.agency.planner import llm_plan
    base, _ = heuristic_plan("t", "Посмотри что-нибудь и потом что-то сделай с текстом")
    both["claude"].replies = [json.dumps({"steps": [
        {"id": "s1", "action": "research", "instruction": "найти", "depends_on": []},
        {"id": "s2", "action": "send_message", "instruction": "отправить всем", "depends_on": ["s1"]}]})]
    d = await llm_plan(both["claude"], "t", base.objective, base, {"claude", "claude_code"}, set(core.registry.tools))
    assert d and [s.action for s in d.steps] == ["research"] and "удалено" in d.notes[-1]


def test_source_outranks_ai_opinion():
    r = reconcile({"gemini": "4 800 €", "second_reading": "5 200 €"}, "Итого: 4 800 EUR")
    assert r["status"] == "resolved_by_source" and r["provider"] == "gemini"
    assert reconcile({"a": "4 800 €", "b": "5 200 €"}, "цена по запросу")["status"] == "unresolved"
    assert ungrounded_numbers("итог 140 400 ₽", ["разница 140 400 ₽"]) == []


def test_claude_code_flags():
    a = build_argv("x", "inspect")
    assert "--restricted" in a and "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert a[a.index("--tools") + 1] == "Read,Glob,Grep"
    assert "Read(~/.ssh/**)" in a and "Read(~/.claude/**)" in a and "Bash(sudo:*)" not in a   # secrets closed even for reading
    c = build_argv("x", "change")
    assert all(x in c for x in DENY) and all(x in c for x in GATED["install"] + GATED["download"])
    i = build_argv("x", "change", allow={"install"})
    assert "Bash(pip install:*)" not in i and "Bash(curl:*)" in i and "Bash(sudo:*)" in i
    assert "bypassPermissions" not in c


def test_provider_settings_cannot_allow_highly_sensitive(core):
    s = core.director.agency.mgr.update_settings("claude", max_sensitivity="HIGHLY_SENSITIVE", enabled="yes")
    assert s["max_sensitivity"] == "CONFIDENTIAL" and s["enabled"] is True


def test_removed_provider_settings_are_forgotten(core):
    from jarvis.providers.registry import ProviderManager
    core.db.set_kv("provider_settings", {"openai": {"enabled": True}, "gemini": {"enabled": True},
                                         "claude": {"enabled": True}})
    ProviderManager(core.db, core.config, list(core.director.agency.mgr.providers.values()))
    assert set(core.db.get_kv("provider_settings")) == {"claude"}


async def test_cancel_kills_claude_code_process(core, tmp_path, monkeypatch):
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
    monkeypatch.setattr(ClaudeCodeProvider, "configured", lambda self: True)      # the fake CLI above
    p = ClaudeCodeProvider(core.config, core.db)
    task = asyncio.create_task(p.generate(StepRequest(Cap.CODE, "x", workdir=str(tmp_path))))
    await wait_for(lambda: marker.exists() and marker.read_text().strip())
    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"


async def test_explicit_internet_search_beats_technical_keywords(core, both):
    d = await plan(core, "Найди в интернете, что такое Hyprland, и напиши об этом короткое сообщение коллеге")
    assert chain(d) == [("research", "claude"), ("write", "claude")]


# ---------------------------------------------------------------- «сделай сайт»: the conversation can always act
class _Builder(FakeProvider):
    """Claude Code double: «builds» the site by writing files into the approved folder."""

    async def generate(self, req, on_delta=None, on_progress=None):
        self.requests.append(req)
        Path(req.workdir, "index.html").write_text("<!doctype html><title>Кафе</title>")
        Path(req.workdir, "style.css").write_text("body{}")
        return GenResult("Сделал сайт кафе: index.html и style.css. Откройте index.html в браузере.", self.id,
                         "claude-code-model", [], {"input_tokens": 1, "output_tokens": 1}, 0.4, 5, 10,
                         {"tools_used": ["Write index.html"], "turns": 3})


async def test_site_request_goes_to_claude_code_after_a_spoken_yes(core, home):
    from conftest import ScriptedClaude, wait_for
    core.config.code_workspaces = [str(home / "Projects")]
    (home / "Projects").mkdir()
    builder = _Builder("claude_code", CC)
    chat = ScriptedClaude(core.config, [[("computer_task", {"instruction": "Сайт для кафе во французском стиле",
                                                             "new_folder": "kafe-france"}),
                                         "Готово: сайт в папке kafe-france."]])
    install(core, claude=chat, claude_code=builder)
    tid = await core.director.submit("Джарвис, мне нужен сайт для кафешки с минималистичным интерьером", "voice")
    await wait_for(lambda: any(a["task_id"] == tid for a in core.director.approvals.pending()))
    a = [a for a in core.director.approvals.pending() if a["task_id"] == tid][0]
    assert a["tool_id"] == "computer_task" and a["risk"] == "high"
    assert "kafe-france (новая папка)" in a["summary"]["action"]
    assert not (home / "Projects" / "kafe-france").exists()          # nothing is created before the «да»
    await core.director.submit("Да, разрешаю.", "voice")
    await wait_for(lambda: core.tasks.get(tid)["state"] == "completed", timeout=10)
    site = home / "Projects" / "kafe-france"
    assert (site / "index.html").exists() and builder.requests[0].workdir == str(site)
    assert chat.tool_results[0]["status"] == "ok"
    h = core.director.history.list()
    assert h and h[0]["folder"] == str(site) and {f["p"] for f in h[0]["files"]} == {"index.html", "style.css"}


async def test_new_project_folder_never_reuses_a_folder_with_files(core, home):
    from jarvis.agency.tools import new_project_dir
    core.config.code_workspaces = [str(home / "Projects")]
    (home / "Projects" / "kafe").mkdir(parents=True)
    assert new_project_dir("Кафе", core.config) == (home / "Projects" / "kafe").resolve()     # empty: fine
    (home / "Projects" / "kafe" / "a.txt").write_text("x")
    assert new_project_dir("Кафе", core.config).name == "kafe-2"
    assert new_project_dir("Сайт для кафе!", core.config).name == "sayt-dlya-kafe"


def test_executor_can_look_things_up_but_never_escalates():
    from jarvis.providers.claude_code import build_argv
    argv = build_argv("x", "change")
    tools = argv[argv.index("--tools") + 1]
    deny = argv[argv.index("--disallowedTools") + 1:]
    assert "WebSearch" in tools and "WebFetch" in tools
    assert "Bash(sudo:*)" in deny and "Bash(pacman:*)" in deny
    assert "Bash(npm install:*)" in deny                               # installing only when the task allows it
    allowed = build_argv("x", "change", allow={"install"})
    assert "Bash(npm install:*)" not in allowed and "Bash(sudo:*)" in allowed


def test_home_wide_workdirs_but_secret_folders_stay_closed(core, home, monkeypatch):
    from jarvis.agency.tools import _in_workspaces
    from jarvis.tools.base import ToolInputError
    monkeypatch.setenv("HOME", str(home))
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    core.config.claude_home_wide = True
    (home / "Games" / "mygame").mkdir(parents=True)
    (home / ".ssh").mkdir()
    assert _in_workspaces(str(home / "Games" / "mygame"), core.config) == (home / "Games" / "mygame").resolve()
    with pytest.raises(ToolInputError):
        _in_workspaces(str(home / ".ssh"), core.config)
    core.config.claude_home_wide = False
    with pytest.raises(ToolInputError):
        _in_workspaces(str(home / "Games" / "mygame"), core.config)

