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
        if "почему" in body["text"].lower():             # no rule for it: the turn goes back to JARVIS's own AI
            return web.json_response({"handoff": True, "turn": body["turn"], "text": body["text"]})
        if "открой" in body["text"].lower():
            return web.json_response({"reply": "Открываю заказы.", "speech": "Открываю.", "client": {"navigate": "/board"}})
        return web.json_response({"reply": "Предлагаю: IDS 35 % для SS27.\n\n| Было | Станет |", "speech":
                                  "IDS тридцать пять процентов. Сохранить?", "proposal": {"id": "p1", "status": "pending"}})

    async def health(req):
        return web.json_response({"ok": True})

    async def ai_spec(req):
        return web.json_response({"system": "Все суммы бери ТОЛЬКО из результатов инструментов.", "tools": []})

    async def ai_tool(req):
        body = await req.json()
        seen.setdefault("tools", []).append(body)
        if body["name"] == "propose_changes":
            return web.json_response({"saved": False, "pending_approval": True, "will_do": ["IDS: 35% для SS27"]})
        return web.json_response({"customers": ["IDS"], "total": "4128.50"})

    async def ai_finish(req):
        body = await req.json()
        seen.setdefault("finished", []).append(body)
        proposed = any(t["name"] == "propose_changes" and t["turn"] == body["turn"] for t in seen.get("tools", []))
        return web.json_response({"reply": body["reply"], "speech": "Коротко.", "client": None, "question": None,
                                  "proposal": {"id": "a" * 24, "status": "pending", "summary": "Наценка IDS 35 %",
                                               "will_do": ["IDS: 35% для SS27"],
                                               "diff": [{"label": "IDS", "before": "100", "after": "135"}]}
                                  if proposed else None})

    async def ai_rules(req):
        body = await req.json()
        seen.setdefault("rules", []).append(body["turn"])
        return web.json_response({"reply": "У IDS 4 128,50 € к оплате.", "speech": "Коротко.", "proposal": None})

    async def commit(req):
        seen.setdefault("decided", []).append((req.match_info["pid"], req.match_info["what"]))
        return web.json_response({"ok": True, "messages": ["IDS: 35% для SS27 — изменено позиций: 3"],
                                  **({"follow_up": seen["follow_up"]} if seen.get("follow_up") else {})})
    app = web.Application()
    app.router.add_post("/api/jarvis/claims", claims)
    app.router.add_post("/api/agent/message", message)
    app.router.add_get("/health", health)
    app.router.add_get("/api/jarvis/ai", ai_spec)
    app.router.add_post("/api/jarvis/ai/tool", ai_tool)
    app.router.add_post("/api/jarvis/ai/finish", ai_finish)
    app.router.add_post("/api/jarvis/ai/rules", ai_rules)
    app.router.add_post("/api/agent/proposals/{pid}/{what}", commit)

    async def proposal(req):
        return web.json_response({"status": seen.get("status", {}).get(req.match_info["pid"], "pending")})
    app.router.add_get("/api/jarvis/proposal/{pid}", proposal)
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
    from jarvis.fashion import Orders
    core.registry.tools["orders"] = Orders(svc)        # (the test core is built with the module off)
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
    assert msg["meta"]["fashion"] == {"proposal": "p1", "status": "pending", "question": None,
                                      "card": {"id": "p1", "status": "pending"}}
    assert sent["jarvis"] is True and sent["turn"] == tid          # the module may hand the turn back to JARVIS
    kinds = []
    while not events.empty():
        ev = events.get_nowait()
        kinds.append(ev["kind"])
        if ev["kind"] == "fashion.turn":
            assert ev["show"] is True and ev["conversation"] == orders.conversation()
    assert "fashion.turn" in kinds
    steps = core.db.all("SELECT tool_id FROM task_steps WHERE task_id=?", (tid,))
    assert [s["tool_id"] for s in steps] == ["window_show"]          # the workspace is shown for a spoken request


async def test_no_rule_answer_means_jarvis_own_conversation_answers_with_the_module_tools(core, orders, fake_app):
    """One assistant: the module hands the turn back; JARVIS's conversation calls the module's tools through the
    Gateway; the module stores the proposal at the end; the owner confirms on the card in JARVIS's chat."""
    from conftest import ScriptedClaude, install
    g = ScriptedClaude(core.config, [[("orders", {"action": "get_customer_allocations", "params": {"customer": "IDS"}}),
                                      ("orders", {"action": "propose_changes", "params": {"summary": "IDS 35 %",
                                       "actions": [{"action": "set_customer_markup", "params": {"customer": "IDS", "markup": "35"}}]}}),
                                      "У IDS сейчас 4 128,50 €. Предлагаю 35 % — подтвердите."]])
    install(core, claude=g)
    tid = await core.director.submit("почему у IDS такая сумма? поставь ей 35", source="text")
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and t["route"] == "fashion"
    assert [x["name"] for x in fake_app["tools"]] == ["get_customer_allocations", "propose_changes"]
    assert all(x["turn"] == tid and x["conversation"] == orders.conversation() for x in fake_app["tools"])
    assert "Все суммы бери ТОЛЬКО" in g.calls[0]["user"]                 # the module's rules, as data for this turn
    assert fake_app["finished"][0]["turn"] == tid and tid not in orders.turns
    steps = [s["tool_id"] for s in t["steps"]]
    assert steps.count("orders") == 2 and "agent_chat" in steps            # through JARVIS's Gateway, in this task
    msg = core.director.messages(core.director.conversation_id())[-1]
    card = msg["meta"]["fashion"]["card"]
    assert card["id"] == "a" * 24 and card["status"] == "pending" and msg["meta"]["fashion"]["status"] == "pending"
    events = core.bus.subscribe()
    out = await core.director.decide_order("a" * 24, True, msg["id"])
    assert out["status"] == "committed" and fake_app["decided"] == [("a" * 24, "commit")]
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert msg["meta"]["fashion"]["status"] == "committed" and msg["meta"]["fashion"]["card"]["status"] == "committed"
    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait()["kind"])
    assert "message.updated" in kinds and "fashion.turn" in kinds


async def test_handoff_without_claude_ends_with_the_module_rules_and_says_why(core, orders, fake_app):
    """As the module did when its own AI failed («Ответила без ИИ»): its full rules answer, the reason is said."""
    tid = await core.director.submit("почему у IDS такая сумма", source="text")       # Claude not configured in tests
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and fake_app["rules"] == [tid]
    msg = core.director.messages(core.director.conversation_id())[-1]["text"]
    assert msg.startswith("⚠ ИИ сейчас недоступен") and "по правилам модуля заказов" in msg and "У IDS 4 128,50" in msg
    assert tid not in orders.turns


async def test_the_conversation_may_use_the_orders_tool_itself(core, orders, fake_app):
    """A request the module did not claim: JARVIS's conversation decided to look at orders — the same tools, the
    proposal is stored when the task ends and shown as a card."""
    from conftest import ScriptedClaude, install
    install(core, claude=ScriptedClaude(core.config, [[("orders", {"action": "propose_changes", "params": {
        "summary": "x", "actions": []}}), "Подготовил изменение."]]))
    tid = await core.director.submit("подготовь изменение наценки для моей подруги", source="text")
    t = await wait_done(core, tid)
    assert t["route"] == "ai" and fake_app["finished"][0]["turn"] == tid
    msg = core.director.messages(core.director.conversation_id())[-1]
    assert msg["meta"]["fashion"]["card"]["id"] == "a" * 24


async def test_decide_order_refuses_a_bad_id(core, orders):
    with pytest.raises(ValueError):
        await core.director.decide_order("../../x", True)


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
    assert "не ответил" in msg and "Ничего не изменено" in msg


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


def test_a_copy_left_by_a_stopping_core_is_found_without_the_pidfile(tmp_path):
    """Restart race (2026-10-25): the pidfile named a copy that had exited while another still held the port."""
    import subprocess
    import sys
    from types import SimpleNamespace

    from jarvis.fashion import FashionService, _alive
    svc = FashionService.__new__(FashionService)
    svc.port, svc.paths = 48123, SimpleNamespace(state=tmp_path)
    (tmp_path / "fashion.pid").write_text("999999")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "app.main:app", "--port", "48124"])
    copy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "app.main:app", "--port", "48123"],
                            start_new_session=True)
    try:
        assert svc._stale() == [copy.pid]
        svc._kill_stale()
        copy.wait(5)
        assert not _alive(copy.pid) and other.poll() is None      # another port's app is left alone
    finally:
        for p in (copy, other):
            p.kill()
            p.wait()


async def test_files_and_figures_the_module_made_are_shown_in_jarvis_chat(core, orders):
    """The module's dock showed its files under the answer (its rules tell the model not to write links): in JARVIS
    they come with the answer — full links to the module, only its own paths."""
    out = core.director.orders_panel({"documents": [{"format": "pdf", "customer": "IDS", "name": "IDS_SS27.pdf",
                                                     "url": "/documents/7/download"},
                                                    {"name": "evil", "url": "https://evil.example/x"}],
                                      "figures": [{"label": "Предоплата", "value": "1518.75"}]})
    assert out["files"] == [{"name": "IDS_SS27.pdf", "url": f"{orders.url}/documents/7/download", "format": "PDF",
                             "customer": "IDS"}]
    assert out["figures"] == [{"label": "Предоплата", "value": "1518.75"}]
    assert core.director.orders_panel(None) == {}



async def test_the_rest_of_the_request_goes_on_after_the_click(core, orders, fake_app):
    """«Поставь IDS 35 % и потом сделай PDF»: the module keeps «сделай PDF» until the change is confirmed."""
    fake_app["follow_up"] = "Сделай Excel и PDF для IDS"
    await core.director.decide_order("b" * 24, True)
    from conftest import wait_for
    await wait_for(lambda: any(m["text"] == "Сделай Excel и PDF для IDS" for m in fake_app["messages"]))



async def test_a_card_for_a_replaced_proposal_changes_nothing(core, orders, fake_app):
    """Card A, then a newer request replaced it (the module cancelled A): «Подтвердить» on A must not report «saved»."""
    fake_app["status"] = {"c" * 24: "cancelled"}
    out = await core.director.decide_order("c" * 24, True)
    assert out == {**out, "status": "cancelled", "acted": False} and not fake_app.get("decided")



async def test_making_an_orders_app_is_not_a_ledger_question(core, orders, fake_app):
    """«Создай приложение для управления заказами» is work for Claude Code; «заказами» alone (a weak claim) must not
    send it to the module."""
    tid = await core.director.submit("Создай небольшое приложение для управления заказами", source="text")
    t = await wait_done(core, tid)
    assert t["route"] != "fashion" and not fake_app["messages"]
    tid = await core.director.submit("покажи все заказы", source="text")       # in the orders view a weak claim still goes
    core.director.view = "orders"
    tid = await core.director.submit("покажи все заказы", source="text")
    t = await wait_done(core, tid)
    assert t["route"] == "fashion"
