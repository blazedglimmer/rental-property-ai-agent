"""Agent 5 — Negotiation.

The offer is computed deterministically inside NegotiationBounds. The LLM only phrases it,
and `guard()` rejects any outgoing text containing an amount we did not compute/quote or
commitment language. Acceptance is never sent without user approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import textparse as tp
from ..schemas import NegotiationBounds

CONCESSION = {"firm": 0.25, "moderate": 0.4, "flexible": 0.6}
COMMIT_PATTERNS = re.compile(
    r"\b(we accept|i accept|deal (is )?done|i will pay|we will pay|i'll pay|confirm(ed)? the deal|"
    r"sending (the )?(token|advance|deposit)|i agree to pay|it'?s a deal|book(ed)? it)\b", re.I)


def r500(x: float) -> int:
    return int(round(x / 500.0) * 500)


@dataclass
class Decision:
    action: str  # no_negotiation_needed | counter | recommend_accept | walk_away | escalate
    offer: dict = field(default_factory=dict)      # terms we propose (only within bounds)
    asks: list[str] = field(default_factory=list)  # human-readable term requests
    reasons: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)  # things outside bounds the user must decide


def decide(b: NegotiationBounds, asking: dict, their_last: dict | None, our_last: dict | None,
           rounds: int, they_refuse: bool = False) -> Decision:
    """asking/their_last: dict with rent, deposit, brokerage, maintenance, lock_in_months, notice_months, escalation_pct."""
    cur = {**asking, **{k: v for k, v in (their_last or {}).items() if v is not None}}
    d = Decision(action="counter")
    rent = cur.get("rent")
    rate = CONCESSION[b.flexibility]
    max_rent, target = b.max_rent, b.target_rent or b.max_rent

    # ---- rent ----
    if rent is None:
        d.issues.append("Rent unknown — ask before negotiating")
    elif max_rent is None or rent <= (target or rent):
        d.reasons.append(f"Rent ₹{rent:,} is at/below target")
    else:
        prev = (our_last or {}).get("rent")
        cap = min(rent, max_rent)
        if prev is None:
            offer = max(r500(target - 0.25 * (rent - target)), r500(rent * 0.85))
        elif rounds >= b.max_rounds - 1:
            offer = cap
        else:
            offer = r500(prev + rate * (cap - prev))
        offer = min(max(offer, (prev or 0)), max_rent)  # never go backwards, never above max
        if rent <= max_rent and (rent - offer) <= max(500, 0.02 * rent):
            d.action = "recommend_accept"
            d.offer["rent"] = rent
            d.reasons.append(f"Their ₹{rent:,} is within your max and the gap is small")
        elif rent > max_rent and (they_refuse or rounds >= b.max_rounds):
            d.action = "escalate"
            d.issues.append(f"Owner holds at ₹{rent:,}, above your max ₹{max_rent:,}")
        elif rent <= max_rent and (they_refuse or rounds >= b.max_rounds):
            d.action = "recommend_accept"
            d.offer["rent"] = rent
            d.reasons.append(f"Owner won't move further; ₹{rent:,} is within your max ₹{max_rent:,}")
        else:
            d.offer["rent"] = offer
            d.asks.append(f"rent of ₹{offer:,}/month")
            d.reasons.append(f"Asking ₹{rent:,}; target ₹{target:,}; max ₹{max_rent:,}; round {rounds + 1}/{b.max_rounds}")

    # ---- deposit ----
    dep = cur.get("deposit")
    if dep is not None and b.max_deposit is not None and dep > b.max_deposit:
        d.offer["deposit"] = b.max_deposit
        d.asks.append(f"security deposit of ₹{b.max_deposit:,}")
        if they_refuse or rounds >= b.max_rounds:
            d.issues.append(f"Deposit ₹{dep:,} exceeds your max ₹{b.max_deposit:,}")
    # ---- brokerage ----
    brk = cur.get("brokerage")
    if brk is not None and b.max_brokerage is not None and brk > b.max_brokerage:
        d.offer["brokerage"] = b.max_brokerage
        d.asks.append("no brokerage" if b.max_brokerage == 0 else f"brokerage of ₹{b.max_brokerage:,}")
        if they_refuse or rounds >= b.max_rounds:
            d.issues.append(f"Brokerage ₹{brk:,} exceeds your max ₹{b.max_brokerage:,}")
    # ---- maintenance ----
    mnt = cur.get("maintenance")
    if mnt and b.max_maintenance is not None and mnt > b.max_maintenance:
        d.offer["maintenance"] = b.max_maintenance
        d.asks.append("maintenance included in rent" if b.max_maintenance == 0 else f"maintenance of ₹{b.max_maintenance:,}")
    # ---- lease terms ----
    for key, mx, label in [("lock_in_months", b.max_lock_in_months, "lock-in"),
                           ("notice_months", b.max_notice_months, "notice period")]:
        v = cur.get(key)
        if v is not None and v > mx:
            d.offer[key] = mx
            d.asks.append(f"{label} of {mx} month{'s' if mx != 1 else ''}")
    esc = cur.get("escalation_pct")
    if esc is not None and esc > b.max_escalation_pct:
        d.offer["escalation_pct"] = b.max_escalation_pct
        d.asks.append(f"annual escalation of {b.max_escalation_pct:g}%")

    if d.action == "counter" and not d.asks:
        d.action = "no_negotiation_needed" if not d.issues else "escalate"
    if d.action == "recommend_accept" and d.issues:
        d.action = "escalate"
    if d.action == "escalate" and not d.issues:
        d.issues.append("Negotiation stalled")
    return d


def counter_text(d: Decision) -> str:
    """Verbatim commercial content handed to the writer (numbers come only from here)."""
    if d.action == "recommend_accept":
        terms = ", ".join(f"{k.replace('_', ' ')} {('₹' + format(v, ',')) if isinstance(v, int) and v >= 500 else v}"
                          for k, v in d.offer.items())
        return (f"In principle, the tenant is happy to proceed at {terms}, subject to visiting the flat "
                f"and reviewing the agreement. Nothing is final until the tenant confirms in writing.")
    asks = "; ".join(d.asks)
    return (f"Would you consider {asks}? The tenant is serious and can move quickly once terms are agreed. "
            f"This is a request, not a commitment.")


def allowed_amounts(offer: dict, asking: dict, extra: list[int] | None = None) -> set[int]:
    vals = {v for v in list(offer.values()) + list(asking.values()) if isinstance(v, (int, float)) and v >= 500}
    return {int(v) for v in vals} | set(extra or [])


def guard(body: str, d: Decision, b: NegotiationBounds, asking: dict) -> list[str]:
    """Return a list of violations; empty means safe to send (after user approval)."""
    problems = []
    allowed = allowed_amounts(d.offer, asking)
    for amt in tp.amounts(body):
        if amt not in allowed:
            problems.append(f"Unexpected amount ₹{amt:,} in message")
    for k, cap in [("rent", b.max_rent), ("deposit", b.max_deposit), ("brokerage", b.max_brokerage)]:
        v = d.offer.get(k)
        if v is not None and cap is not None and v > cap:
            problems.append(f"Offered {k} ₹{v:,} exceeds max ₹{cap:,}")
    if COMMIT_PATTERNS.search(body):
        problems.append("Message contains commitment language")
    if d.action == "recommend_accept" and not re.search(r"subject to|in principle|not final|nothing is final", body, re.I):
        problems.append("Acceptance must be marked 'in principle / subject to'")
    return problems
