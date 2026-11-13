"""Agent 3 — Property Verification.

Produces a legitimacy confidence (0–100) plus explicit flags. Availability is a separate
state machine that only changes on evidence (a reply from the owner/broker, or a listing
marked removed by its source). Nothing is ever marked verified without evidence.
"""
from __future__ import annotations

import io
import logging
import re
import statistics
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Listing, utcnow

log = logging.getLogger(__name__)

SCAM_PATTERNS = [
    (r"\b(army|defen[cs]e|military|crpf|cisf)\s+(officer|personnel|man)\b", "Claims to be armed-forces personnel (common rental-scam script)"),
    (r"\b(pay|transfer|send)\b.{0,40}\b(before|prior to)\b.{0,20}\b(visit|seeing|viewing)\b", "Asks for payment before a visit"),
    (r"\btoken (amount|advance)\b.{0,40}\b(upi|gpay|google pay|phonepe|paytm)\b", "Asks for token amount via UPI"),
    (r"\b(out of (station|town|city)|abroad|transferred)\b.{0,60}\b(key|courier|keys)\b", "Owner 'out of station', offers to courier keys"),
    (r"\bgate ?pass\b|\bvisit(ing)? charges?\b|\bregistration fee\b", "Asks for gate-pass / visiting / registration fee"),
    (r"\bqr code\b.{0,30}\b(scan|receive)\b", "Asks you to scan a QR code to 'receive' money"),
    (r"\bno visit(s)? (needed|required|possible)\b", "Discourages visiting the property"),
]


def _shingles(text: str, k: int = 5) -> set[str]:
    words = re.findall(r"\w+", (text or "").lower())
    return {" ".join(words[i:i + k]) for i in range(max(len(words) - k + 1, 0))}


def jaccard(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def compute_photo_hashes(urls: list[str], limit: int = 4) -> list[str]:
    """Perceptual hashes of the first few photos (best-effort, short timeouts)."""
    try:
        import imagehash
        from PIL import Image
    except ImportError:  # pragma: no cover
        return []
    out = []
    for u in urls[:limit]:
        try:
            r = httpx.get(u, timeout=8, follow_redirects=True)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                out.append(str(imagehash.phash(Image.open(io.BytesIO(r.content)))))
        except Exception:
            continue
    return out


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def assess(db: Session, l: Listing) -> tuple[float, list[dict]]:
    flags: list[dict] = []

    def flag(code: str, msg: str, severity: str, penalty: float):
        flags.append({"code": code, "message": msg, "severity": severity, "penalty": penalty})

    text = " ".join(filter(None, [l.title, l.description]))
    for pat, msg in SCAM_PATTERNS:
        if re.search(pat, text, re.I):
            flag("scam_language", msg, "high", 35)

    # Price anomaly vs comparables (same locality & BHK)
    if l.rent and l.locality and l.bhk is not None:
        comps = db.scalars(select(Listing.rent).where(
            Listing.locality == l.locality, Listing.bhk == l.bhk, Listing.id != l.id,
            Listing.rent.is_not(None), Listing.is_sample == l.is_sample)).all()
        if len(comps) >= 4:
            med = statistics.median(comps)
            if l.rent < 0.6 * med:
                flag("price_low", f"Rent ₹{l.rent:,} is {1 - l.rent / med:.0%} below the local median ₹{int(med):,} "
                     f"({len(comps)} comparables)", "high", 25)
            elif l.rent < 0.75 * med:
                flag("price_lowish", f"Rent noticeably below local median ₹{int(med):,}", "medium", 8)

    # Duplicate photos / descriptions under a different contact
    others = db.scalars(select(Listing).where(Listing.id != l.id, Listing.is_sample == l.is_sample)).all()
    for o in others:
        diff_contact = (o.contact_phone and l.contact_phone and o.contact_phone != l.contact_phone)
        if l.photo_hashes and o.photo_hashes:
            if any(_hamming(a, b) <= 6 for a in l.photo_hashes for b in o.photo_hashes):
                if diff_contact:
                    flag("dup_photos", f"Photos match listing #{o.id} posted by a different contact", "high", 30)
                    break
        if l.description and o.description and len(l.description) > 80 and jaccard(l.description, o.description) > 0.7:
            if diff_contact:
                flag("dup_text", f"Description copied from listing #{o.id} (different contact)", "medium", 15)
                break

    # Staleness
    now = utcnow()
    if l.posted_at and now - l.posted_at > timedelta(days=45):
        flag("stale", f"Posted {(now - l.posted_at).days} days ago — may be outdated", "medium", 10)
    if now - l.last_seen_at > timedelta(days=14):
        flag("not_seen", "Not seen in any source for 14+ days", "medium", 10)

    # Contactability
    if not (l.contact_phone or l.contact_email):
        flag("no_contact", "No contact details — availability cannot be verified directly", "low", 5)

    # Evidence from conversations
    ev = l.availability_evidence or {}
    if ev.get("asks_for_advance_payment"):
        flag("advance_payment", "Owner/broker asked for money before a visit/agreement — do NOT pay", "high", 40)
    if l.contact_role == "broker" and l.brokerage_free:
        flag("broker_no_brokerage", "Listed as brokerage-free but contact is a broker — confirm terms", "low", 5)

    base = 60.0
    if l.availability == "confirmed":
        base += 20
    if l.contact_role == "owner":
        base += 5
    if len(l.photos or []) >= 3:
        base += 5
    if l.is_sample:
        flag("sample", "Sample data for demonstration — not a real listing", "info", 0)
    conf = max(0.0, min(100.0, base - sum(f["penalty"] for f in flags)))
    return round(conf, 1), flags


def apply_reply_evidence(l: Listing, facts: dict, message_id: int) -> str | None:
    """Update availability from a real reply. Returns new availability if it changed."""
    ev = dict(l.availability_evidence or {})
    changed = None
    if facts.get("asks_for_advance_payment"):
        ev["asks_for_advance_payment"] = True
    if facts.get("availability") == "available" and l.availability != "confirmed":
        l.availability = changed = "confirmed"
        ev.update(confirmed_by_message=message_id, confirmed_at=datetime.utcnow().isoformat())
    elif facts.get("availability") == "unavailable":
        l.availability = changed = "unavailable"
        ev.update(unavailable_by_message=message_id, at=datetime.utcnow().isoformat())
    if facts.get("contact_role") in ("owner", "broker"):
        l.contact_role = facts["contact_role"]
    l.availability_evidence = ev
    return changed
