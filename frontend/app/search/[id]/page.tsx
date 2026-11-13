"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, Approvals, Compare, DealPanel, ImportBox, ListingsTable, Visits } from "@/components/Panels";
import { api, Approval, Deal, Event, Listing, Visit, when } from "@/lib/api";
import { useLive } from "@/lib/useLive";

const TABS = ["Listings", "Approvals", "Compare", "Visits", "Deal", "Activity", "Import", "Settings"] as const;

export default function SearchPage({ params }: { params: { id: string } }) {
  const sid = Number(params.id);
  const [tab, setTab] = useState<(typeof TABS)[number]>("Listings");
  const [dash, setDash] = useState<any>(null);
  const [rows, setRows] = useState<Listing[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [visits, setVisits] = useState<Visit[]>([]);
  const [deals, setDeals] = useState<Deal[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const timer = useRef<any>(null);

  const load = useCallback(async () => {
    const [d, r, a, v, dl] = await Promise.all([
      api.get<any>(`/searches/${sid}/dashboard`), api.get<Listing[]>(`/searches/${sid}/listings`),
      api.get<Approval[]>(`/approvals?search_id=${sid}`), api.get<Visit[]>(`/visits?search_id=${sid}`),
      api.get<Deal[]>(`/deals?search_id=${sid}`)]);
    setDash(d); setRows(r); setApprovals(a); setVisits(v); setDeals(dl);
  }, [sid]);

  useEffect(() => {
    load().catch(() => {});
    api.get<Event[]>(`/events?search_id=${sid}&limit=150`).then(setEvents).catch(() => {});
  }, [load, sid]);

  const connected = useLive((e) => {
    if (e.type === "event" && (e.search_id == null || e.search_id === sid)) setEvents((xs) => [e, ...xs].slice(0, 300));
    clearTimeout(timer.current);
    timer.current = setTimeout(() => load().catch(() => {}), 600); // debounce refresh
  });

  const run = async () => { await api.post(`/searches/${sid}/run`); };
  const toggle = async () => { await api.patch(`/searches/${sid}`, { status: dash.search.status === "active" ? "paused" : "active" }); load(); };
  const compareIds = selected.length >= 2 ? selected : rows.filter((r) => r.match?.shortlisted).map((r) => r.id);

  if (!dash) return <div className="muted">Loading…</div>;
  const s = dash.search;
  const running = events[0]?.status === "running";
  const stats: [string, number, string?][] = [
    ["Discovered", dash.discovered], ["Availability confirmed", dash.verified], ["Drafts to approve", dash.drafting, "Approvals"], ["Awaiting response", dash.awaiting_response],
    ["Negotiating", dash.negotiating], ["Visits scheduled", dash.visits_scheduled], ["Shortlisted", dash.shortlisted],
    ["Closing", dash.closing], ["Need your approval", dash.pending_approvals, "Approvals"], ["Suspicious", dash.suspicious]];

  return (
    <div>
      <div className="row between">
        <div><h1>{s.title}</h1>
          <div className="small muted">{s.raw_query}</div>
          <div className="small muted">{s.last_run_at ? `Last discovery run ${when(s.last_run_at)}` : "Discovery not run yet"} · status {s.status}</div></div>
        <div className="row">
          {running && <span className="chip"><span className="dot running" /> agents working</span>}
          <button onClick={run}>Run discovery now</button>
          <button onClick={toggle}>{s.status === "active" ? "Pause" : "Resume"}</button>
        </div>
      </div>
      <div className="grid stats" style={{ marginTop: 14 }}>
        {stats.map(([k, n, t]) => (
          <div key={k} className="panel stat" style={{ cursor: t ? "pointer" : undefined, borderColor: t && n ? "var(--warn)" : undefined }} onClick={() => t && setTab(t as any)}>
            <div className="n">{n}</div><div className="k">{k}</div></div>
        ))}
      </div>
      <div className="tabs">{TABS.map((t) => (
        <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
          {t}{t === "Approvals" && approvals.length ? ` (${approvals.length})` : ""}{t === "Compare" && compareIds.length ? ` (${compareIds.length})` : ""}
        </button>))}</div>

      {tab === "Listings" && <ListingsTable rows={rows} sid={sid} reload={load} selected={selected} setSelected={setSelected} />}
      {tab === "Approvals" && <Approvals items={approvals} reload={load} />}
      {tab === "Compare" && <Compare key={compareIds.join(",")} ids={compareIds} sid={sid} />}
      {tab === "Visits" && <Visits items={visits} reload={load} />}
      {tab === "Deal" && (deals.length ? <div className="stack">{deals.map((d) => <DealPanel key={d.id} d={d} reload={load} />)}</div>
        : <div className="panel muted">Deal closure starts after a visit you liked and your approval to proceed.</div>)}
      {tab === "Activity" && <Activity events={events} connected={connected} />}
      {tab === "Import" && <ImportBox sid={sid} done={load} />}
      {tab === "Settings" && <SettingsPanel search={s} effective={dash} reload={load} />}
    </div>
  );
}

function SettingsPanel({ search, reload }: { search: any; effective: any; reload: () => void }) {
  const [weights, setWeights] = useState<Record<string, number>>(search.weights);
  const [bounds, setBounds] = useState(JSON.stringify(search.bounds, null, 2));
  const [cal, setCal] = useState<any>(null);
  const [slots, setSlots] = useState<string[]>([]);
  const [msg, setMsg] = useState("");
  useEffect(() => { api.get<any>("/settings/calendar").then(setCal); api.get<string[]>("/settings/calendar/slots").then(setSlots); }, []);
  const save = async () => {
    try {
      await api.patch(`/searches/${search.id}`, { weights, bounds: JSON.parse(bounds) });
      if (cal) await api.put("/settings/calendar", cal);
      setMsg("Saved — listings re-scored."); reload();
      api.get<string[]>("/settings/calendar/slots").then(setSlots);
    } catch (e: any) { setMsg(e.message); }
  };
  return (
    <div className="grid cols-2">
      <div className="panel stack">
        <h2>Scoring weights</h2>
        {Object.keys(weights).map((k) => (
          <label key={k} className="f"><span>{k.replace("_", " & ")}: {weights[k]}</span>
            <input type="range" min={0} max={50} value={weights[k]} onChange={(e) => setWeights({ ...weights, [k]: Number(e.target.value) })} /></label>))}
        <h2>Negotiation limits</h2>
        <textarea value={bounds} onChange={(e) => setBounds(e.target.value)} style={{ minHeight: 180, fontFamily: "monospace" }} />
      </div>
      <div className="panel stack">
        <h2>Visit availability</h2>
        {cal && <>
          <label className="f"><span>Calendar ICS URL (private address from Google/Outlook; used for busy times)</span>
            <input value={cal.ics_url ?? ""} onChange={(e) => setCal({ ...cal, ics_url: e.target.value || null })} /></label>
          <label className="f"><span>Weekly windows (JSON: weekday 0=Mon → [[start,end]])</span>
            <textarea value={JSON.stringify(cal.windows)} onChange={(e) => { try { setCal({ ...cal, windows: JSON.parse(e.target.value) }); } catch {} }} /></label>
          <label className="f"><span>Visit length (min)</span><input value={cal.visit_minutes} onChange={(e) => setCal({ ...cal, visit_minutes: Number(e.target.value) || 45 })} /></label>
        </>}
        <div className="small">Next free slots: {slots.map((x) => new Date(x).toLocaleString("en-IN", { weekday: "short", hour: "numeric", minute: "2-digit", day: "numeric", month: "short" })).join(" · ") || "none"}</div>
        <button className="primary" onClick={save}>Save settings</button><span className="small">{msg}</span>
      </div>
    </div>
  );
}
