"""Desktop companion: window geometry, wake listening (only «Джарвис…» reaches the director), speech ownership."""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jarvis.overlay import clamp, default_spot, grow, logical
from jarvis.server import Server

from conftest import wait_for

AREA = {"x": 0, "y": 26, "w": 1280, "h": 694}          # 1920×1080 @1.5 with Waybar (26 px) on top


def test_logical_area_and_default_spot():
    m = {"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.5, "transform": 0, "reserved": [0, 26, 0, 0]}
    assert logical(m) == AREA
    assert default_spot(AREA) == (1280 - 64 - 12, 26 + 12)      # top-right, under Waybar


def test_card_opens_away_from_the_nearest_edges():
    orb_tr = {"x": 1204, "y": 38, "w": 64, "h": 64}             # top-right: opens down-left, stays on screen
    assert grow(orb_tr, 360, 470, AREA) == (1268 - 360, 38)
    orb_bl = {"x": 10, "y": 640, "w": 64, "h": 64}              # bottom-left: opens up-right
    assert grow(orb_bl, 360, 470, AREA) == (10, 704 - 470)
    assert clamp(-50, 5000, 64, 64, AREA) == (4, 26 + 694 - 64 - 4)


@pytest.fixture
async def ws_client(core):
    srv = Server(core)
    tc = TestClient(TestServer(srv.app, port=core.config.port))
    await tc.start_server()
    host = {"Host": f"127.0.0.1:{core.config.port}"}
    await tc.get(f"/?t={srv.token}", headers=host)
    ws = await tc.ws_connect("/ws", headers={**host, "Origin": f"http://127.0.0.1:{core.config.port}"})
    await ws.receive()
    tc.srv, tc.ws, tc.hdr = srv, ws, host
    yield tc
    await ws.close()
    await tc.close()


async def request(tc, typ, rid, **payload):
    await tc.ws.send_str(json.dumps({"type": typ, "req_id": rid, **payload}))
    for _ in range(80):
        m = json.loads((await tc.ws.receive(timeout=5)).data)
        if m["type"] == "reply" and m["req_id"] == rid:
            return m
    raise AssertionError("no reply")


async def test_overlay_page_and_token(ws_client):
    tc = ws_client
    r = await tc.get(f"/overlay?t={tc.srv.token}", headers=tc.hdr, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "/overlay"
    r = await tc.get("/overlay", headers=tc.hdr)
    assert r.status == 200 and "JARVIS · компаньон" in await r.text() and "default-src 'self'" in r.headers["Content-Security-Policy"]


async def test_settings_persist_and_are_announced(ws_client, core):
    tc = ws_client
    assert (await request(tc, "overlay.state", 1))["data"] == {"wake": False, "enabled": True}
    m = await request(tc, "overlay.settings", 2, wake=True)
    assert m["data"]["wake"] is True and core.db.get_kv("wake_listen") is True


class FakeSTT:
    name, texts = "fake", []

    def available(self):
        return True

    async def transcribe(self, audio):
        return self.texts.pop(0)

    async def stop(self):
        pass


WAV = __import__("base64").b64encode(b"RIFF" + bytes(4000)).decode()


async def test_wake_listening_acts_only_on_jarvis(ws_client, core):
    tc = ws_client
    core.director.stt = FakeSTT()
    core.director.stt.texts = ["я сегодня поздно приду домой", "Джарвис, который час", "а какое сегодня число"]
    before = len(core.director.messages(core.director.conversation_id()))
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["ok"] and m["data"] == {"text": "", "ignored": True}
    assert len(core.director.messages(core.director.conversation_id())) == before     # nothing stored
    m = await request(tc, "voice.transcribe", 2, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    tid = m["data"]["task_id"]
    assert tid and tc.srv._speech_owner[tid] == "overlay"
    await wait_for(lambda: core.tasks.get(tid)["state"] == "completed")
    # a follow-up right after the answer needs no «Джарвис»
    m = await request(tc, "voice.transcribe", 3, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["data"].get("task_id")
    texts = [x["text"] for x in core.director.messages(core.director.conversation_id()) if x["role"] == "user"]
    assert "я сегодня поздно приду домой" not in texts and "Джарвис, который час" in texts


async def test_wake_window_closes(ws_client, core):
    tc = ws_client
    core.director.stt = FakeSTT()
    core.director.stt.texts = ["просто разговор рядом"]
    tc.srv._wake_until = 0
    m = await request(tc, "voice.transcribe", 1, audio_b64=WAV, mime="audio/wav", wake=True, client="overlay")
    assert m["data"]["ignored"]


async def test_speech_of_the_companion_is_marked(ws_client, core):
    tc = ws_client
    tc.srv._speech_owner["t_x"] = "overlay"
    core.bus.publish("speech.audio", {"seq": 1, "audio_b64": ""}, "t_x")
    core.bus.publish("speech.audio", {"seq": 1, "audio_b64": ""}, "t_y")
    owners = {}
    for _ in range(40):
        m = json.loads((await tc.ws.receive(timeout=5)).data)
        if m.get("kind") == "speech.audio":
            owners[m["task_id"]] = m.get("owner")
        if len(owners) == 2:
            break
    assert owners == {"t_x": "overlay", "t_y": None}
