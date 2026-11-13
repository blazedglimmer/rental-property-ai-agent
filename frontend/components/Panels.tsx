"use client";
import Link from "next/link";
import { Fragment, useState } from "react";
import { api, Approval, Deal, Event, inr, label, Listing, localWhen, Visit, when } from "@/lib/api";

export function ScoreCell({ l }: { l: Listing }) {
  const s = l.match?.score ?? 0;
  return (
    <span className="score" title={l.match?.hard_fail ? "Fails a hard requirement" : ""}>
      {s.toFixed(0)}<span className="bar"><i style={{ width: `${s}%`, background: l.match?.hard_fail ? "var(--bad)" : s >= 75 ? "var(--good)" : undefined }} /></span>
    </span>
  );
}

export function TrustChip({ l }: { l: Listing }) {
  const c = l.confidence;
  return <span className={`chip ${c >= 70 ? "good" : c >= 40 ? "warn" : "bad"}`} title={(l.risk_flags || []).map((f) => f.message).join("\n")}>trust {c.toFixed(0)}</span>;
}

export function AvailChip({ a }: { a: string }) {
  return <span className={`chip ${a === "confirmed" ? "good" : a === "unavailable" ? "bad" : ""}`}>{a === "confirmed" ? "availability confirmed" : a}</span>;
}

export function Breakdown({ l }: { l: Listing }) {
  if (!l.match) return null;
  return (
    <div className="stack small">
      <table><thead><tr><th>Factor</th><th>Weight</th><th>Points</th><th>Why</th></tr></thead>
        <tbody>{l.match.breakdown.map((b) => (
          <tr key={b.factor}><td>{b.factor.replace("_", " & ")}</td><td>{b.weight}</td><td>{b.points}</td><td>{b.reason}</td></tr>
        ))}</tbody></table>
      {l.match.compromises.length > 0 && <div><b>Compromises:</b> {l.match.compromises.join(" · ")}</div>}
      {l.match.missing.length > 0 && <div><b>Unknown (agent will ask):</b> {l.match.missing.join(", ")}</div>}
      {(l.risk_flags || []).length > 0 && <div><b>Verification flags:</b> {(l.risk_flags).map((f) => <div key={f.code + f.message} className={f.severity === "high" ? "err" : ""}>• {f.message}</div>)}</div>}
    </div>
  );
}

export function ListingsTable({ rows, sid, reload, selected, setSelected }:
  { rows: Listing[]; sid: number; reload: () => void; selected: number[]; setSelected: (s: number[]) => void }) {
  const [open, setOpen] = useState<number | null>(null);
  const act = async (path: string) => { await api.post(path); reload(); };
  if (!rows.length) return <div className="panel muted">No listings yet — discovery is running or no sources are configured. You can also import a listing (Import tab).</div>;
  return (
    <div className="panel tablewrap">
      <table>
        <thead><tr><th></th><th>Score</th><th>Property</th><th>Rent / total</th><th>Deposit</th><th>Status</th><th>Trust</th><th></th></tr></thead>
        <tbody>
          {rows.map((l) => (
            <Fragment key={l.id}>
              <tr style={{ opacity: l.match?.hard_fail || l.availability === "unavailable" ? 0.55 : 1 }}>
                <td><input type="checkbox" style={{ width: "auto" }} checked={selected.includes(l.id)}
                  onChange={(e) => setSelected(e.target.checked ? [...selected, l.id] : selected.filter((x) => x !== l.id))} /></td>
                <td><ScoreCell l={l} /></td>
                <td><Link href={`/listing/${l.id}?search=${sid}`}><b>{label(l)}</b></Link>
                  {l.is_sample && <span className="chip warn" style={{ marginLeft: 6 }}>SAMPLE</span>}
                  <div className="small muted">{[l.furnishing, l.area_sqft && `${l.area_sqft} sqft`, l.contact_role, l.source].filter(Boolean).join(" · ")}</div></td>
                <td>{inr(l.rent)}<div className="small muted">≈ {inr(l.match?.total_monthly_cost)}/mo all-in</div></td>
                <td>{inr(l.deposit)}</td>
                <td><span className="chip">{l.status.replace(/_/g, " ")}</span><div><AvailChip a={l.availability} /></div></td>
                <td><TrustChip l={l} /></td>
                <td className="row" style={{ flexWrap: "nowrap" }}>
                  <button onClick={() => setOpen(open === l.id ? null : l.id)}>Why?</button>
                  <button onClick={() => act(`/listings/${l.id}/shortlist?search_id=${sid}&value=${!l.match?.shortlisted}`)}>{l.match?.shortlisted ? "★" : "☆"}</button>
                  {l.status === "new" && <button onClick={() => act(`/listings/${l.id}/contact?search_id=${sid}`)} title="Draft a first message (needs your approval)">Contact</button>}
                  <button onClick={() => act(`/listings/${l.id}/dismiss?search_id=${sid}`)} title="Hide">✕</button>
                </td>
              </tr>
              {open === l.id && <tr><td colSpan={8}><Breakdown l={l} /></td></tr>}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Compare({ ids, sid }: { ids: number[]; sid: number }) {
  const [rows, setRows] = useState<any[] | null>(null);
  const [err, setErr] = useState("");
  const load = () => api.get<any[]>(`/compare?search_id=${sid}&ids=${ids.join(",")}`).then(setRows).catch((e) => setErr(e.message));
  if (ids.length < 2) return <div className="panel muted">Tick 2–5 listings in the Listings tab (or ★ shortlist them) to compare.</div>;
  if (!rows) { load(); return <div className="panel muted">{err || "Loading…"}</div>; }
  const fields: [string, (l: any) => any][] = [
    ["Score", (l) => l.match?.score?.toFixed(0)], ["Rent", (l) => inr(l.rent)], ["All-in monthly", (l) => inr(l.match?.total_monthly_cost)],
    ["Deposit", (l) => inr(l.deposit)], ["Maintenance", (l) => inr(l.maintenance)],
    ["Brokerage", (l) => (l.brokerage_free ? "None" : inr(l.brokerage))], ["Locality", (l) => l.locality], ["Society", (l) => l.society],
    ["Area", (l) => l.area_sqft && `${l.area_sqft} sqft`], ["Furnishing", (l) => l.furnishing], ["Parking", (l) => l.parking],
    ["Amenities", (l) => (l.amenities || []).join(", ")], ["Tenant pref.", (l) => (l.tenant_pref || []).join(", ")],
    ["Availability", (l) => l.availability], ["Trust", (l) => l.confidence?.toFixed(0)],
    ["Negotiated", (l) => l.negotiation?.our_last_offer ? Object.entries(l.negotiation.our_last_offer).map(([k, v]) => `${k}: ${v}`).join(", ") : "—"],
    ["Compromises", (l) => (l.match?.compromises || []).join("; ")], ["Unknown", (l) => (l.match?.missing || []).join(", ")],
    ["Visit", (l) => l.visits?.[0] ? `${l.visits[0].status} ${localWhen(l.visits[0].start_at)}` : "—"],
  ];
  return (
    <div className="panel tablewrap"><table>
      <thead><tr><th></th>{rows.map((l) => <th key={l.id}><Link href={`/listing/${l.id}?search=${sid}`}>{label(l)}</Link></th>)}</tr></thead>
      <tbody>{fields.map(([k, f]) => <tr key={k}><th>{k}</th>{rows.map((l) => <td key={l.id}>{f(l) ?? "—"}</td>)}</tr>)}</tbody>
    </table></div>
  );
}

export function Approvals({ items, reload }: { items: Approval[]; reload: () => void }) {
  if (!items.length) return <div className="panel muted">Nothing needs your approval right now.</div>;
  return <div className="stack">{items.map((a) => <ApprovalCard key={a.id} a={a} reload={reload} />)}</div>;
}

function ApprovalCard({ a, reload }: { a: Approval; reload: () => void }) {
  const [body, setBody] = useState<string>(a.payload?.body ?? "");
  const [bounds, setBounds] = useState<string>(JSON.stringify({ max_rent: a.payload?.bounds?.max_rent }, null, 0));
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async (decision: string, extra: any = {}) => {
    setBusy(true); setErr("");
    try { await api.post(`/approvals/${a.id}`, { decision, ...extra }); reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const edited = body !== (a.payload?.body ?? "");
  return (
    <div className="panel stack">
      <div className="row between"><b>{a.title}</b><span className="chip warn">{a.kind.replace(/_/g, " ")}</span></div>
      {a.payload?.guard_problems && <div className="err small">Guard: {a.payload.guard_problems.join("; ")}</div>}
      {a.payload?.body != null && (
        <>
          <div className="small muted">Via {a.payload.channel}{a.payload.to ? ` to ${a.payload.to}` : ""}. Edits are re-checked against your limits.</div>
          <textarea value={body} onChange={(e) => setBody(e.target.value)} style={{ minHeight: 140 }} />
        </>
      )}
      {a.kind === "escalation" && (
        <div className="small stack">
          {(a.payload.issues || []).map((i: string) => <div key={i} className="err">• {i}</div>)}
          <div>Their terms: {JSON.stringify(a.payload.asking)}</div>
          <label className="f"><span>New limits (JSON) for “Re-negotiate”</span><input value={bounds} onChange={(e) => setBounds(e.target.value)} /></label>
        </div>
      )}
      {a.kind === "proceed_to_closure" && <div className="small">Terms: {JSON.stringify(a.payload.terms)}</div>}
      {err && <div className="err">{err}</div>}
      <div className="row">
        {a.kind === "escalation" ? (
          <>
            <button className="primary" disabled={busy} onClick={() => send("approve")}>Accept their terms</button>
            <button disabled={busy} onClick={() => { try { send("edit", { bounds: JSON.parse(bounds) }); } catch { setErr("Invalid JSON"); } }}>Re-negotiate with new limits</button>
            <button className="danger" disabled={busy} onClick={() => send("reject")}>Walk away</button>
          </>
        ) : (
          <>
            <button className="primary" disabled={busy} onClick={() => (edited ? send("edit", { body }) : send("approve"))}>{edited ? "Send edited" : "Approve"}</button>
            <button className="danger" disabled={busy} onClick={() => send("reject")}>Reject</button>
          </>
        )}
        <Link className="small" href={`/listing/${a.listing_id}?search=${a.search_id}`}>open listing</Link>
      </div>
    </div>
  );
}

export function Visits({ items, reload }: { items: Visit[]; reload: () => void }) {
  if (!items.length) return <div className="panel muted">No visits yet. The scheduling agent proposes slots from your calendar once terms are workable.</div>;
  return (
    <div className="stack">{items.map((v) => <VisitCard key={v.id} v={v} reload={reload} />)}</div>
  );
}

function VisitCard({ v, reload }: { v: Visit; reload: () => void }) {
  const [notes, setNotes] = useState("");
  const [rating, setRating] = useState(4);
  const fb = async (liked: boolean, cancelled = false) => { await api.post(`/visits/${v.id}/feedback`, { liked, rating, notes, cancelled }); reload(); };
  return (
    <div className="panel stack">
      <div className="row between"><b>{localWhen(v.start_at)} — <Link href={`/listing/${v.listing_id}?search=${v.search_id}`}>{label(v.listing)}</Link></b>
        <span className="chip">{v.status}</span></div>
      <div className="small muted">{v.listing.address || v.listing.locality} · {v.listing.contact_name} {v.listing.contact_phone} · <a href={`/api/visits/${v.id}/ics`}>Add to calendar</a></div>
      <details><summary className="small">Visit checklist</summary><ul className="small">{v.checklist.map((c) => <li key={c}>{c}</li>)}</ul></details>
      {v.status === "confirmed" && (
        <div className="row">
          <input placeholder="Notes after the visit" value={notes} onChange={(e) => setNotes(e.target.value)} style={{ flex: 1, minWidth: 200 }} />
          <select value={rating} onChange={(e) => setRating(Number(e.target.value))} style={{ width: 70 }}>{[1, 2, 3, 4, 5].map((n) => <option key={n}>{n}</option>)}</select>
          <button className="primary" onClick={() => fb(true)}>Liked it</button>
          <button onClick={() => fb(false)}>Not for me</button>
          <button onClick={() => fb(false, true)}>Reschedule</button>
        </div>
      )}
      {v.feedback && <div className="small">Feedback: {v.feedback.liked ? "👍" : "👎"} {v.feedback.rating}/5 {v.feedback.notes}</div>}
    </div>
  );
}

export function DealPanel({ d, reload }: { d: Deal; reload: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const tick = async (key: string, done: boolean) => { await api.patch(`/deals/${d.listing_id}/checklist/${key}`, { done }); reload(); };
  const review = async () => { setBusy(true); try { await api.post(`/deals/${d.listing_id}/agreement`, { text }); reload(); } finally { setBusy(false); } };
  const close = async (closed: boolean) => { await api.post(`/deals/${d.listing_id}/close`, { closed }); reload(); };
  const done = d.checklist.filter((i) => i.done).length;
  const r = d.agreement_review;
  return (
    <div className="panel stack">
      <div className="row between"><b>{d.listing ? label(d.listing) : `Listing ${d.listing_id}`}</b><span className="chip">{d.stage}</span></div>
      <div className="small">Final terms: {Object.entries(d.final_terms).filter(([, v]) => v != null).map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "number" && v >= 500 ? inr(v) : v}`).join(" · ")}</div>
      <div className="row"><b>Checklist {done}/{d.checklist.length}</b><span className="bar" style={{ width: 160 }}><i style={{ width: `${(100 * done) / d.checklist.length}%` }} /></span></div>
      <div className="small">{d.checklist.map((i) => (
        <label key={i.key} className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
          <input type="checkbox" style={{ width: "auto", marginTop: 4 }} checked={i.done} onChange={(e) => tick(i.key, e.target.checked)} />
          <span>{i.label} {i.owner === "user" && <span className="chip">you</span>}</span>
        </label>))}</div>
      <div className="banner small">RentAgent never pays, signs, or vouches for identity. Pay the deposit only to the owner's own bank account after verifying ownership documents.</div>
      <label className="f"><span>Paste the draft agreement for review</span><textarea value={text} onChange={(e) => setText(e.target.value)} /></label>
      <div className="row"><button onClick={review} disabled={busy || !text}>Review agreement</button>
        {d.stage !== "closed" && <><button className="primary" onClick={() => close(true)}>Mark deal closed</button><button className="danger" onClick={() => close(false)}>Abandon</button></>}</div>
      {r && (
        <div className="small stack">
          <b>Review ({r.method}):</b> {r.summary}
          {(r.discrepancies || []).map((x: any, i: number) => <div key={i} className="err">≠ {x.clause}: {x.issue} → {x.suggestion}</div>)}
          {(r.risky_clauses || []).map((x: any, i: number) => <div key={i}>⚠ {x.issue}: “{x.clause.slice(0, 160)}”</div>)}
          {(r.missing_clauses || []).length > 0 && <div>Missing: {r.missing_clauses.join(", ")}</div>}
        </div>
      )}
    </div>
  );
}

export function Activity({ events, connected }: { events: Event[]; connected: boolean }) {
  return (
    <div className="panel">
      <div className="row between"><h2>Agent activity</h2><span className="small muted"><span className={`dot ${connected ? "done" : "error"}`} /> {connected ? "live" : "reconnecting"}</span></div>
      <div className="feed">
        {events.map((e) => (
          <div className="ev" key={e.id}>
            <span className="small muted">{when(e.ts).split(", ").pop()}</span>
            <span><span className={`dot ${e.status}`} /> <b className="small">{e.agent}</b></span>
            <span className={e.level === "error" ? "err" : ""}>{e.message}
              {e.data?.send_link && <> · <a href={e.data.send_link} target="_blank" rel="noreferrer">send from your phone</a></>}
            </span>
          </div>
        ))}
        {!events.length && <div className="muted">Waiting for agent activity…</div>}
      </div>
    </div>
  );
}

export function ImportBox({ sid, done }: { sid: number; done: () => void }) {
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");
  const go = async () => {
    setMsg("Importing…");
    try { const r = await api.post<any>("/import", { search_id: sid, url: url || null, text: text || null }); setMsg(`Imported listing(s) ${r.listing_ids.join(", ")}`); done(); }
    catch (e: any) { setMsg(e.message); }
  };
  return (
    <div className="panel stack">
      <h2>Add a listing you found</h2>
      <div className="small muted">Paste a listing URL (fetched only if the site's robots.txt allows it) or paste the listing text/WhatsApp forward. Portals that forbid automated access: paste the text.</div>
      <input placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} />
      <textarea placeholder="…or paste listing text" value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row"><button className="primary" onClick={go} disabled={!url && !text}>Import & score</button><span className="small">{msg}</span></div>
    </div>
  );
}
