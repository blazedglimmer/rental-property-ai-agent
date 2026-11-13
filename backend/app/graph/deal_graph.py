"""DealGraph — one checkpointed thread per listing: outreach → replies → negotiation → visit → closure.

Interrupts:
  * approval        — user approves/edits/rejects a draft, an escalation, or proceeding to closure
  * await_reply     — waiting for an inbound message from the owner/broker (or a no-reply timeout)
  * visit_feedback  — waiting for the user's feedback after a visit
  * closure         — deal in closure; ends when the user marks it closed or abandoned
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import TypedDict

from dateutil import parser as dtparse
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt
from sqlalchemy import select

from ..agents import closure, communication as comm, matching, negotiation as nego, scheduling, verification
from ..config import get_settings
from ..db import session_scope
from ..models import Approval, Deal, Listing, Match, Message, Negotiation, Search, Visit, utcnow
from ..schemas import NegotiationBounds, ScoringWeights, SearchCriteria
from ..services.events import emit
from ..services.notifications import notify
from .runtime import get_checkpointer, submit, thread_lock

log = logging.getLogger(__name__)
DB_RETRY = RetryPolicy(max_attempts=4, initial_interval=0.5)
TERM_KEYS = ["rent", "deposit", "maintenance", "brokerage", "lock_in_months", "notice_months", "escalation_pct"]


class DealState(TypedDict, total=False):
    listing_id: int
    search_id: int
    phase: str          # contacting | negotiating | scheduling | visit_booked | closing | done
    draft_id: int | None
    purpose: str | None
    route_after_send: str
    inbound_ids: list[int]
    timeout: bool
    nudges: int
    followups: int
    decision: dict
    slots: list[str]
    visit_id: int | None
    outcome: str | None


def _tid(listing_id: int) -> str:
    return f"deal:{listing_id}"


def _label(l: Listing) -> str:
    return " ".join(filter(None, [l.property_type, l.society, l.locality])) or (l.title or f"Listing {l.id}")


def _set_status(db, l: Listing, status: str) -> None:
    l.status = status


# ------------------------------------------------------------------ nodes

def draft_outreach(state: DealState) -> dict:
    with session_scope() as db:
        l, s = db.get(Listing, state["listing_id"]), db.get(Search, state["search_id"])
        reason = comm.outreach_block_reason(db, l, first_contact=True)
        if reason:
            emit("communication", f"Outreach skipped for {_label(l)}: {reason}", level="warn",
                 search_id=s.id, listing_id=l.id)
            return {"outcome": f"blocked: {reason}"}
        m = db.scalar(select(Match).where(Match.search_id == s.id, Match.listing_id == l.id))
        qs = comm.questions_for((m.missing if m else []) or [], first=True)
        subject, body = comm.compose(l, s, "first_contact", qs)
        msg = comm.create_draft(db, l, s, "first_contact", subject, body)
        if l.is_sample:
            msg.channel = "simulated"
        _set_status(db, l, "contacting")
        emit("communication", f"Drafted first message to {l.contact_role or 'contact'} for {_label(l)} via {msg.channel}",
             search_id=s.id, listing_id=l.id)
        return {"draft_id": msg.id, "purpose": "first_contact", "phase": "contacting", "route_after_send": "wait_reply",
                "nudges": 0, "followups": 0}


def route_after_draft(state: DealState) -> str:
    return END if state.get("outcome") else "approve_message"


def approve_message(state: DealState) -> Command:
    st = get_settings()
    with session_scope() as db:
        msg = db.get(Message, state["draft_id"])
        l = db.get(Listing, state["listing_id"])
        purpose = msg.purpose
        auto = (purpose == "followup" and st.auto_followups) or (purpose in ("visit", "visit_confirm") and st.auto_visit_slots)
        if auto:
            return Command(goto="send_message")
        appr = db.scalar(select(Approval).where(Approval.thread_id == _tid(l.id), Approval.status == "pending",
                                                Approval.kind == purpose))
        if not appr:
            appr = Approval(kind=purpose, thread_id=_tid(l.id), search_id=state["search_id"], listing_id=l.id,
                            title={"first_contact": "Send first message", "offer": "Send counter-offer",
                                   "accept": "Agree terms in principle", "followup": "Send follow-up",
                                   "visit": "Propose visit slots"}.get(purpose, "Send message") + f" — {_label(l)}",
                            payload={"message_id": msg.id, "channel": msg.channel, "to": msg.to_addr,
                                     "subject": msg.subject, "body": msg.body, "decision": state.get("decision")})
            db.add(appr)
            db.flush()
            notify("approval_needed", appr.title, msg.body[:300], search_id=state["search_id"], listing_id=l.id)
            emit("orchestrator", f"Waiting for your approval: {appr.title}", status="waiting",
                 search_id=state["search_id"], listing_id=l.id)
        appr_id = appr.id
    answer = interrupt({"kind": "approval", "approval_id": appr_id, "purpose": purpose})
    decision = (answer or {}).get("decision", "reject")
    with session_scope() as db:
        appr, msg = db.get(Approval, appr_id), db.get(Message, state["draft_id"])
        l = db.get(Listing, state["listing_id"])
        appr.decided_at, appr.decision = utcnow(), answer
        if decision == "edit" and answer.get("body"):
            body = comm.ensure_disclosure(answer["body"])
            if purpose in ("offer", "accept"):
                d = nego.Decision(**state["decision"])
                problems = nego.guard(body, d, _bounds(db, state["search_id"]), _asking(db, l))
                if problems:
                    appr.status = "superseded"
                    emit("negotiation", "Edited offer rejected by guard: " + "; ".join(problems), level="warn",
                         listing_id=l.id, search_id=state["search_id"])
                    # Re-ask with the guard explanation
                    new = Approval(kind=purpose, thread_id=_tid(l.id), search_id=state["search_id"], listing_id=l.id,
                                   title=f"Edited offer needs changes — {_label(l)}",
                                   payload=appr.payload | {"guard_problems": problems, "body": body})
                    db.add(new)
                    return Command(goto="approve_message")
            msg.body = body
            appr.status = "edited"
            return Command(goto="send_message")
        if decision == "approve":
            appr.status = "approved"
            return Command(goto="send_message")
        appr.status, msg.status = "rejected", "rejected"
        if purpose == "first_contact":
            _set_status(db, l, "new")
            return Command(goto=END, update={"outcome": "skipped_by_user"})
        emit("orchestrator", f"You rejected the {purpose} message; conversation paused", listing_id=l.id,
             search_id=state["search_id"])
        return Command(goto="wait_reply")


def send_message(state: DealState) -> dict:
    with session_scope() as db:
        msg, l = db.get(Message, state["draft_id"]), db.get(Listing, state["listing_id"])
        if msg.status in ("sent", "awaiting_user_send", "simulated"):  # idempotent on retry
            return {}
        if msg.channel == "simulated":
            msg.status = "simulated"
        else:
            comm.send(db, msg)
        status_map = {"first_contact": "awaiting_reply", "offer": "negotiating", "accept": "negotiating",
                      "visit": "awaiting_reply"}
        if msg.purpose in status_map and l.status not in ("visit_scheduled", "closing"):
            _set_status(db, l, status_map[msg.purpose])
        how = {"sent": f"sent via {msg.channel}", "awaiting_user_send": "ready — tap the link to send from your phone",
               "simulated": "simulated (sample listing, not sent)", "failed": f"FAILED: {msg.error}"}[msg.status]
        emit("communication", f"{msg.purpose.replace('_', ' ').title()} message {how} — {_label(l)}",
             status="error" if msg.status == "failed" else "done", search_id=state["search_id"], listing_id=l.id,
             data={"message_id": msg.id, "send_link": msg.send_link})
        if msg.status == "awaiting_user_send":
            notify("approval_needed", f"Tap to send your message — {_label(l)}", msg.send_link or "",
                   search_id=state["search_id"], listing_id=l.id, external=False)
    return {}


def route_after_send(state: DealState) -> str:
    return state.get("route_after_send") or "wait_reply"


def _unprocessed(db, listing_id: int) -> list[int]:
    return list(db.scalars(select(Message.id).where(Message.listing_id == listing_id, Message.direction == "in",
                                                    Message.extracted.is_(None)).order_by(Message.id)))


def wait_reply(state: DealState) -> dict:
    with session_scope() as db:
        pending = _unprocessed(db, state["listing_id"])
    if pending:
        return {"inbound_ids": pending, "timeout": False}
    answer = interrupt({"kind": "await_reply", "listing_id": state["listing_id"]})
    with session_scope() as db:
        pending = _unprocessed(db, state["listing_id"])
    if pending:
        return {"inbound_ids": pending, "timeout": False}
    return {"inbound_ids": [], "timeout": bool((answer or {}).get("timeout"))}


def route_after_wait(state: DealState) -> str:
    # Spurious resume (message already consumed by a concurrent resume) → keep waiting
    return "analyze_reply" if (state.get("inbound_ids") or state.get("timeout")) else "wait_reply"


def _bounds(db, search_id: int) -> NegotiationBounds:
    s = db.get(Search, search_id)
    return NegotiationBounds(**s.bounds).filled(SearchCriteria(**s.criteria))


def _asking(db, l: Listing) -> dict:
    n = db.scalar(select(Negotiation).where(Negotiation.listing_id == l.id))
    base = {"rent": l.rent, "deposit": l.deposit, "maintenance": l.maintenance, "brokerage": l.brokerage}
    if n and n.asking:
        base.update({k: v for k, v in n.asking.items() if v is not None})
    return {k: v for k, v in base.items() if v is not None}


def analyze_reply(state: DealState) -> Command:
    lid, sid = state["listing_id"], state["search_id"]
    if state.get("timeout"):
        with session_scope() as db:
            l = db.get(Listing, lid)
            if state.get("nudges", 0) >= 1:
                _set_status(db, l, "no_response")
                emit("communication", f"No reply after reminder — parking {_label(l)}", listing_id=lid, search_id=sid)
                return Command(goto=END, update={"outcome": "no_response"})
            s = db.get(Search, sid)
            subj, body = comm.compose(l, s, "followup", ["Just checking whether the flat is still available?"],
                                      extra="Gentle reminder about my earlier message.")
            msg = comm.create_draft(db, l, s, "followup", subj, body)
            if l.is_sample:
                msg.channel = "simulated"
            return Command(goto="approve_message", update={"draft_id": msg.id, "purpose": "followup",
                                                           "nudges": state.get("nudges", 0) + 1,
                                                           "route_after_send": "wait_reply"})
    with session_scope() as db:
        l, s = db.get(Listing, lid), db.get(Search, sid)
        n = db.scalar(select(Negotiation).where(Negotiation.listing_id == lid))
        if not n:
            n = Negotiation(listing_id=lid, search_id=sid, asking={}, history=[])
            db.add(n)
            db.flush()
        msgs = [db.get(Message, i) for i in state.get("inbound_ids", [])]
        text = "\n---\n".join(m.body for m in msgs)
        facts = comm.analyze_reply(text, l, n.our_last_offer).model_dump()
        for m in msgs:
            m.extracted = facts
        changed = verification.apply_reply_evidence(l, facts, msgs[-1].id if msgs else 0)
        # Quoted terms update the listing (owner's word beats the ad)
        quoted = {k: facts.get(k) for k in TERM_KEYS if facts.get(k) is not None}
        if facts.get("brokerage_free"):
            l.brokerage_free, quoted["brokerage"] = True, 0
        for k in ("rent", "deposit", "maintenance", "brokerage"):
            if k in quoted:
                setattr(l, k, quoted[k])
        if facts.get("furnishing"):
            l.furnishing = facts["furnishing"]
        if facts.get("parking"):
            l.parking = facts["parking"]
        if facts.get("tenant_restrictions"):
            l.tenant_pref = sorted(set(l.tenant_pref or []) | {r for r in facts["tenant_restrictions"]})
        n.asking = {**(n.asking or {}), **quoted}
        if facts.get("is_counter_offer") or (n.our_last_offer and quoted):
            n.their_last_offer = quoted
        n.history = (n.history or []) + [{"at": utcnow().isoformat(), "who": "them", "terms": quoted,
                                         "summary": facts.get("summary", "")[:200]}]
        l.confidence, l.risk_flags = verification.assess(db, l)
        r = matching.score_listing(l, SearchCriteria(**s.criteria), ScoringWeights(**s.weights))
        m = db.scalar(select(Match).where(Match.search_id == sid, Match.listing_id == lid))
        if m:
            m.score, m.breakdown, m.compromises, m.missing = r.score, r.breakdown, r.compromises, r.missing
            m.hard_fail, m.total_monthly_cost = r.hard_fail, r.total_monthly_cost
        label = _label(l)
        emit("communication", f"Reply from {label}: {facts.get('summary') or text[:120]}", listing_id=lid, search_id=sid,
             data={"facts": {k: v for k, v in facts.items() if v not in (None, [], False, '')}})
        notify("broker_reply", f"Reply received — {label}", (facts.get("summary") or text)[:300],
               search_id=sid, listing_id=lid, external=False)

        if changed == "unavailable":
            _set_status(db, l, "unavailable")
            notify("unavailable", f"No longer available — {label}", "", search_id=sid, listing_id=lid)
            return Command(goto=END, update={"outcome": "unavailable"})
        if facts.get("asks_for_advance_payment"):
            _set_status(db, l, "rejected")
            notify("scam_warning", f"Payment requested before visit — {label}",
                   "The contact asked for money before a visit/agreement. RentAgent has stopped this conversation. "
                   "Do not pay.", search_id=sid, listing_id=lid)
            return Command(goto=END, update={"outcome": "flagged_advance_payment"})
        phase = state.get("phase", "contacting")
        if phase == "scheduling":
            return Command(goto="confirm_visit")
        # Still missing essentials → targeted follow-up (bounded)
        need = [k for k in ("rent", "deposit") if _asking(db, l).get(k) is None]
        if l.availability != "confirmed":
            need.insert(0, "availability")
        if need and state.get("followups", 0) < 2:
            qs = comm.questions_for(need, first=False)
            subj, body = comm.compose(l, s, "followup", qs)
            msg = comm.create_draft(db, l, s, "followup", subj, body)
            if l.is_sample:
                msg.channel = "simulated"
            return Command(goto="approve_message", update={"draft_id": msg.id, "purpose": "followup",
                                                           "followups": state.get("followups", 0) + 1,
                                                           "route_after_send": "wait_reply"})
    return Command(goto="negotiate")


def negotiate(state: DealState) -> Command:
    lid, sid = state["listing_id"], state["search_id"]
    with session_scope() as db:
        l, s = db.get(Listing, lid), db.get(Search, sid)
        n = db.scalar(select(Negotiation).where(Negotiation.listing_id == lid))
        b = _bounds(db, sid)
        asking = _asking(db, l)
        facts = (db.get(Message, state["inbound_ids"][-1]).extracted if state.get("inbound_ids") else {}) or {}
        if facts.get("accepts_our_offer") and n.our_last_offer:
            n.state = "agreed_in_principle"
            notify("negotiated", f"Owner agreed to your terms — {_label(l)}",
                   ", ".join(f"{k}: {v}" for k, v in n.our_last_offer.items()), search_id=sid, listing_id=lid)
            emit("negotiation", f"Agreed in principle: {n.our_last_offer}", listing_id=lid, search_id=sid)
            n.asking = {**n.asking, **n.our_last_offer}
            for k in ("rent", "deposit", "maintenance", "brokerage"):
                if k in n.our_last_offer:
                    setattr(l, k, n.our_last_offer[k])
            return Command(goto="propose_visit", update={"phase": "scheduling"})
        d = nego.decide(b, asking, n.their_last_offer, n.our_last_offer, n.rounds,
                        they_refuse=bool(facts.get("rejects_negotiation")))
        emit("negotiation", f"Decision: {d.action} — {'; '.join(d.reasons + d.issues)}", listing_id=lid, search_id=sid,
             data={"offer": d.offer, "asks": d.asks})
        dd = d.__dict__
        if d.action == "no_negotiation_needed":
            n.state = "agreed_in_principle"
            return Command(goto="propose_visit", update={"phase": "scheduling", "decision": dd})
        if d.action == "escalate":
            return Command(goto="escalate", update={"decision": dd, "phase": "negotiating"})
        purpose = "accept" if d.action == "recommend_accept" else "offer"
        extra = nego.counter_text(d)
        subj, body = comm.compose(l, s, purpose, [], extra=extra)
        problems = nego.guard(body, d, b, asking)
        if problems:  # LLM phrasing drifted → deterministic text
            emit("negotiation", f"Writer output rejected by guard ({'; '.join(problems)}); using safe template",
                 level="warn", listing_id=lid, search_id=sid)
            greet = f"Hi {l.contact_name}," if l.contact_name else "Hi,"
            body = f"{greet}\n{comm.disclosure()}\n\n{extra}\n\nThank you."
        msg = comm.create_draft(db, l, s, purpose, subj, body)
        if l.is_sample:
            msg.channel = "simulated"
        n.our_last_offer = d.offer
        n.rounds += 1
        n.history = (n.history or []) + [{"at": utcnow().isoformat(), "who": "us", "terms": d.offer, "action": d.action}]
        _set_status(db, l, "negotiating")
        nxt = "propose_visit_after_accept" if purpose == "accept" else "wait_reply"
        return Command(goto="approve_message", update={"draft_id": msg.id, "purpose": purpose, "decision": dd,
                                                       "phase": "negotiating", "route_after_send": nxt})


def propose_visit_after_accept(state: DealState) -> Command:
    with session_scope() as db:
        n = db.scalar(select(Negotiation).where(Negotiation.listing_id == state["listing_id"]))
        n.state = "agreed_in_principle"
    return Command(goto="propose_visit", update={"phase": "scheduling"})


def escalate(state: DealState) -> Command:
    lid, sid = state["listing_id"], state["search_id"]
    d = state["decision"]
    with session_scope() as db:
        l = db.get(Listing, lid)
        appr = db.scalar(select(Approval).where(Approval.thread_id == _tid(lid), Approval.status == "pending",
                                                Approval.kind == "escalation"))
        if not appr:
            appr = Approval(kind="escalation", thread_id=_tid(lid), search_id=sid, listing_id=lid,
                            title=f"Decision needed — {_label(l)}",
                            payload={"issues": d.get("issues"), "asking": _asking(db, l),
                                     "bounds": _bounds(db, sid).model_dump(),
                                     "options": ["approve: proceed at their terms (you authorise exceeding limits)",
                                                 "edit: provide new bounds and re-negotiate",
                                                 "reject: walk away"]})
            db.add(appr)
            db.flush()
            notify("approval_needed", appr.title, "; ".join(d.get("issues") or []), search_id=sid, listing_id=lid)
        appr_id = appr.id
    answer = interrupt({"kind": "approval", "approval_id": appr_id, "purpose": "escalation"}) or {}
    with session_scope() as db:
        appr, l = db.get(Approval, appr_id), db.get(Listing, lid)
        appr.decided_at, appr.decision = utcnow(), answer
        n = db.scalar(select(Negotiation).where(Negotiation.listing_id == lid))
        if answer.get("decision") == "approve":
            appr.status = "approved"
            n.state = "agreed_in_principle"
            return Command(goto="propose_visit", update={"phase": "scheduling"})
        if answer.get("decision") == "edit" and answer.get("bounds"):
            appr.status = "edited"
            s = db.get(Search, sid)
            s.bounds = {**s.bounds, **answer["bounds"]}
            n.rounds = max(0, n.rounds - 1)
            return Command(goto="negotiate")
        appr.status = "rejected"
        n.state = "walked_away"
        _set_status(db, l, "rejected")
        emit("negotiation", f"Walked away from {_label(l)}", listing_id=lid, search_id=sid)
        return Command(goto=END, update={"outcome": "walked_away"})


def propose_visit(state: DealState) -> dict:
    lid, sid = state["listing_id"], state["search_id"]
    with session_scope() as db:
        l, s = db.get(Listing, lid), db.get(Search, sid)
        slots = scheduling.free_slots(db, n=3)
        if not slots:
            emit("scheduling", "No free visiting slots in the next 7 days — check calendar settings", level="warn",
                 listing_id=lid, search_id=sid)
        txt = "Could we visit at one of these times? " + "; ".join(scheduling.fmt_slot(t) for t in slots) + \
              ". Happy to adjust if another time suits you."
        subj, body = comm.compose(l, s, "visit", [], extra=txt)
        # numbers in slot text must survive; fall back to deterministic text if writer dropped them
        if not all(scheduling.fmt_slot(t).split(",")[0] in body for t in slots):
            body = f"{comm.disclosure()}\n\n{txt}\n\nThank you!"
        msg = comm.create_draft(db, l, s, "visit", subj, body)
        if l.is_sample:
            msg.channel = "simulated"
        emit("scheduling", f"Proposing visit slots for {_label(l)}: {', '.join(scheduling.fmt_slot(t) for t in slots)}",
             listing_id=lid, search_id=sid)
    return {"draft_id": msg.id, "purpose": "visit", "slots": [t.isoformat() for t in slots],
            "phase": "scheduling", "route_after_send": "wait_reply"}


def _pick_slot(text: str, facts: dict, slots: list[datetime]) -> datetime | None:
    for iso in facts.get("proposed_visit_times") or []:
        try:
            return dtparse.parse(iso).replace(tzinfo=None)
        except (ValueError, OverflowError):
            continue
    low = text.lower()
    for t in slots:
        if t.strftime("%A").lower() in low or t.strftime("%a").lower() + " " in low or t.strftime("%d %b").lower() in low:
            return t
    m = re.search(r"\b(first|second|third|1st|2nd|3rd|option\s*([123]))\b", low)
    if m:
        idx = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}.get(m.group(1), None)
        if idx is None and m.group(2):
            idx = int(m.group(2)) - 1
        if idx is not None and idx < len(slots):
            return slots[idx]
    if facts.get("confirms_visit") or re.search(r"\b(ok|okay|fine|sure|yes|works)\b", low):
        return slots[0] if slots else None
    return None


def confirm_visit(state: DealState) -> Command:
    lid, sid = state["listing_id"], state["search_id"]
    slots = [datetime.fromisoformat(x) for x in state.get("slots") or []]
    with session_scope() as db:
        l, s = db.get(Listing, lid), db.get(Search, sid)
        msgs = [db.get(Message, i) for i in state.get("inbound_ids", [])]
        text = "\n".join(m.body for m in msgs)
        facts = msgs[-1].extracted if msgs else {}
        t = _pick_slot(text, facts or {}, slots)
        if t is None:
            emit("scheduling", f"Couldn't determine a visit time from the reply — waiting for clarification",
                 level="warn", listing_id=lid, search_id=sid)
            return Command(goto="wait_reply")
        if not scheduling.is_free(db, t):
            emit("scheduling", f"Owner proposed {scheduling.fmt_slot(t)} but you're busy then; re-proposing",
                 listing_id=lid, search_id=sid)
            return Command(goto="propose_visit")
        mins = scheduling.get_prefs(db)["visit_minutes"]
        v = Visit(listing_id=lid, search_id=sid, start_at=t, end_at=t + timedelta(minutes=mins), status="confirmed")
        db.add(v)
        db.flush()
        _set_status(db, l, "visit_scheduled")
        subj, body = comm.compose(l, s, "visit_confirm", [],
                                  extra=f"Confirming the visit on {scheduling.fmt_slot(t)}. Please share the exact address / location pin.")
        if scheduling.fmt_slot(t).split(",")[0] not in body:
            body = f"{comm.disclosure()}\n\nConfirming the visit on {scheduling.fmt_slot(t)}. Please share the exact address / location pin. Thank you!"
        msg = comm.create_draft(db, l, s, "visit_confirm", subj, body)
        if l.is_sample:
            msg.channel = "simulated"
        notify("visit_scheduled", f"Visit booked: {scheduling.fmt_slot(t)} — {_label(l)}",
               l.address or l.locality or "", search_id=sid, listing_id=lid)
        emit("scheduling", f"Visit confirmed for {scheduling.fmt_slot(t)}", listing_id=lid, search_id=sid)
        return Command(goto="approve_message", update={"visit_id": v.id, "draft_id": msg.id, "purpose": "visit_confirm",
                                                       "phase": "visit_booked", "route_after_send": "wait_feedback"})


def wait_feedback(state: DealState) -> Command:
    answer = interrupt({"kind": "visit_feedback", "visit_id": state.get("visit_id")}) or {}
    lid, sid = state["listing_id"], state["search_id"]
    with session_scope() as db:
        l = db.get(Listing, lid)
        if answer.get("cancelled"):
            v = db.get(Visit, state.get("visit_id"))
            if v:
                v.status = "cancelled"
            return Command(goto="propose_visit")
        v = db.get(Visit, state.get("visit_id"))
        if v:
            v.status, v.feedback = "done", answer
        _set_status(db, l, "visited")
        emit("scheduling", f"Visit feedback recorded for {_label(l)}: {'liked' if answer.get('liked') else 'not liked'}",
             listing_id=lid, search_id=sid, data=answer)
        if not answer.get("liked"):
            _set_status(db, l, "rejected")
            return Command(goto=END, update={"outcome": "rejected_after_visit"})
        m = db.scalar(select(Match).where(Match.search_id == sid, Match.listing_id == lid))
        if m:
            m.shortlisted = True
        _set_status(db, l, "shortlisted")
        appr = Approval(kind="proceed_to_closure", thread_id=_tid(lid), search_id=sid, listing_id=lid,
                        title=f"Proceed to finalise — {_label(l)}?",
                        payload={"terms": _asking(db, l), "feedback": answer})
        db.add(appr)
        db.flush()
        appr_id = appr.id
        notify("approval_needed", appr.title, "Start agreement & documentation checklist.", search_id=sid, listing_id=lid)
    return Command(goto="approve_closure", update={"decision": {"approval_id": appr_id}})


def approve_closure(state: DealState) -> Command:
    appr_id = state["decision"]["approval_id"]
    answer = interrupt({"kind": "approval", "approval_id": appr_id, "purpose": "proceed_to_closure"}) or {}
    with session_scope() as db:
        appr = db.get(Approval, appr_id)
        appr.decided_at, appr.decision = utcnow(), answer
        appr.status = "approved" if answer.get("decision") == "approve" else "rejected"
        if appr.status == "rejected":
            return Command(goto=END, update={"outcome": "kept_on_shortlist"})
    return Command(goto="closure_prep")


def closure_prep(state: DealState) -> dict:
    lid, sid = state["listing_id"], state["search_id"]
    with session_scope() as db:
        l = db.get(Listing, lid)
        n = db.scalar(select(Negotiation).where(Negotiation.listing_id == lid))
        terms = {**_asking(db, l), **((n.our_last_offer or {}) if n and n.state == "agreed_in_principle" else {})}
        s = db.get(Search, sid)
        c = SearchCriteria(**s.criteria)
        terms.update({"lease_months": c.lease_months, "move_in_by": c.move_in_by.isoformat() if c.move_in_by else None,
                      "owner_name": l.contact_name, "address": l.address or ", ".join(filter(None, [l.society, l.locality, l.city]))})
        deal = db.scalar(select(Deal).where(Deal.listing_id == lid))
        if not deal:
            deal = Deal(listing_id=lid, search_id=sid, final_terms=terms, checklist=closure.checklist(l.city, terms),
                        stage="documents")
            db.add(deal)
        _set_status(db, l, "closing")
        emit("closure", f"Deal file opened for {_label(l)} — checklist with {len(deal.checklist)} items",
             listing_id=lid, search_id=sid)
    return {"phase": "closing"}


def wait_closure(state: DealState) -> dict:
    answer = interrupt({"kind": "closure", "listing_id": state["listing_id"]}) or {}
    with session_scope() as db:
        l = db.get(Listing, state["listing_id"])
        deal = db.scalar(select(Deal).where(Deal.listing_id == l.id))
        if answer.get("closed"):
            deal.stage = "closed"
            _set_status(db, l, "closed")
            s = db.get(Search, state["search_id"])
            s.status = "closed"
            emit("closure", f"Deal closed — {_label(l)}. Search marked complete.", listing_id=l.id,
                 search_id=state["search_id"])
            return {"phase": "done", "outcome": "closed"}
        deal.stage = "abandoned"
        _set_status(db, l, "rejected")
        return {"phase": "done", "outcome": "abandoned"}


# ------------------------------------------------------------------ graph

def build():
    g = StateGraph(DealState)
    g.add_node("draft_outreach", draft_outreach, retry_policy=DB_RETRY)
    g.add_node("approve_message", approve_message,
               destinations=("send_message", "approve_message", "wait_reply", END))
    g.add_node("send_message", send_message, retry_policy=DB_RETRY)
    g.add_node("wait_reply", wait_reply)
    g.add_node("analyze_reply", analyze_reply, destinations=("approve_message", "negotiate", "confirm_visit", END))
    g.add_node("negotiate", negotiate, destinations=("approve_message", "propose_visit", "escalate"))
    g.add_node("escalate", escalate, destinations=("propose_visit", "negotiate", END))
    g.add_node("propose_visit_after_accept", propose_visit_after_accept, destinations=("propose_visit",))
    g.add_node("propose_visit", propose_visit, retry_policy=DB_RETRY)
    g.add_node("confirm_visit", confirm_visit, destinations=("approve_message", "wait_reply", "propose_visit"))
    g.add_node("wait_feedback", wait_feedback, destinations=("approve_closure", "propose_visit", END))
    g.add_node("approve_closure", approve_closure, destinations=("closure_prep", END))
    g.add_node("closure_prep", closure_prep, retry_policy=DB_RETRY)
    g.add_node("wait_closure", wait_closure)

    g.add_edge(START, "draft_outreach")
    g.add_conditional_edges("draft_outreach", route_after_draft, ["approve_message", END])
    g.add_conditional_edges("send_message", route_after_send,
                            ["wait_reply", "wait_feedback", "propose_visit_after_accept"])
    g.add_conditional_edges("wait_reply", route_after_wait, ["analyze_reply", "wait_reply"])
    g.add_edge("propose_visit", "approve_message")
    g.add_edge("closure_prep", "wait_closure")
    g.add_edge("wait_closure", END)
    return g.compile(checkpointer=get_checkpointer())


_graph = None


def graph():
    global _graph
    if _graph is None:
        _graph = build()
    return _graph


def _cfg(lid: int) -> dict:
    return {"configurable": {"thread_id": _tid(lid)}}


def start(listing_id: int, search_id: int) -> dict | None:
    with thread_lock(_tid(listing_id)):
        st = graph().get_state(_cfg(listing_id))
        if st and st.values:  # already running/finished for this listing
            return None
        return graph().invoke({"listing_id": listing_id, "search_id": search_id}, _cfg(listing_id))


def pending_interrupt(listing_id: int) -> dict | None:
    st = graph().get_state(_cfg(listing_id))
    for t in (st.tasks if st else []):
        for it in t.interrupts:
            return it.value
    return None


def resume(listing_id: int, payload: dict, expect_kind: str | None = None) -> bool:
    """Resume a paused deal thread. Returns False if it's not waiting for this kind of input."""
    with thread_lock(_tid(listing_id)):
        pend = pending_interrupt(listing_id)
        if not pend:
            return False
        if expect_kind and pend.get("kind") != expect_kind:
            return False
        if pend.get("kind") == "approval" and payload.get("approval_id") not in (None, pend.get("approval_id")):
            return False
        graph().invoke(Command(resume=payload), _cfg(listing_id))
        return True


def resume_async(listing_id: int, payload: dict, expect_kind: str | None = None):
    return submit(resume, listing_id, payload, expect_kind)


def state(listing_id: int) -> dict:
    st = graph().get_state(_cfg(listing_id))
    return {"values": st.values if st else {}, "next": list(st.next) if st else [],
            "interrupt": pending_interrupt(listing_id)}
