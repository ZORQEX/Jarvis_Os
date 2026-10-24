"""Inside JARVIS: which spoken phrases are about orders (so JARVIS hands them to this assistant), the embedding
config, and that web pages from other sites cannot change data here. Synthetic fixture only; no network."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_factory_settlement import db, imported  # noqa: F401
from app import agent
from app.config import settings
from app.database import get_db
from app.jarvis_routes import claims
from app.main import app


@pytest.mark.parametrize("said,claim", [
    ("поставь ай ди эс тридцать пять процентов", "strong"),          # spoken customer code
    ("сколько должна AA", "strong"),
    ("открой диор круиз", "strong"),                                 # brand said in Russian
    ("покажи сезон SS27", "strong"),
    ("посчитай предоплаты", "strong"),
    ("какая наценка у фабрики", "strong"),
    ("покажи заказы", "weak"),                                       # generic: JARVIS decides
    ("открой браузер", None),
    ("который час", None),
    ("закажи такси", None),                                          # the verb is not an order
])
def test_what_is_about_orders(db, tmp_path, said, claim):  # noqa: F811
    imported(db, tmp_path)
    assert claims(db, said)["claim"] == claim, claims(db, said)


def test_an_answer_to_a_pending_proposal_belongs_here(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    conv = "d" * 16
    assert claims(db, "да, сохрани", conv)["claim"] is None            # nothing pending: «да» is JARVIS's
    result = agent.handle(db, conv, "Поставь IDS 35%")                 # the rule path prepares a proposal
    assert result.proposal and result.proposal["status"] == "pending"
    out = claims(db, "да, сохрани", conv)
    assert out["claim"] == "strong" and "предложение" in out["why"]
    agent.commit_proposal(db, result.proposal["id"])
    assert claims(db, "да", conv)["claim"] is None                    # saved: no longer pending


@pytest.fixture
def client(db, monkeypatch):  # noqa: F811
    app.dependency_overrides[get_db] = lambda: (yield db)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def test_endpoints_and_cross_site_writes(client, db, tmp_path, monkeypatch):  # noqa: F811
    imported(db, tmp_path)
    r = client.post("/api/jarvis/claims", json={"text": "поставь эс эс тридцать процентов"})
    assert r.json()["claim"] == "strong" and "SS" in r.json()["normalized"] and "30%" in r.json()["normalized"]
    state = client.get("/api/jarvis/state").json()
    assert state["seasons"] == ["SS27"] and state["items"] > 0
    refused = client.post("/api/jarvis/claims", json={"text": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert refused.status_code == 403
    assert client.post("/api/jarvis/claims", json={"text": "x"}, headers={"Sec-Fetch-Site": "same-site"}).status_code == 200
    monkeypatch.setattr(settings, "jarvis_origin", "http://127.0.0.1:8791")
    page = client.get("/board").text
    assert "&#34;jarvis&#34;: &#34;http://127.0.0.1:8791&#34;" in page or '"jarvis": "http://127.0.0.1:8791"' in page


@pytest.mark.parametrize("heard,expected", [
    ("Покажи заказы клиента СС.", "Покажи заказы клиента SS."),       # «эс-эс» written in Cyrillic by the recogniser
    ("Сделай Excel и PDF для ИДС", "Сделай Excel и PDF для IDS"),
    ("Сделай Excel EPDF для IDS.", "Сделай Excel и PDF для IDS."),   # «Excel и PDF» run together
    ("Сделай экселли PDF для IDS.", "Сделай Excel и PDF для IDS."),
    ("ООО Ромашка", "ООО Ромашка"),                                   # not a customer code: unchanged
    ("размер S и M", "размер S и M"),                                 # sizes are not codes
    ("Джарвис, поставь ID 35%.", "Джарвис, поставь IDS 35%."),       # the last letter dropped by the recogniser
    ("Покажи все заказы S.", "Покажи все заказы SS."),
    ("Покажи заказы размера S клиента IDS", "Покажи заказы размера S клиента IDS"),
    ("Какой у меня Apple ID", "Какой у меня Apple ID"),               # not about orders: left alone
])
def test_what_the_local_recogniser_writes(heard, expected):
    from app.voice import normalize_transcript
    assert normalize_transcript(heard, ["AA", "IDS", "SS", "ALINA"], ["Dior cruise"]) == expected


# --- JARVIS's own AI on this app's tools (app/jarvis_ai.py) and the page inside JARVIS's frame (app/jarvis_embed.py) --
def test_no_rule_answer_is_handed_to_jarvis_and_its_tools_make_a_proposal(client, db, tmp_path, monkeypatch):  # noqa: F811
    imported(db, tmp_path)
    monkeypatch.setattr(settings, "ai_provider", "claude-code")
    from app import claude_code

    def no_second_assistant(*a, **k):
        raise AssertionError("inside JARVIS this app never starts its own AI")
    monkeypatch.setattr(claude_code, "run_turn", no_second_assistant)
    conv = "e" * 16
    out = client.post("/api/agent/message", json={"conversation": conv, "text": "Почему у IDS такая сумма и что с ней делать?",
                                                   "jarvis": True, "turn": "t_test1"}).json()
    assert out["handoff"] is True and out["turn"] == "t_test1" and "IDS" in out["text"]
    spec = client.get("/api/jarvis/ai").json()
    names = {t["name"] for t in spec["tools"]}
    assert "propose_changes" in names and "open_file_picker" not in names and "ТОЛЬКО из результатов" in spec["system"]
    alloc = client.post("/api/jarvis/ai/tool", json={"turn": "t_test1", "conversation": conv,
                                                     "name": "get_customer_allocations", "params": {"customer": "IDS"}}).json()
    assert "error" not in alloc
    prop = client.post("/api/jarvis/ai/tool", json={"turn": "t_test1", "conversation": conv, "name": "propose_changes",
                                                    "params": {"summary": "IDS 35 %", "actions": [{"action": "set_customer_markup",
                                                               "params": {"customer": "IDS", "markup": "35"}}]}}).json()
    assert prop["pending_approval"] is True and prop["saved"] is False and "Подтвердить" in prop["note"]
    fin = client.post("/api/jarvis/ai/finish", json={"turn": "t_test1", "reply": "Предлагаю IDS 35 %."}).json()
    assert fin["proposal"]["status"] == "pending" and fin["reply"] == "Предлагаю IDS 35 %." and fin["speech"]
    assert claims(db, "да, сохрани", conv)["claim"] == "strong"          # «да» now answers JARVIS's proposal
    hist = agent.history(db, conv)
    assert [m.role for m in hist][-2:] == ["user", "assistant"] and hist[-1].text == "Предлагаю IDS 35 %."
    bad = client.post("/api/jarvis/ai/tool", json={"turn": "../x", "conversation": conv, "name": "check_data"})
    assert bad.status_code == 422
    assert client.post("/api/jarvis/ai/tool", json={"turn": "t_test2", "conversation": conv,
                                                    "name": "shell", "params": {}}).json()["error"].startswith("Неизвестный")


def test_without_jarvis_the_app_keeps_its_own_assistant(client, db, tmp_path, monkeypatch):  # noqa: F811
    imported(db, tmp_path)
    monkeypatch.setattr(settings, "ai_provider", "")
    out = client.post("/api/agent/message", json={"conversation": "f" * 16, "text": "Почему у IDS такая сумма?"}).json()
    assert "handoff" not in out and out["reply"]                         # rules answer (no key here), as before


def test_a_page_in_jarvis_frame_is_part_of_jarvis(client, db, tmp_path, monkeypatch):  # noqa: F811
    imported(db, tmp_path)
    monkeypatch.setattr(settings, "jarvis_origin", "http://127.0.0.1:8791")
    framed = client.get("/board", headers={"Sec-Fetch-Dest": "iframe"}).text
    assert "<html" in framed and "data-jarvis" in framed and "/static/jarvis.css" in framed and 'class="in-jarvis' in framed
    direct = client.get("/board", headers={"Sec-Fetch-Dest": "document"}).text
    assert "data-jarvis" not in direct and "/static/jarvis.css" not in direct        # opened directly: the app as is
    r = client.get("/assistant", headers={"Sec-Fetch-Dest": "iframe"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/board"              # the assistant is JARVIS
    monkeypatch.setattr(settings, "jarvis_origin", "")
    assert "data-jarvis" not in client.get("/board", headers={"Sec-Fetch-Dest": "iframe"}).text


def test_without_jarvis_ai_the_handed_back_turn_ends_with_the_full_rules(client, db, tmp_path, monkeypatch):  # noqa: F811
    imported(db, tmp_path)
    monkeypatch.setattr(settings, "ai_provider", "claude-code")
    conv = "c" * 16
    out = client.post("/api/agent/message", json={"conversation": conv, "text": "Сделай Excel и PDF для IDS",
                                                   "jarvis": True, "turn": "t_rules"}).json()
    if not out.get("handoff"):
        pytest.skip("the fast rules answered this phrase themselves")
    r = client.post("/api/jarvis/ai/rules", json={"turn": "t_rules"}).json()
    assert r["reply"] and r["speech"]
    assert agent.history(db, conv)[-1].role == "assistant"
    assert client.post("/api/jarvis/ai/rules", json={"turn": "t_rules"}).json()["reply"] == ""     # the turn is over
