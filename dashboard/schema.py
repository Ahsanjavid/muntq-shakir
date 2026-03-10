from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .models import Base
from .settings import get_tenant_timezone


logger = logging.getLogger(__name__)


async def ensure_dashboard_schema(engine: AsyncEngine) -> None:
    """Ensure dashboard tables, legacy schema upgrades, and indexes exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # create_all() does not alter existing tables. Ensure legacy DBs get
        # the new materialized date column used by filters and upserts.
        await conn.execute(
            text(
                "ALTER TABLE sales_order_snapshots "
                "ADD COLUMN IF NOT EXISTS creation_date DATE"
            )
        )

        # Ensure indexes exist for common filtered queries.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_so_snap_tenant_changed "
                "ON sales_order_snapshots (tenant_id, last_changed_at)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_so_snap_tenant_cdate "
                "ON sales_order_snapshots (tenant_id, creation_date)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_so_snap_payload_gin "
                "ON sales_order_snapshots USING gin (payload)"
            )
        )


async def backfill_creation_date(engine: AsyncEngine) -> None:
    """Backfill creation_date from payload->>'CreationDate' for old rows."""
    try:
        async with engine.begin() as conn:
            needs_backfill = await conn.scalar(
                text(
                    "SELECT EXISTS("
                    "  SELECT 1 FROM sales_order_snapshots"
                    "  WHERE creation_date IS NULL AND payload->>'CreationDate' IS NOT NULL"
                    "  LIMIT 1"
                    ")"
                )
            )
            if not needs_backfill:
                return

            tz_name = get_tenant_timezone()
            logger.info("Backfilling creation_date column (timezone: %s)...", tz_name)
            await conn.execute(
                text(
                    "UPDATE sales_order_snapshots SET creation_date = ("
                    "  (TIMESTAMP WITH TIME ZONE 'epoch' +"
                    "   CAST(NULLIF(substring(payload->>'CreationDate'"
                    "     FROM '/Date\\((\\d+)'), '') AS BIGINT) / 1000"
                    "   * INTERVAL '1 second')"
                    "  AT TIME ZONE :tz"
                    ")::date"
                    " WHERE creation_date IS NULL"
                    "   AND payload->>'CreationDate' IS NOT NULL"
                ).bindparams(tz=tz_name)
            )
            logger.info("creation_date backfill complete")
    except Exception as exc:
        logger.warning("creation_date backfill skipped or failed: %s", exc)
