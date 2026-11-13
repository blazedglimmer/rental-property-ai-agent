"""Agent 4 — Owner & Broker Communication.

* Personalised messages (Claude) with a code-enforced AI disclosure.
* Anti-spam: one first contact per contact+listing, cooldown, daily cap, do-not-contact list.
* Fact extraction from replies → ReplyFacts.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import textparse as tp
from ..config import get_settings
from ..models import Contact, Listing, Message, Search, utcnow
from ..schemas import ReplyFacts, SearchCriteria
from ..services import channels
from ..services.llm import LLMUnavailable, get_llm

log = logging.getLogger(__name__)

STOP_WORDS = re.compile(r"\b(stop|unsubscribe|don'?t (message|contact)|not interested|remove my number)\b", re.I)


def disclosure() -> str:
    return (f"(I'm an AI assistant writing on behalf of {get_settings().user_name}, who is looking to rent. "
            f"{get_settings().user_name} reviews every message and makes all decisions.)")


def ensure_disclosure(body: str) -> str:
    if "AI assistant" in body:
        return body
    return f"{disclosure()}\n\n{body}"


# ---------------- composing ----------------

COMPOSE_SYSTEM = """You write short, polite WhatsApp/email messages to property owners or brokers in India
on behalf of a tenant. Tone: warm, specific, concise (≤110 words), plain text, no emojis spam.
Hard rules:
- Begin with the exact disclosure sentence provided. Do not remove or reword it.
- Refer to specifics of THIS listing (locality, BHK, society, rent) so it is clearly not a template.
- Never commit to anything, never promise payment, never share the tenant's phone/ID.
- Only include amounts that are given to you in the brief. Do not invent numbers.
- Ask only the questions listed in the brief, grouped naturally."""


def _listing_brief(l: Listing) -> str:
    bits = [l.property_type, l.society, l.locality, l.city]
    s = " in ".join(filter(None, [l.property_type, ", ".join(filter(None, bits[1:]))]))
    if l.rent:
        s += f", listed at ₹{l.rent:,}/month"
    return s or (l.title or "your property")


def _tenant_line(c: SearchCriteria) -> str:
    parts = []
    if c.tenant_type:
        parts.append(c.tenant_type.replace("_", " "))
    if c.move_in_by:
        parts.append(f"looking to move in by {c.move_in_by.strftime('%d %b')}")
    if c.lease_months:
        parts.append(f"{c.lease_months}-month lease")
    return ", ".join(parts)


QUESTIONS = {
    "availability": "Is it still available?",
    "rent": "What is the final monthly rent?",
    "deposit": "How much is the security deposit?",
    "maintenance": "Is maintenance included, or how much is it per month?",
    "brokerage": "Is there any brokerage?",
    "furnishing": "What furnishing is included?",
    "parking": "Is parking available (car/two-wheeler)?",
    "car parking": "Is covered car parking available?",
    "restrictions": "Any restrictions on tenant type, pets or food?",
    "lease": "What lock-in period and notice period do you expect?",
    "photos": "Could you share a few recent photos or a short video?",
    "property condition / age": "How old is the building and is the flat freshly painted?",
}


def questions_for(missing: list[str], first: bool) -> list[str]:
    keys = (["availability"] if first else []) + [m for m in missing if m in QUESTIONS]
    if first:
        keys += [k for k in ("deposit", "maintenance", "brokerage", "restrictions", "lease", "photos") if k not in keys]
    for m in missing:
        if m.startswith("amenity: "):
            keys.append(m)
    seen, out = set(), []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append(QUESTIONS.get(k) or f"Does the property have {k.split(': ', 1)[1]}?")
    return out[:7]


def compose(l: Listing, search: Search, purpose: str, questions: list[str], extra: str = "",
            contact_name: str | None = None) -> tuple[str, str]:
    c = SearchCriteria(**search.criteria)
    subject = f"Enquiry: {_listing_brief(l)}"
    brief = (f"Disclosure sentence: {disclosure()}\nPurpose: {purpose}\nListing: {_listing_brief(l)}\n"
             f"Contact name: {contact_name or l.contact_name or 'unknown'} ({l.contact_role or 'owner/broker'})\n"
             f"Tenant: {_tenant_line(c)}\nQuestions to ask:\n- " + "\n- ".join(questions) +
             (f"\nAdditional content to convey (verbatim numbers): {extra}" if extra else ""))
    llm = get_llm()
    body = None
    if llm.available:
        try:
            body = llm.text(COMPOSE_SYSTEM, brief, effort="low", max_tokens=2000)
        except LLMUnavailable:
            body = None
    if not body:
        greet = f"Hi {contact_name or l.contact_name}," if (contact_name or l.contact_name) else "Hi,"
        intro = {"first_contact": f"I came across your listing for the {_listing_brief(l)}. "
                                  f"The tenant ({_tenant_line(c) or 'details on request'}) is interested.",
                 "followup": f"Thanks for the details on the {_listing_brief(l)}. A few more questions:",
                 }.get(purpose, f"Regarding the {_listing_brief(l)}:")
        q = "\n".join(f"• {x}" for x in questions)
        body = f"{greet}\n{disclosure()}\n\n{intro}\n{extra + chr(10) if extra else ''}{q}\n\nThank you!"
    return subject, ensure_disclosure(body.strip())


# ---------------- reply analysis ----------------

ANALYZE_SYSTEM = """Extract facts from an owner's/broker's reply about a rental property (India).
Only record what the reply states; unknown → null/false. Amounts in INR integers.
"2 months deposit" with known rent R → deposit = 2R. is_counter_offer = they propose different terms than we offered.
accepts_our_offer = they clearly agree to our last proposed terms. asks_for_advance_payment = they ask for any
money (token, visit fee, deposit) before a visit or before an agreement is drafted."""


def analyze_reply(text: str, l: Listing, our_last_offer: dict | None = None) -> ReplyFacts:
    llm = get_llm()
    if llm.available:
        try:
            return llm.structured(ANALYZE_SYSTEM,
                                  f"Listing: {_listing_brief(l)}\nOur last offer: {our_last_offer or 'none'}\n\n"
                                  f"<reply>\n{text}\n</reply>", ReplyFacts)
        except LLMUnavailable:
            pass
    return rule_analyze(text, l, our_last_offer)


def rule_analyze(text: str, l: Listing, our_last_offer: dict | None = None) -> ReplyFacts:
    low = text.lower()
    f = ReplyFacts(summary=text[:200])
    if re.search(r"\b(not available|already (rented|given|let out|booked)|no longer|sorry,? (it'?s|its) (gone|taken)|rented out)\b", low):
        f.availability = "unavailable"
    elif re.search(r"\b(available|yes|still vacant|vacant|you can come|come and see|can visit)\b", low):
        f.availability = "available"
    f.rent = tp.amount_near(text, ["rent", "final", "monthly"])
    f.deposit = tp.amount_near(text, ["deposit", "security"])
    m = re.search(r"(\d{1,2})\s*months?\s*(?:rent\s*)?(?:as\s*)?(?:security\s*)?deposit", low)
    base_rent = f.rent or l.rent
    if m and base_rent:
        f.deposit = int(m.group(1)) * base_rent
    f.maintenance = tp.amount_near(text, ["maintenance"])
    if re.search(r"maintenance (is )?(included|inclusive)", low):
        f.maintenance = 0
    if re.search(r"no brokerage|zero brokerage|without brokerage|owner here|i am the owner|i'm the owner", low):
        f.brokerage_free, f.brokerage = True, 0
    else:
        f.brokerage = tp.amount_near(text, ["brokerage", "commission"])
        m = re.search(r"(one|1|half|0\.5|15 days)\s*(month'?s?)?\s*(rent\s*)?(as\s*)?(brokerage|commission)", low)
        if m and base_rent:
            f.brokerage = base_rent // 2 if m.group(1) in ("half", "0.5", "15 days") else base_rent
    f.lock_in_months = tp.months_near(text, ["lock-in", "lock in", "lockin"])
    f.notice_months = tp.months_near(text, ["notice"])
    f.furnishing = tp.furnishing(text)  # type: ignore[assignment]
    if "parking" in low:
        f.parking = "car" if "car" in low else "available"
    f.tenant_restrictions = [x for x in ["family only", "bachelors not allowed", "no pets", "veg only", "no non-veg"]
                             if x in low]
    if "owner" in low and "broker" not in low:
        f.contact_role = "owner"
    elif re.search(r"\b(broker|agent|brokerage)\b", low):
        f.contact_role = "broker"
    if our_last_offer and our_last_offer.get("rent"):
        if re.search(r"\b(ok|okay|agreed|fine|deal|done|accepted|acceptable)\b", low) and not re.search(r"\b(not|can'?t|cannot|no)\b", low):
            f.accepts_our_offer = True
        elif re.search(r"\b(final|fixed|non[- ]negotiable|can'?t reduce|cannot reduce|no negotiation)\b", low):
            f.rejects_negotiation = True
        if f.rent and f.rent != our_last_offer.get("rent"):
            f.is_counter_offer = True
    f.confirms_visit = bool(re.search(r"\b(confirm(ed)?|see you|come at|ok for|fine for)\b", low)) and \
        bool(re.search(r"\b(visit|tomorrow|today|am|pm|o'?clock|saturday|sunday|monday|tuesday|wednesday|thursday|friday)\b", low))
    f.asks_for_advance_payment = bool(re.search(
        r"\b(pay|transfer|send)\b.{0,40}\b(token|advance|booking|visit(ing)? (fee|charges?)|gate ?pass)\b|"
        r"\b(token|advance|booking amount)\b.{0,40}\b(before|first|upi|gpay|phonepe|paytm)\b", low))
    f.media_urls = re.findall(r"https?://\S+", text)
    return f


# ---------------- channel choice & sending ----------------

def contact_key(l: Listing) -> str | None:
    return l.contact_phone or (l.contact_email.lower() if l.contact_email else None)


def has_open_whatsapp_window(db: Session, phone: str) -> bool:
    since = utcnow() - timedelta(hours=23)
    return bool(db.scalar(select(func.count(Message.id)).where(
        Message.direction == "in", Message.channel == "whatsapp", Message.from_addr == phone,
        Message.created_at >= since)))


def choose_channel(db: Session, l: Listing, first_contact: bool) -> tuple[str, str | None]:
    st = channels.channels_status()
    if l.contact_email and st["email_send"]:
        return "email", l.contact_email
    if l.contact_phone:
        if st["whatsapp_cloud"] and has_open_whatsapp_window(db, l.contact_phone):
            return "whatsapp", l.contact_phone
        return "assisted", l.contact_phone
    if l.contact_email:
        return "assisted_email", l.contact_email
    return "none", None


def outreach_block_reason(db: Session, l: Listing, first_contact: bool) -> str | None:
    key = contact_key(l)
    if not key:
        return "No contact details on the listing"
    c = db.scalar(select(Contact).where(Contact.key == key))
    if c and c.do_not_contact:
        return "Contact asked not to be messaged"
    if not first_contact:
        return None
    s = get_settings()
    already = db.scalar(select(func.count(Message.id)).where(
        Message.listing_id == l.id, Message.direction == "out", Message.purpose == "first_contact",
        Message.status.in_(["sent", "awaiting_user_send"])))
    if already:
        return "Already contacted for this listing"
    if c and c.last_contacted_at and utcnow() - c.last_contacted_at < timedelta(hours=s.contact_cooldown_hours):
        return f"Contacted within the last {s.contact_cooldown_hours}h (cooldown)"
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = db.scalar(select(func.count(Message.id)).where(
        Message.direction == "out", Message.purpose == "first_contact", Message.created_at >= today,
        Message.status.in_(["sent", "awaiting_user_send"])))
    if sent_today >= s.daily_outreach_cap:
        return f"Daily outreach cap ({s.daily_outreach_cap}) reached"
    return None


def create_draft(db: Session, l: Listing, search: Search, purpose: str, subject: str, body: str) -> Message:
    ch, to = choose_channel(db, l, purpose == "first_contact")
    m = Message(listing_id=l.id, search_id=search.id, direction="out", channel=ch, purpose=purpose,
                to_addr=to, subject=subject, body=body, status="pending_approval")
    db.add(m)
    db.flush()
    return m


def send(db: Session, m: Message) -> Message:
    """Dispatch an approved message. Assisted channels produce a link for the user to send."""
    l = db.get(Listing, m.listing_id) if m.listing_id else None
    m.body = ensure_disclosure(m.body)
    try:
        if m.channel == "email":
            r = channels.send_email(m.to_addr, m.subject or "Rental enquiry", m.body, reply_tag=f"l{m.listing_id}")
            m.external_id, m.status = r.external_id, "sent"
        elif m.channel == "whatsapp":
            r = channels.send_whatsapp_text(m.to_addr, m.body)
            m.external_id, m.status = r.external_id, "sent"
        elif m.channel in ("assisted", "assisted_email"):
            m.send_link = channels.assisted_link("email" if m.channel == "assisted_email" else "whatsapp",
                                                 m.to_addr, m.body, m.subject)
            m.status = "awaiting_user_send"
        else:
            m.status, m.error = "failed", "no channel available"
    except channels.ChannelError as e:
        # Fall back to assisted sending rather than silently failing
        m.channel, m.error = "assisted", str(e)
        m.send_link = channels.assisted_link("whatsapp", l.contact_phone if l else None, m.body) if l and l.contact_phone else None
        m.status = "awaiting_user_send" if m.send_link else "failed"
    if m.status in ("sent", "awaiting_user_send") and l:
        key = contact_key(l)
        c = db.scalar(select(Contact).where(Contact.key == key)) if key else None
        if key and not c:
            c = Contact(key=key, name=l.contact_name, role=l.contact_role)
            db.add(c)
        if c:
            c.last_contacted_at = utcnow()
    db.flush()
    return m


def record_inbound(db: Session, listing_id: int | None, channel: str, from_addr: str | None, body: str,
                   external_id: str | None = None, subject: str | None = None) -> Message:
    m = Message(listing_id=listing_id, direction="in", channel=channel, from_addr=from_addr, body=body,
                external_id=external_id, subject=subject, status="received")
    if listing_id:
        l = db.get(Listing, listing_id)
        m.search_id = db.scalar(select(Message.search_id).where(Message.listing_id == listing_id,
                                                                Message.search_id.is_not(None)).limit(1))
        if STOP_WORDS.search(body) and l:
            key = contact_key(l)
            c = db.scalar(select(Contact).where(Contact.key == key)) if key else None
            if c:
                c.do_not_contact = True
    db.add(m)
    db.flush()
    return m


def match_inbound_to_listing(db: Session, from_addr: str, in_reply_to: str | None = None) -> int | None:
    if in_reply_to:
        lid = db.scalar(select(Message.listing_id).where(Message.external_id == in_reply_to))
        if lid:
            return lid
    norm = from_addr.lower()
    digits = re.sub(r"\D", "", from_addr)
    if len(digits) >= 10:
        norm = "+91" + digits[-10:]
    return db.scalar(select(Message.listing_id).where(
        Message.direction == "out", Message.to_addr.in_([from_addr, norm, from_addr.lower()]))
        .order_by(Message.created_at.desc()).limit(1))


_ = datetime
