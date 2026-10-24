"""«Заказы» inside JARVIS: phrases about orders go to the orders assistant (same dialog as the workspace), JARVIS speaks
its short answer and shows the workspace; its own commands and approvals stay its own. VERIFIED MOCK: a scripted
orders app over real HTTP. The last test starts the REAL app (fashion/) on a free port with throw-away data."""
import asyncio
import json
import socket

import pytest
from aiohttp import web

from jarvis.config import Paths
from jarvis.fashion import FashionService

from conftest import wait_done


class Alive:
    def poll(self):
        return None


@pytest.fixture
async def fake_app():
    """The orders app as JARVIS sees it: claims by keyword, a proposal for a markup, a navigation for «открой»."""
    seen = {"messages": [], "claims": []}

    async def claims(req):
        t = (await req.json())["text"]
        seen["claims"].append(t)
        t = t.lower()
        kind = "strong" if any(w in t for w in ("ids", "ай ди эс", "предоплат", "да, сохрани")) else \
            "weak" if "заказ" in t else None
        return web.json_response({"claim": kind, "why": "тест", "normalized": t})

    async def message(req):
        body = await req.json()
        seen["messages"].append(body)
        if "открой" in body["text"].lower():
            return web.json_response({"reply": "Открываю заказы.", "speech": "Открываю.", "client": {"navigate": "/board"}})
        return web.json_response({"reply": "Предлагаю: IDS 35 % для SS27.\n\n| Было | Станет |", "speech":
                                  "IDS тридцать пять процентов. Сохранить?", "proposal": {"id": "p1", "status": "pending"}})

    async def health(req):
        return web.json_response({"ok": True})
    app = web.Application()
    app.router.add_post("/api/jarvis/claims", claims)
    app.router.add_post("/api/agent/message", message)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    seen["port"] = port
    yield seen
    await runner.cleanup()


@pytest.fixture
def orders(core, fake_app):
    svc = FashionService(core.config, core.paths, core.db, core.audit)
    svc.port, svc.proc = fake_app["port"], Alive()
    core.director.fashion = svc
    return svc


async def test_a_spoken_markup_goes_to_the_orders_assistant(core, orders, fake_app):
    events = core.bus.subscribe()
    tid = await core.director.submit("Джарвис, поставь IDS тридцать пять процентов", source="voice")
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and t["route"] == "fashion"
    sent = fake_app["messages"][0]
    assert sent["text"] == "поставь IDS тридцать пять процентов" and sent["channel"] == "voice"       # wake word off
    assert sent["conversation"] == orders.conversation() and len(sent["conversation"]) == 16
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert msg["text"].startswith("Предлагаю") and msg["meta"]["speech"] == "IDS тридцать пять процентов. Сохранить?"
    assert msg["meta"]["fashion"] == {"proposal": "p1", "status": "pending", "question": False}
    kinds = []
    while not events.empty():
        ev = events.get_nowait()
        kinds.append(ev["kind"])
        if ev["kind"] == "fashion.turn":
            assert ev["show"] is True and ev["conversation"] == orders.conversation()
    assert "fashion.turn" in kinds
    steps = core.db.all("SELECT tool_id FROM task_steps WHERE task_id=?", (tid,))
    assert [s["tool_id"] for s in steps] == ["window_show"]          # the workspace is shown for a spoken request


async def test_the_name_does_not_hide_the_answer(core, orders, fake_app):
    """«Джарвис, да, сохрани» is checked without the name: the app sees «да, сохрани» (its pending-answer rule)."""
    tid = await core.director.submit("Джарвезда, сохрани.", source="voice")       # «Джарвис, да» run together
    await wait_done(core, tid)
    assert fake_app["claims"][-1] == "да, сохрани."


async def test_the_spoken_answer_is_the_short_version(core):
    """SpeechManager speaks a message's meta «speech» (the app's short summary) instead of its markdown table."""
    said = []
    sm = core.director.speech
    sm.streams["t1"] = type("St", (), {"finish": lambda self, t=None: said.append(t), "turn_id": None})()
    sm.mark = lambda *a, **k: None
    sm._on_event("message", {"task_id": "t1", "message": {"role": "assistant", "text": "| таблица |",
                                                           "meta": {"speech": "Коротко."}}})
    sm.streams["t2"] = type("St", (), {"finish": lambda self, t=None: said.append(t), "turn_id": None})()
    sm._on_event("message", {"task_id": "t2", "message": {"role": "assistant", "text": "Обычный ответ.", "meta": {}}})
    assert said == ["Коротко.", "Обычный ответ."]


async def test_typed_navigation_does_not_pull_the_window(core, orders):
    core.director.view = "chat"
    tid = await core.director.submit("открой заказы IDS", source="text")
    t = await wait_done(core, tid)
    assert t["route"] == "fashion"
    assert not core.db.all("SELECT tool_id FROM task_steps WHERE task_id=?", (tid,))        # typed: no window_show


async def test_jarvis_keeps_its_own_commands(core, orders, fake_app):
    tid = await core.director.submit("который час", source="voice")
    t = await wait_done(core, tid)
    assert t["route"] != "fashion" and not fake_app["messages"]
    # «заказ» alone is weak: a local JARVIS command for the phrase wins…
    (core.paths.data.parent.parent / "files" / "заказы").mkdir(parents=True, exist_ok=True)
    tid = await core.director.submit("открой папку заказы", source="text")
    t = await wait_done(core, tid)
    assert t["route"] == "search_open" and not fake_app["messages"]
    # …but in the «Заказы» workspace the orders assistant gets it.
    core.director.view = "orders"
    tid = await core.director.submit("покажи все заказы", source="text")
    t = await wait_done(core, tid)
    assert t["route"] == "fashion" and len(fake_app["messages"]) == 1


async def test_app_down_means_jarvis_as_usual(core, orders):
    orders.proc = None                         # not running: no claim, no delay
    tid = await core.director.submit("сколько предоплата у IDS", source="text")
    t = await wait_done(core, tid)
    assert t["route"] != "fashion"


async def test_failed_app_answer_is_reported_honestly(core, orders, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("приложение заказов ответило 500")
    monkeypatch.setattr(orders, "ask", boom)
    tid = await core.director.submit("предоплата IDS", source="text")
    t = await wait_done(core, tid)
    assert t["state"] == "failed"
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert "не ответило" in msg and "Ничего не изменено" in msg


async def test_window_requests(ws_client, core):
    from test_overlay import request
    svc = FashionService(core.config, core.paths, core.db)
    core.director.fashion = svc
    st = (await request(ws_client, "fashion.state", "r1"))["data"]
    assert st["installed"] in (True, False) and st["running"] is False and len(st["conversation"]) == 16
    await request(ws_client, "fashion.conversation", "r2", id="0123456789abcdef")
    assert svc.conversation() == "0123456789abcdef"
    await request(ws_client, "fashion.conversation", "r3", id="../../etc")      # not a dialog id: ignored
    assert svc.conversation() == "0123456789abcdef"
    await request(ws_client, "ui.view", "r4", view="orders")
    assert core.director.view == "orders"


async def test_the_window_may_frame_only_the_orders_app(ws_client, core):
    svc = FashionService(core.config, core.paths, core.db)
    core.director.fashion = svc
    r = await ws_client.get("/", headers=ws_client.hdr)
    csp = r.headers["Content-Security-Policy"]
    assert csp.endswith(f"; frame-src http://127.0.0.1:{svc.port}") and "frame-ancestors 'none'" in csp
    core.director.fashion = None
    r = await ws_client.get("/", headers=ws_client.hdr)
    assert "frame-src" not in r.headers["Content-Security-Policy"]


REAL = FashionService.__init__.__globals__["RUNTIME"]


@pytest.mark.skipif(not REAL.exists(), reason="the orders app runtime is not installed (scripts/install.sh)")
async def test_the_real_app_starts_claims_answers_and_stops(core, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        core.config.fashion_port = s.getsockname()[1]
    core.config.fashion_enabled = True
    svc = FashionService(core.config, Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s"), core.db, core.audit)
    svc.paths.state.mkdir(parents=True)
    try:
        assert await svc.start(), (tmp_path / "s" / "fashion.log").read_text()[-2000:]
        assert svc.running() and (tmp_path / "d" / "fashion" / "data" / "fashion_orders.db").exists()   # data outside the code
        st = await svc.state()
        assert st["running"] and st["seasons"] == [] and st["items"] == 0
        claim = await svc.claims("посчитай предоплаты фабрике")
        assert claim["claim"] == "strong"
        assert (await svc.claims("открой браузер"))["claim"] is None
        out = await svc.ask("Что сейчас открыто?", "text")        # a rule answer: no AI, no subscription use
        assert out.get("reply")
        pid = svc.proc.pid
    finally:
        await svc.stop()
    await asyncio.sleep(0.2)
    assert not svc.running() and not (tmp_path / "s" / "fashion.pid").exists()
    with pytest.raises(OSError):
        open(f"/proc/{pid}/status").read()
    assert json.loads(json.dumps(st))                                       # state is plain JSON for the window
