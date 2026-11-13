"""Agent 1 — Property Discovery: run sources, normalise, dedupe, persist with provenance."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

from dateutil import parser as dtparse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Listing, ListingSource, utcnow
from ...schemas import ListingData
from ..verification import compute_photo_hashes, jaccard
from .sources.base import RawListing, Source
from .sources.email_alerts import EmailAlertSource
from .sources.partner_feed import PartnerFeedSource
from .sources.web_search import WebSearchSource

log = logging.getLogger(__name__)

REGISTRY: dict[str, type[Source]] = {
    "web_search": WebSearchSource,
    "email_alerts": EmailAlertSource,
    "partner_feed": PartnerFeedSource,
}

DEFAULT_SOURCES = [{"type": "web_search"}, {"type": "email_alerts"}]


def build_sources(configs: list[dict] | None) -> list[Source]:
    out = []
    for cfg in configs or DEFAULT_SOURCES:
        cls = REGISTRY.get(cfg.get("type", ""))
        if cls:
            out.append(cls(cfg))
    return out


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def normalise(d: ListingData) -> ListingData:
    if d.property_type:
        d.property_type = d.property_type.upper().replace(" ", "").replace("-", "")
        if d.bhk is None:
            m = re.match(r"(\d(?:\.5)?)BHK", d.property_type)
            d.bhk = float(m.group(1)) if m else (0.5 if d.property_type == "1RK" else None)
    elif d.bhk:
        d.property_type = "1RK" if d.bhk < 1 else f"{int(d.bhk) if d.bhk == int(d.bhk) else d.bhk}BHK"
    if d.locality:
        d.locality = d.locality.strip().title()
    if d.city:
        d.city = d.city.strip().title()
    if d.contact_phone:
        digits = re.sub(r"\D", "", d.contact_phone)
        d.contact_phone = "+91" + digits[-10:] if len(digits) >= 10 else None
    if d.rent is not None and not (1000 <= d.rent <= 2_000_000):
        d.rent = None  # implausible monthly rent → treat as unknown
    if d.brokerage == 0:
        d.brokerage_free = True
    return d


def fingerprint(d: ListingData) -> str:
    rent_bucket = (d.rent // 500) if d.rent else "x"
    ident = d.contact_phone or _norm(d.society) or (str(d.area_sqft) if d.area_sqft else "") or _norm(d.title)[:60] \
        or (d.source_url or "")
    key = "|".join(map(str, [_norm(d.city), _norm(d.locality), d.bhk, rent_bucket, ident]))
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _find_duplicate(db: Session, d: ListingData) -> Listing | None:
    """Fuzzy match: same locality & BHK, rent within 5%, and same phone or similar text/URL."""
    if d.source_url:
        hit = db.scalar(select(Listing).where(Listing.source_url == d.source_url))
        if hit:
            return hit
    if not (d.locality and d.bhk is not None and d.rent):
        return None
    cands = db.scalars(select(Listing).where(Listing.locality == d.locality, Listing.bhk == d.bhk,
                                             Listing.rent.between(int(d.rent * 0.95), int(d.rent * 1.05)))).all()
    for c in cands:
        if d.contact_phone and c.contact_phone == d.contact_phone:
            return c
        if d.society and c.society and _norm(d.society) == _norm(c.society) and \
                (d.area_sqft is None or c.area_sqft is None or abs(d.area_sqft - c.area_sqft) <= 30):
            return c
        # Same text under a *different* phone is a re-post by someone else (possible scam) —
        # keep it separate so verification can flag it, rather than silently merging.
        phones_conflict = d.contact_phone and c.contact_phone and d.contact_phone != c.contact_phone
        if not phones_conflict and d.description and c.description and jaccard(d.description, c.description) > 0.6:
            return c
    return None


def upsert(db: Session, r: RawListing, hash_photos: bool = True) -> tuple[Listing, bool]:
    d = normalise(r.data)
    fp = fingerprint(d)
    l = db.scalar(select(Listing).where(Listing.fingerprint == fp)) or _find_duplicate(db, d)
    created = l is None
    now = utcnow()
    if created:
        l = Listing(fingerprint=fp, first_seen_at=now, is_sample=r.is_sample, source=r.source)
        db.add(l)
    # Merge: fill unknowns; newer explicit values for prices win (listings get revised)
    for k, v in d.model_dump().items():
        if k == "posted_at" or v in (None, [], ""):
            continue
        cur = getattr(l, k, None)
        if cur in (None, [], "") or k in ("rent", "deposit", "maintenance"):
            setattr(l, k, v)
        elif k in ("amenities", "photos", "tenant_pref"):
            setattr(l, k, sorted(set(cur) | set(v)) if k != "photos" else list(dict.fromkeys(cur + v)))
    if d.posted_at:
        try:
            l.posted_at = dtparse.parse(d.posted_at, dayfirst=True).replace(tzinfo=None)
        except (ValueError, OverflowError):
            pass
    l.last_seen_at = now
    if l.photos and not l.photo_hashes and hash_photos:
        l.photo_hashes = compute_photo_hashes(l.photos)
    db.flush()
    if not db.scalar(select(ListingSource).where(ListingSource.listing_id == l.id,
                                                 ListingSource.source == r.source,
                                                 ListingSource.external_id == r.external_id)):
        db.add(ListingSource(listing_id=l.id, source=r.source, url=r.url, external_id=r.external_id,
                             raw=r.raw, fetched_at=now))
    return l, created


def run_source(src: Source, criteria, locality: str | None) -> list[RawListing]:
    ok, why = src.available()
    if not ok:
        log.info("source %s skipped: %s", src.name, why)
        return []
    return src.search(criteria, locality)


def to_jsonable(r: RawListing) -> dict:
    return {"data": r.data.model_dump(), "source": r.source, "url": r.url, "external_id": r.external_id,
            "raw": r.raw, "is_sample": r.is_sample}


def from_jsonable(d: dict) -> RawListing:
    return RawListing(ListingData(**d["data"]), d["source"], d.get("url"), d.get("external_id"),
                      d.get("raw") or {}, d.get("is_sample", False))


_ = datetime  # keep import for type hints in callers
