"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";

const EXAMPLE = "Find me a furnished 1BHK in Baner or Aundh, Pune, under ₹20,000. I need a decent society, good ventilation, kitchen, parking and a reasonable deposit. I want to move within 30 days.";
const WEIGHTS = ["budget", "location", "condition", "furnishing", "amenities", "deposit_brokerage", "connectivity"];
const DEFAULT_W: Record<string, number> = { budget: 25, location: 20, condition: 15, furnishing: 10, amenities: 10, deposit_brokerage: 10, connectivity: 10 };

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const num = (s: string) => (s.trim() === "" ? null : Number(s.replace(/[,₹\s]/g, "")));

export default function NewSearch() {
  const router = useRouter();
  const [text, setText] = useState(EXAMPLE);
  const [c, setC] = useState<any | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [bounds, setBounds] = useState<any>({ target_rent: "", max_rent: "", max_deposit: "", max_brokerage: "", flexibility: "moderate", max_lock_in_months: 6 });
  const [weights, setWeights] = useState(DEFAULT_W);
  const [sources, setSources] = useState({ web_search: true, email_alerts: true, feed: "" , sample: false });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const parse = async () => {
    setBusy(true); setErr("");
    try {
      const r = await api.post<any>("/parse", { text });
      setC(r.criteria); setNotes([...r.assumptions, ...r.unclear]);
      setBounds((b: any) => ({ ...b, max_rent: r.criteria.max_rent ?? "", max_deposit: r.criteria.max_deposit ?? "" }));
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };

  const create = async () => {
    setBusy(true); setErr("");
    const srcs: any[] = [];
    if (sources.web_search) srcs.push({ type: "web_search" });
    if (sources.email_alerts) srcs.push({ type: "email_alerts" });
    if (sources.feed) srcs.push({ type: "partner_feed", url: sources.feed, name: "partner" });
    if (sources.sample) srcs.push({ type: "partner_feed", url: "samples/pune_sample_feed.json", name: "sample", sample: true });
    const b: any = { flexibility: bounds.flexibility, max_lock_in_months: Number(bounds.max_lock_in_months) || 6 };
    for (const k of ["target_rent", "max_rent", "max_deposit", "max_brokerage"]) b[k] = num(String(bounds[k] ?? ""));
    try {
      const r = await api.post<any>("/searches", { raw_query: text, criteria: c ?? undefined, bounds: b, weights, sources: srcs });
      router.push(`/search/${r.id}`);
    } catch (e: any) { setErr(e.message); setBusy(false); }
  };

  const set = (k: string, v: any) => setC({ ...c, [k]: v });

  return (
    <div className="panel stack">
      <h2>New search</h2>
      <label className="f"><span>Describe what you need, in your own words</span>
        <textarea value={text} onChange={(e) => setText(e.target.value)} /></label>
      <div className="row"><button onClick={parse} disabled={busy}>Understand requirements</button>
        <span className="muted small">or skip straight to “Start search” — the agent will parse it.</span></div>

      {c && (
        <>
          {notes.length > 0 && <div className="banner small">{notes.map((n, i) => <div key={i}>• {n}</div>)}</div>}
          <h3>Requirements</h3>
          <div className="fields">
            <label className="f"><span>City</span><input value={c.city ?? ""} onChange={(e) => set("city", e.target.value)} /></label>
            <label className="f"><span>Localities (comma-separated)</span><input value={c.localities.join(", ")} onChange={(e) => set("localities", list(e.target.value))} /></label>
            <label className="f"><span>Min rent</span><input value={c.min_rent ?? ""} onChange={(e) => set("min_rent", num(e.target.value))} /></label>
            <label className="f"><span>Max rent</span><input value={c.max_rent ?? ""} onChange={(e) => set("max_rent", num(e.target.value))} /></label>
            <label className="f"><span>Property type</span><input value={c.property_types.join(", ")} placeholder="1RK, 1BHK, 2BHK" onChange={(e) => set("property_types", list(e.target.value).map((x) => x.toUpperCase()))} /></label>
            <label className="f"><span>Furnishing</span>
              <select value={c.furnishing[0] ?? ""} onChange={(e) => set("furnishing", e.target.value ? [e.target.value] : [])}>
                <option value="">Any</option><option>furnished</option><option>semi-furnished</option><option>unfurnished</option></select></label>
            <label className="f"><span>Preferred societies</span><input value={c.preferred_societies.join(", ")} onChange={(e) => set("preferred_societies", list(e.target.value))} /></label>
            <label className="f"><span>Building type</span><input value={c.building_types.join(", ")} onChange={(e) => set("building_types", list(e.target.value))} /></label>
            <label className="f"><span>Min bedrooms / bathrooms</span>
              <div className="row" style={{ flexWrap: "nowrap" }}><input value={c.min_bedrooms ?? ""} onChange={(e) => set("min_bedrooms", num(e.target.value))} />
                <input value={c.min_bathrooms ?? ""} onChange={(e) => set("min_bathrooms", num(e.target.value))} /></div></label>
            <label className="f"><span>Parking</span>
              <select value={c.parking ?? ""} onChange={(e) => set("parking", e.target.value || null)}>
                <option value="">Not needed</option><option value="any">Any</option><option value="two_wheeler">Two-wheeler</option><option value="car">Car</option></select></label>
            <label className="f"><span>Amenities</span><input value={c.amenities.join(", ")} onChange={(e) => set("amenities", list(e.target.value))} /></label>
            <label className="f"><span>Max deposit</span><input value={c.max_deposit ?? ""} onChange={(e) => set("max_deposit", num(e.target.value))} /></label>
            <label className="f"><span>Brokerage</span>
              <select value={c.brokerage} onChange={(e) => set("brokerage", e.target.value)}>
                <option value="any">Any</option><option value="no_brokerage">No brokerage</option><option value="up_to_half_month">≤ ½ month</option><option value="up_to_one_month">≤ 1 month</option></select></label>
            <label className="f"><span>Tenant type</span>
              <select value={c.tenant_type ?? ""} onChange={(e) => set("tenant_type", e.target.value || null)}>
                <option value="">—</option><option value="family">Family</option><option value="couple">Couple</option><option value="bachelor_male">Bachelor (male)</option><option value="bachelor_female">Bachelor (female)</option><option value="company">Company lease</option></select></label>
            <label className="f"><span>Move in by</span><input type="date" value={c.move_in_by ?? ""} onChange={(e) => set("move_in_by", e.target.value || null)} /></label>
            <label className="f"><span>Lease (months)</span><input value={c.lease_months ?? ""} onChange={(e) => set("lease_months", num(e.target.value))} /></label>
            <label className="f"><span>Commute landmarks</span><input value={c.landmarks.join(", ")} onChange={(e) => set("landmarks", list(e.target.value))} /></label>
            <label className="f" style={{ gridColumn: "1 / -1" }}><span>Other requirements</span><input value={c.other_requirements.join(", ")} onChange={(e) => set("other_requirements", list(e.target.value))} /></label>
          </div>
        </>
      )}

      <h3>Negotiation limits <span className="muted small">— the agent never offers beyond these, and never commits without you</span></h3>
      <div className="fields">
        {[["target_rent", "Target rent"], ["max_rent", "Max acceptable rent"], ["max_deposit", "Max deposit"], ["max_brokerage", "Max brokerage (0 = none)"]].map(([k, l]) => (
          <label key={k} className="f"><span>{l}</span><input value={bounds[k] ?? ""} onChange={(e) => setBounds({ ...bounds, [k]: e.target.value })} placeholder="auto" /></label>
        ))}
        <label className="f"><span>Max lock-in (months)</span><input value={bounds.max_lock_in_months} onChange={(e) => setBounds({ ...bounds, max_lock_in_months: e.target.value })} /></label>
        <label className="f"><span>Flexibility</span><select value={bounds.flexibility} onChange={(e) => setBounds({ ...bounds, flexibility: e.target.value })}>
          <option>firm</option><option>moderate</option><option>flexible</option></select></label>
      </div>

      <details><summary>Scoring weights</summary>
        <div className="fields" style={{ marginTop: 8 }}>
          {WEIGHTS.map((k) => (
            <label key={k} className="f"><span>{k.replace("_", " & ")}: {weights[k]}</span>
              <input type="range" min={0} max={50} value={weights[k]} onChange={(e) => setWeights({ ...weights, [k]: Number(e.target.value) })} /></label>
          ))}
        </div>
      </details>

      <details><summary>Discovery sources</summary>
        <div className="stack small" style={{ marginTop: 8 }}>
          <label className="row"><input type="checkbox" style={{ width: "auto" }} checked={sources.web_search} onChange={(e) => setSources({ ...sources, web_search: e.target.checked })} /> Web search via Claude (needs API key)</label>
          <label className="row"><input type="checkbox" style={{ width: "auto" }} checked={sources.email_alerts} onChange={(e) => setSources({ ...sources, email_alerts: e.target.checked })} /> Portal alert emails in your mailbox (needs IMAP)</label>
          <label className="f"><span>Licensed partner feed URL (JSON/CSV)</span><input value={sources.feed} onChange={(e) => setSources({ ...sources, feed: e.target.value })} /></label>
          <label className="row"><input type="checkbox" style={{ width: "auto" }} checked={sources.sample} onChange={(e) => setSources({ ...sources, sample: e.target.checked })} /> Include SAMPLE Pune listings (demo only — clearly labelled, messages are simulated)</label>
        </div>
      </details>

      {err && <div className="err">{err}</div>}
      <div><button className="primary" onClick={create} disabled={busy}>{busy ? "Working…" : "Start search"}</button></div>
    </div>
  );
}
