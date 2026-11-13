# RentAgent — Architecture

An AI rental broker: the user states requirements once; a set of cooperating agents discovers,
verifies, ranks, contacts, negotiates, schedules visits and tracks closure — pausing for the user
at every consequential step.

---

## 1. System overview

```
┌──────────────────────── Next.js dashboard (TypeScript) ────────────────────────┐
│ New search (NL + form) · Listings & scores · Compare · Inbox · Approvals ·     │
│ Visits · Deal tracker · Live agent activity (WebSocket)                         │
└───────────────▲──────────────────────────────────────────────┬─────────────────┘
                │ REST + WS                                    │
┌───────────────┴──────────── FastAPI (app/main.py) ───────────▼─────────────────┐
│  API routes · WebSocket event stream · WhatsApp webhook · approvals            │
│                                                                                │
│  ┌──────────── LangGraph orchestrator (app/graph) ───────────────────────┐     │
│  │ SearchGraph (one thread per search)                                   │     │
│  │   intake → plan → [discover × (locality × source)] (parallel Send)    │     │
│  │   → dedupe → verify → score → notify → pick_outreach → spawn deals    │     │
│  │ DealGraph (one thread per listing)                                    │     │
│  │   draft → APPROVAL → send → WAIT_REPLY → analyze → verify/negotiate   │     │
│  │   → APPROVAL(offer) → schedule visit → WAIT_FEEDBACK → closure        │     │
│  │ Checkpointer: SQLite (dev) / Postgres (prod) → resumable threads      │     │
│  └───────────────────────────────────────────────────────────────────────┘     │
│  Agents: discovery · matching · verification · communication · negotiation ·   │
│          scheduling · closure      Services: LLM (Claude), channels, events    │
└───────▲───────────────────────▲────────────────────────▲───────────────────────┘
        │                       │                        │
   PostgreSQL / SQLite     Redis + Celery beat      External (authorized only)
   (system of record)      (periodic discovery,     Claude API + web search tool,
                            inbox polling,           IMAP/SMTP, WhatsApp Cloud API,
                            reminders)               partner feeds, ICS calendars
```

**Design principle:** LLMs do language (parsing requirements, extracting facts from messy text,
writing personalised messages, reviewing agreements). **Deterministic code owns every number and
every boundary** — scores, negotiation offers, limit checks, approval gates, scam rules. An LLM
can never move an offer past the user's ceiling because the offer is computed in code and the
generated message is validated against it before it can be sent.

Every LLM-backed function has a deterministic fallback, so the system runs (in reduced form)
without an API key and degrades gracefully if the API is unavailable.

---

## 2. Agent responsibilities

| Agent | Owns | Never does |
|---|---|---|
| **Intake** (`agents/intake.py`) | NL → `SearchCriteria`; fills defaults; reports what it could not infer | Invent localities or budgets the user didn't state |
| **1. Discovery** (`agents/discovery/`) | Runs source adapters per (locality × source) in parallel; normalises; fingerprints; dedupes; records provenance and `last_seen_at` | Fetch pages disallowed by robots.txt or the domain policy; log in; solve CAPTCHAs |
| **2. Matching** (`agents/matching.py`) | Weighted score with per-factor explanation; total monthly cost; compromises; missing info | Hide hard-constraint violations (they are flagged, and cap the score) |
| **3. Verification** (`agents/verification.py`) | Scam/staleness/duplicate signals; price anomaly vs comparables; confidence 0–100; availability state machine (`unverified → confirmed/unavailable`) driven only by real replies | Mark anything "available" or "owner verified" without evidence |
| **4. Communication** (`agents/communication.py`) | Personalised first contact with mandatory AI disclosure; follow-ups for missing facts; reply fact-extraction; full message log | Send first contact without approval; send to the same contact twice for the same listing; exceed daily outreach cap |
| **5. Negotiation** (`agents/negotiation.py`) | Concession strategy inside `NegotiationBounds`; counter-offers on rent/deposit/brokerage/lock-in/etc.; guard that validates every outgoing number | Exceed max rent/deposit/brokerage; accept or commit — acceptance is always a user approval |
| **6. Scheduling** (`agents/scheduling.py`) | Free slots from user windows minus ICS busy times; proposes slots; confirms visits; .ics invite; reminders; feedback → status | Book a slot that collides with a busy calendar block |
| **7. Closure** (`agents/closure.py`) | Final-terms sheet; jurisdiction-aware checklist (e.g. Maharashtra Leave & Licence registration); agreement review vs negotiated terms; document tracker | Transfer money, sign, or verify identity on its own — it lists what to verify and records the user's confirmation |

The orchestrator (LangGraph) owns sequencing, fan-out, retries, persistence, and the approval
interrupts. Agents communicate through **shared graph state** (ids, phase, facts) and the
**database** (full records), and announce progress through the **event bus** (`events` table +
WebSocket + Redis pub/sub).

---

## 3. LangGraph workflow

### 3.1 SearchGraph — thread id `search:{id}`

```
START → intake ─▶ plan_discovery ──Send()──▶ discover_one (×N, parallel, RetryPolicy 3×)
                                                 │  (reducer: operator.add on raw listings)
                                                 ▼
                         dedupe_and_store → verify → score → notify → pick_outreach → END
```
* Re-run periodically by Celery beat (`discovery_interval_minutes`) — the same thread
  accumulates; new listings that beat the current best trigger a "better property" notification.
* `pick_outreach` selects top-ranked, not-yet-contacted listings above `min_score_to_contact`,
  respecting `daily_outreach_cap`, and starts a DealGraph for each.

### 3.2 DealGraph — thread id `deal:{listing_id}`

```
START → draft_outreach → ⏸ approve_message ──reject──▶ END(skipped)
                               │approve/edit
                               ▼
                           send_message → ⏸ wait_reply ◀──────────────┐
                                              │ (resumed by inbound msg)│
                                              ▼                         │
                                         analyze_reply                  │
         ┌──────────────┬──────────────┬──────┴─────────┬──────────────┤
   unavailable     missing info     negotiate        ready for visit   │
     → END          → followup ─────────┘  │               │           │
                                     ⏸ approve offer   propose_slots   │
                                           │               │           │
                                       send_message ───────┴───────────┘
                         visit confirmed → ⏸ wait_visit_feedback
                                  liked → ⏸ approve_shortlist_to_close → closure_prep
                                         → ⏸ closure_actions (user-driven) → END(closed)
```

`⏸` = `interrupt()`. Two kinds of interrupts:
* **Approval** — creates an `approvals` row; dashboard shows it; `POST /approvals/{id}` resumes
  the thread with `Command(resume={"decision": "approve"|"reject"|"edit", ...})`.
* **External event** — waiting for an inbound message or visit feedback; the inbound handler
  (IMAP poller, WhatsApp webhook, manual paste) resumes `deal:{listing_id}`.

Because state is checkpointed after every node, a crash or deploy mid-workflow resumes exactly at
the last completed step (`GET /deals/{id}/state` shows the pending interrupt).

---

## 4. Data sources and their access restrictions

This is the hard constraint that shapes the product. Status as assessed for the Indian market:

| Source | Official API? | Automated access permitted? | How RentAgent uses it |
|---|---|---|---|
| 99acres, MagicBricks, Housing.com, NoBroker, SquareYards | No public listing API | Terms of use prohibit scraping/automated collection; contact details sit behind login | **Not scraped.** Listings reach the system via (a) the portals' own **email alerts** the user subscribes to, read from the user's mailbox over IMAP; (b) **web-search results** (titles/snippets/URLs) returned by Claude's web search tool; (c) the user pasting a URL or listing text. Pages on these domains are only fetched if robots.txt allows **and** the operator has whitelisted the domain in `DOMAIN_POLICY` after reviewing its terms. |
| Claude web search tool (`web_search_20260209`) | Yes (Anthropic API) | Yes | Primary automated discovery: per-locality searches; Claude returns structured leads with source URLs. |
| Portal email alerts (user's own inbox) | IMAP (standard) | Yes — user's own mail | `EmailAlertSource` parses alert emails into listings with links. |
| Partner / broker inventory feeds | Per agreement | Yes, under licence | `PartnerFeedSource`: JSON/CSV/XML feeds from brokers or societies that share inventory. |
| User-supplied URLs / text | — | User-initiated, single page, robots-checked | `UrlImportSource` (httpx; Playwright for JS-rendered pages on allowed domains). |
| Facebook groups / Telegram | Groups API deprecated | No automated access | Out of scope; user can paste posts. |

Communication channels:

| Channel | Constraint | Implementation |
|---|---|---|
| Email (SMTP/IMAP) | Normal deliverability rules | Fully automated send/receive after approval. |
| WhatsApp Cloud API | Business-initiated messages to people who haven't messaged you require **opt-in and a pre-approved template**; free-form only inside the 24 h customer-service window | Used for replies once the owner has messaged the business number. Webhook ingests inbound. |
| Assisted WhatsApp/SMS/call | None — user's own phone | Agent drafts; user taps a `wa.me` link to send from their own number; replies are pasted/forwarded back. This is the default for cold first contact on WhatsApp. |

Anti-spam controls: one first-contact per (listing, contact), per-contact cooldown, daily outreach
cap, every first contact approved by the user, AI disclosure inserted by code (not left to the model).

---

## 5. Database schema (SQLAlchemy, `app/models.py`)

```
searches(id, title, raw_query, criteria JSON, bounds JSON, weights JSON, status, created_at, last_run_at)
listings(id, fingerprint UNIQUE, title, city, locality, address, lat, lng, bhk, property_type,
         rent, deposit, maintenance, brokerage, area_sqft, furnishing, bathrooms, parking,
         amenities JSON, tenant_pref, available_from, description, photos JSON,
         contact_name, contact_phone, contact_email, contact_role, posted_at,
         first_seen_at, last_seen_at, availability(unverified|confirmed|unavailable),
         confidence, risk_flags JSON, is_sample, status)
listing_sources(id, listing_id→, source, url, external_id, raw JSON, fetched_at)    -- provenance
matches(id, search_id→, listing_id→, score, breakdown JSON, compromises JSON, missing JSON,
        total_monthly_cost, hard_fail, shortlisted, rank, updated_at)  UNIQUE(search,listing)
contacts(id, name, phone, email, role, do_not_contact, last_contacted_at)
messages(id, listing_id→, search_id→, direction(out|in), channel, to_addr, from_addr, subject,
         body, status(draft|pending_approval|sent|failed|received), extracted JSON, external_id, created_at)
negotiations(id, listing_id→, search_id→, state, current_terms JSON, our_last_offer JSON,
             their_last_offer JSON, rounds INT, outcome, updated_at)
visits(id, listing_id→, search_id→, start_at, end_at, status(proposed|confirmed|done|cancelled),
       reminder_sent, feedback JSON)
deals(id, listing_id→, search_id→, stage, final_terms JSON, checklist JSON, documents JSON,
      agreement_review JSON, updated_at)
approvals(id, kind, listing_id, search_id, thread_id, title, payload JSON, status, decided_at, decision JSON)
events(id, ts, agent, level, search_id, listing_id, message, data JSON)          -- activity feed
notifications(id, ts, kind, title, body, search_id, listing_id, read, delivered JSON)
settings(key, value JSON)                                                     -- calendar, channels, caps
```
LangGraph checkpoints live in their own tables (`checkpoints`, `writes`) in the same DB.

**pgvector:** not used in v1. Duplicate detection uses perceptual image hashes (`imagehash`) and
text shingle similarity, which are deterministic and need no embedding provider (Claude has no
embedding endpoint). pgvector becomes worthwhile in phase 4 for "find listings similar to the one
I liked", using an embeddings provider — the schema adds an `embedding vector(1024)` column then.

---

## 6. Human approval checkpoints

| Checkpoint | Trigger | Default |
|---|---|---|
| `first_contact` | Any first message to an owner/broker | Always required |
| `followup` | Info-gathering follow-up in an existing thread | Auto if `auto_followups=true`, else required |
| `offer` | Any message containing a counter-offer / commercial term | Always required |
| `accept_terms` | Agent believes terms are acceptable | Always required (non-binding "in principle" wording only) |
| `visit_slot` | Proposing/confirming a visit time | Auto if slot is free in calendar, else required |
| `proceed_to_closure` | After positive visit feedback | Always required |
| `closure_action` | Token/deposit payment, signing, sharing ID documents | **Never automated** — the system records the user's own action |

Approvals support approve / reject / edit-then-approve; edited text is re-validated by the
negotiation guard (numbers) and the disclosure check before sending.

---

## 7. Scoring engine

`score = Σ wᵢ · sᵢ` over factors with `sᵢ ∈ [0,1]`, weights configurable per search and normalised to
100. Factors: budget (on total monthly cost), location, condition, furnishing, amenities,
deposit & brokerage, connectivity, plus a verification-confidence modifier. Hard-constraint
violations (over max rent, wrong BHK, tenant-type mismatch) cap the score at 40 and are flagged.
Unknown values score neutral (0.5) and are listed under "missing info" for the Communication agent
to ask about. Each factor returns a human-readable reason shown in the UI.

---

## 8. Technology choices

| Layer | Choice | Note |
|---|---|---|
| Frontend | Next.js 14 + TypeScript | As suggested. |
| API | FastAPI | As suggested. |
| Orchestration | LangGraph + SQLite/Postgres checkpointer | As suggested. |
| LLM | Claude (`claude-opus-5-5`) with structured outputs and server-side refusal fallbacks | Effort `low` for extraction, `medium` for writing/review. |
| Discovery | Claude web search tool, IMAP alerts, feeds, httpx/Playwright for permitted pages | Playwright only on whitelisted domains. |
| DB | PostgreSQL (prod), SQLite (dev / single user) | One `DATABASE_URL`. |
| Jobs | Celery + Redis beat; in-process scheduler fallback | Fallback so a single-machine install works without Redis. |
| Realtime | WebSocket; Redis pub/sub across processes | |
| Notifications | In-app + email; WhatsApp only to the user's own opted-in number | |
| Vector search | Deferred (see §5) | |

---

## 9. Phased roadmap

1. **Foundation (this release)** — schema, intake, scoring, verification rules, discovery
   adapters (web search, email alerts, feeds, URL import), communication (email, WhatsApp Cloud,
   assisted), negotiation engine + guard, scheduling, closure checklist + agreement review,
   both LangGraph graphs with approvals and resume, dashboard, notifications, tests.
2. **Reliability** — per-source extraction eval sets, Postgres + Celery production deploy,
   observability (OpenTelemetry), backoff/circuit breakers per source, multi-user auth.
3. **Reach** — partner-feed onboarding for brokers, WhatsApp Business templates approved for
   follow-ups, Google Calendar OAuth, voice-call summaries the user records.
4. **Intelligence** — locality rent index from accumulated data, similar-listing search
   (pgvector), negotiation outcome learning, commute-time scoring via a maps API.
5. **Closure partners** — e-registration / e-stamping partner integrations where officially
   available; document vault with encryption at rest.
