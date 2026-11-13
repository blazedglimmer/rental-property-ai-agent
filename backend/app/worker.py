"""Background jobs.

With REDIS_URL set:   celery -A app.worker.celery worker -B -l info
Without Redis:        the API process runs `inprocess_scheduler()` (single-machine install).
"""
from __future__ import annotations

import asyncio
import logging

from .config import get_settings
from .db import init_db

log = logging.getLogger(__name__)
settings = get_settings()

celery = None
if settings.redis_url:
    from celery import Celery

    celery = Celery("rentagent", broker=settings.redis_url, backend=settings.redis_url)
    celery.conf.beat_schedule = {
        "poll-replies": {"task": "app.worker.poll_replies", "schedule": 120.0},
        "housekeeping": {"task": "app.worker.housekeeping_task", "schedule": 600.0},
    }
    celery.conf.task_acks_late = True
    celery.conf.worker_prefetch_multiplier = 1

    @celery.task(name="app.worker.run_search", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
    def run_search(search_id: int, auto_outreach: bool = True):
        init_db()
        from .graph import search_graph
        search_graph.run(search_id, auto_outreach)

    @celery.task(name="app.worker.poll_replies")
    def poll_replies():
        init_db()
        from .services.inbound import poll_email_replies
        return poll_email_replies()

    @celery.task(name="app.worker.housekeeping_task")
    def housekeeping_task():
        init_db()
        from .services.inbound import housekeeping
        return housekeeping()


def enqueue_search(search_id: int, auto_outreach: bool = True) -> None:
    if celery is not None:
        celery.send_task("app.worker.run_search", args=[search_id, auto_outreach])
    else:
        from .graph import search_graph
        from .graph.runtime import submit
        submit(search_graph.run, search_id, auto_outreach)


async def inprocess_scheduler(interval: int = 300) -> None:
    from .services.inbound import housekeeping
    await asyncio.sleep(15)
    while True:
        try:
            await asyncio.to_thread(housekeeping)
        except Exception:  # pragma: no cover
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval)
