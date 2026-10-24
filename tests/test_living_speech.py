"""Quick commands said in living speech run at once, without the AI (2026-10-24: they waited 5–10 s for Claude)."""
import pytest

from jarvis import router

from conftest import wait_done


@pytest.mark.parametrize("said,kind,tool,args", [
    ("Мне нужно, знаешь что, открыть папку с колледжем.", "search_open", "search_files",
     {"query": "колледж", "kind": "dir"}),
    ("слушай, открой браузер", "tool", "app_open", {"app": "browser"}),
    ("Можешь открыть телеграм?", "tool", "app_open", {"app": "telegram"}),
    ("открыть калькулятор", "tool", "app_open", {"app": "calculator"}),
    ("пожалуйста, открой документы", "tool", "open_path", None),
    ("а теперь найди файл отчёт", "tool", "search_files", {"query": "отчет"}),
    ("ну который час", "reply", None, None),
])
def test_quick_commands_in_living_speech(said, kind, tool, args):
    r = router.route(said)
    assert r and r.kind == kind and r.tool == tool
    if args:
        assert r.args == args


@pytest.mark.parametrize("said", ["не открывай браузер", "а теперь стоп", "слушай отправь маме привет",
                                  "расскажи что-нибудь", "мне нужно подумать"])
def test_simplifying_never_changes_meaning(said):
    r = router.route(said)
    assert r is None or r.kind not in ("tool", "search_open")


def test_folder_said_in_any_case():
    assert router._stem("колледжем") == "колледж" and router._stem("лабораторные") == "лабораторн"
    assert router._stem("college") == "college"


async def test_the_folder_itself_opens_not_a_folder_inside_it(core, home, monkeypatch):
    base = home / "files"
    (base / "Колледж" / "Лабораторные" / "колледж-задания").mkdir(parents=True)
    (base / "колледж.txt").write_text("x")
    opened = []
    tool = core.registry.get("open_path")

    async def fake_run(args, ctx):
        opened.append(args["path"])
        from jarvis.tools.base import ToolResult
        return ToolResult({"path": args["path"]}, "Открыл.")

    async def fake_verify(args, result, ctx):
        from jarvis.tools.base import Verification
        return Verification("verified", "открыт")
    monkeypatch.setattr(tool, "run", fake_run)
    monkeypatch.setattr(tool, "verify", fake_verify)
    t = await wait_done(core, await core.director.submit("Мне нужно открыть папку с колледжем"))
    assert t["state"] == "completed" and opened == [str(base / "Колледж")]
