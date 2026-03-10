from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import engine
from .schema import ensure_dashboard_schema
from .scheduler import run_aggregation_if_pending
from .settings import RUN_SCHEDULER, RUN_STARTUP_SYNC


logger = logging.getLogger(__name__)


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("Aggregation worker starting...")
    await ensure_dashboard_schema(engine)
    logger.info("Aggregation DB schema ensured")

    if RUN_STARTUP_SYNC:
        logger.info("Aggregation startup sync enabled; checking pending aggregate")
        await run_aggregation_if_pending()

    scheduler = AsyncIOScheduler()
    if RUN_SCHEDULER:
        scheduler.add_job(
            run_aggregation_if_pending,
            "interval",
            minutes=5,
            id="sales_aggregate_poll",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        logger.info("Aggregation scheduler started (poll every 5 minutes)")
    else:
        logger.info("RUN_SCHEDULER=false; aggregation scheduler disabled")

    try:
        await asyncio.Event().wait()
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        await engine.dispose()
        logger.info("Aggregation worker stopped")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
