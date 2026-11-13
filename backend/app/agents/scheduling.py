"""Agent 6 — Visit Scheduling.

Availability = user's weekly visiting windows minus busy blocks from an ICS calendar feed
(Google/Outlook "secret iCal address") minus already-booked visits.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from dateutil import parser as dtparse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Listing, Setting, Visit

log = logging.getLogger(__name__)

DEFAULT_WINDOWS = {  # weekday (0=Mon) → list of (start, end) "HH:MM"
    0: [("18:30", "20:30")], 1: [("18:30", "20:30")], 2: [("18:30", "20:30")],
    3: [("18:30", "20:30")], 4: [("18:30", "20:30")],
    5: [("10:00", "13:00"), ("16:00", "19:00")], 6: [("10:00", "13:00"), ("16:00", "19:00")],
}


def get_prefs(db: Session) -> dict:
    s = db.get(Setting, "calendar")
    v = (s.value if s else None) or {}
    return {"windows": {int(k): w for k, w in (v.get("windows") or DEFAULT_WINDOWS).items()},
            "ics_url": v.get("ics_url"), "visit_minutes": v.get("visit_minutes", 45),
            "buffer_minutes": v.get("buffer_minutes", 30), "timezone": v.get("timezone", "Asia/Kolkata")}


def local_now(tz: str = "Asia/Kolkata") -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)


def busy_from_ics(ics_url: str | None, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    if not ics_url:
        return []
    try:
        from icalendar import Calendar
        r = httpx.get(ics_url, timeout=20, follow_redirects=True)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        log.warning("ICS fetch failed: %s", e)
        return []
    out = []
    for ev in cal.walk("VEVENT"):
        try:
            s, e = ev.decoded("dtstart"), ev.decoded("dtend")
        except KeyError:
            continue
        if isinstance(s, date) and not isinstance(s, datetime):
            s, e = datetime.combine(s, time.min), datetime.combine(e, time.min)
        tz = ZoneInfo("Asia/Kolkata")
        s = s.astimezone(tz).replace(tzinfo=None) if s.tzinfo else s
        e = e.astimezone(tz).replace(tzinfo=None) if e.tzinfo else e
        if e > start and s < end:
            out.append((s, e))
    return out


def free_slots(db: Session, days: int = 7, n: int = 3, now: datetime | None = None,
               not_before_hours: int = 18) -> list[datetime]:
    p = get_prefs(db)
    now = now or local_now(p["timezone"])
    start = now + timedelta(hours=not_before_hours)
    end = now + timedelta(days=days)
    busy = busy_from_ics(p["ics_url"], start, end)
    busy += [(v.start_at - timedelta(minutes=p["buffer_minutes"]), v.end_at + timedelta(minutes=p["buffer_minutes"]))
             for v in db.scalars(select(Visit).where(Visit.status.in_(["proposed", "confirmed"]),
                                                     Visit.start_at >= start, Visit.start_at <= end))]
    dur = timedelta(minutes=p["visit_minutes"])
    slots: list[datetime] = []
    d = start.date()
    while d <= end.date() and len(slots) < n * 4:
        for ws, we in p["windows"].get(d.weekday(), []):
            t = datetime.combine(d, dtparse.parse(ws).time())
            stop = datetime.combine(d, dtparse.parse(we).time())
            while t + dur <= stop:
                if t >= start and not any(t < be and t + dur > bs for bs, be in busy):
                    slots.append(t)
                    break  # one slot per window keeps proposals varied
                t += timedelta(minutes=30)
        d += timedelta(days=1)
    # spread suggestions across different days
    picked, used_days = [], set()
    for s in slots:
        if s.date() not in used_days:
            picked.append(s)
            used_days.add(s.date())
        if len(picked) == n:
            break
    return picked or slots[:n]


def is_free(db: Session, t: datetime) -> bool:
    p = get_prefs(db)
    dur = timedelta(minutes=p["visit_minutes"])
    busy = busy_from_ics(p["ics_url"], t - timedelta(hours=2), t + timedelta(hours=2))
    busy += [(v.start_at, v.end_at) for v in db.scalars(select(Visit).where(Visit.status == "confirmed"))]
    return not any(t < be and t + dur > bs for bs, be in busy)


def fmt_slot(t: datetime) -> str:
    return t.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def ics_invite(v: Visit, l: Listing) -> str:
    from icalendar import Calendar, Event
    cal = Calendar()
    cal.add("prodid", "-//RentAgent//visit//EN")
    cal.add("version", "2.0")
    e = Event()
    e.add("uid", f"visit-{v.id}@rentagent")
    e.add("summary", f"Flat visit: {l.property_type or ''} {l.society or ''} {l.locality or ''}".strip())
    e.add("dtstart", v.start_at)
    e.add("dtend", v.end_at)
    e.add("location", l.address or ", ".join(filter(None, [l.society, l.locality, l.city])))
    e.add("description", f"Contact: {l.contact_name or ''} {l.contact_phone or ''}\n{l.source_url or ''}")
    cal.add_component(e)
    return cal.to_ical().decode()


def visit_checklist() -> list[str]:
    return ["Water pressure & supply timings", "Mobile network / broadband availability", "Ventilation & sunlight",
            "Seepage or dampness on walls/ceilings", "Condition of fittings, appliances listed as included",
            "Parking spot allotted (ask to see it)", "Society rules: tenants, pets, visitors, move-in charges",
            "Meet the actual owner or see authorisation if a broker", "Electricity meter & pending bills",
            "Commute check at peak hours"]
