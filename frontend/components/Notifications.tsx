"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, Notification, when } from "@/lib/api";
import { useLive } from "@/lib/useLive";

export default function Notifications() {
  const [items, setItems] = useState<Notification[]>([]);
  const [open, setOpen] = useState(false);
  const load = useCallback(() => api.get<Notification[]>("/notifications?limit=40").then(setItems).catch(() => {}), []);
  useEffect(() => { load(); }, [load]);
  useLive((e) => {
    if (e.type === "notification") {
      load();
      if (typeof window !== "undefined" && "Notification" in window && window.Notification.permission === "granted")
        new window.Notification(e.title, { body: e.body });
    }
  });
  const unread = items.filter((n) => !n.read).length;
  const toggle = async () => {
    setOpen(!open);
    if ("Notification" in window && window.Notification.permission === "default") window.Notification.requestPermission();
    if (!open && unread) { await api.post("/notifications/read"); load(); }
  };
  return (
    <div className="bell" style={{ position: "relative" }}>
      <button onClick={toggle} aria-label="Notifications">🔔{unread > 0 && <span className="badge">{unread}</span>}</button>
      {open && (
        <div className="panel drop stack">
          {items.length === 0 && <div className="muted">No notifications yet.</div>}
          {items.map((n) => (
            <div key={n.id}>
              <div className="row between"><b>{n.title}</b><span className="chip">{n.kind.replace(/_/g, " ")}</span></div>
              {n.body && <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{n.body.slice(0, 220)}</div>}
              <div className="small muted">{when(n.ts)}
                {n.listing_id && n.search_id && <> · <Link href={`/listing/${n.listing_id}?search=${n.search_id}`} onClick={() => setOpen(false)}>open</Link></>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
