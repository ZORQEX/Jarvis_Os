"""Smart home through a fake Home Assistant REST API (VERIFIED MOCK — no real Home Assistant here)."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import wait_done, wait_for
from jarvis.router import route
from jarvis.tools.home import parse_home

TOKEN = "t" * 40


def ent(eid, state, name, **a):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name, **a}}


class FakeHA:
    def __init__(self):
        self.states = {s["entity_id"]: s for s in [
            ent("light.kitchen", "off", "Свет на кухне"),
            ent("light.bedroom", "on", "Свет в спальне", brightness=255),
            ent("switch.kettle", "off", "Розетка чайника"),
            ent("climate.living", "heat", "Кондиционер", current_temperature=21.5, temperature=22, min_temp=16, max_temp=30),
            ent("lock.front_door", "locked", "Входная дверь"),
            ent("cover.garage", "closed", "Гараж", device_class="garage"),
            ent("cover.blinds", "closed", "Шторы в спальне"),
            ent("sensor.outside", "7.5", "Улица", device_class="temperature"),
            ent("script.leave_home", "off", "Уйти из дома"),
            *[ent(f"light.hall_{i}", "off", f"Лампа коридора {i}") for i in range(6)],
        ]}
        self.calls = []
        self.stuck = False

    async def get_states(self, req):
        if req.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"message": "no"}, status=401)
        return web.json_response(list(self.states.values()))

    async def service(self, req):
        if req.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"message": "no"}, status=401)
        dom, svc = req.match_info["domain"], req.match_info["service"]
        body = await req.json()
        self.calls.append((dom, svc, body))
        ids = body["entity_id"] if isinstance(body["entity_id"], list) else [body["entity_id"]]
        new = {"turn_on": "on", "turn_off": "off", "lock": "locked", "unlock": "unlocked", "open_cover": "open",
               "close_cover": "closed", "set_temperature": None}.get(svc)
        for i in ids:
            if new and not self.stuck:
                self.states[i]["state"] = new
            if "brightness_pct" in body:
                self.states[i]["attributes"]["brightness"] = round(body["brightness_pct"] * 2.55)
            if "temperature" in body:
                self.states[i]["attributes"]["temperature"] = body["temperature"]
        return web.json_response([self.states[i] for i in ids])

    def app(self):
        a = web.Application()
        a.router.add_get("/api/states", self.get_states)
        a.router.add_post("/api/services/{domain}/{service}", self.service)
        return a


class Store(dict):
    def store(self, k, v, label):
        self[k] = v

    def clear(self, k):
        self.pop(k, None)


@pytest.fixture
async def ha(core):
    fake = FakeHA()
    server = TestServer(fake.app())
    await server.start_server()
    core.director.home.store = Store()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


async def connected(core, ha):
    return await core.director.home.connect(ha.url, TOKEN)


async def test_connecting_checks_the_token_before_storing_it(core, ha):
    with pytest.raises(Exception, match="не принял токен"):
        await core.director.home.connect(ha.url, "x" * 40)
    assert not core.director.home.configured()
    st = await connected(core, ha)
    assert st["devices"] == len(ha.states) and core.director.home.configured()
    with pytest.raises(Exception, match="homeassistant.local"):
        await core.director.home.connect("ftp://user:pw@host", TOKEN)


async def test_phrases_run_without_the_ai_only_when_connected(core, ha):
    cid = core.director.conversation_id()
    before, _ = core.director._route_for("включи свет на кухне", cid)
    assert before is None or not str(before.tool).startswith("home")      # not connected: no smart-home guess
    await connected(core, ha)
    after, _ = core.director._route_for("включи свет на кухне", cid)
    assert after.tool == "home_control"
    t = await wait_done(core, await core.director.submit("Джарвис, включи свет на кухне", source="text"))
    assert t["state"] == "completed", t
    assert ha.calls == [("light", "turn_on", {"entity_id": "light.kitchen"})]
    assert t["steps"][-1]["verification"]["status"] == "verified"


async def test_brightness_and_temperature(core, ha):
    await connected(core, ha)
    await wait_done(core, await core.director.submit("сделай свет в спальне на 30 процентов", source="text"))
    await wait_done(core, await core.director.submit("поставь кондиционер на 22 градуса", source="text"))
    assert ("light", "turn_on", {"entity_id": "light.bedroom", "brightness_pct": 30}) in ha.calls
    assert ("climate", "set_temperature", {"entity_id": "climate.living", "temperature": 22.0}) in ha.calls


async def test_a_lock_is_never_touched_without_a_click(core, ha):
    await connected(core, ha)
    r = route("открой входную дверь", {"home": True})
    assert r is None or r.tool != "home_control"
    tid = await core.director.submit("выключи весь свет", source="text")        # many lamps → asked first
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "home_secure" and a["risk"] == "high" and not ha.calls
    assert not core.director.voice_approvable("home_secure")                  # «да» said aloud is not enough
    core.director.decide(a["id"], True)
    t = await wait_done(core, tid)
    assert t["state"] == "completed" and ha.calls[0][:2] == ("light", "turn_off") and len(ha.calls[0][2]["entity_id"]) == 8


async def test_the_low_risk_tool_refuses_locks_gates_scripts_and_crowds(core, ha):
    from jarvis.tools.base import ToolContext
    await connected(core, ha)
    ctl = core.director.gw.registry.get("home_control")
    c = ToolContext(config=core.config, task_id="t")
    for args in ({"action": "turn_off", "entity": "входная дверь", "domain": "lock"},
                 {"action": "turn_on", "entity": "гараж"},
                 {"action": "turn_on", "entity": "уйти из дома", "domain": "script"},
                 {"action": "turn_on", "entity": "лампа коридора"}):
        with pytest.raises(Exception, match="home_secure"):
            await ctl.run(ctl.validate(args, c), c)
    assert not ha.calls
    res = await ctl.run({"action": "turn_on", "entity": "шторы в спальне"}, c)     # ordinary blinds are fine
    assert ha.calls == [("cover", "open_cover", {"entity_id": "cover.blinds"})] and "Открыл" in res.summary


async def test_a_device_that_does_not_change_is_reported_failed(core, ha):
    await connected(core, ha)
    ha.stuck = True
    t = await wait_done(core, await core.director.submit("включи розетку чайника", source="text"), timeout=10)
    assert t["steps"][-1]["verification"]["status"] == "failed"


async def test_status_in_russian(core, ha):
    await connected(core, ha)
    t = await wait_done(core, await core.director.submit("что включено дома", source="text"))
    s = t["steps"][-1]["result"]["summary"]
    assert "Свет в спальне: включено, 100 %" in s and "Входная дверь: заперто" in s and "цель 22 °C" in s


async def test_home_assistant_down_is_said(core, ha):
    await connected(core, ha)
    core.db.set_kv("home_url", "http://127.0.0.1:9")
    st = await core.director.home.state()
    assert "не отвечает" in st["error"]


@pytest.mark.parametrize("said, tool, args", [
    ("включи свет на кухне", "home_control", {"action": "turn_on", "entity": "свет на кухне"}),
    ("выключи телевизор", "home_control", {"action": "turn_off", "entity": "телевизор"}),
    ("включи свет в спальне на 40%", "home_control", {"action": "turn_on", "entity": "свет в спальне", "value": 40.0}),
    ("закрой шторы", "home_control", {"action": "turn_off", "entity": "шторы"}),
    ("выключи весь свет", "home_secure", {"action": "turn_off", "entity": "весь свет"}),
])
def test_home_phrases(said, tool, args):
    h = parse_home(said)
    assert h["tool"] == tool and h["args"] == args


def test_other_commands_stay_theirs():
    for said in ("открой браузер", "закрой телеграм", "включи музыку", "включи ночной свет"):
        r = route(said, {"home": True})
        assert r is None or not r.tool.startswith("home"), said
