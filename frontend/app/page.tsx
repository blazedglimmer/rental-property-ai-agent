"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import NewSearch from "@/components/NewSearch";
import { api, Search, when } from "@/lib/api";

export default function Home() {
  const [searches, setSearches] = useState<Search[]>([]);
  const [status, setStatus] = useState<any>(null);
  useEffect(() => {
    api.get<Search[]>("/searches").then(setSearches).catch(() => {});
    api.get<any>("/status").then(setStatus).catch(() => setStatus({ error: true }));
  }, []);
  return (
    <div className="grid cols-2" style={{ alignItems: "start" }}>
      <NewSearch />
      <div className="stack">
        <div className="panel">
          <h2>Your searches</h2>
          {searches.length === 0 && <div className="muted">None yet.</div>}
          {searches.map((s) => (
            <div key={s.id} className="row between" style={{ padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
              <Link href={`/search/${s.id}`}>{s.title}</Link>
              <span className="row"><span className={`chip ${s.status === "active" ? "good" : ""}`}>{s.status}</span>
                <span className="small muted">{s.last_run_at ? `ran ${when(s.last_run_at)}` : "not run yet"}</span></span>
            </div>
          ))}
        </div>
        <div className="panel small stack">
          <h2>System status</h2>
          {status?.error && <div className="err">Backend not reachable on :8000</div>}
          {status && !status.error && (
            <>
              <div>LLM (Claude): <b className={status.llm ? "" : "err"}>{status.llm ? status.model : "not configured — rule-based fallbacks active"}</b></div>
              <div>Background jobs: {status.background}</div>
              <div>Channels: {Object.entries(status.channels).map(([k, v]) => <span key={k} className={`chip ${v ? "good" : ""}`} style={{ marginRight: 4 }}>{k.replace(/_/g, " ")}</span>)}</div>
              <div>Sources: {Object.entries(status.sources).map(([k, v]: any) => <span key={k} title={v.reason || v.compliance} className={`chip ${v.available ? "good" : ""}`} style={{ marginRight: 4 }}>{k.replace("_", " ")}</span>)}</div>
              <div className="muted">Daily first-contact cap: {status.daily_outreach_cap}. Page fetching allowed on: {status.fetch_allowed_domains.join(", ") || "user-supplied URLs only (robots.txt checked)"}.</div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
