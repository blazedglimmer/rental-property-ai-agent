from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Search(Base):
    __tablename__ = "searches"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    raw_query: Mapped[str | None] = mapped_column(Text)
    criteria: Mapped[dict] = mapped_column(JSON)
    bounds: Mapped[dict] = mapped_column(JSON)
    weights: Mapped[dict] = mapped_column(JSON)
    sources: Mapped[list] = mapped_column(JSON, default=list)  # enabled source configs
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(80))
    locality: Mapped[str | None] = mapped_column(String(120), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    society: Mapped[str | None] = mapped_column(String(200))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    property_type: Mapped[str | None] = mapped_column(String(20))  # 1RK, 1BHK...
    bhk: Mapped[float | None] = mapped_column(Float)
    bathrooms: Mapped[int | None] = mapped_column(Integer)
    rent: Mapped[int | None] = mapped_column(Integer)
    deposit: Mapped[int | None] = mapped_column(Integer)
    maintenance: Mapped[int | None] = mapped_column(Integer)
    brokerage: Mapped[int | None] = mapped_column(Integer)  # in rupees; None = unknown
    brokerage_free: Mapped[bool | None] = mapped_column(Boolean)
    area_sqft: Mapped[int | None] = mapped_column(Integer)
    furnishing: Mapped[str | None] = mapped_column(String(20))
    parking: Mapped[str | None] = mapped_column(String(40))
    floor: Mapped[str | None] = mapped_column(String(40))
    amenities: Mapped[list] = mapped_column(JSON, default=list)
    tenant_pref: Mapped[list] = mapped_column(JSON, default=list)  # family/bachelor/couple
    available_from: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    photo_hashes: Mapped[list] = mapped_column(JSON, default=list)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_phone: Mapped[str | None] = mapped_column(String(40), index=True)
    contact_email: Mapped[str | None] = mapped_column(String(200))
    contact_role: Mapped[str | None] = mapped_column(String(20))  # owner|broker|unknown
    source_url: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(40))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    availability: Mapped[str] = mapped_column(String(20), default="unverified")
    availability_evidence: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=50.0)
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="new")
    # new|contacting|awaiting_reply|negotiating|visit_scheduled|visited|shortlisted|closing|closed|rejected|unavailable


class ListingSource(Base):
    __tablename__ = "listing_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    url: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(120))
    raw: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("search_id", "listing_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column(Float)
    breakdown: Mapped[list] = mapped_column(JSON)
    compromises: Mapped[list] = mapped_column(JSON, default=list)
    missing: Mapped[list] = mapped_column(JSON, default=list)
    total_monthly_cost: Mapped[int | None] = mapped_column(Integer)
    hard_fail: Mapped[bool] = mapped_column(Boolean, default=False)
    shortlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)  # normalised phone or email
    name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str | None] = mapped_column(String(20))
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"), index=True)
    search_id: Mapped[int | None] = mapped_column(ForeignKey("searches.id", ondelete="SET NULL"))
    direction: Mapped[str] = mapped_column(String(4))  # out|in
    channel: Mapped[str] = mapped_column(String(20))  # email|whatsapp|assisted|manual
    purpose: Mapped[str | None] = mapped_column(String(30))  # first_contact|followup|offer|visit|accept
    to_addr: Mapped[str | None] = mapped_column(String(200))
    from_addr: Mapped[str | None] = mapped_column(String(200))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))  # draft|pending_approval|sent|failed|received|awaiting_user_send
    send_link: Mapped[str | None] = mapped_column(Text)  # wa.me / mailto for assisted send
    extracted: Mapped[dict | None] = mapped_column(JSON)
    external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Negotiation(Base):
    __tablename__ = "negotiations"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), unique=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(20), default="open")  # open|agreed_in_principle|failed|walked_away
    asking: Mapped[dict] = mapped_column(JSON, default=dict)
    our_last_offer: Mapped[dict | None] = mapped_column(JSON)
    their_last_offer: Mapped[dict | None] = mapped_column(JSON)
    history: Mapped[list] = mapped_column(JSON, default=list)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Visit(Base):
    __tablename__ = "visits"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="proposed")  # proposed|confirmed|done|cancelled
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    feedback: Mapped[dict | None] = mapped_column(JSON)


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), unique=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(30), default="terms")
    # terms|documents|agreement_review|registration|move_in|closed|abandoned
    final_terms: Mapped[dict] = mapped_column(JSON, default=dict)
    checklist: Mapped[list] = mapped_column(JSON, default=list)
    agreement_review: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    thread_id: Mapped[str] = mapped_column(String(80), index=True)
    search_id: Mapped[int | None] = mapped_column(Integer)
    listing_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|edited|superseded
    decision: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    agent: Mapped[str] = mapped_column(String(30))
    level: Mapped[str] = mapped_column(String(10), default="info")
    status: Mapped[str] = mapped_column(String(15), default="done")  # running|done|error|waiting
    search_id: Mapped[int | None] = mapped_column(Integer)
    listing_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    search_id: Mapped[int | None] = mapped_column(Integer)
    listing_id: Mapped[int | None] = mapped_column(Integer)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered: Mapped[dict] = mapped_column(JSON, default=dict)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | None] = mapped_column(JSON)
