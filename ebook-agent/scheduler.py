"""Background jobs (APScheduler 3.x). Runs as its own process: python scheduler.py

Schedule (all times in SCHEDULER_TIMEZONE, default Asia/Kolkata):
- every 15 min: send due onboarding and launch emails; publish due Instagram posts within the publishing quota
- every 30 min: retry failed webhook events
- every 6 h:    re-read recent Gumroad sales from the API (recovers any ping that never arrived)
- daily at ANALYTICS_CRON_HOUR: AnalyticsAgent report for every ebook and for the whole business
- weekly, on MARKETING_CRON_WEEKDAY at MARKETING_CRON_HOUR: schedule the next draft posts that have images
- every 30 min: mark agent runs stuck in queued or running for over 2 hours as failed (process restarts)
"""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from config import settings, utcnow
from core import pipeline
from db.database import SessionLocal, init_db
from db.models import AgentRun, Ebook
from services import buyers, marketing, sales

logger = logging.getLogger("scheduler")


def job_email_sequences() -> dict[str, int]:
    with SessionLocal() as db:
        return buyers.process_due_enrollments(db)


def job_publish_posts() -> dict[str, Any]:
    with SessionLocal() as db:
        return marketing.publish_due_posts(db)


def job_retry_webhooks() -> int:
    return sales.retry_failed_webhooks()


def job_reconcile_gumroad() -> dict[str, int]:
    return sales.reconcile_gumroad()


def job_daily_analytics() -> int:
    with SessionLocal() as db:
        ebook_ids: list[int | None] = list(db.scalars(select(Ebook.id)).all())
    ebook_ids.append(None)  # one report for the whole business
    for ebook_id in ebook_ids:
        with SessionLocal() as db:
            run = AgentRun(task="analytics", ebook_id=ebook_id, status="queued", input_json='{"days": 30}')
            db.add(run)
            db.commit()
            run_id = run.id
        pipeline.execute_run(run_id)  # records its own success or failure
    return len(ebook_ids)


def job_weekly_slots() -> int:
    with SessionLocal() as db:
        return marketing.schedule_weekly_slots(db)


STALE_RUN_AFTER = timedelta(hours=2)


def job_reap_stale_runs() -> int:
    """Background runs live in the API process. If it restarts mid-run, the row would stay 'running' forever."""
    cutoff = utcnow() - STALE_RUN_AFTER
    with SessionLocal() as db:
        stale = db.scalars(
            select(AgentRun).where(AgentRun.status.in_(["queued", "running"]), AgentRun.created_at < cutoff)
        ).all()
        for run in stale:
            run.status = "failed"
            run.error = "Interrupted: the service stopped before this run finished. Run it again."
            run.finished_at = utcnow()
        db.commit()
        return len(stale)


def run_job(name: str, func: Callable[[], Any]) -> None:
    """Run one job and log the outcome. An exception is logged and never stops the scheduler."""
    try:
        result = func()
        logger.info("%s finished: %s", name, result)
    except Exception:
        logger.exception("%s failed", name)


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(
        timezone=settings.scheduler_timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
    )
    scheduler.add_job(run_job, "interval", minutes=15, args=["email sequences", job_email_sequences], id="email-sequences")
    scheduler.add_job(run_job, "interval", minutes=15, args=["publish posts", job_publish_posts], id="publish-posts")
    scheduler.add_job(run_job, "interval", minutes=30, args=["retry webhooks", job_retry_webhooks], id="retry-webhooks")
    scheduler.add_job(run_job, "interval", hours=6, args=["gumroad reconcile", job_reconcile_gumroad], id="gumroad-reconcile")
    scheduler.add_job(run_job, "interval", minutes=30, args=["stale runs", job_reap_stale_runs], id="reap-stale-runs")
    scheduler.add_job(
        run_job,
        CronTrigger(hour=settings.analytics_cron_hour, minute=0),
        args=["daily analytics", job_daily_analytics],
        id="daily-analytics",
    )
    scheduler.add_job(
        run_job,
        CronTrigger(day_of_week=settings.marketing_cron_weekday, hour=settings.marketing_cron_hour, minute=0),
        args=["weekly post slots", job_weekly_slots],
        id="weekly-post-slots",
    )
    return scheduler


def main() -> None:
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    scheduler = build_scheduler()

    def stop(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; stopping scheduler", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("Scheduler starting in %s with %d jobs", settings.scheduler_timezone, len(scheduler.get_jobs()))
    scheduler.start()


if __name__ == "__main__":
    main()
