"""Listings from property-portal alert emails in the user's own mailbox (IMAP).

The user creates saved-search alerts on the portals they use (99acres, MagicBricks,
Housing.com, NoBroker…) pointed at the mailbox RentAgent reads. Reading one's own mail is
permitted; we never log in to the portals.
"""
from __future__ import annotations

import hashlib
import re

from ....config import get_settings
from ....schemas import SearchCriteria
from ....services.channels import ChannelError, fetch_emails
from ..extraction import extract, html_to_text
from .base import RawListing, Source, SourceError

ALERT_SENDERS = ["99acres", "magicbricks", "housing.com", "nobroker", "squareyards", "makaan", "commonfloor",
                 "olx", "quikr", "proptiger"]


class EmailAlertSource(Source):
    name = "email_alerts"
    per_locality = False
    compliance = "Reads portal alert emails from the user's own mailbox."

    def available(self):
        s = get_settings()
        return (bool(s.imap_host and s.imap_user and s.imap_password), "IMAP not configured")

    def search(self, c: SearchCriteria, locality: str | None) -> list[RawListing]:
        try:
            mails = fetch_emails(get_settings().imap_alert_folder, since_days=self.config.get("since_days", 3),
                                 unseen_only=False)
        except ChannelError as e:
            raise SourceError(str(e)) from e
        out: list[RawListing] = []
        for m in mails:
            if not any(s in m.from_addr for s in ALERT_SENDERS):
                continue
            text = m.body
            links: list[str] = []
            if m.html:
                t, _, _ = html_to_text(m.html)
                text = text or t
                links = re.findall(r'href="(https?://[^"]+)"', m.html)
            for ld in extract(text, None, c.city):
                if not ld.source_url:
                    # pick the first link that looks like a property detail page
                    ld.source_url = next((u for u in links if re.search(r"(property|listing|rent|flat|detail)", u, re.I)), None)
                ext = hashlib.sha1(f"{m.message_id}|{ld.title}|{ld.rent}".encode()).hexdigest()[:16]
                out.append(RawListing(ld, self.name, ld.source_url, ext,
                                      raw={"email_from": m.from_addr, "subject": m.subject}))
        return out
