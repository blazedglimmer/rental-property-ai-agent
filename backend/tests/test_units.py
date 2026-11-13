from datetime import date

from app.agents import communication as comm, negotiation as nego
from app.agents.closure import checklist, rule_review
from app.agents.intake import parse_query
from app.agents.matching import score_listing
from app.models import Listing
from app.schemas import NegotiationBounds, ScoringWeights, SearchCriteria
from app import textparse as tp


def test_parse_example_query():
    pq = parse_query("Find me a furnished 1BHK in Baner or Aundh, Pune, under ₹20,000. I need a decent society, "
                     "good ventilation, kitchen, parking and a reasonable deposit. I want to move within 30 days.",
                     today=date(2026, 11, 6))
    c = pq.criteria
    assert c.city == "Pune"
    assert set(c.localities) == {"Baner", "Aundh"}
    assert c.max_rent == 20000
    assert c.property_types == ["1BHK"]
    assert c.furnishing == ["furnished"]
    assert c.parking in ("any", "car")
    assert "ventilation" in c.amenities and "kitchen" in c.amenities
    assert c.move_in_by == date(2026, 12, 6)
    assert c.building_types == ["gated society"]
    assert c.max_deposit == 60000  # assumed 3 months, flagged in assumptions
    assert any("deposit" in a.lower() for a in pq.assumptions)


def test_amounts():
    assert tp.amounts("rent 21k, deposit ₹63,000, 2 BHK, 620 sqft") == [21000, 63000]
    assert tp.amount_near("Deposit is 1.5 lakh", ["deposit"]) == 150000


def _listing(**kw):
    base = dict(id=1, title="t", city="Pune", locality="Baner", property_type="1BHK", bhk=1, rent=19000,
                deposit=40000, maintenance=1000, furnishing="furnished", amenities=["parking", "kitchen", "ventilation"],
                photos=[], confidence=60, tenant_pref=[], contact_role="owner", brokerage_free=True,
                description="newly painted, near metro")
    base.update(kw)
    return Listing(**base)


def test_scoring_explains_and_caps_hard_fails():
    c = SearchCriteria(city="Pune", localities=["Baner"], max_rent=20000, property_types=["1BHK"],
                       furnishing=["furnished"], amenities=["kitchen", "ventilation"], parking="any", max_deposit=60000)
    good = score_listing(_listing(), c, ScoringWeights())
    assert good.score > 75 and not good.hard_fail
    assert {r["factor"] for r in good.breakdown} == set(ScoringWeights().model_dump())
    assert all(r["reason"] for r in good.breakdown)
    bad = score_listing(_listing(property_type="2BHK", bhk=2), c, ScoringWeights())
    assert bad.hard_fail and bad.score <= 40
    over = score_listing(_listing(rent=25000), c, ScoringWeights())
    assert over.hard_fail
    assert good.total_monthly_cost == 20000


def test_negotiation_never_exceeds_bounds():
    b = NegotiationBounds(target_rent=18000, max_rent=20000, max_deposit=60000, max_brokerage=0, flexibility="flexible")
    asking = {"rent": 22000, "deposit": 90000, "brokerage": 22000}
    our = None
    their = None
    for r in range(5):
        d = nego.decide(b, asking, their, our, r)
        for k, cap in [("rent", 20000), ("deposit", 60000), ("brokerage", 0)]:
            if k in d.offer:
                assert d.offer[k] <= cap
        our = d.offer or our
        if d.action != "counter":
            break
    assert d.action == "escalate"  # 22k > max after rounds → user must decide


def test_negotiation_recommends_accept_within_max():
    b = NegotiationBounds(target_rent=18000, max_rent=20000)
    d = nego.decide(b, {"rent": 21000}, {"rent": 19500}, {"rent": 18500}, 2)
    assert d.action == "recommend_accept" and d.offer["rent"] == 19500


def test_guard_blocks_unexpected_amounts_and_commitments():
    b = NegotiationBounds(target_rent=18000, max_rent=20000)
    d = nego.decide(b, {"rent": 22000}, None, None, 0)
    good = f"Would you consider rent of ₹{d.offer['rent']:,}/month?"
    assert nego.guard(good, d, b, {"rent": 22000}) == []
    assert nego.guard("Could you do ₹21,000?", d, b, {"rent": 22000})
    assert nego.guard(f"We accept ₹{d.offer['rent']:,}", d, b, {"rent": 22000})


def test_disclosure_always_present():
    assert "AI assistant" in comm.ensure_disclosure("Hello, is it available?")


def test_reply_rules_detect_scam_and_terms():
    l = _listing()
    f = comm.rule_analyze("Yes available. Rent 21000, 2 months deposit. Pay 5000 token via UPI before visit.", l)
    assert f.availability == "available" and f.rent == 21000 and f.deposit == 42000
    assert f.asks_for_advance_payment


def test_maharashtra_checklist_and_agreement_review():
    items = {i["key"] for i in checklist("Pune", {"rent": 1})}
    assert {"registration", "police_verification", "stamp_duty"} <= items
    r = rule_review("The monthly rent of Rs. 21,000 ... security deposit of Rs. 60,000. Lock-in period of 11 months. "
                    "Deposit shall be forfeited.", {"rent": 19500, "deposit": 60000, "lock_in_months": 6})
    keys = {d["clause"] for d in r["discrepancies"]}
    assert "rent" in keys and "lock_in_months" in keys and "deposit" not in keys
    assert r["risky_clauses"]
