"""Agent 2 — Property Matching: explainable weighted score."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Listing
from ..schemas import ScoringWeights, SearchCriteria

# Small adjacency hints so a neighbouring locality is not scored like a different city.
NEARBY: dict[str, set[str]] = {
    "baner": {"aundh", "balewadi", "pashan", "sus", "mahalunge", "bavdhan"},
    "aundh": {"baner", "pashan", "bopodi", "pimple saudagar", "sangvi"},
    "balewadi": {"baner", "wakad", "mahalunge"},
    "wakad": {"hinjewadi", "balewadi", "pimple saudagar", "tathawade"},
    "hinjewadi": {"wakad", "maan", "marunji", "tathawade"},
    "kharadi": {"viman nagar", "wagholi", "chandan nagar"},
    "viman nagar": {"kharadi", "kalyani nagar", "yerwada"},
    "koramangala": {"hsr layout", "btm layout", "indiranagar", "ejipura"},
    "hsr layout": {"koramangala", "btm layout", "bellandur", "agara"},
    "indiranagar": {"domlur", "koramangala", "hal", "jeevan bhima nagar"},
    "andheri west": {"juhu", "versova", "jogeshwari west", "vile parle west"},
    "powai": {"chandivali", "vikhroli", "kanjurmarg"},
}
CONNECTIVITY_WORDS = ["metro", "railway", "station", "bus stop", "highway", "it park", "tech park",
                      "walking distance", "near", "close to", "minutes from", "mins from", "ring road"]
GOOD_CONDITION = ["newly painted", "new construction", "brand new", "renovated", "well maintained",
                  "well-maintained", "spacious", "new flat", "first owner", "ready to move", "premium"]
BAD_CONDITION = ["old building", "needs repair", "seepage", "leakage", "old construction", "dilapidated"]


@dataclass
class ScoreResult:
    score: float
    breakdown: list[dict]
    compromises: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    hard_fail: bool = False
    total_monthly_cost: int | None = None


def total_monthly_cost(l: Listing, lease_months: int = 11) -> int | None:
    if l.rent is None:
        return None
    cost = l.rent + (l.maintenance or 0)
    if l.brokerage:
        cost += l.brokerage // max(lease_months, 1)
    return int(cost)


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def _text(l: Listing) -> str:
    return " ".join(filter(None, [l.title, l.description, l.address, l.society, " ".join(l.amenities or [])])).lower()


def score_listing(l: Listing, c: SearchCriteria, w: ScoringWeights) -> ScoreResult:
    W = w.normalised()
    rows: list[dict] = []
    compromises: list[str] = []
    missing: list[str] = []
    hard = False
    text = _text(l)

    def add(factor: str, s: float, reason: str) -> None:
        s = max(0.0, min(1.0, s))
        rows.append({"factor": factor, "weight": round(W[factor], 1), "score": round(s, 2),
                     "points": round(W[factor] * s, 1), "reason": reason})

    # Budget — on total monthly cost, not advertised rent
    tmc = total_monthly_cost(l, c.lease_months or 11)
    if tmc is None:
        add("budget", 0.5, "Rent not stated")
        missing.append("rent")
    elif c.max_rent:
        if l.rent and l.rent > c.max_rent:
            over = (l.rent - c.max_rent) / c.max_rent
            add("budget", max(0, 0.4 - over * 3), f"Rent ₹{l.rent:,} is {over:.0%} over your max ₹{c.max_rent:,}")
            compromises.append(f"Rent above budget by ₹{l.rent - c.max_rent:,}")
            hard = over > 0.10  # >10% over is a hard miss; within 10% may be negotiable
        else:
            floor = c.min_rent or int(c.max_rent * 0.6)
            span = max(c.max_rent - floor, 1)
            s = 0.75 + 0.25 * min(1, max(0, (c.max_rent - tmc) / span))
            if tmc > c.max_rent:
                s = 0.6
                compromises.append(f"Rent fits but total monthly cost ₹{tmc:,} (incl. maintenance/brokerage) exceeds max")
            add("budget", s, f"Total monthly cost ≈ ₹{tmc:,} vs max ₹{c.max_rent:,}")
        if l.maintenance is None:
            missing.append("maintenance")
    else:
        add("budget", 0.7, "No budget set")

    # Location
    loc = _norm(l.locality) + " " + _norm(l.address)
    prefs = [_norm(x) for x in c.localities]
    if not prefs:
        add("location", 0.7 if c.city and c.city.lower() in (loc + _norm(l.city)) else 0.5, "No locality preference")
    elif any(p and p in loc for p in prefs):
        add("location", 1.0, f"In preferred locality ({l.locality})")
    elif any(_norm(l.locality) in NEARBY.get(p, set()) for p in prefs):
        add("location", 0.65, f"{l.locality} is adjacent to your preferred localities")
        compromises.append(f"Adjacent locality: {l.locality}")
    elif l.locality:
        add("location", 0.2, f"{l.locality} is outside your preferred localities")
        compromises.append(f"Different locality: {l.locality}")
    else:
        add("location", 0.4, "Locality unknown")
        missing.append("exact location")
    if c.preferred_societies and l.society and any(_norm(s) in _norm(l.society) for s in c.preferred_societies):
        rows[-1]["score"] = 1.0
        rows[-1]["points"] = round(W["location"], 1)
        rows[-1]["reason"] += f"; preferred society {l.society}"

    # Condition (text signals + evidence)
    good = [g for g in GOOD_CONDITION if g in text]
    bad = [b for b in BAD_CONDITION if b in text]
    s = 0.5 + 0.12 * len(good) - 0.25 * len(bad) + (0.1 if len(l.photos or []) >= 4 else 0)
    reason = ("Positive: " + ", ".join(good) if good else "No condition details") + (
        "; concerns: " + ", ".join(bad) if bad else "")
    add("condition", s, reason)
    if not good and not bad:
        missing.append("property condition / age")

    # Furnishing
    if not c.furnishing:
        add("furnishing", 0.8, "No furnishing preference")
    elif l.furnishing is None:
        add("furnishing", 0.5, "Furnishing not stated")
        missing.append("furnishing")
    elif l.furnishing in c.furnishing:
        add("furnishing", 1.0, f"{l.furnishing.title()} as requested")
    else:
        order = ["unfurnished", "semi-furnished", "furnished"]
        gap = abs(order.index(l.furnishing) - order.index(c.furnishing[0])) if c.furnishing[0] in order else 2
        add("furnishing", 0.55 if gap == 1 else 0.2, f"{l.furnishing.title()} (you want {c.furnishing[0]})")
        compromises.append(f"Furnishing: {l.furnishing}")

    # Amenities (+ parking requirement)
    wanted = [a.lower() for a in c.amenities]
    have = {a.lower() for a in (l.amenities or [])}
    hit = [a for a in wanted if a in have or a in text]
    s = (len(hit) / len(wanted)) if wanted else 0.7
    reason = f"Has {len(hit)}/{len(wanted)} requested: {', '.join(hit) or '—'}" if wanted else "No amenity preference"
    if c.parking and c.parking != "none":
        has_parking = bool(l.parking) or "parking" in have or "parking" in text
        if not has_parking:
            s *= 0.6
            reason += "; parking not confirmed"
            missing.append("parking")
        elif c.parking == "car" and "car" not in _norm(l.parking) + text and "covered" not in text:
            reason += "; parking type unclear"
            missing.append("car parking")
    missing += [f"amenity: {a}" for a in wanted if a not in hit]
    add("amenities", s, reason)

    # Deposit & brokerage
    parts, ds = [], []
    if l.deposit is None:
        ds.append(0.5)
        missing.append("deposit")
        parts.append("deposit unknown")
    elif c.max_deposit:
        if l.deposit <= c.max_deposit:
            ds.append(1.0 - 0.3 * l.deposit / c.max_deposit)
            parts.append(f"deposit ₹{l.deposit:,} ≤ max ₹{c.max_deposit:,}")
        else:
            ds.append(0.1)
            parts.append(f"deposit ₹{l.deposit:,} over max ₹{c.max_deposit:,}")
            compromises.append(f"Deposit above max by ₹{l.deposit - c.max_deposit:,}")
    else:
        ds.append(0.7)
    cap = c.brokerage_cap()
    if l.brokerage_free or l.contact_role == "owner" and l.brokerage in (None, 0):
        ds.append(1.0)
        parts.append("no brokerage")
    elif l.brokerage is not None:
        if cap is not None and l.brokerage > cap:
            ds.append(0.1)
            parts.append(f"brokerage ₹{l.brokerage:,} exceeds your limit")
            compromises.append("Brokerage above your preference")
            if c.brokerage == "no_brokerage":
                hard = True
        else:
            ds.append(0.7)
            parts.append(f"brokerage ₹{l.brokerage:,}")
    else:
        ds.append(0.5)
        missing.append("brokerage")
        parts.append("brokerage unknown")
    add("deposit_brokerage", sum(ds) / len(ds), "; ".join(parts))

    # Connectivity
    cw = [w_ for w_ in CONNECTIVITY_WORDS if w_ in text]
    lm = [x for x in c.landmarks if x.lower() in text]
    s = min(1.0, 0.45 + 0.12 * len(cw) + 0.25 * len(lm))
    add("connectivity", s, ("Mentions: " + ", ".join(cw + lm)) if cw or lm else "No connectivity info")

    # Hard constraints
    if c.property_types and l.property_type and l.property_type.upper() not in c.property_types:
        hard = True
        compromises.append(f"Type {l.property_type} (wanted {', '.join(c.property_types)})")
    if c.tenant_type and l.tenant_pref:
        t = c.tenant_type.split("_")[0]
        if t not in l.tenant_pref and not (t == "couple" and "family" in l.tenant_pref):
            hard = True
            compromises.append(f"Owner prefers {', '.join(l.tenant_pref)}")
    if c.min_bathrooms and l.bathrooms is not None and l.bathrooms < c.min_bathrooms:
        compromises.append(f"Only {l.bathrooms} bathroom(s)")

    total = sum(r["points"] for r in rows)
    # Verification confidence modifies the score (a likely scam should never rank high)
    conf = l.confidence if l.confidence is not None else 50
    if conf < 50:
        total *= 0.5 + conf / 100
    if hard:
        total = min(total, 40.0)
    seen = set()
    missing = [m for m in missing if not (m in seen or seen.add(m))]
    return ScoreResult(round(total, 1), rows, compromises, missing, hard, tmc)
