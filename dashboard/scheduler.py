import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .aggregator import calculate_aggregates
from .database import AsyncSessionLocal
from .models import BillingSnapshot, CuratedAggregate, JobRun, SalesOrderSnapshot
from .sales_filters import DEFAULT_TENANT_ID, SALES_DATASET_KEY
from .sap_extractor import fetch_billing_documents, fetch_sales_orders
from .settings import BATCH_UPSERT_SIZE, INCREMENTAL_LOOKBACK_HOURS, MAX_YEARS, get_tenant_timezone

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
TENANT_ID = DEFAULT_TENANT_ID
DATASET_KEY = SALES_DATASET_KEY
SAP_DATE_RE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")

MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = [5, 15, 45]

_job_lock = asyncio.Lock()
_ingest_lock = asyncio.Lock()
_aggregate_lock = asyncio.Lock()

# asyncpg/PostgreSQL cannot handle more than 32767 bound params in one query.
# Keep a conservative cap to stay safe even if model columns change.
_MAX_SAFE_UPSERT_BATCH = 3000
_EFFECTIVE_UPSERT_BATCH = min(BATCH_UPSERT_SIZE, _MAX_SAFE_UPSERT_BATCH)

if BATCH_UPSERT_SIZE > _MAX_SAFE_UPSERT_BATCH:
    logger.warning(
        "BATCH_UPSERT_SIZE=%d is too high for safe Postgres parameter limits; "
        "using %d instead",
        BATCH_UPSERT_SIZE,
        _EFFECTIVE_UPSERT_BATCH,
    )


def _parse_sap_datetime(raw_value):
    if not raw_value:
        return None

    raw = str(raw_value).strip()
    if raw.lower() == "none":
        return None

    if raw.startswith("/Date(") and raw.endswith(")/"):
        match = SAP_DATE_RE.match(raw)
        if not match:
            return None
        return datetime.utcfromtimestamp(int(match.group(1)) / 1000)

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _parse_sap_creation_date(raw_value):
    """Extract a Python date from SAP CreationDate, in the tenant's local timezone."""
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo
    if not raw_value:
        return None
    raw = str(raw_value).strip()
    match = SAP_DATE_RE.match(raw)
    if match:
        ts = int(match.group(1)) / 1000
        try:
            tz = ZoneInfo(get_tenant_timezone())
        except Exception:
            tz = _tz.utc
        return datetime.fromtimestamp(ts, tz=_tz.utc).astimezone(tz).date()
    # ISO fallback
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        try:
            tz = ZoneInfo(get_tenant_timezone())
        except Exception:
            tz = _tz.utc
        return dt.astimezone(tz).date()
    except Exception:
        return None


async def _fetch_with_retry(fetch_func, *args, label="fetch", **kwargs):
    last_exc = None
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            result = await fetch_func(*args, **kwargs)
            if attempt > 0:
                logger.info("%s succeeded on attempt %d", label, attempt + 1)
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_FETCH_RETRIES - 1:
                delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "%s failed on attempt %d/%d: %s; retrying in %ds",
                    label,
                    attempt + 1,
                    MAX_FETCH_RETRIES,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("%s failed after %d attempts: %s", label, MAX_FETCH_RETRIES, exc, exc_info=True)
    raise last_exc


async def _upsert_sales_snapshots(db, rows):
    now = datetime.utcnow()
    values = []

    for row in rows:
        sales_order = str(row.get("SalesOrder", "")).strip()
        if not sales_order:
            continue

        values.append(
            {
                "tenant_id": TENANT_ID,
                "sales_order": sales_order,
                "payload": row,
                "creation_date": _parse_sap_creation_date(row.get("CreationDate")),
                "last_changed_at": _parse_sap_datetime(row.get("LastChangeDateTime"))
                or _parse_sap_datetime(row.get("CreationDate")),
                "updated_at": now,
            }
        )

    if not values:
        return 0

    upserted = 0
    for i in range(0, len(values), _EFFECTIVE_UPSERT_BATCH):
        batch = values[i : i + _EFFECTIVE_UPSERT_BATCH]
        stmt = pg_insert(SalesOrderSnapshot).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "sales_order"],
            set_={
                "payload": stmt.excluded.payload,
                "creation_date": stmt.excluded.creation_date,
                "last_changed_at": stmt.excluded.last_changed_at,
                "updated_at": now,
            },
        )
        await db.execute(stmt)
        await db.commit()
        upserted += len(batch)
        if upserted % 5000 == 0:
            logger.info("Upserted %d/%d sales order snapshots", upserted, len(values))
    return len(values)


async def _upsert_billing_snapshots(db, rows):
    now = datetime.utcnow()
    values = []

    for row in rows:
        billing_doc = str(row.get("BillingDocument", "")).strip()
        if not billing_doc:
            continue

        values.append(
            {
                "tenant_id": TENANT_ID,
                "billing_document": billing_doc,
                "payload": row,
                "billing_date": _parse_sap_datetime(row.get("BillingDocumentDate")),
                "updated_at": now,
            }
        )

    if not values:
        return 0

    upserted = 0
    for i in range(0, len(values), _EFFECTIVE_UPSERT_BATCH):
        batch = values[i : i + _EFFECTIVE_UPSERT_BATCH]
        stmt = pg_insert(BillingSnapshot).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "billing_document"],
            set_={
                "payload": stmt.excluded.payload,
                "billing_date": stmt.excluded.billing_date,
                "updated_at": now,
            },
        )
        await db.execute(stmt)
        await db.commit()
        upserted += len(batch)
        if upserted % 5000 == 0:
            logger.info("Upserted %d/%d billing snapshots", upserted, len(values))
    return len(values)


async def run_ingestion_job():
    if _ingest_lock.locked():
        logger.warning("Skipping ingestion job - previous run still in progress")
        return

    async with _ingest_lock:
        started_at = datetime.utcnow()
        job_id = started_at.strftime("%Y%m%d_%H%M%S")
        logger.info("[ingest:%s] Starting SAP ingestion job", job_id)

        try:
            async with AsyncSessionLocal() as db:
                # Set statement timeout (20 min for large backfill with 500K+ rows)
                await db.execute(text("SET statement_timeout = '1200s'"))

                t0 = time.monotonic()

                snapshot_count = await db.scalar(
                    select(func.count()).select_from(SalesOrderSnapshot).where(SalesOrderSnapshot.tenant_id == TENANT_ID)
                )
                has_snapshot_data = bool(snapshot_count)

                if has_snapshot_data:
                    end_time = datetime.utcnow()
                    latest_changed_at = await db.scalar(
                        select(func.max(SalesOrderSnapshot.last_changed_at)).where(SalesOrderSnapshot.tenant_id == TENANT_ID)
                    )
                    if latest_changed_at:
                        start_time = latest_changed_at - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)
                    else:
                        start_time = end_time - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)

                    so_rows = await _fetch_with_retry(
                        fetch_sales_orders,
                        start_time,
                        end_time,
                        filter_field="LastChangeDateTime",
                        expand_items=True,
                        label="SalesOrder-incremental",
                    )
                else:
                    end_time = datetime.utcnow()
                    start_time = end_time - timedelta(days=365 * MAX_YEARS)
                    so_rows = await _fetch_with_retry(
                        fetch_sales_orders,
                        start_time,
                        end_time,
                        filter_field="CreationDate",
                        expand_items=True,
                        label="SalesOrder-backfill",
                    )

                so_upserted = await _upsert_sales_snapshots(db, so_rows)
                del so_rows

                billing_count = await db.scalar(
                    select(func.count()).select_from(BillingSnapshot).where(BillingSnapshot.tenant_id == TENANT_ID)
                )
                has_billing_data = bool(billing_count)

                if has_billing_data:
                    bill_end = datetime.utcnow()
                    latest_billing_date = await db.scalar(
                        select(func.max(BillingSnapshot.billing_date)).where(BillingSnapshot.tenant_id == TENANT_ID)
                    )
                    if latest_billing_date:
                        bill_start = latest_billing_date - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)
                    else:
                        bill_start = bill_end - timedelta(hours=INCREMENTAL_LOOKBACK_HOURS)

                    bill_rows = await _fetch_with_retry(
                        fetch_billing_documents,
                        bill_start,
                        bill_end,
                        label="BillingDoc-incremental",
                    )
                else:
                    bill_end = datetime.utcnow()
                    bill_start = bill_end - timedelta(days=365 * MAX_YEARS)
                    bill_rows = await _fetch_with_retry(
                        fetch_billing_documents,
                        bill_start,
                        bill_end,
                        label="BillingDoc-backfill",
                    )

                bill_upserted = await _upsert_billing_snapshots(db, bill_rows)
                del bill_rows

                db.add(
                    JobRun(
                        tenant_id=TENANT_ID,
                        job_type="sales_ingest_hourly",
                        status="success",
                        row_count=so_upserted + bill_upserted,
                        started_at=started_at,
                        finished_at=datetime.utcnow(),
                    )
                )
                await db.commit()

                logger.info(
                    "[ingest:%s] Completed in %.1fs (so=%d, billing=%d)",
                    job_id,
                    time.monotonic() - t0,
                    so_upserted,
                    bill_upserted,
                )

        except Exception as exc:
            logger.error("[ingest:%s] Failed: %s", job_id, exc, exc_info=True)
            try:
                async with AsyncSessionLocal() as db:
                    db.add(
                        JobRun(
                            tenant_id=TENANT_ID,
                            job_type="sales_ingest_hourly",
                            status="failed",
                            error_message=str(exc)[:500],
                            started_at=started_at,
                            finished_at=datetime.utcnow(),
                        )
                    )
                    await db.commit()
            except Exception:
                logger.error("[ingest:%s] Failed to record ingest failure", job_id, exc_info=True)


_AGG_STREAM_CHUNK = 10000  # rows per DB fetch chunk during aggregation
_AGG_MAX_PAYLOADS = 600_000  # hard ceiling on payloads loaded for aggregation


async def _stream_payloads(db, model_class, tenant_id: str) -> list[dict]:
    """Fetch all payloads in chunks to keep DB memory low."""
    rows = []
    offset = 0
    while True:
        result = await db.execute(
            select(model_class.payload)
            .where(model_class.tenant_id == tenant_id)
            .order_by(model_class.id)
            .offset(offset)
            .limit(_AGG_STREAM_CHUNK)
        )
        chunk = [row[0] for row in result.all()]
        if not chunk:
            break
        rows.extend(chunk)
        offset += len(chunk)
        if len(rows) % 50000 == 0:
            logger.info("Loaded %d payloads from %s", len(rows), model_class.__tablename__)
        if len(chunk) < _AGG_STREAM_CHUNK:
            break
        if len(rows) >= _AGG_MAX_PAYLOADS:
            logger.warning(
                "Hit payload ceiling of %d for %s — stopping",
                _AGG_MAX_PAYLOADS, model_class.__tablename__,
            )
            break
    return rows


async def run_aggregation_job():
    if _aggregate_lock.locked():
        logger.warning("Skipping aggregate job - previous run still in progress")
        return

    async with _aggregate_lock:
        started_at = datetime.utcnow()
        job_id = started_at.strftime("%Y%m%d_%H%M%S")
        logger.info("[agg:%s] Starting aggregate rebuild", job_id)

        try:
            async with AsyncSessionLocal() as db:
                # Set statement timeout (20 min for large datasets with 500K+ rows)
                await db.execute(text("SET statement_timeout = '1200s'"))

                t0 = time.monotonic()

                all_so_rows = await _stream_payloads(db, SalesOrderSnapshot, TENANT_ID)
                logger.info("[agg:%s] Loaded %d sales order payloads", job_id, len(all_so_rows))

                all_bill_rows = await _stream_payloads(db, BillingSnapshot, TENANT_ID)
                logger.info("[agg:%s] Loaded %d billing payloads", job_id, len(all_bill_rows))

                try:
                    metrics, fingerprint = await asyncio.wait_for(
                        asyncio.to_thread(calculate_aggregates, all_so_rows, all_bill_rows),
                        timeout=900,  # 15 min ceiling for pandas computation on large datasets
                    )
                except asyncio.TimeoutError:
                    logger.error("[agg:%s] calculate_aggregates timed out after 900s", job_id)
                    raise
                finally:
                    del all_so_rows, all_bill_rows

                await db.execute(text("SELECT pg_advisory_xact_lock(42)"))

                result = await db.execute(
                    select(CuratedAggregate)
                    .where(
                        CuratedAggregate.tenant_id == TENANT_ID,
                        CuratedAggregate.dataset_key == DATASET_KEY,
                    )
                    .order_by(CuratedAggregate.computed_at.desc())
                )
                existing_rows = result.scalars().all()
                current = existing_rows[0] if existing_rows else None
                data_changed = False

                if not current:
                    db.add(
                        CuratedAggregate(
                            tenant_id=TENANT_ID,
                            dataset_key=DATASET_KEY,
                            grain="multi",
                            metrics=metrics,
                            fingerprint=fingerprint,
                            computed_at=datetime.utcnow(),
                        )
                    )
                    data_changed = True
                elif current.fingerprint != fingerprint:
                    current.grain = "multi"
                    current.metrics = metrics
                    current.fingerprint = fingerprint
                    current.computed_at = datetime.utcnow()
                    data_changed = True

                if len(existing_rows) > 1:
                    stale_ids = [row.id for row in existing_rows[1:]]
                    await db.execute(delete(CuratedAggregate).where(CuratedAggregate.id.in_(stale_ids)))
                    data_changed = True

                db.add(
                    JobRun(
                        tenant_id=TENANT_ID,
                        job_type="sales_aggregate_hourly",
                        status="success",
                        row_count=None,
                        started_at=started_at,
                        finished_at=datetime.utcnow(),
                    )
                )

                await db.commit()
                del metrics

                if data_changed:
                    try:
                        await db.execute(text("SELECT refresh_sales_views()"))
                        await db.commit()
                    except Exception as view_exc:
                        logger.error("[agg:%s] Failed to refresh materialized views: %s", job_id, view_exc, exc_info=True)

                logger.info("[agg:%s] Completed in %.1fs", job_id, time.monotonic() - t0)

        except Exception as exc:
            logger.error("[agg:%s] Failed: %s", job_id, exc, exc_info=True)
            try:
                async with AsyncSessionLocal() as db:
                    db.add(
                        JobRun(
                            tenant_id=TENANT_ID,
                            job_type="sales_aggregate_hourly",
                            status="failed",
                            error_message=str(exc)[:500],
                            started_at=started_at,
                            finished_at=datetime.utcnow(),
                        )
                    )
                    await db.commit()
            except Exception:
                logger.error("[agg:%s] Failed to record aggregate failure", job_id, exc_info=True)


async def run_aggregation_if_pending():
    async with AsyncSessionLocal() as db:
        latest_ingest = await db.scalar(
            select(func.max(JobRun.finished_at)).where(
                JobRun.tenant_id == TENANT_ID,
                JobRun.job_type == "sales_ingest_hourly",
                JobRun.status == "success",
            )
        )
        latest_agg = await db.scalar(
            select(func.max(JobRun.finished_at)).where(
                JobRun.tenant_id == TENANT_ID,
                JobRun.job_type == "sales_aggregate_hourly",
                JobRun.status == "success",
            )
        )

    if not latest_ingest:
        logger.info("Skipping aggregate - no successful ingestion yet")
        return

    if latest_agg and latest_agg >= latest_ingest:
        logger.info("Skipping aggregate - no new ingestion since last aggregate")
        return

    await run_aggregation_job()


async def run_hourly_job():
    # Backward compatible combined flow
    if _job_lock.locked():
        logger.warning("Skipping combined job - previous run still in progress")
        return

    async with _job_lock:
        await run_ingestion_job()
        await run_aggregation_if_pending()


def start_scheduler():
    scheduler.add_job(
        run_hourly_job,
        "interval",
        hours=1,
        id="sales_orders_hourly_job",
        replace_existing=True,
        max_instances=1,
    )
    if not scheduler.running:
        scheduler.start()
    logger.info("Scheduler started (combined hourly pipeline)")


async def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped gracefully")
