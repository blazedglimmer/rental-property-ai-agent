"""Checkpointer, per-thread locks and background execution."""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from ..config import get_settings

log = logging.getLogger(__name__)
_saver = None
_saver_lock = threading.Lock()
_thread_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="agent")


def get_checkpointer():
    """SQLite for dev / single machine; Postgres when CHECKPOINT_URL is postgres://."""
    global _saver
    with _saver_lock:
        if _saver is not None:
            return _saver
        url = get_settings().checkpoint_url
        if url.startswith("postgres"):
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg_pool import ConnectionPool
            pool = ConnectionPool(url, kwargs={"autocommit": True, "prepare_threshold": 0}, max_size=10)
            _saver = PostgresSaver(pool)
        else:
            from langgraph.checkpoint.sqlite import SqliteSaver
            path = url.replace("sqlite:///", "")
            conn = sqlite3.connect(path, check_same_thread=False)
            _saver = SqliteSaver(conn)
        _saver.setup()
        return _saver


def thread_lock(thread_id: str) -> threading.Lock:
    return _thread_locks[thread_id]


def submit(fn, *args, **kwargs):
    """Run agent work off the request thread. Errors are logged + emitted, never lost silently."""
    def _wrapped():
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # pragma: no cover - surfaced via events
            log.exception("background task failed")
            from ..services.events import emit
            emit("orchestrator", f"Task {getattr(fn, '__name__', fn)} failed: {e}", status="error", level="error")
    return _pool.submit(_wrapped)
