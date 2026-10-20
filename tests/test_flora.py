"""Acceptance tests for the synthetic «Флора» workflow (spec §2.1, §27B–27E)."""
import hashlib

import pytest

from jarvis.flora.mail import FIXTURES
from jarvis.tasks import S

from conftest import wait_done, wait_for


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def wf(core):
    return core.director.flora


def test_triage_excludes_marketing_and_injection(wf):
    a = wf.analyze()
    cats = {e["id"]: e["category"] for e in a["excluded"]}
    assert cats["m-1003"] == "MARKETING"           # «СРОЧНО!!!» promo is not a decision
    assert cats["m-1009"] == "MARKETING"
    assert cats["m-1004"] == "SUSPICIOUS"          # prompt injection quarantined
    assert cats["m-1008"] == "OTHER_PROJECT"
    decisions = [i["id"] for i in a["items"] if i["type"] == "DECISION_REQUIRED"]
    assert set(decisions) == {"m-1001", "m-1005"}
    # every item traces to a real message, and every fact quotes its source exactly
    for it in a["items"]:
        msg = wf.mail.get(it["id"])
        for f in it["facts"]:
            ev = f["evidence"]
            assert msg[ev["field"]][ev["start"]:ev["end"]] == ev["quote"]


def test_numbers_are_recomputed_not_invented(wf):
    c = wf.analyze()["comparison"]
    assert c["base_total"] == 323_100 and c["new_total"] == 463_500 and c["delta"] == 140_400
    assert c["file_delta"] == 128_400                              # the file's own (wrong) total
    an = [x for x in c["anomalies"] if x["kind"] == "total_formula_range"]
    assert an and an[0]["cell"] == "F10" and an[0]["missing_sum"] == 12_000


def test_cost_claim_and_contradiction_detected(wf):
    a = wf.analyze()
    claim = a["cost_claims"][0]
    assert claim["stated"] == 128_000 and not claim["matches_lines"] and claim["matches_file_total"]
    contr = [c for c in a["contradictions"] if c["element"] == "W-3P" and c["attribute"] == "height"]
    assert contr and {v["value"] for v in contr[0]["values"]} == {2000, 2100}
    assert contr[0]["status"] == "UNRESOLVED"
    assert not a["alternative"]["comparable"] and len(a["alternative"]["reasons"]) == 3


def test_summary_mentions_grounded_facts_only(wf):
    s = wf.summary(wf.analyze())
    assert "140\u202f400 ₽" in s and "2000 мм" in s and "2100 мм" in s
    assert "подозрительные — 1" in s and "рассылки — 2" in s
    assert "скидк" not in s.casefold()


async def test_send_requires_approval_and_is_verified_once(core, wf):
    d = core.director
    tid = await d.submit("подготовь ответ по Флоре")
    t = await wait_done(core, tid)
    assert t["state"] == S.COMPLETED
    draft = wf.draft()
    assert "140\u202f400 ₽" in draft["body"] and draft["status"] == "draft"
    assert not wf.mail.sent_all()
    tid2 = await d.submit("Флора: отправь ответ")
    await wait_for(lambda: core.approvals.pending())
    a = core.approvals.pending()[0]
    assert a["tool_id"] == "flora_send" and a["risk"] == "high"
    assert a["args"]["body"] == draft["body"] and "Алина Соколова" in a["args"]["to"]
    assert not wf.mail.sent_all()                                   # nothing sent before approval
    d.decide(a["id"], True)
    t2 = await wait_done(core, tid2)
    step = [s for s in t2["steps"] if s["tool_id"] == "flora_send"][0]
    assert step["verification"]["status"] == "verified"
    assert len(wf.mail.sent_all()) == 1
    # Repeating the same operation id never produces a second message.
    again = wf.mail.send(draft["operation_id"], {"to": "x", "subject": "x", "body": "x"})
    assert again["duplicate"] and len(wf.mail.sent_all()) == 1
    assert any(x["status"] == "clarification_requested" for x in wf.decisions())
    assert wf.analyze()["items"][1 if wf.analyze()["items"][0]["id"] == "m-1005" else 0]["reply"]


async def test_edit_after_approval_request_invalidates(core, wf):
    d = core.director
    await wait_done(core, await d.submit("подготовь ответ по Флоре"))
    draft = wf.draft()
    tid = await d.submit("Флора: отправь")
    await wait_for(lambda: core.approvals.pending())
    aid = core.approvals.pending()[0]["id"]
    wf.save_draft({"body": draft["body"] + "\nP.S. правка"}, draft["id"])
    assert core.approvals.get(aid)["status"] == "expired"
    t = await wait_done(core, tid)
    assert [s for s in t["steps"] if s["tool_id"] == "flora_send"][0]["state"] == "denied"
    assert not wf.mail.sent_all()


async def test_rejected_send_sends_nothing(core, wf):
    d = core.director
    await wait_done(core, await d.submit("подготовь ответ по Флоре"))
    tid = await d.submit("Флора: отправь")
    await wait_for(lambda: core.approvals.pending())
    d.decide(core.approvals.pending()[0]["id"], False)
    await wait_done(core, tid)
    assert not wf.mail.sent_all() and wf.draft()["status"] == "draft"


def test_sources_never_modified_and_annotations_are_sidecar(wf):
    before = {p.name: sha(p) for p in FIXTURES.iterdir()}
    a = wf.analyze()
    rev_c = a["drawings"][1]["file"]
    saved = wf.save_annotations(rev_c, [{"type": "rect", "points": [[1, 2], [30, 40]]},
                                        {"type": "script", "points": []}])
    assert [s["type"] for s in saved["shapes"]] == ["rect"]          # unknown shape types dropped
    assert wf.annotations(rev_c)["shapes"][0]["points"] == [[1.0, 2.0], [30.0, 40.0]]
    with pytest.raises(ValueError):
        wf.save_annotations("../../etc/passwd", [])
    assert {p.name: sha(p) for p in FIXTURES.iterdir()} == before


async def test_injection_mail_cannot_trigger_actions(core, wf):
    """The suspicious mail asks to forward all estimates. Nothing in the triage path
    sends, writes or creates approvals."""
    tid = await core.director.submit("Что важного по Флоре?")
    t = await wait_done(core, tid)
    assert [s["tool_id"] for s in t["steps"]] == ["flora_inbox"]
    assert not core.approvals.pending() and not wf.mail.sent_all()
