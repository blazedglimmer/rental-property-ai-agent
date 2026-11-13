# RentAgent — AI rental broker

You describe the home you want once. A team of agents finds listings, checks them, ranks them, contacts
owners and brokers, negotiates within your limits, books visits around your calendar and tracks the
paperwork. You approve every message that commits you to anything.

* Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) covers the architecture, each agent's job, the LangGraph
  workflows, the data sources and their access limits, the schema, the approval checkpoints and the roadmap.
* Backend: `backend/` (FastAPI, LangGraph, SQLAlchemy, Claude)
* Dashboard: `frontend/` (Next.js 14, TypeScript)

## Quick start (single machine, no Docker)

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env        # add ANTHROPIC_API_KEY, plus SMTP/IMAP if you want email outreach
uvicorn app.main:app --port 8000
```

```bash
cd frontend
npm install
npm run build
npm start                   # http://localhost:3000
```

Without Redis the API runs the background loop in its own process. That loop polls for replies, sends
follow-ups when no one answers, sends visit reminders and re-runs discovery every
`DISCOVERY_INTERVAL_MINUTES`.

## Production

```bash
docker compose up --build   # Postgres + Redis + API + Celery worker/beat + dashboard
```

## Try it without any credentials

In **New search**, open *Discovery sources* and tick **Include SAMPLE Pune listings**. These listings are
marked SAMPLE everywhere, and messages to them are simulated, never sent. The steps:

1. Start the search. You get 10 listings ranked with an explanation for each score. The fake "army
   officer" listing gets trust 0, and the copied listing is flagged.
2. **Approvals** shows the drafted first messages. Approve one.
3. Open the listing and type the owner's reply. For example: *"Available. Rent 22000, deposit 2 months,
   no brokerage."* The agent drafts a counter-offer that stays within your maximum and asks for your approval.
4. Reply *"Ok agreed"*. The agent proposes visit slots from your free time.
5. Reply *"First option works"*. The visit is booked, and an .ics file is available.
6. Under **Visits**, choose *Liked it*, then approve *Proceed*. **Deal** opens a checklist for your city
   (Maharashtra includes Leave & Licence registration) and checks pasted agreements for discrepancies.

Without `ANTHROPIC_API_KEY`, rule-based fallbacks do the parsing and extraction, and messages come from
templates. With the key, Claude does this work and adds web-search discovery.

## Where real listings come from

| Source | Setup |
|---|---|
| Claude web search | `ANTHROPIC_API_KEY`. Runs once per locality. Only URLs that appear in the actual search results are kept. |
| Portal email alerts | Create saved-search alerts on 99acres, MagicBricks, Housing.com, NoBroker and similar sites, sent to the mailbox in `IMAP_*`. |
| Partner feeds | JSON or CSV feed URL in the search's sources (`{"type":"partner_feed","url":...}`). |
| Your own finds | **Import** tab: paste a URL (fetched only if robots.txt allows it) or the listing text. |

The system does not scrape portals whose terms forbid it. It does not log in to them or get around
CAPTCHAs.

## How messages are sent

* **Email**: sent automatically through SMTP once you approve. Replies are read over IMAP and matched to
  the listing.
* **WhatsApp Cloud API**: used once an owner has messaged your business number, inside WhatsApp's
  24-hour window. Point the webhook at `POST /webhooks/whatsapp`.
* **Assisted**: the default for cold WhatsApp contact. You get a `wa.me` link and send the message from
  your own phone. Paste the reply back on the listing page.

## Safety guarantees (enforced in code, not left to the model)

* Every message starts with a sentence saying it comes from an AI assistant.
* No first contact, offer or acceptance goes out without your approval.
* Offers are calculated deterministically within `NegotiationBounds`. A guard rejects any outgoing text
  that contains an unexpected amount or commitment wording, including text you edited.
* Acceptances are always worded as "in principle, subject to visit and agreement".
* A listing is marked available only after the owner or broker confirms it in a reply.
* A conversation stops if the other side asks for money before a visit.
* Outreach is limited per day, each contact has a cooldown, and a contact who says "stop" is never
  messaged again.
* The system never pays, signs or vouches for anyone's identity.

## Tests

```bash
cd backend && python -m pytest -q
```

`tests/test_workflow.py` runs the whole flow through both LangGraph graphs and the HTTP API: discovery,
verification, scoring, approval, reply, counter-offer, acceptance, visit, feedback, closure, agreement
review and close. It also covers the "unavailable" and "advance payment" stops.
