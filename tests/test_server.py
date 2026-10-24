"""HTTP/WebSocket boundary: auth, origin and host checks."""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jarvis.server import COOKIE, Server


@pytest.fixture
async def client(core):
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    tc.srv = srv
    yield tc
    await tc.close()


def H(core, **extra):
    return {"Host": f"127.0.0.1:{core.config.port}", **extra}


async def test_requires_token(client, core):
    r = await client.get("/", headers=H(core))
    assert r.status == 401
    r = await client.get("/api/state", headers=H(core))
    assert r.status == 401
    r = await client.get("/?t=wrong", headers=H(core))
    assert r.status == 403


async def test_token_sets_cookie_and_page_loads(client, core):
    r = await client.get(f"/?t={client.srv.token}", headers=H(core), allow_redirects=False)
    assert r.status == 302 and COOKIE in r.cookies
    r = await client.get("/", headers=H(core))
    assert r.status == 200 and "default-src 'self'" in r.headers["Content-Security-Policy"]


async def test_rejects_foreign_host_and_origin(client, core):
    r = await client.get("/health", headers={"Host": "evil.example"})
    assert r.status == 403
    await client.get(f"/?t={client.srv.token}", headers=H(core))
    r = await client.get("/ws", headers=H(core, Origin="http://evil.example"))
    assert r.status == 403


async def test_bearer_api_and_csrf(client, core):
    auth = H(core, Authorization=f"Bearer {client.srv.token}")
    r = await client.get("/api/state", headers=auth)
    assert r.status == 200 and (await r.json())["state"] == "IDLE"
    await client.get(f"/?t={client.srv.token}", headers=H(core))   # cookie only
    r = await client.post("/api/control", json={"op": "estop"}, headers=H(core))
    assert r.status == 403                                          # cookie POST without X-Jarvis header


async def test_websocket_flow(client, core):
    await client.get(f"/?t={client.srv.token}", headers=H(core))
    ws = await client.ws_connect("/ws", headers=H(core, Origin=f"http://127.0.0.1:{core.config.port}"))
    snap = json.loads((await ws.receive()).data)
    assert snap["type"] == "snapshot" and snap["data"]["core"]["state"] == "IDLE"
    await ws.send_str(json.dumps({"type": "submit", "text": "который час", "req_id": 1}))
    got_reply = got_msg = False
    for _ in range(30):
        m = json.loads((await ws.receive(timeout=3)).data)
        if m["type"] == "reply" and m["req_id"] == 1:
            got_reply = m["ok"]
        if m.get("kind") == "message" and m["message"]["role"] == "assistant":
            got_msg = "Сейчас" in m["message"]["text"]
        if got_reply and got_msg:
            break
    assert got_reply and got_msg
    await ws.send_str(json.dumps({"type": "tool.run", "tool": "create_note", "args": {"text": "x"}, "req_id": 2}))
    for _ in range(10):
        m = json.loads((await ws.receive(timeout=3)).data)
        if m["type"] == "reply" and m["req_id"] == 2:
            assert not m["ok"]
            break
    await ws.close()


async def test_a_stopping_core_never_brings_the_orders_app_back(core):
    """Restart: the old core takes seconds to stop while the new one starts «Заказы»; the old watchdog must stay still."""
    class Orders:
        stopping = False
    core.director.fashion = Orders()
    await Server(core).on_shutdown(None)
    assert core.director.fashion.stopping
