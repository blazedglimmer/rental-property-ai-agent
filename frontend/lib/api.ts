export type Breakdown = { factor: string; weight: number; score: number; points: number; reason: string };
export type Match = {
  id: number; search_id: number; listing_id: number; score: number; breakdown: Breakdown[];
  compromises: string[]; missing: string[]; total_monthly_cost: number | null; hard_fail: boolean;
  shortlisted: boolean; dismissed: boolean;
};
export type Flag = { code: string; message: string; severity: string; penalty: number };
export type Listing = {
  id: number; title: string | null; city: string | null; locality: string | null; address: string | null;
  society: string | null; property_type: string | null; bhk: number | null; bathrooms: number | null;
  rent: number | null; deposit: number | null; maintenance: number | null; brokerage: number | null;
  brokerage_free: boolean | null; area_sqft: number | null; furnishing: string | null; parking: string | null;
  amenities: string[]; tenant_pref: string[]; description: string | null; photos: string[];
  contact_name: string | null; contact_phone: string | null; contact_email: string | null; contact_role: string | null;
  source_url: string | null; source: string | null; availability: string; confidence: number; risk_flags: Flag[];
  is_sample: boolean; status: string; first_seen_at: string; last_seen_at: string; match?: Match;
};
export type Message = {
  id: number; direction: "in" | "out"; channel: string; purpose: string | null; to_addr: string | null;
  from_addr: string | null; body: string; status: string; send_link: string | null; extracted: any;
  created_at: string; error: string | null;
};
export type Approval = {
  id: number; kind: string; listing_id: number; search_id: number; title: string; payload: any; status: string;
  created_at: string;
};
export type Search = {
  id: number; title: string; raw_query: string | null; criteria: any; bounds: any; weights: Record<string, number>;
  sources: any[]; status: string; created_at: string; last_run_at: string | null; effective_bounds?: any;
};
export type Visit = { id: number; listing_id: number; search_id: number; start_at: string; end_at: string; status: string; feedback: any; listing: Listing; checklist: string[] };
export type ChecklistItem = { key: string; label: string; owner: string; done: boolean; note: string };
export type Deal = { id: number; listing_id: number; stage: string; final_terms: any; checklist: ChecklistItem[]; agreement_review: any; listing?: Listing };
export type Event = { id: number; ts: string; agent: string; status: string; level: string; message: string; search_id: number | null; listing_id: number | null; data?: any };
export type Notification = { id: number; ts: string; kind: string; title: string; body: string; read: boolean; search_id: number | null; listing_id: number | null };

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.json();
}

export const api = {
  get: <T,>(p: string) => req<T>(p),
  post: <T,>(p: string, body?: any) => req<T>(p, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: <T,>(p: string, body?: any) => req<T>(p, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  put: <T,>(p: string, body?: any) => req<T>(p, { method: "PUT", body: JSON.stringify(body ?? {}) }),
};

export const inr = (n: number | null | undefined) => (n == null ? "—" : `₹${n.toLocaleString("en-IN")}`);
export const label = (l: Listing) =>
  [l.property_type, l.society, l.locality].filter(Boolean).join(" · ") || l.title || `Listing ${l.id}`;
export const when = (s: string) => new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z").toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
export const localWhen = (s: string) => new Date(s).toLocaleString("en-IN", { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
