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
