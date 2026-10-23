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


# ---------------------------------------------------------------- act, don't explain
@pytest.mark.parametrize("text,chans", [
    ("подключи телеграм", ["telegram"]),
    ("Подключись к WhatsApp, к Телеграму и так далее, чтоб ты мог отправлять сообщения", ["whatsapp", "telegram"]),
    ("подключи почту", ["email"]),
    ("войди в телегу", ["telegram"]),
])
def test_connect_phrases(text, chans):
    r = router.route(text)
    assert r and r.kind == "connect" and r.args["channels"] == chans


@pytest.mark.parametrize("text", ["подключи вайфай", "подключись к интернету"])
def test_connect_needs_a_service(text):
    r = router.route(text)
    assert r is None or r.kind != "connect"


@pytest.mark.parametrize("text,app", [("открой ватсап веб", "whatsapp"), ("открой WhatsApp Web", "whatsapp"),
                                      ("открой веб-версию ватсапа", "whatsapp"), ("открой телеграм веб", "telegram")])
def test_web_versions_open_without_ai(text, app):
    r = router.route(text)
    assert r and r.kind == "tool" and r.tool == "app_open" and r.args == {"app": app}


def navigations(q):
    out = []
    while not q.empty():
        ev = q.get_nowait()
        if ev["kind"] == "ui.navigate":
            out.append(ev["view"])
    return out


async def test_connect_opens_the_card_instead_of_explaining(core):
    core.director.gw.registry.get("window_show").presence = lambda: True
    q = core.bus.subscribe()
    tid = await core.director.submit("подключи телеграм")
    t = await wait_done(core, tid)
    text = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert t["state"] == "completed" and text.startswith("Открыл раздел Telegram") and "api_id" in text
    assert navigations(q) == ["telegram"]
    step = [s for s in t["steps"] if s["tool_id"] == "window_show"][0]
    assert step["state"] == "succeeded" and step["verification"]["status"] == "verified"


async def test_connect_whatsapp_starts_the_qr_itself(core, monkeypatch):
    from jarvis.comms.whatsapp import WhatsAppProvider
    started = []

    async def begin(self):
        started.append(True)
        return {"stage": "starting"}
    monkeypatch.setattr(WhatsAppProvider, "begin_link", begin)
    core.director.gw.registry.get("window_show").presence = lambda: True
    q = core.bus.subscribe()
    tid = await core.director.submit("подключи ватсап и почту")
    await wait_done(core, tid)
    text = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert started and "QR-код" in text and "«подключи почту»" in text
    assert navigations(q) == ["whatsapp"]


async def test_window_show_opens_a_closed_window_first(core):
    wt = core.director.gw.registry.get("window_show")
    state = {"open": False}
    wt.presence = lambda: state["open"]
    wt.opener = lambda: state.update(open=True)
    q = core.bus.subscribe()
    t = core.tasks.create("x", None)
    out = await core.director.gw.execute(t, "window_show", {"view": "tasks"}, "test")
    assert out.status == "ok" and out.result.data["window_opened"] and navigations(q) == ["tasks"]


async def test_window_show_reports_a_window_that_did_not_open(core):
    wt = core.director.gw.registry.get("window_show")
    wt.presence = lambda: False
    t = core.tasks.create("x", None)
    out = await core.director.gw.execute(t, "window_show", {"view": "files"}, "test")
    assert out.status == "failed" and "не открылось" in out.error


def test_model_sees_state_and_action_first_rules(core):
    from jarvis.agency.agency import jarvis_state_line
    from jarvis.providers.claude_chat import CHAT_SYSTEM
    line = jarvis_state_line(core.director)
    assert line.startswith("[Состояние JARVIS:") and "Telegram — не подключено" in line
    assert "ДЕЙСТВУЕШЬ, а не объясняешь" in CHAT_SYSTEM and "window_show" in CHAT_SYSTEM
    assert "window_show" in [t.spec.id for t in core.director.gw.registry.model_tools()]


@pytest.mark.parametrize("text,app", [("открой настройки звука", "audio"), ("открой микшер", "audio"),
                                      ("открой вайфай", "wifi"), ("открой блютуз", "bluetooth")])
def test_system_settings_open_without_ai(text, app):
    r = router.route(text)
    assert r and r.kind == "tool" and r.tool == "app_open" and r.args == {"app": app}


async def test_window_show_connect_starts_whatsapp_and_grounds_the_model(core, monkeypatch):
    from jarvis.comms.whatsapp import WhatsAppProvider
    started = []

    async def begin(self):
        started.append(True)
        return {"stage": "starting"}
    monkeypatch.setattr(WhatsAppProvider, "begin_link", begin)
    wt = core.director.gw.registry.get("window_show")
    wt.presence = lambda: True
    t = core.tasks.create("x", None)
    out = await core.director.gw.execute(t, "window_show", {"view": "whatsapp", "connect": True}, "test")
    assert out.status == "ok" and started and out.result.data["connect_started"]
    assert "QR-код" in out.for_model()["summary"]
    # Telegram: the model is told what the card needs and where to get it — so it can open that page itself.
    out = await core.director.gw.execute(t, "window_show", {"view": "telegram"}, "test")
    assert "my.telegram.org/apps" in out.for_model()["summary"] and not out.result.data["connect_started"]



@pytest.mark.parametrize("text,woke", [("Джарвес, который час.", True), ("Джарвис, который час?", True),
                                       ("Жарвис открой браузер", True), ("Эй, Джарвес, стоп", True), ("Джарвиз", True),
                                       ("Дарвин был учёным", False), ("Давай поговорим", False), ("Джордж привет", False),
                                       ("привет как дела", False)])
def test_wake_word_survives_speaker_to_mic_spelling(text, woke):
    assert bool(router.wake_len(text)) is woke
    if woke:
        assert router.strip_wake(text) == text[router.wake_len(text):]
