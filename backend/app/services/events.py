"""Activity log + live fan-out.

Every agent step calls `emit(...)`. The event is persisted (dashboard history) and pushed to
WebSocket subscribers in this process; if REDIS_URL is set it is also published so the API
process sees events produced by Celery workers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime

from ..config import get_settings
from ..db import after_commit, session_scope
from ..models import Event

log = logging.getLogger(__name__)
CHANNEL = "rentagent:events"

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None
_lock = threading.Lock()
_redis = None


def _redis_client():
    global _redis
    url = get_settings().redis_url
    if url and _redis is None:
        try:
            import redis
            _redis = redis.Redis.from_url(url)
        except Exception as e:  # pragma: no cover
            log.warning("redis unavailable: %s", e)
    return _redis


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def broadcast_local(payload: dict) -> None:
    if not _loop:
        return
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        def _put(q=q):
            if not q.full():
                q.put_nowait(payload)
        _loop.call_soon_threadsafe(_put)


def publish(payload: dict) -> None:
    r = _redis_client()
    if r is not None:
        try:
            r.publish(CHANNEL, json.dumps(payload, default=str))
            return  # the API process's redis listener re-broadcasts locally
        except Exception as e:  # pragma: no cover
            log.warning("redis publish failed: %s", e)
    broadcast_local(payload)


def emit(*args, **kwargs) -> None:
    after_commit(lambda: _emit(*args, **kwargs))


def _emit(agent: str, message: str, *, status: str = "done", level: str = "info",
         search_id: int | None = None, listing_id: int | None = None, data: dict | None = None,
         kind: str = "event") -> None:
    with session_scope() as s:
        ev = Event(agent=agent, message=message, status=status, level=level,
                   search_id=search_id, listing_id=listing_id, data=data)
        s.add(ev)
        s.flush()
        payload = {"type": kind, "id": ev.id, "ts": datetime.now().isoformat(), "agent": agent,
                   "status": status, "level": level, "message": message,
                   "search_id": search_id, "listing_id": listing_id, "data": data}
    log.info("[%s] %s", agent, message)
    publish(payload)


async def redis_listener() -> None:  # pragma: no cover - needs redis
    r = _redis_client()
    if r is None:
        return
    import redis.asyncio as aioredis
    ar = aioredis.Redis.from_url(get_settings().redis_url)
    ps = ar.pubsub()
    await ps.subscribe(CHANNEL)
    async for msg in ps.listen():
        if msg.get("type") == "message":
            try:
                broadcast_local(json.loads(msg["data"]))
            except Exception:
                pass
