from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select

from . import worker
from .agents import closure, scheduling
from .agents.discovery import agent as discovery
from .agents.discovery.sources.base import FetchNotPermitted
from .agents.discovery.sources.url_import import import_text, import_url
from .agents.intake import parse_query
from .config import get_settings
from .db import init_db, session_scope
from .graph import deal_graph, search_graph
from .graph.runtime import submit
from .models import (Approval, Deal, Event, Listing, ListingSource, Match, Message, Negotiation, Notification,
                     Search, Setting, Visit)
from .schemas import NegotiationBounds, ParsedQuery, ScoringWeights, SearchCriteria
from .services import events, inbound
from .services.channels import channels_status
from .services.llm import get_llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    events.bind_loop(asyncio.get_running_loop())
    tasks = []
    if settings.redis_url:
        tasks.append(asyncio.create_task(events.redis_listener()))
    else:
        tasks.append(asyncio.create_task(worker.inprocess_scheduler()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="RentAgent", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","), allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ serialisers

def ser_listing(l: Listing) -> dict:
    d = {c.name: getattr(l, c.name) for c in Listing.__table__.columns if c.name not in ("photo_hashes", "fingerprint")}
    return d


def ser(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ------------------------------------------------------------------ status

@app.get("/api/status")
def status():
    from .agents.discovery.agent import REGISTRY
    srcs = {}
    for name, cls in REGISTRY.items():
        ok, why = cls({"url": "x"} if name == "partner_feed" else {}).available()
        srcs[name] = {"available": ok, "reason": why, "compliance": cls.compliance}
    return {"llm": get_llm().available, "model": settings.llm_model, "channels": channels_status(), "sources": srcs,
            "fetch_allowed_domains": sorted(settings.allowed_domains), "daily_outreach_cap": settings.daily_outreach_cap,
            "background": "celery" if settings.redis_url else "in-process"}


# ------------------------------------------------------------------ searches

@app.post("/api/parse", response_model=ParsedQuery)
def parse(text: str = Body(..., embed=True)):
    return parse_query(text)


class SearchIn(BaseModel):
    title: str | None = None
    raw_query: str | None = None
    criteria: SearchCriteria | None = None
    bounds: NegotiationBounds = NegotiationBounds()
    weights: ScoringWeights = ScoringWeights()
    sources: list[dict] | None = None
    auto_outreach: bool = True
    run_now: bool = True


@app.post("/api/searches")
def create_search(body: SearchIn):
    assumptions: list[str] = []
    criteria = body.criteria
    if criteria is None:
        if not body.raw_query:
            raise HTTPException(400, "Provide criteria or raw_query")
        pq = parse_query(body.raw_query)
        criteria, assumptions = pq.criteria, pq.assumptions + pq.unclear
    title = body.title or " · ".join(filter(None, [
        "/".join(criteria.property_types) or "Home", ", ".join(criteria.localities[:2]) or criteria.city,
        f"≤₹{criteria.max_rent:,}" if criteria.max_rent else None]))
    with session_scope() as db:
        s = Search(title=title[:200], raw_query=body.raw_query, criteria=criteria.model_dump(mode="json"),
                   bounds=body.bounds.model_dump(), weights=body.weights.model_dump(),
                   sources=body.sources or discovery.DEFAULT_SOURCES)
        db.add(s)
        db.flush()
        sid = s.id
    events.emit("orchestrator", f"New search created: {title}", search_id=sid, data={"assumptions": assumptions})
    if body.run_now:
        worker.enqueue_search(sid, body.auto_outreach)
    return {"id": sid, "title": title, "criteria": criteria, "assumptions": assumptions}


@app.get("/api/searches")
def list_searches():
    with session_scope() as db:
        return [ser(s) for s in db.scalars(select(Search).order_by(Search.id.desc()))]


@app.get("/api/searches/{sid}")
def get_search(sid: int):
    with session_scope() as db:
        s = db.get(Search, sid) or _404()
        return ser(s) | {"effective_bounds": NegotiationBounds(**s.bounds).filled(SearchCriteria(**s.criteria)).model_dump()}


class SearchPatch(BaseModel):
    criteria: SearchCriteria | None = None
    bounds: NegotiationBounds | None = None
    weights: ScoringWeights | None = None
    sources: list[dict] | None = None
    status: str | None = None
    title: str | None = None


@app.patch("/api/searches/{sid}")
def patch_search(sid: int, body: SearchPatch):
    with session_scope() as db:
        s = db.get(Search, sid) or _404()
        if body.criteria:
            s.criteria = body.criteria.model_dump(mode="json")
        if body.bounds:
            s.bounds = body.bounds.model_dump()
        if body.weights:
            s.weights = body.weights.model_dump()
        if body.sources is not None:
            s.sources = body.sources
        if body.status:
            s.status = body.status
        if body.title:
            s.title = body.title
    if body.criteria or body.weights:
        search_graph.rescore(sid)
    return get_search(sid)


@app.post("/api/searches/{sid}/run")
def run_search(sid: int, auto_outreach: bool = True):
    with session_scope() as db:
        db.get(Search, sid) or _404()
    worker.enqueue_search(sid, auto_outreach)
    return {"queued": True}


@app.get("/api/searches/{sid}/listings")
def search_listings(sid: int, shortlisted: bool = False, include_dismissed: bool = False, limit: int = 200):
    with session_scope() as db:
        q = (select(Match, Listing).join(Listing, Listing.id == Match.listing_id).where(Match.search_id == sid))
        if shortlisted:
            q = q.where(Match.shortlisted.is_(True))
        if not include_dismissed:
            q = q.where(Match.dismissed.is_(False))
        rows = db.execute(q.order_by(Match.hard_fail, Match.score.desc()).limit(limit)).all()
        return [ser_listing(l) | {"match": ser(m)} for m, l in rows]


@app.get("/api/searches/{sid}/dashboard")
def dashboard(sid: int):
    with session_scope() as db:
        s = db.get(Search, sid) or _404()
        lids = select(Match.listing_id).where(Match.search_id == sid)
        cnt = lambda q: db.scalar(select(func.count()).select_from(q.subquery()))  # noqa: E731
        by_status = dict(db.execute(select(Listing.status, func.count()).where(Listing.id.in_(lids))
                                    .group_by(Listing.status)).all())
        return {
            "search": ser(s),
            "discovered": cnt(select(Match.id).where(Match.search_id == sid)),
            "verified": cnt(select(Listing.id).where(Listing.id.in_(lids), Listing.availability == "confirmed")),
            "suspicious": cnt(select(Listing.id).where(Listing.id.in_(lids), Listing.confidence < 40)),
            "awaiting_response": by_status.get("awaiting_reply", 0),
            "drafting": by_status.get("contacting", 0),
            "negotiating": by_status.get("negotiating", 0),
            "visits_scheduled": cnt(select(Visit.id).where(Visit.search_id == sid, Visit.status == "confirmed")),
            "shortlisted": cnt(select(Match.id).where(Match.search_id == sid, Match.shortlisted.is_(True))),
            "closing": by_status.get("closing", 0) + by_status.get("closed", 0),
            "pending_approvals": cnt(select(Approval.id).where(Approval.search_id == sid, Approval.status == "pending")),
            "by_status": by_status,
            "top": [ser_listing(l) | {"match": ser(m)} for m, l in db.execute(
                select(Match, Listing).join(Listing, Listing.id == Match.listing_id)
                .where(Match.search_id == sid, Match.dismissed.is_(False), Match.hard_fail.is_(False))
                .order_by(Match.score.desc()).limit(5)).all()],
        }


# ------------------------------------------------------------------ listings

@app.get("/api/listings/{lid}")
def get_listing(lid: int, search_id: int | None = None):
    with session_scope() as db:
        l = db.get(Listing, lid) or _404()
        mq = select(Match).where(Match.listing_id == lid)
        if search_id:
            mq = mq.where(Match.search_id == search_id)
        m = db.scalar(mq.limit(1))
        out = ser_listing(l) | {
            "match": ser(m) if m else None,
            "sources": [ser(x) for x in db.scalars(select(ListingSource).where(ListingSource.listing_id == lid))],
            "messages": [ser(x) for x in db.scalars(select(Message).where(Message.listing_id == lid).order_by(Message.id))],
            "negotiation": ser(n) if (n := db.scalar(select(Negotiation).where(Negotiation.listing_id == lid))) else None,
            "visits": [ser(v) for v in db.scalars(select(Visit).where(Visit.listing_id == lid))],
            "deal": ser(d) if (d := db.scalar(select(Deal).where(Deal.listing_id == lid))) else None,
            "approvals": [ser(a) for a in db.scalars(select(Approval).where(Approval.listing_id == lid,
                                                                             Approval.status == "pending"))],
        }
    try:
        out["workflow"] = deal_graph.state(lid)
        out["workflow"]["values"] = {k: v for k, v in out["workflow"]["values"].items() if k != "decision"}
    except Exception:
        out["workflow"] = None
    return out


@app.post("/api/listings/{lid}/shortlist")
def shortlist(lid: int, search_id: int, value: bool = True):
    with session_scope() as db:
        m = db.scalar(select(Match).where(Match.listing_id == lid, Match.search_id == search_id)) or _404()
        m.shortlisted = value
    return {"ok": True}


@app.post("/api/listings/{lid}/dismiss")
def dismiss(lid: int, search_id: int):
    with session_scope() as db:
        m = db.scalar(select(Match).where(Match.listing_id == lid, Match.search_id == search_id)) or _404()
        m.dismissed = True
    return {"ok": True}


@app.post("/api/listings/{lid}/contact")
def start_contact(lid: int, search_id: int):
    """Manually start the deal workflow for a listing (drafts first message → approval)."""
    with session_scope() as db:
        db.get(Listing, lid) or _404()
    if deal_graph.state(lid)["values"]:
        raise HTTPException(409, "Workflow already started for this listing")
    submit(deal_graph.start, lid, search_id)
    return {"queued": True}


@app.post("/api/listings/{lid}/inbound")
def paste_reply(lid: int, body: str = Body(..., embed=True), channel: str = Body("manual", embed=True),
                sync: bool = False):
    """User pastes a reply received on their phone (assisted channel) or simulates one for a sample listing."""
    with session_scope() as db:
        db.get(Listing, lid) or _404()
    mid = inbound.handle_inbound(lid, channel, None, body, sync=sync)
    return {"message_id": mid}


@app.post("/api/messages/{mid}/mark_sent")
def mark_sent(mid: int):
    with session_scope() as db:
        m = db.get(Message, mid) or _404()
        if m.status == "awaiting_user_send":
            m.status = "sent"
    return {"ok": True}


class ImportIn(BaseModel):
    search_id: int
    url: str | None = None
    text: str | None = None


@app.post("/api/import")
def import_listing(body: ImportIn):
    with session_scope() as db:
        s = db.get(Search, body.search_id) or _404()
        city = s.criteria.get("city")
    try:
        raws = import_url(body.url, city) if body.url and not body.text else import_text(body.text or "", city, body.url)
    except FetchNotPermitted as e:
        raise HTTPException(422, f"{e}. Paste the listing text instead.")
    if not raws:
        raise HTTPException(422, "Couldn't find listing details in that content")
    ids = []
    with session_scope() as db:
        for r in raws:
            l, _ = discovery.upsert(db, r)
            ids.append(l.id)
    st = {"search_id": body.search_id, "touched_ids": ids, "new_ids": ids}
    search_graph.verify(st)
    search_graph.score(st)
    events.emit("discovery", f"Imported {len(ids)} listing(s) from {'URL' if body.url else 'pasted text'}",
                search_id=body.search_id)
    return {"listing_ids": ids}


@app.get("/api/compare")
def compare(ids: str, search_id: int):
    out = []
    for i in [int(x) for x in ids.split(",") if x.strip()][:5]:
        d = get_listing(i, search_id)
        d.pop("messages", None)
        d.pop("workflow", None)
        out.append(d)
    return out


# ------------------------------------------------------------------ approvals

@app.get("/api/approvals")
def list_approvals(status: str = "pending", search_id: int | None = None):
    with session_scope() as db:
        q = select(Approval).where(Approval.status == status)
        if search_id:
            q = q.where(Approval.search_id == search_id)
        return [ser(a) for a in db.scalars(q.order_by(Approval.id.desc()))]


class Decision(BaseModel):
    decision: str  # approve | reject | edit
    body: str | None = None
    bounds: dict | None = None


@app.post("/api/approvals/{aid}")
def decide(aid: int, d: Decision):
    if d.decision not in ("approve", "reject", "edit"):
        raise HTTPException(400, "decision must be approve|reject|edit")
    with session_scope() as db:
        a = db.get(Approval, aid) or _404()
        if a.status != "pending":
            raise HTTPException(409, f"Approval already {a.status}")
        lid = a.listing_id
    ok = deal_graph.resume(lid, {"approval_id": aid, **d.model_dump()}, expect_kind="approval")
    if not ok:
        raise HTTPException(409, "Workflow is not waiting for this approval (it may have been superseded)")
    return {"ok": True}


# ------------------------------------------------------------------ visits

@app.get("/api/visits")
def list_visits(search_id: int | None = None):
    with session_scope() as db:
        q = select(Visit, Listing).join(Listing, Listing.id == Visit.listing_id)
        if search_id:
            q = q.where(Visit.search_id == search_id)
        return [ser(v) | {"listing": ser_listing(l), "checklist": scheduling.visit_checklist()}
                for v, l in db.execute(q.order_by(Visit.start_at)).all()]


@app.get("/api/visits/{vid}/ics")
def visit_ics(vid: int):
    with session_scope() as db:
        v = db.get(Visit, vid) or _404()
        ics = scheduling.ics_invite(v, db.get(Listing, v.listing_id))
    return Response(ics, media_type="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=visit-{vid}.ics"})


class Feedback(BaseModel):
    liked: bool = False
    rating: int | None = None
    notes: str = ""
    cancelled: bool = False


@app.post("/api/visits/{vid}/feedback")
def visit_feedback(vid: int, fb: Feedback):
    with session_scope() as db:
        v = db.get(Visit, vid) or _404()
        lid = v.listing_id
    if not deal_graph.resume(lid, fb.model_dump(), expect_kind="visit_feedback"):
        raise HTTPException(409, "Workflow is not waiting for visit feedback")
    return {"ok": True}


# ------------------------------------------------------------------ deals

@app.get("/api/deals")
def list_deals(search_id: int | None = None):
    with session_scope() as db:
        q = select(Deal, Listing).join(Listing, Listing.id == Deal.listing_id)
        if search_id:
            q = q.where(Deal.search_id == search_id)
        return [ser(d) | {"listing": ser_listing(l)} for d, l in db.execute(q).all()]


@app.patch("/api/deals/{lid}/checklist/{key}")
def tick(lid: int, key: str, done: bool = Body(..., embed=True), note: str = Body("", embed=True)):
    with session_scope() as db:
        d = db.scalar(select(Deal).where(Deal.listing_id == lid)) or _404()
        items = [dict(i) for i in d.checklist]
        for i in items:
            if i["key"] == key:
                i["done"], i["note"] = done, note
        d.checklist = items
    return {"ok": True}


@app.post("/api/deals/{lid}/agreement")
def review(lid: int, text: str = Body(..., embed=True)):
    with session_scope() as db:
        d = db.scalar(select(Deal).where(Deal.listing_id == lid)) or _404()
        terms = {k: v for k, v in d.final_terms.items() if k in
                 ("rent", "deposit", "maintenance", "lock_in_months", "notice_months", "escalation_pct", "lease_months")}
        if "lease_months" in terms:
            terms["term_months"] = terms.pop("lease_months")
    result = closure.review_agreement(text, terms)
    with session_scope() as db:
        d = db.scalar(select(Deal).where(Deal.listing_id == lid))
        d.agreement_review = result
        d.stage = "agreement_review"
        items = [dict(i) for i in d.checklist]
        for i in items:
            if i["key"] in ("agreement_draft", "agreement_review"):
                i["done"] = True
        d.checklist = items
    events.emit("closure", f"Agreement reviewed: {result.get('summary', '')}", listing_id=lid)
    return result


@app.post("/api/deals/{lid}/close")
def close_deal(lid: int, closed: bool = Body(True, embed=True)):
    if not deal_graph.resume(lid, {"closed": closed}, expect_kind="closure"):
        raise HTTPException(409, "Deal is not in the closure stage")
    return {"ok": True}


# ------------------------------------------------------------------ activity / notifications / settings

@app.get("/api/events")
def list_events(search_id: int | None = None, listing_id: int | None = None, limit: int = 100):
    with session_scope() as db:
        q = select(Event)
        if search_id:
            q = q.where((Event.search_id == search_id) | (Event.search_id.is_(None)))
        if listing_id:
            q = q.where(Event.listing_id == listing_id)
        return [ser(e) for e in db.scalars(q.order_by(Event.id.desc()).limit(limit))]


@app.get("/api/notifications")
def list_notifications(unread: bool = False, limit: int = 50):
    with session_scope() as db:
        q = select(Notification)
        if unread:
            q = q.where(Notification.read.is_(False))
        return [ser(n) for n in db.scalars(q.order_by(Notification.id.desc()).limit(limit))]


@app.post("/api/notifications/read")
def read_all():
    with session_scope() as db:
        for n in db.scalars(select(Notification).where(Notification.read.is_(False))):
            n.read = True
    return {"ok": True}


@app.get("/api/settings/calendar")
def get_calendar():
    with session_scope() as db:
        return scheduling.get_prefs(db)


@app.put("/api/settings/calendar")
def put_calendar(value: dict = Body(...)):
    with session_scope() as db:
        s = db.get(Setting, "calendar") or Setting(key="calendar")
        s.value = value
        db.merge(s)
    return {"ok": True}


@app.get("/api/settings/calendar/slots")
def slots(n: int = 5):
    with session_scope() as db:
        return [t.isoformat() for t in scheduling.free_slots(db, n=n)]


@app.post("/api/housekeeping")
def run_housekeeping():
    return inbound.housekeeping()


# ------------------------------------------------------------------ WhatsApp webhook (official Cloud API)

@app.get("/webhooks/whatsapp")
def wa_verify(mode: str = Query(None, alias="hub.mode"), token: str = Query(None, alias="hub.verify_token"),
              challenge: str = Query(None, alias="hub.challenge")):
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return Response(challenge or "", media_type="text/plain")
    raise HTTPException(403)


@app.post("/webhooks/whatsapp")
async def wa_inbound(request: Request):
    payload = await request.json()
    n = await asyncio.to_thread(inbound.handle_whatsapp, payload)
    return {"processed": n}


# ------------------------------------------------------------------ websocket

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    q = events.subscribe()
    try:
        while True:
            payload = await q.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        events.unsubscribe(q)


def _404():
    raise HTTPException(404, "not found")


_ = date
