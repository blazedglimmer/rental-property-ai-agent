"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Approvals, AvailChip, Breakdown, DealPanel, ScoreCell, TrustChip } from "@/components/Panels";
import { api, inr, label, Message, when } from "@/lib/api";
import { useLive } from "@/lib/useLive";

export default function ListingPage({ params }: { params: { id: string } }) {
  const lid = Number(params.id);
  const sid = Number(useSearchParams().get("search")) || undefined;
  const [d, setD] = useState<any>(null);
  const [reply, setReply] = useState("");
  const [err, setErr] = useState("");
  const load = useCallback(() => api.get<any>(`/listings/${lid}${sid ? `?search_id=${sid}` : ""}`).then(setD).catch((e) => setErr(e.message)), [lid, sid]);
  useEffect(() => { load(); }, [load]);
  useLive((e) => { if (e.listing_id === lid) setTimeout(load, 300); });

  if (!d) return <div className="muted">{err || "Loading…"}</div>;
  const wf = d.workflow?.interrupt;
  const submitReply = async () => {
    setErr("");
    try { await api.post(`/listings/${lid}/inbound`, { body: reply }); setReply(""); setTimeout(load, 800); } catch (e: any) { setErr(e.message); }
  };
  const markSent = async (m: Message) => { await api.post(`/messages/${m.id}/mark_sent`); load(); };

  return (
    <div className="stack">
      {sid && <Link href={`/search/${sid}`} className="small">← back to search</Link>}
      <div className="row between">
        <div><h1>{label(d)} {d.is_sample && <span className="chip warn">SAMPLE — not a real listing</span>}</h1>
          <div className="muted">{[d.address, d.locality, d.city].filter(Boolean).join(", ")}</div></div>
        <div className="row">{d.match && <ScoreCell l={d} />}<TrustChip l={d} /><AvailChip a={d.availability} /><span className="chip">{d.status.replace(/_/g, " ")}</span></div>
      </div>
      {wf && <div className="banner small">Workflow: {wf.kind === "approval" ? "waiting for your approval" : wf.kind === "await_reply" ? "waiting for the owner/broker to reply" :
        wf.kind === "visit_feedback" ? "waiting for your visit feedback (Visits tab)" : wf.kind === "closure" ? "in closure — complete the checklist" : wf.kind}</div>}

      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="panel stack">
          <h2>Details</h2>
          <div className="fields small">
            {[["Rent", inr(d.rent)], ["Deposit", inr(d.deposit)], ["Maintenance", inr(d.maintenance)], ["Brokerage", d.brokerage_free ? "None" : inr(d.brokerage)],
              ["All-in monthly", inr(d.match?.total_monthly_cost)], ["Area", d.area_sqft ? `${d.area_sqft} sqft` : "—"], ["Furnishing", d.furnishing ?? "—"],
              ["Parking", d.parking ?? "—"], ["Bathrooms", d.bathrooms ?? "—"], ["Tenants", (d.tenant_pref || []).join(", ") || "—"],
              ["Contact", `${d.contact_name ?? "—"} (${d.contact_role ?? "?"})`], ["Phone / email", d.contact_phone || d.contact_email || "—"]].map(([k, v]) => (
              <div key={k as string}><div className="muted">{k}</div><b>{v}</b></div>))}
          </div>
          <div className="small"><b>Amenities:</b> {(d.amenities || []).join(", ") || "—"}</div>
          {d.description && <div className="small" style={{ whiteSpace: "pre-wrap" }}>{d.description}</div>}
          <div className="small muted">Sources: {d.sources.map((s: any) => <span key={s.id}>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.source}</a> : s.source} </span>)} · first seen {when(d.first_seen_at)}</div>
          {d.availability_evidence && <div className="small muted">Availability evidence: {JSON.stringify(d.availability_evidence)}</div>}
          <h2>Why this score</h2>
          <Breakdown l={d} />
        </div>

        <div className="stack">
          <div className="panel stack">
            <h2>Conversation</h2>
            {d.messages.length === 0 && <div className="muted small">No messages yet.</div>}
            {d.messages.map((m: Message) => (
              <div key={m.id} className={`msg ${m.direction}`}>
                <div className="small muted">{m.direction === "out" ? `→ ${m.purpose?.replace(/_/g, " ")} · ${m.channel}` : `← ${m.channel}`} · {m.status.replace(/_/g, " ")} · {when(m.created_at)}</div>
                {m.body}
                {m.status === "awaiting_user_send" && m.send_link && (
                  <div className="row" style={{ marginTop: 6 }}><a className="btn primary" href={m.send_link} target="_blank" rel="noreferrer">Send from my phone</a>
                    <button onClick={() => markSent(m)}>I've sent it</button></div>)}
                {m.extracted && m.direction === "in" && <div className="small muted" style={{ marginTop: 4 }}>Understood: {m.extracted.summary || Object.entries(m.extracted).filter(([, v]) => v && (!Array.isArray(v) || v.length)).map(([k, v]) => `${k}=${v}`).join(", ")}</div>}
                {m.error && <div className="small err">{m.error}</div>}
              </div>))}
            <label className="f"><span>{d.is_sample ? "Simulate the owner's reply (sample listing)" : "Paste a reply you received on WhatsApp/SMS/call notes"}</span>
              <textarea value={reply} onChange={(e) => setReply(e.target.value)} /></label>
            <div className="row"><button className="primary" onClick={submitReply} disabled={!reply}>Add reply</button>
              {d.status === "new" && sid && <button onClick={() => api.post(`/listings/${lid}/contact?search_id=${sid}`).then(() => setTimeout(load, 800))}>Start outreach</button>}
              {err && <span className="err small">{err}</span>}</div>
          </div>
          {d.approvals.length > 0 && <Approvals items={d.approvals} reload={load} />}
          {d.negotiation && (
            <div className="panel small stack">
              <h2>Negotiation · {d.negotiation.state.replace(/_/g, " ")} · round {d.negotiation.rounds}</h2>
              {d.negotiation.history.map((h: any, i: number) => (
                <div key={i}><b>{h.who === "us" ? "We proposed" : "They said"}</b>: {Object.entries(h.terms || {}).map(([k, v]) => `${k} ${typeof v === "number" && v >= 500 ? inr(v) : v}`).join(", ") || h.summary}</div>))}
            </div>)}
          {d.deal && <DealPanel d={d.deal} reload={load} />}
        </div>
      </div>
    </div>
  );
}
