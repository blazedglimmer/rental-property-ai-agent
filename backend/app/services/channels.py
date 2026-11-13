"""Outbound/inbound transport. Only official, authorised channels.

* email     — SMTP send / IMAP receive on a mailbox the user controls.
* whatsapp  — WhatsApp Cloud API. Free-form text only inside the 24h window opened by the
              other party; first contact needs an approved template + opt-in, so cold
              WhatsApp outreach defaults to `assisted`.
* assisted  — we draft; the user sends from their own phone via a wa.me / sms: / mailto: link
              and pastes the reply back. No platform rules are bypassed.
"""
from __future__ import annotations

import email as email_lib
import imaplib
import logging
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from urllib.parse import quote

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class ChannelError(RuntimeError):
    pass


@dataclass
class SendResult:
    status: str  # sent | awaiting_user_send
    external_id: str | None = None
    send_link: str | None = None


def send_email(to: str, subject: str, body: str, reply_tag: str | None = None) -> SendResult:
    s = get_settings()
    if not (s.smtp_host and s.smtp_user and s.smtp_password):
        raise ChannelError("SMTP not configured")
    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg_id = make_msgid(idstring=reply_tag or uuid.uuid4().hex[:8])
    msg["Message-ID"] = msg_id
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
    return SendResult("sent", external_id=msg_id)


@dataclass
class InboundEmail:
    message_id: str
    in_reply_to: str | None
    references: str
    from_addr: str
    subject: str
    body: str
    html: str | None
    date: datetime | None


def fetch_emails(folder: str, since_days: int = 3, unseen_only: bool = True) -> list[InboundEmail]:
    s = get_settings()
    if not (s.imap_host and s.imap_user and s.imap_password):
        raise ChannelError("IMAP not configured")
    out: list[InboundEmail] = []
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(s.imap_host) as M:
        M.login(s.imap_user, s.imap_password)
        M.select(folder)
        crit = f'(SINCE "{since}"{" UNSEEN" if unseen_only else ""})'
        typ, data = M.search(None, crit)
        for num in (data[0].split() if data and data[0] else []):
            typ, msg_data = M.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            m = email_lib.message_from_bytes(msg_data[0][1])
            text, html = "", None
            for part in m.walk():
                ct = part.get_content_type()
                if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
                    continue
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                if ct == "text/plain":
                    text += payload.decode(charset, "replace")
                elif ct == "text/html":
                    html = (html or "") + payload.decode(charset, "replace")
            try:
                date = email_lib.utils.parsedate_to_datetime(m.get("Date")) if m.get("Date") else None
            except Exception:
                date = None
            out.append(InboundEmail(
                message_id=m.get("Message-ID", ""), in_reply_to=m.get("In-Reply-To"),
                references=m.get("References", ""), from_addr=parseaddr(m.get("From", ""))[1].lower(),
                subject=str(email_lib.header.make_header(email_lib.header.decode_header(m.get("Subject", "")))),
                body=text, html=html, date=date))
    return out


def _wa_url() -> str:
    s = get_settings()
    return f"https://graph.facebook.com/v21.0/{s.whatsapp_phone_number_id}/messages"


def _wa_post(payload: dict) -> str:
    s = get_settings()
    if not (s.whatsapp_token and s.whatsapp_phone_number_id):
        raise ChannelError("WhatsApp Cloud API not configured")
    r = httpx.post(_wa_url(), json=payload, timeout=30,
                   headers={"Authorization": f"Bearer {s.whatsapp_token}"})
    if r.status_code >= 400:
        raise ChannelError(f"WhatsApp API {r.status_code}: {r.text[:300]}")
    return r.json().get("messages", [{}])[0].get("id", "")


def send_whatsapp_text(to: str, body: str) -> SendResult:
    """Free-form text — only valid inside the 24h customer-service window."""
    mid = _wa_post({"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
                    "text": {"body": body, "preview_url": False}})
    return SendResult("sent", external_id=mid)


def send_whatsapp_template(to: str, template: str, params: list[str], lang: str = "en") -> SendResult:
    mid = _wa_post({"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "template",
                    "template": {"name": template, "language": {"code": lang},
                                 "components": [{"type": "body", "parameters": [
                                     {"type": "text", "text": p} for p in params]}]}})
    return SendResult("sent", external_id=mid)


def assisted_link(channel: str, to: str | None, body: str, subject: str | None = None) -> str | None:
    if not to:
        return None
    if channel == "email":
        return f"mailto:{to}?subject={quote(subject or '')}&body={quote(body)}"
    digits = "".join(ch for ch in to if ch.isdigit())
    if channel == "sms":
        return f"sms:+{digits}?body={quote(body)}"
    return f"https://wa.me/{digits}?text={quote(body)}"


def channels_status() -> dict:
    s = get_settings()
    return {
        "email_send": bool(s.smtp_host and s.smtp_user and s.smtp_password),
        "email_receive": bool(s.imap_host and s.imap_user and s.imap_password),
        "whatsapp_cloud": bool(s.whatsapp_token and s.whatsapp_phone_number_id),
        "whatsapp_first_contact_template": bool(s.whatsapp_first_contact_template),
        "assisted": True,
    }
