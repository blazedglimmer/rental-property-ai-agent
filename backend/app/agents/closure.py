"""Agent 7 — Deal Closure.

Builds the final-terms sheet and a jurisdiction-aware checklist, reviews agreement text
against negotiated terms, and tracks progress. It never pays, signs or certifies identity:
those checklist items can only be ticked by the user.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .. import textparse as tp
from ..services.llm import LLMUnavailable, get_llm


def checklist(city: str | None, terms: dict) -> list[dict]:
    c = (city or "").lower()
    items = [
        ("identity", "See owner's government photo ID in person; name must match ownership papers", "user"),
        ("ownership", "Verify ownership: latest property tax receipt / electricity bill / sale-deed index-II in owner's name", "user"),
        ("poa", "If someone other than the owner signs: obtain a registered Power of Attorney", "user"),
        ("society_noc", "Society NOC for tenancy (gated societies usually require it)", "user"),
        ("terms_sheet", "Final terms sheet matches what was negotiated (agent-generated)", "agent"),
        ("agreement_draft", "Agreement draft received from owner/broker", "user"),
        ("agreement_review", "Agreement reviewed by RentAgent for discrepancies", "agent"),
        ("inventory", "Inventory list of furniture/appliances attached to agreement, with photos", "user"),
        ("payment_details", "Deposit paid only to the owner's own bank account (name matches ID), never cash/UPI to third party", "user"),
        ("receipt", "Written receipt for deposit and any token amount", "user"),
        ("meter_readings", "Electricity/gas/water meter readings noted on move-in day", "user"),
        ("keys", "Keys and access cards handed over", "user"),
    ]
    if any(x in c for x in ["pune", "mumbai", "thane", "nagpur", "nashik", "navi mumbai"]):
        items[6:6] = [
            ("registration", "Leave & Licence agreement registered (compulsory in Maharashtra, Sec. 55 MRC Act) — "
                             "online e-registration via IGR Maharashtra or at sub-registrar", "user"),
            ("stamp_duty", "Stamp duty & registration fee paid (computed on rent + deposit for the term)", "user"),
            ("police_verification", "Tenant police verification submitted (Maharashtra Police online)", "user"),
        ]
    elif "bangalore" in c or "bengaluru" in c:
        items[6:6] = [("stamp_paper", "Agreement executed on correct-value stamp paper; register if term > 11 months", "user"),
                      ("police_verification", "Tenant verification via Bengaluru City Police (Suraksha)", "user")]
    elif any(x in c for x in ["delhi", "gurgaon", "noida"]):
        items[6:6] = [("stamp_paper", "Agreement on e-stamp paper; register if term > 11 months", "user"),
                      ("police_verification", "Tenant police verification (state police portal)", "user")]
    else:
        items[6:6] = [("stamp_paper", "Agreement on correct stamp paper per state rules; register if required", "user"),
                      ("police_verification", "Tenant police verification as per local police", "user")]
    return [{"key": k, "label": lbl, "owner": who, "done": k == "terms_sheet" and bool(terms), "note": ""}
            for k, lbl, who in items]


class ClauseIssue(BaseModel):
    severity: str = Field(description="high | medium | low")
    clause: str
    issue: str
    suggestion: str


class AgreementReview(BaseModel):
    extracted_terms: dict = Field(default_factory=dict, description="rent, deposit, maintenance, lock_in_months, notice_months, escalation_pct, term_months, start_date, licensor_name")
    discrepancies: list[ClauseIssue] = Field(default_factory=list)
    risky_clauses: list[ClauseIssue] = Field(default_factory=list)
    missing_clauses: list[str] = Field(default_factory=list)
    summary: str = ""


REVIEW_SYSTEM = """You review Indian residential rental / leave & licence agreements for a TENANT.
Compare the agreement with the negotiated terms. Report:
- discrepancies: any number or term that differs from what was negotiated (rent, deposit, maintenance, lock-in,
  notice, escalation, term, start date, parking, furnishing/inventory, who pays which charges).
- risky_clauses: e.g. deposit forfeiture beyond actual damages, lock-in penalty of remaining rent, unilateral
  termination by licensor without notice, painting/cleaning deductions without cap, escalation above agreed,
  owner entry without notice, tenant bearing structural repairs or society transfer charges.
- missing_clauses: deposit refund timeline, inventory list, maintenance responsibility, notice period symmetry.
Quote clause text briefly. You are not a lawyer; keep the summary factual and recommend legal review for high-severity items."""


def review_agreement(text: str, negotiated: dict) -> dict:
    llm = get_llm()
    if llm.available:
        try:
            r = llm.structured(REVIEW_SYSTEM, f"Negotiated terms: {negotiated}\n\n<agreement>\n{text[:150000]}\n</agreement>",
                               AgreementReview, effort="medium")
            return r.model_dump() | {"method": "llm"}
        except LLMUnavailable:
            pass
    return rule_review(text, negotiated)


def rule_review(text: str, negotiated: dict) -> dict:
    low = text.lower()
    ext = {"rent": tp.amount_near(text, ["monthly rent", "licence fee", "license fee", "rent of", "rent"], 60),
           "deposit": tp.amount_near(text, ["security deposit", "deposit"], 60),
           "maintenance": tp.amount_near(text, ["maintenance"], 60),
           "lock_in_months": tp.months_near(text, ["lock-in", "lock in", "lockin"]),
           "notice_months": tp.months_near(text, ["notice"]),
           "term_months": tp.months_near(text, ["period of", "term of", "for a period"])}
    m = re.search(r"(\d{1,2}(?:\.\d)?)\s*%\s*(?:increase|escalation|hike)", low)
    ext["escalation_pct"] = float(m.group(1)) if m else None
    disc = []
    for k, v in negotiated.items():
        if k in ext and ext[k] is not None and v is not None and float(ext[k]) != float(v):
            disc.append({"severity": "high", "clause": k, "issue": f"Agreement says {ext[k]}, negotiated {v}",
                         "suggestion": f"Ask for {k.replace('_', ' ')} to be corrected to {v}"})
    risky = []
    for pat, issue in [(r"forfeit", "Deposit forfeiture clause"),
                       (r"remaining (period|months).{0,40}(rent|licen[cs]e fee)", "Lock-in penalty of remaining rent"),
                       (r"without (any )?notice", "Action 'without notice' — check who it favours"),
                       (r"painting charges|deduct.{0,30}painting", "Painting deduction — ask for a cap"),
                       (r"non[- ]refundable", "Non-refundable amount")]:
        mm = re.search(pat, low)
        if mm:
            risky.append({"severity": "medium", "clause": text[max(0, mm.start() - 60): mm.end() + 60].strip(),
                          "issue": issue, "suggestion": "Clarify or remove"})
    missing = [lbl for key, lbl in [("refund", "Deposit refund timeline"), ("inventory", "Inventory list"),
                                    ("notice", "Notice period")] if key not in low]
    return {"extracted_terms": ext, "discrepancies": disc, "risky_clauses": risky, "missing_clauses": missing,
            "summary": f"{len(disc)} discrepancies, {len(risky)} clauses to review (rule-based check; "
                       f"configure ANTHROPIC_API_KEY for a full review).", "method": "rules"}
