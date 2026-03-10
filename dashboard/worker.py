from __future__ import annotations

import asyncio
import logging

from .database import engine
from .schema import backfill_creation_date, ensure_dashboard_schema
from .scheduler import run_hourly_job, start_scheduler, stop_scheduler
from .settings import RUN_SCHEDULER, RUN_STARTUP_SYNC


logger = logging.getLogger(__name__)


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("Dashboard worker starting...")
    await ensure_dashboard_schema(engine)
    await backfill_creation_date(engine)
    logger.info("Worker DB schema ensured")

    if RUN_STARTUP_SYNC:
        logger.info("Worker startup sync enabled; running one immediate ingestion job")
        try:
            await run_hourly_job()
            logger.info("Worker startup sync completed")
        except Exception as exc:
            logger.error("Worker startup sync failed: %s", exc, exc_info=True)
    else:
        logger.info("Worker startup sync disabled")

    if RUN_SCHEDULER:
        start_scheduler()
        logger.info("Worker scheduler started")
    else:
        logger.info("Worker scheduler disabled by RUN_SCHEDULER=false")

    try:
        await asyncio.Event().wait()
    finally:
        if RUN_SCHEDULER:
            await stop_scheduler()
        await engine.dispose()
        logger.info("Dashboard worker stopped")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
