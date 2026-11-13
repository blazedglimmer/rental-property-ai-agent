"""User notifications: in-app always; email / WhatsApp to the user's own address when configured."""
from __future__ import annotations

import logging

from ..config import get_settings
from ..db import after_commit, session_scope
from ..models import Notification
from . import events

log = logging.getLogger(__name__)

KINDS = {
    "high_match": "Highly matching property found",
    "broker_reply": "Owner/broker replied",
    "negotiated": "Rent negotiated",
    "unavailable": "Property no longer available",
    "visit_scheduled": "Visit scheduled",
    "better_property": "A better property is available",
    "approval_needed": "Your approval is needed",
    "scam_warning": "Potential scam flagged",
    "visit_reminder": "Visit reminder",
}


def notify(*args, **kwargs) -> None:
    after_commit(lambda: _notify(*args, **kwargs))


def _notify(kind: str, title: str, body: str = "", *, search_id: int | None = None,
            listing_id: int | None = None, external: bool = True) -> int:
    delivered: dict = {"in_app": True}
    s = get_settings()
    if external and s.user_email and s.smtp_host:
        try:
            from .channels import send_email
            send_email(s.user_email, f"[RentAgent] {title}", body or title)
            delivered["email"] = True
        except Exception as e:
            delivered["email"] = f"failed: {e}"
    if external and s.user_whatsapp and s.whatsapp_token and kind in {"approval_needed", "high_match", "visit_scheduled"}:
        # The user is messaging their own assistant number: they have opted in.
        try:
            from .channels import send_whatsapp_text
            send_whatsapp_text(s.user_whatsapp, f"{title}\n{body}"[:1000])
            delivered["whatsapp"] = True
        except Exception as e:
            delivered["whatsapp"] = f"failed: {e}"
    with session_scope() as db:
        n = Notification(kind=kind, title=title, body=body, search_id=search_id,
                         listing_id=listing_id, delivered=delivered)
        db.add(n)
        db.flush()
        nid = n.id
    events.publish({"type": "notification", "id": nid, "kind": kind, "title": title, "body": body,
                    "search_id": search_id, "listing_id": listing_id})
    return nid
