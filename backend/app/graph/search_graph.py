"""SearchGraph: intake → parallel discovery (Send) → dedupe → verify → score → notify → outreach."""
from __future__ import annotations

import logging
import operator
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send
from sqlalchemy import select

from ..agents import matching, verification
from ..agents.communication import outreach_block_reason
from ..agents.discovery import agent as discovery
from ..agents.discovery.sources.base import SourceError
from ..config import get_settings
from ..db import session_scope
from ..models import Listing, Match, Search, utcnow
from ..schemas import ScoringWeights, SearchCriteria
from ..services.events import emit
from ..services.notifications import notify
from .runtime import get_checkpointer, thread_lock

log = logging.getLogger(__name__)
DB_RETRY = RetryPolicy(max_attempts=4, initial_interval=0.5)


class SearchState(TypedDict, total=False):
    search_id: int
    run: int
    tasks: list[dict]
    raw: Annotated[list[dict], operator.add]
    errors: Annotated[list[str], operator.add]
    new_ids: list[int]
    touched_ids: list[int]
    outreach_ids: list[int]
    auto_outreach: bool


class DiscoverTask(TypedDict):
    search_id: int
    source: dict
    locality: str | None


def intake(state: SearchState) -> dict:
    with session_scope() as db:
        s = db.get(Search, state["search_id"])
        c = SearchCriteria(**s.criteria)
        srcs = discovery.build_sources(s.sources)
        tasks = []
        for src in srcs:
            ok, why = src.available()
            if not ok:
                emit("discovery", f"Source {src.name} skipped: {why}", status="done", level="warn", search_id=s.id)
                continue
            if src.per_locality:
                for loc in (c.localities or [None]):
                    tasks.append({"search_id": s.id, "source": src.config | {"type": src.config.get("type", src.name)},
                                  "locality": loc})
            else:
                tasks.append({"search_id": s.id, "source": src.config | {"type": src.config.get("type", src.name)},
                              "locality": None})
        s.last_run_at = utcnow()
    emit("orchestrator", f"Search run started: {len(tasks)} discovery task(s) across {len(c.localities) or 1} locality(ies)",
         status="running", search_id=state["search_id"])
    return {"tasks": tasks}


def fan_out(state: SearchState):
    if not state.get("tasks"):
        return "store"
    return [Send("discover_one", t) for t in state["tasks"]]


def discover_one(task: DiscoverTask) -> dict:
    src = discovery.build_sources([task["source"]])[0]
    with session_scope() as db:
        c = SearchCriteria(**db.get(Search, task["search_id"]).criteria)
    label = f"{src.name}{' · ' + task['locality'] if task['locality'] else ''}"
    emit("discovery", f"Searching {label}", status="running", search_id=task["search_id"])
    last_err = None
    for attempt in range(3):  # transient-error retry with backoff; SourceError is final
        try:
            found = discovery.run_source(src, c, task["locality"])
            emit("discovery", f"{label}: {len(found)} listing(s) found", search_id=task["search_id"])
            return {"raw": [discovery.to_jsonable(r) for r in found]}
        except SourceError as e:
            last_err = str(e)
            break
        except Exception as e:  # network etc.
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * 2 ** attempt)
    emit("discovery", f"{label} failed: {last_err}", status="error", level="warn", search_id=task["search_id"])
    return {"errors": [f"{label}: {last_err}"]}


def store(state: SearchState) -> dict:
    new_ids, touched = [], []
    with session_scope() as db:
        for d in state.get("raw", []):
            l, created = discovery.upsert(db, discovery.from_jsonable(d))
            touched.append(l.id)
            if created:
                new_ids.append(l.id)
    touched = list(dict.fromkeys(touched))
    emit("discovery", f"Stored {len(touched)} listing(s): {len(new_ids)} new, {len(touched) - len(new_ids)} merged/updated",
         search_id=state["search_id"], data={"new": len(new_ids), "touched": len(touched)})
    return {"new_ids": new_ids, "touched_ids": touched}


def _search_listings(db, s: Search) -> list[Listing]:
    c = SearchCriteria(**s.criteria)
    q = select(Listing).where(Listing.status.not_in(["rejected"]))
    if c.city:
        q = q.where((Listing.city.is_(None)) | (Listing.city.ilike(c.city)))
    return list(db.scalars(q))


def verify(state: SearchState) -> dict:
    flagged = 0
    with session_scope() as db:
        s = db.get(Search, state["search_id"])
        # Re-assess everything in scope: new listings can make old ones look duplicated/anomalous.
        for l in _search_listings(db, s):
            had_high = any(f.get("severity") == "high" for f in (l.risk_flags or []))
            l.confidence, l.risk_flags = verification.assess(db, l)
            if not had_high and any(f["severity"] == "high" for f in l.risk_flags):
                flagged += 1
                notify("scam_warning", f"Suspicious listing: {l.title or l.locality}",
                       "; ".join(f["message"] for f in l.risk_flags if f["severity"] == "high"),
                       search_id=s.id, listing_id=l.id, external=False)
    emit("verification", f"Verified listings; {flagged} newly flagged as suspicious", search_id=state["search_id"])
    return {}


def score(state: SearchState) -> dict:
    with session_scope() as db:
        s = db.get(Search, state["search_id"])
        c, w = SearchCriteria(**s.criteria), ScoringWeights(**s.weights)
        n = 0
        for l in _search_listings(db, s):
            r = matching.score_listing(l, c, w)
            m = db.scalar(select(Match).where(Match.search_id == s.id, Match.listing_id == l.id))
            if not m:
                m = Match(search_id=s.id, listing_id=l.id, score=0, breakdown=[])
                db.add(m)
            m.score, m.breakdown, m.compromises, m.missing = r.score, r.breakdown, r.compromises, r.missing
            m.hard_fail, m.total_monthly_cost = r.hard_fail, r.total_monthly_cost
            n += 1
    emit("matching", f"Scored {n} listing(s)", search_id=state["search_id"])
    return {}


def notify_matches(state: SearchState) -> dict:
    new = set(state.get("new_ids") or [])
    with session_scope() as db:
        sid = state["search_id"]
        rows = db.execute(select(Match, Listing).join(Listing, Listing.id == Match.listing_id)
                          .where(Match.search_id == sid, Match.dismissed.is_(False)).order_by(Match.score.desc())).all()
        best_old = max((m.score for m, l in rows if l.id not in new), default=0)
        for m, l in rows:
            if m.notified or m.hard_fail or l.availability == "unavailable":
                continue
            label = f"{l.property_type or ''} {l.society or ''} {l.locality or ''}".strip()
            if m.score >= 80:
                notify("high_match", f"{m.score:.0f}% match: {label}",
                       f"₹{l.rent:,}/mo · {l.furnishing or 'furnishing ?'}" if l.rent else label,
                       search_id=sid, listing_id=l.id)
                m.notified = True
            elif l.id in new and best_old and m.score > best_old + 5 and m.score >= 65:
                notify("better_property", f"Better option found: {label} ({m.score:.0f})",
                       f"Beats your previous best ({best_old:.0f}).", search_id=sid, listing_id=l.id)
                m.notified = True
    return {}


def pick_outreach(state: SearchState) -> dict:
    if not state.get("auto_outreach", True):
        return {"outreach_ids": []}
    st = get_settings()
    picked = []
    with session_scope() as db:
        sid = state["search_id"]
        s = db.get(Search, sid)
        if s.status != "active":
            return {"outreach_ids": []}
        rows = db.execute(select(Match, Listing).join(Listing, Listing.id == Match.listing_id)
                          .where(Match.search_id == sid, Match.hard_fail.is_(False), Match.dismissed.is_(False),
                                 Match.score >= st.min_score_to_contact, Listing.status == "new",
                                 Listing.availability != "unavailable", Listing.confidence >= 40)
                          .order_by(Match.score.desc())).all()
        for m, l in rows:
            if len(picked) >= st.daily_outreach_cap:
                break
            if outreach_block_reason(db, l, first_contact=True):
                continue
            picked.append(l.id)
    from . import deal_graph
    for lid in picked:
        deal_graph.start(lid, state["search_id"])
    emit("orchestrator", f"Run complete. Started outreach drafts for {len(picked)} listing(s) (awaiting your approval)",
         search_id=state["search_id"], status="done")
    return {"outreach_ids": picked}


def build():
    g = StateGraph(SearchState)
    g.add_node("intake", intake, retry_policy=DB_RETRY)
    g.add_node("discover_one", discover_one)
    g.add_node("store", store, retry_policy=DB_RETRY)
    g.add_node("verify", verify, retry_policy=DB_RETRY)
    g.add_node("score", score, retry_policy=DB_RETRY)
    g.add_node("notify", notify_matches, retry_policy=DB_RETRY)
    g.add_node("pick_outreach", pick_outreach, retry_policy=DB_RETRY)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", fan_out, ["discover_one", "store"])
    g.add_edge("discover_one", "store")
    g.add_edge("store", "verify")
    g.add_edge("verify", "score")
    g.add_edge("score", "notify")
    g.add_edge("notify", "pick_outreach")
    g.add_edge("pick_outreach", END)
    return g.compile(checkpointer=get_checkpointer())


_graph = None


def graph():
    global _graph
    if _graph is None:
        _graph = build()
    return _graph


def run(search_id: int, auto_outreach: bool = True) -> dict:
    """Execute one discovery run. Each run is its own checkpointed thread."""
    tid = f"search:{search_id}"
    with thread_lock(tid):
        with session_scope() as db:
            s = db.get(Search, search_id)
            run_no = int((s.last_run_at or utcnow()).timestamp())
        cfg = {"configurable": {"thread_id": f"{tid}:run:{run_no}:{time.time_ns()}"}}
        return graph().invoke({"search_id": search_id, "raw": [], "errors": [], "auto_outreach": auto_outreach}, cfg)


def rescore(search_id: int) -> None:
    """Re-score without discovery (weights/criteria changed)."""
    score({"search_id": search_id})
