"""Intake: natural-language requirements → SearchCriteria."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .. import textparse as tp
from ..schemas import ParsedQuery, SearchCriteria
from ..services.llm import LLMUnavailable, get_llm

KNOWN_CITIES = ["pune", "mumbai", "bangalore", "bengaluru", "hyderabad", "chennai", "delhi", "gurgaon",
                "gurugram", "noida", "kolkata", "ahmedabad", "navi mumbai", "thane"]

SYSTEM = """You convert a tenant's rental requirements (India) into structured search criteria.
Rules:
- Only fill a field if the user stated or clearly implied it. Never invent localities, budgets or dates.
- Amounts are monthly INR integers ("20k" = 20000, "1.5 lakh" = 150000).
- "under/below/max X" sets max_rent. "around X" sets max_rent to X*1.1 and min_rent to X*0.8 rounded to 500.
- "decent society" → building_types ["gated society"]; "good ventilation" → amenities ["ventilation"].
- "move within N days" → move_in_by = today + N days.
- "reasonable deposit" with no number → leave max_deposit null and add to `unclear`.
- Put anything that doesn't fit a field in other_requirements.
- List every default you applied in `assumptions`."""


def parse_query(text: str, today: date | None = None) -> ParsedQuery:
    today = today or date.today()
    llm = get_llm()
    if llm.available:
        try:
            pq = llm.structured(SYSTEM, f"Today is {today.isoformat()}.\n\nRequirements:\n{text}", ParsedQuery)
            return _post_process(pq, text, today)
        except LLMUnavailable:
            pass
    return _post_process(rule_parse(text, today), text, today)


def _post_process(pq: ParsedQuery, text: str, today: date) -> ParsedQuery:
    c = pq.criteria
    c.property_types = [p.upper().replace(" ", "") for p in c.property_types]
    if c.lease_months is None:
        c.lease_months = 11
        pq.assumptions.append("Lease duration defaulted to 11 months (standard leave & licence term).")
    if c.max_deposit is None and c.max_rent:
        # Typical outside Bengaluru: 2–3 months. Use 3 as an upper bound, flag it.
        c.max_deposit = c.max_rent * 3
        pq.assumptions.append(f"Max deposit assumed as 3 months' rent (₹{c.max_deposit:,}). Edit if needed.")
    if not c.localities:
        pq.unclear.append("No locality given — discovery will search the whole city.")
    if not c.city:
        pq.unclear.append("City not recognised.")
    return pq


def rule_parse(text: str, today: date) -> ParsedQuery:
    low = text.lower()
    c = SearchCriteria()
    assumptions: list[str] = []
    for city in KNOWN_CITIES:
        if re.search(rf"\b{city}\b", low):
            c.city = {"bengaluru": "Bangalore", "gurugram": "Gurgaon"}.get(city, city.title())
            break
    # localities: "in A or B, City" / "in A, B and C"
    m = re.search(r"\bin\s+([a-z][a-z .,&/-]+?)(?:,\s*(?:" + "|".join(KNOWN_CITIES) + r")\b|\s+under|\s+below|\s+for|\s+within|[.;]|$)", low)
    if m:
        parts = re.split(r",|\bor\b|\band\b|/|&", m.group(1))
        locs = [p.strip().title() for p in parts if p.strip() and p.strip() not in KNOWN_CITIES]
        c.localities = [l for l in locs if len(l) < 40][:8]
    ptype, n = tp.bhk(text)
    if ptype:
        c.property_types = [ptype]
        c.min_bedrooms = int(n) if n and n >= 1 else None
    m = re.search(r"(?:under|below|max(?:imum)?|upto|up to|within|less than|<)\s*(?:₹|rs\.?|inr)?\s*([\d,.]+)\s*(k|lakh|l)?", low)
    if m:
        c.max_rent = tp.parse_amount(m.group(1), m.group(2))
    m = re.search(r"(?:between|from)\s*(?:₹|rs\.?)?\s*([\d,.]+)\s*(k)?\s*(?:and|to|-)\s*(?:₹|rs\.?)?\s*([\d,.]+)\s*(k)?", low)
    if m:
        c.min_rent = tp.parse_amount(m.group(1), m.group(2) or m.group(4))
        c.max_rent = tp.parse_amount(m.group(3), m.group(4))
    m = re.search(r"around\s*(?:₹|rs\.?)?\s*([\d,.]+)\s*(k)?", low)
    if m and not c.max_rent:
        v = tp.parse_amount(m.group(1), m.group(2)) or 0
        c.max_rent, c.min_rent = int(v * 1.1 // 500 * 500), int(v * 0.8 // 500 * 500)
    f = tp.furnishing(text)
    if f:
        c.furnishing = [f]
    if "parking" in low:
        c.parking = "car" if re.search(r"car parking|4[\s-]?wheeler|covered parking", low) else "any"
    if re.search(r"decent society|gated|good society|society", low):
        c.building_types = ["gated society"]
    c.amenities = [a for a in tp.amenities(text) if a != "parking"]
    m = re.search(r"deposit[^\d]{0,20}([\d,.]+)\s*(k|lakh|l)?", low)
    if m:
        c.max_deposit = tp.parse_amount(m.group(1), m.group(2))
    if re.search(r"no brokerage|without broker|owner only|no broker", low):
        c.brokerage = "no_brokerage"
    for word, t in [("family", "family"), ("couple", "couple"), ("bachelor", "bachelor")]:
        if word in low:
            c.tenant_type = t  # type: ignore[assignment]
            break
    m = re.search(r"(?:within|in)\s*(\d+)\s*days", low)
    if m:
        c.move_in_by = today + timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*months?\s*(?:lease|agreement)", low)
    if m:
        c.lease_months = int(m.group(1))
    if "reasonable deposit" in low and c.max_deposit is None:
        assumptions.append("'Reasonable deposit' interpreted as up to 3 months' rent.")
    return ParsedQuery(criteria=c, assumptions=assumptions + ["Parsed with rule-based parser (no LLM key)."])
