"""End-to-end: search → discovery (sample feed) → verify → score → outreach approval → replies →
negotiation → visit → feedback → closure. Runs without an API key (deterministic fallbacks)."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.graph import deal_graph, search_graph
from app.main import app
from app.models import Approval, Listing, Message, Negotiation, Visit

client = TestClient(app)
QUERY = ("Find me a furnished 1BHK in Baner or Aundh, Pune, under ₹20,000. I need a decent society, good "
         "ventilation, kitchen, parking and a reasonable deposit. I want to move within 30 days.")


def _approve_pending(lid, kind=None, decision="approve", **extra):
    with session_scope() as db:
        q = select(Approval).where(Approval.listing_id == lid, Approval.status == "pending")
        if kind:
            q = q.where(Approval.kind == kind)
        a = db.scalars(q.order_by(Approval.id.desc())).first()
        assert a, f"no pending approval ({kind}) for listing {lid}"
        aid, body = a.id, a.payload.get("body")
    r = client.post(f"/api/approvals/{aid}", json={"decision": decision, **extra})
    assert r.status_code == 200, r.text
    return body


def test_full_workflow():
    r = client.post("/api/searches", json={
        "raw_query": QUERY, "run_now": False,
        "bounds": {"target_rent": 18000, "max_rent": 20000, "max_brokerage": 0, "flexibility": "moderate"},
        "sources": [{"type": "partner_feed", "url": "samples/pune_sample_feed.json", "name": "sample", "sample": True}]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    out = search_graph.run(sid)  # synchronous for the test
    ranked = client.get(f"/api/searches/{sid}/listings").json()
    assert len(ranked) == 10
    by_title = {l["title"]: l for l in ranked}

    # Scam listing flagged and ranked low; 2BHK is a hard fail; duplicates flagged
    scam = next(l for l in ranked if "army" in (l["description"] or ""))
    assert scam["confidence"] < 40 and scam["match"]["score"] < 40
    assert any(f["code"] == "scam_language" for f in scam["risk_flags"])
    assert by_title["2BHK semi-furnished Baner"]["match"]["hard_fail"]
    copy = next(l for l in ranked if l["contact_name"] == "Rahul (agent)")
    assert any(f["code"] == "dup_text" for f in copy["risk_flags"])
    assert all(r["reason"] for r in ranked[0]["match"]["breakdown"])

    # Outreach drafts created and waiting for approval (cap = 3), never sent without approval
    assert 1 <= len(out["outreach_ids"]) <= 3
    assert scam["id"] not in out["outreach_ids"]
    with session_scope() as db:
        assert not db.scalar(select(Message).where(Message.status.in_(["sent", "simulated"])))
    lid = out["outreach_ids"][0]
    assert deal_graph.pending_interrupt(lid)["kind"] == "approval"

    body = _approve_pending(lid, "first_contact")
    assert "AI assistant" in body
    assert deal_graph.pending_interrupt(lid)["kind"] == "await_reply"
    with session_scope() as db:
        rent_before = db.get(Listing, lid).rent

    # Owner replies with terms above budget → counter-offer within bounds needs approval
    client.post(f"/api/listings/{lid}/inbound?sync=true",
                json={"body": "Hi, yes it is available. Rent is 22000, deposit 2 months. No brokerage, I am the owner."})
    deal_graph.resume(lid, {}, expect_kind="await_reply")  # inbound resume is async in API; ensure processed
    with session_scope() as db:
        l = db.get(Listing, lid)
        assert l.availability == "confirmed" and l.rent == 22000
        n = db.scalar(select(Negotiation).where(Negotiation.listing_id == lid))
        assert n.our_last_offer["rent"] <= 20000
        offer_rent = n.our_last_offer["rent"]
        assert n.our_last_offer.get("deposit", 0) <= 60000
    assert rent_before is not None
    body = _approve_pending(lid, "offer")
    assert f"{offer_rent:,}" in body

    # Owner accepts → visit slots proposed automatically (auto_visit_slots) → wait reply
    client.post(f"/api/listings/{lid}/inbound?sync=true", json={"body": f"Ok, {offer_rent} is fine. Agreed."})
    deal_graph.resume(lid, {}, expect_kind="await_reply")
    st = deal_graph.state(lid)
    assert st["values"]["phase"] == "scheduling", st
    assert st["interrupt"]["kind"] == "await_reply"

    client.post(f"/api/listings/{lid}/inbound?sync=true", json={"body": "First option works, see you then."})
    deal_graph.resume(lid, {}, expect_kind="await_reply")
    assert deal_graph.pending_interrupt(lid)["kind"] == "visit_feedback"
    with session_scope() as db:
        v = db.scalar(select(Visit).where(Visit.listing_id == lid))
        assert v.status == "confirmed"
        vid = v.id
    assert client.get(f"/api/visits/{vid}/ics").text.startswith("BEGIN:VCALENDAR")

    assert client.post(f"/api/visits/{vid}/feedback", json={"liked": True, "rating": 4, "notes": "Good light"}).status_code == 200
    _approve_pending(lid, "proceed_to_closure")
    assert deal_graph.pending_interrupt(lid)["kind"] == "closure"
    deal = client.get(f"/api/listings/{lid}").json()["deal"]
    assert deal["final_terms"]["rent"] == offer_rent
    assert any(i["key"] == "registration" for i in deal["checklist"])

    rev = client.post(f"/api/deals/{lid}/agreement",
                      json={"text": f"Monthly licence fee of Rs. 21,000. Security deposit Rs. 40,000. Notice 1 month."}).json()
    assert any(d["clause"] == "rent" for d in rev["discrepancies"])

    assert client.post(f"/api/deals/{lid}/close", json={"closed": True}).status_code == 200
    with session_scope() as db:
        assert db.get(Listing, lid).status == "closed"
    dash = client.get(f"/api/searches/{sid}/dashboard").json()
    assert dash["discovered"] == 10 and dash["closing"] >= 1


def test_unavailable_reply_and_advance_payment_stop():
    r = client.post("/api/searches", json={
        "raw_query": QUERY, "run_now": False,
        "sources": [{"type": "partner_feed", "url": "samples/pune_sample_feed.json", "name": "sample", "sample": True}]})
    sid = r.json()["id"]
    search_graph.run(sid)
    # pick listings that haven't been started yet
    with session_scope() as db:
        ids = [l.id for l in db.scalars(select(Listing).where(Listing.status.in_(["contacting"])))]
    assert ids
    a, rest = ids[0], ids[1:]
    _approve_pending(a, "first_contact")
    client.post(f"/api/listings/{a}/inbound?sync=true", json={"body": "Sorry, it's already rented out."})
    deal_graph.resume(a, {}, expect_kind="await_reply")
    with session_scope() as db:
        assert db.get(Listing, a).availability == "unavailable"
    if rest:
        b = rest[0]
        _approve_pending(b, "first_contact")
        client.post(f"/api/listings/{b}/inbound?sync=true",
                    json={"body": "Available. Please transfer 5000 token amount via GPay first, then visit."})
        deal_graph.resume(b, {}, expect_kind="await_reply")
        with session_scope() as db:
            l = db.get(Listing, b)
            assert l.status == "rejected"
            assert any(f["code"] == "advance_payment" for f in l.risk_flags)
