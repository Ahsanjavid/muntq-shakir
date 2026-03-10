from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import engine
from .schema import backfill_creation_date, ensure_dashboard_schema
from .scheduler import run_ingestion_job
from .settings import RUN_SCHEDULER, RUN_STARTUP_SYNC


logger = logging.getLogger(__name__)


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("Ingestion worker starting...")
    await ensure_dashboard_schema(engine)
    await backfill_creation_date(engine)
    logger.info("Ingestion DB schema ensured")

    if RUN_STARTUP_SYNC:
        logger.info("Ingestion startup sync enabled; running immediate ingestion")
        await run_ingestion_job()

    scheduler = AsyncIOScheduler()
    if RUN_SCHEDULER:
        scheduler.add_job(
            run_ingestion_job,
            "interval",
            hours=1,
            id="sales_ingest_hourly",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        logger.info("Ingestion scheduler started (hourly)")
    else:
        logger.info("RUN_SCHEDULER=false; ingestion scheduler disabled")

    try:
        await asyncio.Event().wait()
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        await engine.dispose()
        logger.info("Ingestion worker stopped")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
