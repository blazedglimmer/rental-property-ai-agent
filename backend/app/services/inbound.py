"""Inbound messages + periodic housekeeping (reply polling, no-reply timeouts, visit reminders)."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import func, select

from ..agents import communication as comm
from ..config import get_settings
from ..db import session_scope
from ..models import Listing, Message, Search, Visit, utcnow
from .events import emit
from .notifications import notify

log = logging.getLogger(__name__)
REPLY_TIMEOUT_HOURS = 72


def handle_inbound(listing_id: int, channel: str, from_addr: str | None, body: str,
                   external_id: str | None = None, subject: str | None = None, sync: bool = False) -> int:
    from ..graph import deal_graph
    with session_scope() as db:
        if external_id and db.scalar(select(Message.id).where(Message.external_id == external_id,
                                                              Message.direction == "in")):
            return 0  # duplicate delivery
        m = comm.record_inbound(db, listing_id, channel, from_addr, body, external_id, subject)
        mid = m.id
    emit("communication", f"Inbound {channel} message received", listing_id=listing_id, data={"message_id": mid})
    if sync:
        deal_graph.resume(listing_id, {"message_id": mid}, expect_kind="await_reply")
    else:
        deal_graph.resume_async(listing_id, {"message_id": mid}, expect_kind="await_reply")
    return mid


def poll_email_replies() -> int:
    s = get_settings()
    if not (s.imap_host and s.imap_user):
        return 0
    from .channels import fetch_emails
    n = 0
    for e in fetch_emails(s.imap_reply_folder, since_days=7, unseen_only=True):
        with session_scope() as db:
            lid = comm.match_inbound_to_listing(db, e.from_addr, e.in_reply_to)
        if not lid:
            continue
        body = re.split(r"\n\s*On .{5,80}wrote:\s*\n", e.body)[0].strip()  # drop quoted history
        if handle_inbound(lid, "email", e.from_addr, body, e.message_id, e.subject):
            n += 1
    return n


def parse_whatsapp_webhook(payload: dict) -> list[tuple[str, str, str]]:
    """→ [(from_phone, text, wamid)]"""
    out = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            for msg in ch.get("value", {}).get("messages", []) or []:
                text = (msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("text") or ""
                if msg.get("type") in ("image", "video", "document"):
                    text = text or f"[{msg['type']} received]"
                out.append(("+" + msg.get("from", "").lstrip("+"), text, msg.get("id", "")))
    return out


def handle_whatsapp(payload: dict) -> int:
    n = 0
    for phone, text, wamid in parse_whatsapp_webhook(payload):
        with session_scope() as db:
            lid = comm.match_inbound_to_listing(db, phone)
        if lid and text:
            handle_inbound(lid, "whatsapp", phone, text, wamid)
            n += 1
    return n


def check_reply_timeouts() -> int:
    from ..graph import deal_graph
    cutoff = utcnow() - timedelta(hours=REPLY_TIMEOUT_HOURS)
    n = 0
    with session_scope() as db:
        rows = db.execute(select(Listing.id, func.max(Message.created_at))
                          .join(Message, Message.listing_id == Listing.id)
                          .where(Listing.status.in_(["awaiting_reply", "negotiating"]), Message.direction == "out",
                                 Message.status.in_(["sent", "awaiting_user_send"]))
                          .group_by(Listing.id)).all()
        last_in = dict(db.execute(select(Message.listing_id, func.max(Message.created_at))
                                  .where(Message.direction == "in").group_by(Message.listing_id)).all())
    for lid, last_out in rows:
        if last_out < cutoff and (last_in.get(lid) is None or last_in[lid] < last_out):
            if deal_graph.resume(lid, {"timeout": True}, expect_kind="await_reply"):
                n += 1
    return n


def send_visit_reminders() -> int:
    n = 0
    from ..agents.scheduling import local_now
    now = local_now()  # visits are stored in local (IST) time
    with session_scope() as db:
        for v in db.scalars(select(Visit).where(Visit.status == "confirmed", Visit.reminder_sent.is_(False),
                                                Visit.start_at <= now + timedelta(hours=24), Visit.start_at >= now)):
            l = db.get(Listing, v.listing_id)
            notify("visit_reminder", f"Visit {v.start_at.strftime('%a %d %b %I:%M %p')} — {l.society or l.locality}",
                   f"{l.address or ''} · {l.contact_name or ''} {l.contact_phone or ''}",
                   search_id=v.search_id, listing_id=l.id)
            v.reminder_sent = True
            n += 1
    return n


def run_due_searches() -> int:
    from ..graph import search_graph
    s = get_settings()
    due = []
    with session_scope() as db:
        for srch in db.scalars(select(Search).where(Search.status == "active")):
            if not srch.last_run_at or utcnow() - srch.last_run_at > timedelta(minutes=s.discovery_interval_minutes):
                due.append(srch.id)
    for sid in due:
        search_graph.run(sid)
    return len(due)


def housekeeping() -> dict:
    out = {}
    for name, fn in [("replies", poll_email_replies), ("timeouts", check_reply_timeouts),
                     ("reminders", send_visit_reminders), ("searches", run_due_searches)]:
        try:
            out[name] = fn()
        except Exception as e:
            log.exception("housekeeping %s failed", name)
            out[name] = f"error: {e}"
    return out
