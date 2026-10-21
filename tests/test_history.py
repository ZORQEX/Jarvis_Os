"""Action history: lasting changes only (sites, code, files, notes) — never messages, volume or questions."""
from conftest import wait_for


def _finished(core, request, steps):
    tid = core.tasks.create(request, core.director.conversation_id(), route="ai", level=2)
    for i, (tool, state, args, result, ver) in enumerate(steps):
        sid = f"s_{tid}_{i}"
        core.db.execute("INSERT INTO task_steps(id, task_id, seq, tool_id, args, state, duration_ms, result, verification)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (sid, tid, i, tool, __import__("json").dumps(args), state, 1500,
                         __import__("json").dumps(result), __import__("json").dumps(ver)))
    from jarvis.tasks import S
    core.tasks.transition(tid, S.RUNNING)
    core.tasks.transition(tid, S.COMPLETED)
    return tid


SITE = {"summary": "Claude Code: готово", "data": {
    "text": "**Готово.** Сделал сайт-визитку: index.html и style.css.\n\nПроверил в браузере.",
    "git": {"repo": False, "changed": ["index.html", "style.css"], "backup": None},
    "tests": {}, "model": "claude-opus-5-5", "cost_usd": 0.04}}


async def test_a_created_site_is_kept_with_folder_and_files(core):
    _finished(core, "Джарвис, создай сайт-визитку в папке site",
              [("claude_code_change", "succeeded", {"workdir": "/home/u/site", "instruction": "x"}, SITE,
                {"status": "unverified", "detail": "Изменённые файлы: index.html, style.css"})])
    h = core.director.history.list()
    assert len(h) == 1
    e = h[0]
    assert e["title"] == "Создай сайт-визитку в папке site" and e["status"] == "done" and e["kind"] == "change"
    assert e["folder"] == "/home/u/site" and [f["p"] for f in e["files"]] == ["index.html", "style.css"]
    assert e["summary"].startswith("Готово. Сделал сайт-визитку")
    assert e["details"]["cost_usd"] == 0.04


async def test_messages_volume_and_questions_are_not_kept(core):
    _finished(core, "Отправь маме в Telegram: привет",
              [("message_send", "succeeded", {}, {"summary": "ok", "data": {}}, {"status": "verified"})])
    _finished(core, "Звук на 40", [("volume_set", "succeeded", {"level": 40}, {"summary": "ok", "data": {}}, {})])
    _finished(core, "Что такое чёрная дыра?", [("agent_chat", "succeeded", {}, {"summary": "ok", "data": {}}, {})])
    _finished(core, "Какие окна открыты", [("window_list", "succeeded", {}, {"summary": "ok", "data": {}}, {})])
    assert core.director.history.list() == []


async def test_a_denied_change_is_not_kept_but_a_failed_run_is(core):
    _finished(core, "Удали временные файлы проекта", [("claude_code_change", "denied", {"workdir": "/p"}, None, None)])
    assert core.director.history.list() == []
    _finished(core, "Установи зависимости проекта",
              [("claude_code_change", "failed", {"workdir": "/p"}, {"summary": "x", "data": {"git": {"changed": []}}},
                {"status": "failed", "detail": "ошибка сети"})])
    e = core.director.history.list()[0]
    assert e["status"] == "failed" and e["title"] == "Установи зависимости проекта"


async def test_note_through_the_real_path_is_kept(core):
    tid = await core.director.submit("запиши заметку: позвонить архитектору до пятницы")
    await wait_for(lambda: core.tasks.get(tid)["state"] in ("waiting_for_approval", "completed"))
    a = [x for x in core.director.approvals.pending() if x["task_id"] == tid]
    if a:                                   # notes are moderate risk: approve like the user would
        core.director.decide(a[0]["id"], True)
    await wait_for(lambda: core.tasks.get(tid)["state"] == "completed")
    h = core.director.history.list()
    assert len(h) == 1 and h[0]["kind"] == "note" and h[0]["files"][0]["p"].endswith(".md")


async def test_deleting_an_entry_keeps_the_files(core, tmp_path):
    f = tmp_path / "keep.txt"
    f.write_text("x")
    _finished(core, "Сделай файл", [("claude_code_change", "succeeded", {"workdir": str(tmp_path)},
                                    {"summary": "ok", "data": {"text": "ok", "git": {"changed": ["keep.txt"]}}},
                                    {"status": "verified"})])
    e = core.director.history.list()[0]
    assert core.director.history.delete(e["id"]) and core.director.history.list() == [] and f.exists()


async def test_history_over_the_window_connection(core):
    import json
    from aiohttp.test_utils import TestClient, TestServer
    from jarvis.server import Server
    _finished(core, "Создай сайт", [("claude_code_change", "succeeded", {"workdir": "/s"}, SITE, {})])
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    try:
        H = {"Host": f"127.0.0.1:{core.config.port}"}
        await tc.get(f"/?t={srv.token}", headers=H)
        ws = await tc.ws_connect("/ws", headers={**H, "Origin": f"http://127.0.0.1:{core.config.port}"})
        await ws.receive()                                   # snapshot

        async def req(typ, rid, **kw):
            await ws.send_str(json.dumps({"type": typ, "req_id": rid, **kw}))
            for _ in range(20):
                m = json.loads((await ws.receive(timeout=3)).data)
                if m["type"] == "reply" and m["req_id"] == rid:
                    return m
        r = await req("history.list", 1)
        assert r["ok"] and r["data"][0]["title"] == "Создай сайт"
        r2 = await req("history.delete", 2, id=r["data"][0]["id"])
        assert r2["ok"] and r2["data"]["deleted"] is True
        await ws.close()
    finally:
        await tc.close()


def test_summary_skips_headings_and_files_keep_their_change_kind():
    from jarvis.history import _file, _summary
    assert _summary("Отчёт\n\n# Итог\nСохранил результаты поиска в файл Basecamp.md.") == \
        "Сохранил результаты поиска в файл Basecamp.md."
    assert _file("+ site/index.html") == {"p": "site/index.html", "op": "new"}
    assert _file("- old.txt") == {"p": "old.txt", "op": "del"}
    assert _file("src/app.py") == {"p": "src/app.py", "op": "edit"}

