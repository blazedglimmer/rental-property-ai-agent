"use client";
import { useEffect, useRef, useState } from "react";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws";

/** Subscribes to the backend event stream; reconnects with backoff. Calls onEvent for every payload. */
export function useLive(onEvent: (e: any) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let stop = false;
    let delay = 1000;
    const connect = () => {
      if (stop) return;
      ws = new WebSocket(WS_URL);
      ws.onopen = () => { setConnected(true); delay = 1000; };
      ws.onmessage = (m) => { try { cb.current(JSON.parse(m.data)); } catch {} };
      ws.onclose = () => { setConnected(false); if (!stop) setTimeout(connect, (delay = Math.min(delay * 2, 15000))); };
      ws.onerror = () => ws?.close();
    };
    connect();
    return () => { stop = true; ws?.close(); };
  }, []);
  return connected;
}
