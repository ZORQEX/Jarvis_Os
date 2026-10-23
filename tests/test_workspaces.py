"""Core support for the WorkOS window: workspace navigation by voice/text and read-only browsing from a workspace."""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jarvis import router
from jarvis.server import Server

from conftest import wait_done


# ---------------------------------------------------------------- navigation phrases (no model call)
@pytest.mark.parametrize("text,view", [
    ("перейди в задачи", "tasks"),
    ("Джарвис, домой", "home"),
    ("вернись на главную", "home"),
    ("перейди в телеграм", "telegram"),
    ("перейди в WhatsApp", "whatsapp"),
    ("открой раздел файлы", "files"),
    ("переключись на вкладку почта", "mail"),
    ("покажи задачи", "tasks"),
    ("перейди во флору", "flora"),
    ("открой вкладку настройки", "settings"),
])
def test_navigation_phrases(text, view):
    r = router.route(text)
    assert r and r.kind == "ui" and r.args == {"view": view}


@pytest.mark.parametrize("text,kind,tool", [
    ("открой телеграм", "tool", "app_open"),                  # the app, not the workspace
    ("перейди на 3 рабочий стол", "tool", "workspace_switch"),  # Hyprland desktops keep their meaning
    ("какие задачи", "reply", None),
])
def test_navigation_does_not_steal_other_commands(text, kind, tool):
    r = router.route(text)
    assert r and r.kind == kind and r.tool == tool


@pytest.mark.parametrize("text", ["перейди на сайт ya.ru", "перейди в загрузки", "перейди"])
def test_not_navigation(text):
    r = router.route(text)
    assert r is None or r.kind != "ui"


async def test_ui_route_publishes_navigate(core):
    q = core.bus.subscribe()
    tid = await core.director.submit("перейди в задачи")
    task = await wait_done(core, tid)
    assert task["state"] == "completed"
    kinds = []
    while not q.empty():
        ev = q.get_nowait()
        kinds.append(ev["kind"])
        if ev["kind"] == "ui.navigate":
            assert ev["view"] == "tasks"
    assert "ui.navigate" in kinds
    msgs = core.director.messages(core.director.conversation_id())
    assert msgs[-1]["role"] == "assistant" and "Задачи" in msgs[-1]["text"]


# ---------------------------------------------------------------- read-only browsing (Files workspace)
async def test_ui_read_lists_folder_through_gateway(core, home):
    d = home / "files" / "Документы"
    d.mkdir()
    (d / "смета.txt").write_text("x")
    res = await core.director.run_ui_read("folder_list", {"path": str(d)})
    assert res["status"] == "ok" and [e["name"] for e in res["data"]["entries"]] == ["смета.txt"]
    assert res["verification"]["status"] == "verified"
    # recorded as a task with a step (audit trail), but no chat message and not counted as JARVIS working
    t = core.tasks.recent(5, exclude_route=None)[0]
    assert t["route"] == "ui_read" and t["state"] == "completed" and t["steps"][0]["tool_id"] == "folder_list"
    assert not core.director.messages(core.director.conversation_id())
    assert all(x["route"] != "ui_read" for x in core.tasks.recent(5, exclude_route="ui_read"))


async def test_ui_read_respects_scopes_and_whitelist(core, home):
    res = await core.director.run_ui_read("folder_list", {"path": "/etc"})
    assert res["status"] in ("invalid", "denied") and res["error"]
    with pytest.raises(ValueError):
        await core.director.run_ui_read("create_note", {"text": "x"})     # never a writing tool


async def test_ui_read_search(core, home):
    (home / "files" / "план работ.md").write_text("x")
    res = await core.director.run_ui_read("search_files", {"query": "план"})
    assert res["status"] == "ok" and res["data"]["results"][0]["name"] == "план работ.md"


async def test_ui_read_and_task_list_over_websocket(core):
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    try:
        host = {"Host": f"127.0.0.1:{core.config.port}"}
        await tc.get(f"/?t={srv.token}", headers=host)
        ws = await tc.ws_connect("/ws", headers={**host, "Origin": f"http://127.0.0.1:{core.config.port}"})
        await ws.receive()                                   # snapshot
        root = core.config.read_scopes[0]
        await ws.send_str(json.dumps({"type": "ui.read", "tool": "folder_list", "args": {"path": root}, "req_id": 1}))
        await ws.send_str(json.dumps({"type": "tasks.list", "limit": 10, "req_id": 2}))
        replies = {}
        for _ in range(40):
            m = json.loads((await ws.receive(timeout=3)).data)
            if m["type"] == "reply":
                replies[m["req_id"]] = m
            if len(replies) == 2:
                break
        assert replies[1]["ok"] and replies[1]["data"]["status"] == "ok"
        assert replies[2]["ok"] and all(t["route"] != "ui_read" for t in replies[2]["data"])
        await ws.close()
    finally:
        await tc.close()


async def test_ui_read_preview_text(core, home):
    f = home / "files" / "заметка.md"
    f.write_text("# План\n- позвонить")
    res = await core.director.run_ui_read("read_file", {"path": str(f)})
    assert res["status"] == "ok" and "позвонить" in res["data"]["text"]


async def test_task_detail_and_file_roots_over_websocket(core):
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    try:
        host = {"Host": f"127.0.0.1:{core.config.port}"}
        await tc.get(f"/?t={srv.token}", headers=host)
        ws = await tc.ws_connect("/ws", headers={**host, "Origin": f"http://127.0.0.1:{core.config.port}"})
        await ws.receive()
        tid = await core.director.submit("запиши заметку: проверить окна")      # asks for confirmation
        a = None
        for _ in range(100):
            pend = core.approvals.pending()
            if pend:
                a = pend[0]
                break
            await __import__("asyncio").sleep(0.02)
        assert a and a["task_id"] == tid
        core.director.decide(a["id"], False)
        await wait_done(core, tid)
        await ws.send_str(json.dumps({"type": "task.detail", "task_id": tid, "req_id": 1}))
        await ws.send_str(json.dumps({"type": "files.roots", "req_id": 2}))
        await ws.send_str(json.dumps({"type": "task.detail", "task_id": "nope", "req_id": 3}))
        replies = {}
        for _ in range(60):
            m = json.loads((await ws.receive(timeout=3)).data)
            if m["type"] == "reply":
                replies[m["req_id"]] = m
            if len(replies) == 3:
                break
        d = replies[1]["data"]
        assert d["task"]["id"] == tid and d["approvals"][0]["status"] == "denied"
        assert replies[2]["ok"] and replies[2]["data"]["roots"] == [str(core.config.expand(core.config.read_scopes[0]))]
        assert not replies[3]["ok"]
        await ws.close()
    finally:
        await tc.close()
