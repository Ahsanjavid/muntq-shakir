import asyncio
import logging
import re
import time
import uuid
from datetime import date as date_cls, datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Any, Optional
from pydantic import BaseModel
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
from .views import create_views
from .database import engine, get_db, AsyncSessionLocal
from .scheduler import start_scheduler, stop_scheduler, run_hourly_job
from .schema import backfill_creation_date, ensure_dashboard_schema
from .models import (
    Base,
    BillingSnapshot,
    CuratedAggregate,
    DashboardWidget,
    SalesOrderSnapshot,
)
from .sales_filters import (
    DEFAULT_TENANT_ID,
    EXTENDED_DIMENSIONS,
    FILTER_DIMENSIONS,
    SALES_DATASET_KEY,
    normalize_dimension_value,
)
from .aggregator import (
    _build_billing_metrics,
    _build_custom_period_metrics,
    _build_filter_options,
    _build_mtd_metrics,
    _build_orders_metrics,
    _build_qtd_metrics,
    _build_today_metrics,
    _build_trends_metrics,
    _parse_creation_dates,
)
from .settings import (
    CHATBOT_WIDGET_URL,
    DASHBOARD_TIMEZONE,
    RUN_SCHEDULER,
    get_tenant_timezone,
    RUN_STARTUP_SYNC,
    SERVE_LOCAL_CHATBOT_UI,
)
import json as _json
from sqlalchemy import cast, literal, select, and_, text
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

try:
    _LOCAL_TZ = ZoneInfo(get_tenant_timezone())
except Exception:
    from datetime import timezone as _tz_fallback

    _LOCAL_TZ = _tz_fallback.utc


class DashboardWidgetCreate(BaseModel):
    title: str
    widget_type: str  # chart | table
    payload: dict[str, Any]
    source: str = "chatbot"


class DashboardWidgetOut(BaseModel):
    id: str
    tenant_id: str
    title: str
    widget_type: str
    payload: dict[str, Any]
    source: str
    created_at: str | None


def _parse_filter_csv(value):
    if value is None:
        return None
    values = [v.strip() for v in value.split(",") if v.strip()]
    if not values:
        return None
    return set(values)


def _apply_cluster_filters(cluster_rows, filters):
    filtered = []
    for row in cluster_rows:
        keep_row = True
        for key, allowed_values in filters.items():
            if not allowed_values:
                continue
            if str(row.get(key, "UNKNOWN")) not in allowed_values:
                keep_row = False
                break
        if keep_row:
            filtered.append(row)
    return filtered


def _sum_daily(rows):
    daily_totals = {}
    total_sales = 0.0
    order_count = 0

    for row in rows:
        date_key = str(row.get("date"))
        sales = float(row.get("total_sales", 0) or 0)
        count = int(row.get("order_count", 0) or 0)
        daily_totals[date_key] = daily_totals.get(date_key, 0.0) + sales
        total_sales += sales
        order_count += count

    return {
        "daily": {k: daily_totals[k] for k in sorted(daily_totals)},
        "total_sales": total_sales,
        "order_count": order_count,
    }


async def _get_latest_sales_metrics(db: AsyncSession):
    try:
        result = await db.execute(
            select(CuratedAggregate)
            .where(
                CuratedAggregate.tenant_id == DEFAULT_TENANT_ID,
                CuratedAggregate.dataset_key == SALES_DATASET_KEY,
            )
            .order_by(CuratedAggregate.computed_at.desc())
            .limit(1)
        )
    except Exception as exc:
        logger.error("DB error fetching sales metrics: %s", exc)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    aggregate = result.scalars().first()
    if not aggregate:
        raise HTTPException(status_code=404, detail="No sales aggregate available yet.")
    return aggregate.metrics or {}


def _today_local() -> date_cls:
    return datetime.now(_LOCAL_TZ).date()


def _safe_last_year(value: date_cls) -> date_cls:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _resolve_reference_date(df: pd.DataFrame, fallback: Optional[date_cls] = None) -> date_cls:
    today = fallback or _today_local()
    if df is None or df.empty or "date" not in df.columns:
        return today
    try:
        max_date = df["date"].max()
    except Exception:
        return today
    if pd.isna(max_date):
        return today
    if isinstance(max_date, pd.Timestamp):
        max_date = max_date.date()
    return max_date if max_date <= today else today


def _latest_sales_date_in_df(df: pd.DataFrame) -> Optional[date_cls]:
    if df is None or df.empty or "date" not in df.columns:
        return None
    try:
        max_date = df["date"].max()
    except Exception:
        return None
    if pd.isna(max_date):
        return None
    if isinstance(max_date, pd.Timestamp):
        max_date = max_date.date()
    return max_date


def _apply_today_freshness(
    payload: dict[str, Any],
    today: date_cls,
    latest_sales_date: Optional[date_cls],
) -> dict[str, Any]:
    """
    Force /sales/today to represent calendar-today only.
    If latest data is older than today, return explicit zeroed values.
    """
    is_fresh = latest_sales_date == today
    payload["is_fresh_today_data"] = is_fresh
    payload["latest_sales_date"] = str(latest_sales_date) if latest_sales_date else None
    payload["no_new_data"] = not is_fresh

    if is_fresh:
        return payload

    payload["date"] = str(today)
    payload["sales_order_count"] = 0
    payload["sales_order_value"] = 0.0
    payload["same_day_ly"] = 0.0
    payload["pct_change_ly"] = 0.0
    payload["actual_sales"] = 0.0
    payload["actual_sales_ly"] = 0.0
    payload["blocked_count"] = 0
    payload["unblocked_count"] = 0
    payload["blocked_value"] = 0.0
    payload["unblocked_value"] = 0.0
    payload["blocked_orders_list"] = []
    payload["by_sales_org"] = []
    payload["by_sales_group"] = []
    payload["by_order_type"] = []
    payload["by_customer"] = []
    payload["last_4_days"] = {str(today - timedelta(days=i)): 0.0 for i in range(3, -1, -1)}
    payload["today_billings"] = []
    payload["billing_by_org"] = []
    return payload


def _has_required_keys(payload: Any, required_keys: set[str]) -> bool:
    return isinstance(payload, dict) and required_keys.issubset(payload.keys())


def _build_today_metrics_safe(df: pd.DataFrame, billing: dict[str, Any], reference_date: Optional[date_cls] = None):
    try:
        return _build_today_metrics(df, billing, reference_date=reference_date)
    except TypeError:
        return _build_today_metrics(df, billing)


def _build_mtd_metrics_safe(df: pd.DataFrame, billing: dict[str, Any], reference_date: Optional[date_cls] = None):
    try:
        return _build_mtd_metrics(df, billing, reference_date=reference_date)
    except TypeError:
        return _build_mtd_metrics(df, billing)


def _build_qtd_metrics_safe(df: pd.DataFrame, billing: dict[str, Any], reference_date: Optional[date_cls] = None):
    try:
        return _build_qtd_metrics(df, billing, reference_date=reference_date)
    except TypeError:
        return _build_qtd_metrics(df, billing)


def _build_billing_metrics_safe(all_billing_rows: list[dict], reference_date: Optional[date_cls] = None):
    try:
        return _build_billing_metrics(all_billing_rows, reference_date=reference_date)
    except TypeError:
        return _build_billing_metrics(all_billing_rows)


def _latest_cached_sales_date(metrics: dict[str, Any]) -> Optional[date_cls]:
    daily = metrics.get("daily", {})
    if not isinstance(daily, dict) or not daily:
        return None
    parsed_dates = [_try_parse_date(key) for key in daily.keys()]
    parsed_dates = [d for d in parsed_dates if d is not None]
    if not parsed_dates:
        return None
    return max(parsed_dates)


def _is_cached_period_stale(metrics: dict[str, Any]) -> bool:
    today_payload = metrics.get("today", {})
    if not isinstance(today_payload, dict):
        return False
    cache_ref_date = _try_parse_date(today_payload.get("date"))
    latest_sales_date = _latest_cached_sales_date(metrics)
    return bool(cache_ref_date and latest_sales_date and latest_sales_date < cache_ref_date)


async def _get_latest_sales_metrics_or_none(db: AsyncSession) -> Optional[dict[str, Any]]:
    try:
        return await _get_latest_sales_metrics(db)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


async def _should_run_startup_sync() -> bool:
    """Run startup sync only if there is no aggregate yet or it is from a previous local day."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CuratedAggregate.computed_at)
            .where(
                CuratedAggregate.tenant_id == DEFAULT_TENANT_ID,
                CuratedAggregate.dataset_key == SALES_DATASET_KEY,
            )
            .order_by(CuratedAggregate.computed_at.desc().nullslast())
            .limit(1)
        )
        computed_at = result.scalars().first()

    if not computed_at:
        return True

    # Stored timestamps are UTC-naive in DB; normalize to UTC first.
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)

    latest_local_day = computed_at.astimezone(_LOCAL_TZ).date()
    today_local_day = datetime.now(_LOCAL_TZ).date()
    return latest_local_day < today_local_day


async def _ensure_mcp_readonly_grants() -> None:
    """Ensure MCP read-only role can query current and future dashboard objects."""
    grant_sql = """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
            GRANT USAGE ON SCHEMA public TO mcp_readonly;
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_readonly;
            ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
                GRANT SELECT ON TABLES TO mcp_readonly;

            IF to_regclass('public.mcp_query_log') IS NOT NULL THEN
                GRANT INSERT ON TABLE public.mcp_query_log TO mcp_readonly;
            END IF;
            IF to_regclass('public.mcp_query_log_id_seq') IS NOT NULL THEN
                GRANT USAGE, SELECT ON SEQUENCE public.mcp_query_log_id_seq TO mcp_readonly;
            END IF;
        END IF;
    END $$;
    """
    async with engine.begin() as conn:
        # Serialize ACL changes to avoid concurrent GRANT races during startup.
        await conn.execute(text("SELECT pg_advisory_xact_lock(62190411)"))
        await conn.execute(text(grant_sql))


@asynccontextmanager
async def lifespan(app):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("Dashboard starting up...")

    await ensure_dashboard_schema(engine)
    logger.info("Database schema ensured")
    await backfill_creation_date(engine)

    await create_views(engine)
    logger.info("Materialized views created")

    try:
        await _ensure_mcp_readonly_grants()
        logger.info("MCP read-only grants ensured")
    except Exception as exc:
        logger.warning("Could not ensure MCP grants: %s", exc)

    async def _startup_sync():
        """Run initial ETL in the background so the dashboard starts immediately."""
        should_sync = await _should_run_startup_sync()
        if should_sync:
            logger.info("Running startup sync job in background (stale or missing aggregate)...")
            try:
                await run_hourly_job()
                logger.info("Startup sync job completed")
            except Exception as e:
                logger.error("Startup sync job failed: %s", e)
        else:
            logger.info("Skipping startup sync job (already up to date for local day)")

    if RUN_STARTUP_SYNC:
        asyncio.create_task(_startup_sync())
    else:
        logger.info("RUN_STARTUP_SYNC is disabled; skipping startup sync task")

    if RUN_SCHEDULER:
        start_scheduler()
    else:
        logger.info("RUN_SCHEDULER is disabled; web process will not run scheduler")
    logger.info("Dashboard ready")

    yield

    logger.info("Dashboard shutting down...")
    if RUN_SCHEDULER:
        await stop_scheduler()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if SERVE_LOCAL_CHATBOT_UI:
    chatbot_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if chatbot_frontend_dir.exists():
        app.mount(
            "/chatbot-ui",
            StaticFiles(directory=chatbot_frontend_dir, html=True),
            name="chatbot-ui",
        )
    else:
        logger.warning(
            "SERVE_LOCAL_CHATBOT_UI enabled but frontend directory not found: %s",
            chatbot_frontend_dir,
        )


@app.get("/health")
async def health():
    """
    Liveness probe (fast):
    Must not depend on DB/network so container healthcheck stays stable
    even if SAP/DB are temporarily slow.
    """
    return {"status": "ok"}


@app.get("/ready")
async def readiness(db: AsyncSession = Depends(get_db)):
    """
    Readiness probe (deeper checks):
    Verifies DB connectivity and scheduler state.
    """
    db_ok = False
    try:
        await db.execute(select(CuratedAggregate.computed_at).limit(1))
        db_ok = True
    except Exception as exc:
        logger.warning("Readiness DB check failed: %s", exc)

    from .scheduler import scheduler as _sched
    sched_running = _sched.running if _sched else False
    scheduler_ok = sched_running if RUN_SCHEDULER else True
    status = "healthy" if db_ok and scheduler_ok else "degraded"

    return {
        "status": status,
        "database": "ok" if db_ok else "error",
        "scheduler": "running" if sched_running else ("disabled" if not RUN_SCHEDULER else "stopped"),
    }


@app.get("/")
async def dashboard_home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/ui-config")
async def ui_config():
    tz_name = get_tenant_timezone()
    try:
        tz_obj = ZoneInfo(tz_name)
        utc_offset = datetime.now(tz_obj).strftime("%z")  # e.g. "+0300"
        utc_offset_display = f"UTC{utc_offset[:3]}:{utc_offset[3:]}"  # e.g. "UTC+03:00"
    except Exception:
        utc_offset_display = "UTC"
    return {
        "chatbot_widget_url": CHATBOT_WIDGET_URL,
        "serve_local_chatbot_ui": SERVE_LOCAL_CHATBOT_UI,
        "dashboard_timezone": tz_name,
        "timezone_display": utc_offset_display,
    }


# ---------- Existing filter/graph endpoints ----------


@app.get("/sales/filters")
async def get_sales_filters(db: AsyncSession = Depends(get_db)):
    metrics = await _get_latest_sales_metrics_or_none(db)
    if metrics is None:
        # Fallback path while aggregate is not ready yet.
        today = _today_local()
        rows = await _query_filtered_snapshots(
            db,
            {},
            date_from=today - timedelta(days=730),
            date_to=today,
            row_limit=50_000,
        )
        df = _normalize_sales_df(rows)
        return {
            "dimensions": FILTER_DIMENSIONS,
            "filters": _build_filter_options(df),
            "totals": {
                "total_sales": float(df["TotalNetAmount"].sum()) if not df.empty else 0.0,
                "order_count": int(df["SalesOrder"].nunique()) if not df.empty else 0,
            },
        }

    return {
        "dimensions": FILTER_DIMENSIONS,
        "filters": metrics.get("filter_options", {}),
        "totals": metrics.get("totals", {"total_sales": 0.0, "order_count": 0}),
    }


@app.get("/sales/graph/daily")
async def get_sales_daily_graph(
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    metrics = await _get_latest_sales_metrics_or_none(db)

    filters = {
        "sales_order_type": _parse_filter_csv(sales_order_type),
        "sold_to_party": _parse_filter_csv(sold_to_party),
        "purchase_order_by_customer": _parse_filter_csv(purchase_order_by_customer),
        "sales_organization": _parse_filter_csv(sales_organization),
        "distribution_channel": _parse_filter_csv(distribution_channel),
        "sales_group": _parse_filter_csv(sales_group),
    }
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}

    cluster_rows = []
    if metrics is not None:
        cluster_rows = metrics.get("cluster_daily", [])
        if not isinstance(cluster_rows, list):
            cluster_rows = []

    if cluster_rows:
        filtered_rows = _apply_cluster_filters(cluster_rows, filters)
        response = _sum_daily(filtered_rows)
    else:
        if metrics is None:
            today = _today_local()
            raw_rows = await _query_filtered_snapshots(
                db,
                filters,
                date_from=today - timedelta(days=180),
                date_to=today,
                row_limit=50_000,
            )
            df = _normalize_sales_df(raw_rows)
            if df.empty:
                response = {"daily": {}, "total_sales": 0.0, "order_count": 0}
            else:
                daily = df.groupby("date")["TotalNetAmount"].sum().sort_index()
                response = {
                    "daily": {str(k): float(v) for k, v in daily.items()},
                    "total_sales": float(df["TotalNetAmount"].sum()),
                    "order_count": int(df["SalesOrder"].nunique()),
                }
        elif applied_filters:
            response = {"daily": {}, "total_sales": 0.0, "order_count": 0}
        else:
            response = {
                "daily": metrics.get("daily", {}),
                "total_sales": float(metrics.get("totals", {}).get("total_sales", 0.0)),
                "order_count": int(metrics.get("totals", {}).get("order_count", 0)),
            }

    response["applied_filters"] = applied_filters
    return response


@app.get("/sales/graph/breakdown")
async def get_sales_breakdown(
    dimension: str = Query(..., description="One of FILTER_DIMENSIONS keys"),
    top_n: int = Query(default=20, ge=1, le=200),
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    if dimension not in FILTER_DIMENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid dimension. Use one of: {', '.join(FILTER_DIMENSIONS.keys())}",
        )

    metrics = await _get_latest_sales_metrics_or_none(db)
    cluster_rows = []
    if metrics is not None:
        cluster_rows = metrics.get("cluster_daily", [])
        if not isinstance(cluster_rows, list):
            cluster_rows = []

    filters = {
        "sales_order_type": _parse_filter_csv(sales_order_type),
        "sold_to_party": _parse_filter_csv(sold_to_party),
        "purchase_order_by_customer": _parse_filter_csv(purchase_order_by_customer),
        "sales_organization": _parse_filter_csv(sales_organization),
        "distribution_channel": _parse_filter_csv(distribution_channel),
        "sales_group": _parse_filter_csv(sales_group),
    }
    filters.pop(dimension, None)
    if cluster_rows:
        filtered_rows = _apply_cluster_filters(cluster_rows, filters)
        buckets = {}
        for row in filtered_rows:
            key = str(row.get(dimension, "UNKNOWN"))
            sales = float(row.get("total_sales", 0) or 0)
            count = int(row.get("order_count", 0) or 0)
            if key not in buckets:
                buckets[key] = {"value": key, "total_sales": 0.0, "order_count": 0}
            buckets[key]["total_sales"] += sales
            buckets[key]["order_count"] += count

        items = sorted(
            buckets.values(),
            key=lambda item: (-item["total_sales"], item["value"]),
        )[:top_n]
    else:
        today = _today_local()
        raw_rows = await _query_filtered_snapshots(
            db,
            filters,
            date_from=today - timedelta(days=180),
            date_to=today,
            row_limit=50_000,
        )
        df = _normalize_sales_df(raw_rows)
        if df.empty:
            items = []
        else:
            grouped = (
                df.groupby(dimension, dropna=False)
                .agg(total_sales=("TotalNetAmount", "sum"), order_count=("SalesOrder", "nunique"))
                .reset_index()
                .sort_values(["total_sales", dimension], ascending=[False, True])
                .head(top_n)
            )
            items = [
                {
                    "value": str(r[dimension]),
                    "total_sales": float(r["total_sales"]),
                    "order_count": int(r["order_count"]),
                }
                for r in grouped.to_dict(orient="records")
            ]

    return {
        "dimension": dimension,
        "items": items,
        "applied_filters": {k: sorted(v) for k, v in filters.items() if v},
    }


# ---------- SPG page endpoints ----------


@app.get("/sales/today")
async def get_sales_today(
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    sales_office: Optional[str] = Query(default=None),
    sales_district: Optional[str] = Query(default=None),
    customer_group: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    filters = _build_filter_map(
        sales_order_type,
        sold_to_party,
        purchase_order_by_customer,
        sales_organization,
        distribution_channel,
        sales_group,
        sales_office,
        sales_district,
        customer_group,
    )
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}
    df_from = _try_parse_date(date_from)
    df_to = _try_parse_date(date_to)
    has_filters = bool(applied_filters) or df_from or df_to

    if not has_filters:
        today = _today_local()
        metrics = await _get_latest_sales_metrics_or_none(db)
        if metrics is not None:
            cached_today = metrics.get("today", {})
            cache_stale = _is_cached_period_stale(metrics)
            cached_date = _try_parse_date(cached_today.get("date"))
            if _has_required_keys(
                cached_today,
                {"date", "sales_order_count", "sales_order_value"},
            ) and not cache_stale and cached_date == today:
                return _apply_today_freshness(
                    dict(cached_today),
                    today=today,
                    latest_sales_date=_latest_cached_sales_date(metrics),
                )
            if _has_required_keys(
                cached_today,
                {"date", "sales_order_count", "sales_order_value"},
            ) and not cache_stale and cached_date != today:
                # Cached data is from an older day: return explicit "no new data"
                # without triggering an expensive full snapshot re-query.
                return _apply_today_freshness(
                    dict(cached_today),
                    today=today,
                    latest_sales_date=_latest_cached_sales_date(metrics),
                )
            logger.warning("Cached 'today' metrics are stale or missing required keys; recomputing from snapshots")

        filtered_rows = await _query_filtered_snapshots(
            db,
            {},
            date_from=today - timedelta(days=730),
            date_to=today,
            row_limit=50_000,
        )
        df = _normalize_sales_df(filtered_rows)
        latest_sales_date = _latest_sales_date_in_df(df)
        reference_date = _resolve_reference_date(df, fallback=today)
        billing = await _query_billing_metrics(db, reference_date=reference_date)
        result = _build_today_metrics_safe(df, billing, reference_date=reference_date)
        return _apply_today_freshness(result, today=today, latest_sales_date=latest_sales_date)

    filtered_rows = await _query_filtered_snapshots(db, filters, df_from, df_to)
    df = _normalize_sales_df(filtered_rows)
    today = _today_local()
    latest_sales_date = _latest_sales_date_in_df(df)
    reference_date = _resolve_reference_date(df)

    # Skip billing on filtered path — billing isn't dimension-filtered
    result = _build_today_metrics_safe(df, _EMPTY_BILLING, reference_date=reference_date)
    result = _apply_today_freshness(result, today=today, latest_sales_date=latest_sales_date)
    result["applied_filters"] = applied_filters
    return result


@app.get("/sales/mtd")
async def get_sales_mtd(
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    sales_office: Optional[str] = Query(default=None),
    sales_district: Optional[str] = Query(default=None),
    customer_group: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    filters = _build_filter_map(
        sales_order_type,
        sold_to_party,
        purchase_order_by_customer,
        sales_organization,
        distribution_channel,
        sales_group,
        sales_office,
        sales_district,
        customer_group,
    )
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}
    df_from = _try_parse_date(date_from)
    df_to = _try_parse_date(date_to)
    has_filters = bool(applied_filters) or df_from or df_to

    if not has_filters:
        metrics = await _get_latest_sales_metrics_or_none(db)
        if metrics is not None:
            cached_mtd = metrics.get("mtd", {})
            cache_stale = _is_cached_period_stale(metrics)
            if _has_required_keys(cached_mtd, {"period_start", "order_count", "sales_total"}) and not cache_stale:
                return cached_mtd
            logger.warning("Cached 'mtd' metrics are stale or missing required keys; recomputing from snapshots")

        today = _today_local()
        filtered_rows = await _query_filtered_snapshots(
            db,
            {},
            date_from=today - timedelta(days=730),
            date_to=today,
            row_limit=50_000,
        )
        df = _normalize_sales_df(filtered_rows)
        reference_date = _resolve_reference_date(df, fallback=today)
        billing = await _query_billing_metrics(db, reference_date=reference_date)
        return _build_mtd_metrics_safe(df, billing, reference_date=reference_date)

    filtered_rows = await _query_filtered_snapshots(db, filters, df_from, df_to)
    df = _normalize_sales_df(filtered_rows)
    reference_date = _resolve_reference_date(df)

    result = _build_mtd_metrics_safe(df, _EMPTY_BILLING, reference_date=reference_date)
    result["applied_filters"] = applied_filters
    return result


@app.get("/sales/qtd")
async def get_sales_qtd(
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    sales_office: Optional[str] = Query(default=None),
    sales_district: Optional[str] = Query(default=None),
    customer_group: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    filters = _build_filter_map(
        sales_order_type,
        sold_to_party,
        purchase_order_by_customer,
        sales_organization,
        distribution_channel,
        sales_group,
        sales_office,
        sales_district,
        customer_group,
    )
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}
    df_from = _try_parse_date(date_from)
    df_to = _try_parse_date(date_to)
    has_filters = bool(applied_filters) or df_from or df_to

    if not has_filters:
        metrics = await _get_latest_sales_metrics_or_none(db)
        if metrics is not None:
            cached_qtd = metrics.get("qtd", {})
            cache_stale = _is_cached_period_stale(metrics)
            if _has_required_keys(cached_qtd, {"period_start", "order_count", "sales_total"}) and not cache_stale:
                return cached_qtd
            logger.warning("Cached 'qtd' metrics are stale or missing required keys; recomputing from snapshots")

        today = _today_local()
        filtered_rows = await _query_filtered_snapshots(
            db,
            {},
            date_from=today - timedelta(days=730),
            date_to=today,
            row_limit=50_000,
        )
        df = _normalize_sales_df(filtered_rows)
        reference_date = _resolve_reference_date(df, fallback=today)
        billing = await _query_billing_metrics(db, reference_date=reference_date)
        return _build_qtd_metrics_safe(df, billing, reference_date=reference_date)

    filtered_rows = await _query_filtered_snapshots(db, filters, df_from, df_to)
    df = _normalize_sales_df(filtered_rows)
    reference_date = _resolve_reference_date(df)

    result = _build_qtd_metrics_safe(df, _EMPTY_BILLING, reference_date=reference_date)
    result["applied_filters"] = applied_filters
    return result


@app.get("/sales/period")
async def get_sales_period(
    date_from: str = Query(..., description="Start date YYYY-MM-DD (required)"),
    date_to: str = Query(..., description="End date YYYY-MM-DD (required)"),
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    sales_office: Optional[str] = Query(default=None),
    sales_district: Optional[str] = Query(default=None),
    customer_group: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Sales metrics for an arbitrary date range (e.g. 'January sales', 'last 3 months')."""
    df_from = _try_parse_date(date_from)
    df_to = _try_parse_date(date_to)
    if not df_from or not df_to:
        raise HTTPException(status_code=400, detail="Both date_from and date_to are required in YYYY-MM-DD format.")

    filters = _build_filter_map(
        sales_order_type,
        sold_to_party,
        purchase_order_by_customer,
        sales_organization,
        distribution_channel,
        sales_group,
        sales_office,
        sales_district,
        customer_group,
    )
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}

    filtered_rows = await _query_filtered_snapshots(db, filters, df_from, df_to)
    df = _normalize_sales_df(filtered_rows)

    result = _build_custom_period_metrics(df, df_from, df_to, _EMPTY_BILLING)
    result["applied_filters"] = applied_filters
    return result


@app.get("/sales/trends")
async def get_sales_trends(
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    sales_office: Optional[str] = Query(default=None),
    sales_district: Optional[str] = Query(default=None),
    customer_group: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    filters = _build_filter_map(
        sales_order_type,
        sold_to_party,
        purchase_order_by_customer,
        sales_organization,
        distribution_channel,
        sales_group,
        sales_office,
        sales_district,
        customer_group,
    )
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}
    df_from = _try_parse_date(date_from)
    df_to = _try_parse_date(date_to)
    has_filters = bool(applied_filters) or df_from or df_to

    if not has_filters:
        metrics = await _get_latest_sales_metrics_or_none(db)
        if metrics is not None:
            return metrics.get("trends", {})

        today = _today_local()
        filtered_rows = await _query_filtered_snapshots(
            db,
            {},
            date_from=today - timedelta(days=730),
            date_to=today,
            row_limit=100_000,
        )
        df = _normalize_sales_df(filtered_rows)
        return _build_trends_metrics(df)

    filtered_rows = await _query_filtered_snapshots(db, filters, df_from, df_to)
    df = _normalize_sales_df(filtered_rows)

    result = _build_trends_metrics(df)
    result["applied_filters"] = applied_filters
    return result


def _try_parse_date(value):
    if not value:
        return None
    try:
        return date_cls.fromisoformat(str(value))
    except Exception:
        return None


def _build_filter_map(
    sales_order_type,
    sold_to_party,
    purchase_order_by_customer,
    sales_organization,
    distribution_channel,
    sales_group,
    sales_office=None,
    sales_district=None,
    customer_group=None,
):
    return {
        "sales_order_type": _parse_filter_csv(sales_order_type),
        "sold_to_party": _parse_filter_csv(sold_to_party),
        "purchase_order_by_customer": _parse_filter_csv(purchase_order_by_customer),
        "sales_organization": _parse_filter_csv(sales_organization),
        "distribution_channel": _parse_filter_csv(distribution_channel),
        "sales_group": _parse_filter_csv(sales_group),
        "sales_office": _parse_filter_csv(sales_office),
        "sales_district": _parse_filter_csv(sales_district),
        "customer_group": _parse_filter_csv(customer_group),
    }


# ── All dimension keys → SAP JSONB field names ──────────────────────
_ALL_DIMS = {**FILTER_DIMENSIONS, **EXTENDED_DIMENSIONS}


_DEFAULT_FILTERED_ROW_LIMIT = 100_000  # safety cap to prevent OOM on filtered queries

# Empty billing stub — used on filtered paths where billing isn't dimension-aware
_EMPTY_BILLING: dict[str, Any] = {
    "today_actual_sales": 0.0, "today_actual_sales_ly": 0.0,
    "mtd_actual_sales": 0.0, "mtd_actual_sales_ly": 0.0,
    "qtd_actual_sales": 0.0, "qtd_actual_sales_ly": 0.0,
    "today_billings": [], "mtd_billings": [], "qtd_billings": [],
    "billing_by_org": [],
}

# ── Filtered result cache (avoids re-querying same data on tab switches) ──
_filtered_cache: dict[str, tuple[float, list[dict]]] = {}  # key → (expiry_ts, rows)
_FILTERED_CACHE_TTL = 60  # seconds
_FILTERED_CACHE_MAX = 20  # max cached filter combos


def _make_cache_key(filters: dict, date_from, date_to, row_limit) -> str:
    """Deterministic cache key for a filter combination."""
    parts = []
    for k in sorted(filters.keys()):
        v = filters[k]
        if v:
            parts.append(f"{k}={','.join(sorted(v))}")
    parts.append(f"df={date_from}")
    parts.append(f"dt={date_to}")
    parts.append(f"lim={row_limit}")
    return "|".join(parts)


def _get_cached_rows(key: str) -> Optional[list[dict]]:
    entry = _filtered_cache.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    _filtered_cache.pop(key, None)
    return None


def _set_cached_rows(key: str, rows: list[dict]) -> None:
    # Evict oldest if at capacity
    if len(_filtered_cache) >= _FILTERED_CACHE_MAX:
        oldest_key = min(_filtered_cache, key=lambda k: _filtered_cache[k][0])
        del _filtered_cache[oldest_key]
    _filtered_cache[key] = (time.time() + _FILTERED_CACHE_TTL, rows)


async def _query_filtered_snapshots(
    db: AsyncSession,
    filters: dict,
    date_from=None,
    date_to=None,
    row_limit: Optional[int] = None,
) -> list[dict]:
    """
    Query sales_order_snapshots with DB-level filtering.

    Uses indexed creation_date column for date range and GIN index for
    dimension containment queries. Results are cached for 60s so tab
    switches reuse the same data without re-querying.
    """
    # Check cache first
    cache_key = _make_cache_key(filters, date_from, date_to, row_limit)
    cached = _get_cached_rows(cache_key)
    if cached is not None:
        return cached

    # Set a statement timeout to prevent runaway queries
    await db.execute(text("SET LOCAL statement_timeout = '60s'"))

    conditions = [SalesOrderSnapshot.tenant_id == DEFAULT_TENANT_ID]

    # Dimension filters: use @> containment for single-value (leverages GIN index),
    # fall back to payload->>'Field' IN (...) for multi-value.
    for dim_key, allowed_values in filters.items():
        if not allowed_values:
            continue
        sap_field = _ALL_DIMS.get(dim_key)
        if not sap_field:
            continue
        if len(allowed_values) == 1:
            # @> uses the GIN index on payload
            val = next(iter(allowed_values))
            containment = _json.dumps({sap_field: val})
            conditions.append(
                SalesOrderSnapshot.payload.op("@>")(
                    cast(literal(containment), PG_JSONB)
                )
            )
        else:
            jsonb_field = SalesOrderSnapshot.payload[sap_field].astext
            conditions.append(jsonb_field.in_(allowed_values))

    # Date range: use the materialized creation_date column (indexed BTREE).
    # This replaces the previous per-row regex extraction from JSONB, giving
    # orders-of-magnitude faster filtering via index scan.
    if date_from:
        conditions.append(SalesOrderSnapshot.creation_date >= date_from)
    if date_to:
        conditions.append(SalesOrderSnapshot.creation_date <= date_to)

    # Always apply a row limit to prevent OOM
    effective_limit = row_limit if (row_limit and row_limit > 0) else _DEFAULT_FILTERED_ROW_LIMIT

    stmt = (
        select(SalesOrderSnapshot.payload)
        .where(and_(*conditions))
        .limit(effective_limit)
    )
    try:
        result = await db.execute(stmt)
    except Exception as exc:
        logger.error("DB error in _query_filtered_snapshots: %s", exc)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    rows = [row[0] for row in result.all()]

    # Cache for tab switches
    _set_cached_rows(cache_key, rows)

    return rows




def _normalize_sales_df(filtered_rows):
    """Convert raw SAP payload dicts to a Pandas DataFrame with normalized columns."""
    df = pd.DataFrame(filtered_rows)
    if "CreationDate" not in df.columns:
        df["CreationDate"] = None
    if "TotalNetAmount" not in df.columns:
        df["TotalNetAmount"] = 0
    if "SalesOrder" not in df.columns:
        df["SalesOrder"] = ""

    for dim_key, sap_field in _ALL_DIMS.items():
        if sap_field not in df.columns:
            df[sap_field] = None
        # Vectorized normalize: strip whitespace, replace empty/None → "UNKNOWN"
        s = df[sap_field].fillna("").astype(str).str.strip()
        s = s.where(~(s.eq("") | s.str.lower().eq("none")), "UNKNOWN")
        df[dim_key] = s

    for col in (
        "OverallSDProcessStatus",
        "TotalCreditCheckStatus",
        "OverallSDDocumentRejectionSts",
        "OverallTotalDeliveryStatus",
    ):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    df["CreationDate"] = _parse_creation_dates(df["CreationDate"])
    df["TotalNetAmount"] = pd.to_numeric(df["TotalNetAmount"], errors="coerce").fillna(
        0
    )
    df = df[df["CreationDate"].notna()].copy()
    df["date"] = (
        df["CreationDate"].dt.date if not df.empty else pd.Series(dtype="object")
    )
    return df


async def _query_billing_metrics(
    db: AsyncSession,
    reference_date: Optional[date_cls] = None,
) -> dict:
    """Fetch billing snapshots and build billing metrics.

    Uses pre-computed aggregate billing when available (fast path),
    falls back to loading raw snapshots only when needed.
    """
    # Try fast path: use pre-computed billing from aggregate
    try:
        metrics = await _get_latest_sales_metrics(db)
        mtd_data = metrics.get("mtd", {})
        today_data = metrics.get("today", {})
        cache_ref_date = _try_parse_date(today_data.get("date"))
        can_use_cache = (
            mtd_data.get("actual_sales") is not None
            and (reference_date is None or cache_ref_date == reference_date)
        )
        if can_use_cache:
            # Pre-computed billing exists — extract what we need
            return {
                "today_actual_sales": today_data.get("actual_sales", 0.0),
                "today_actual_sales_ly": today_data.get("actual_sales_ly", 0.0),
                "mtd_actual_sales": mtd_data.get("actual_sales", 0.0),
                "mtd_actual_sales_ly": mtd_data.get("actual_sales_ly", 0.0),
                "qtd_actual_sales": metrics.get("qtd", {}).get("actual_sales", 0.0),
                "qtd_actual_sales_ly": metrics.get("qtd", {}).get("actual_sales_ly", 0.0),
                "today_billings": today_data.get("today_billings", []),
                "mtd_billings": [],
                "qtd_billings": [],
                "billing_by_org": today_data.get("billing_by_org", []),
            }
    except HTTPException:
        pass  # No aggregate yet — fall back to raw query

    # Slow path: load raw billing snapshots (capped to prevent OOM)
    try:
        await db.execute(text("SET LOCAL statement_timeout = '60s'"))
        bill_result = await db.execute(
            select(BillingSnapshot.payload)
            .where(BillingSnapshot.tenant_id == DEFAULT_TENANT_ID)
            .order_by(BillingSnapshot.billing_date.desc())
            .limit(100_000)
        )
    except Exception as exc:
        logger.error("DB error in _query_billing_metrics slow path: %s", exc)
        return {
            "today_actual_sales": 0.0, "today_actual_sales_ly": 0.0,
            "mtd_actual_sales": 0.0, "mtd_actual_sales_ly": 0.0,
            "qtd_actual_sales": 0.0, "qtd_actual_sales_ly": 0.0,
            "today_billings": [], "mtd_billings": [], "qtd_billings": [],
            "billing_by_org": [],
        }
    all_billing_rows = [row[0] for row in bill_result.all()]
    return _build_billing_metrics_safe(all_billing_rows, reference_date=reference_date)


@app.get("/sales/orders")
async def get_sales_orders(
    # FILTER_DIMENSIONS
    sales_order_type: Optional[str] = Query(default=None),
    sold_to_party: Optional[str] = Query(default=None),
    purchase_order_by_customer: Optional[str] = Query(default=None),
    sales_organization: Optional[str] = Query(default=None),
    distribution_channel: Optional[str] = Query(default=None),
    sales_group: Optional[str] = Query(default=None),
    # EXTENDED_DIMENSIONS
    sales_office: Optional[str] = Query(default=None),
    sales_district: Optional[str] = Query(default=None),
    customer_group: Optional[str] = Query(default=None),
    # Date range
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    filters = {
        "sales_order_type": _parse_filter_csv(sales_order_type),
        "sold_to_party": _parse_filter_csv(sold_to_party),
        "purchase_order_by_customer": _parse_filter_csv(purchase_order_by_customer),
        "sales_organization": _parse_filter_csv(sales_organization),
        "distribution_channel": _parse_filter_csv(distribution_channel),
        "sales_group": _parse_filter_csv(sales_group),
        "sales_office": _parse_filter_csv(sales_office),
        "sales_district": _parse_filter_csv(sales_district),
        "customer_group": _parse_filter_csv(customer_group),
    }
    applied_filters = {k: sorted(v) for k, v in filters.items() if v}

    df_from = _try_parse_date(date_from)
    df_to = _try_parse_date(date_to)

    has_filters = bool(applied_filters) or df_from or df_to

    if not has_filters:
        # No filters - return pre-computed aggregate (fast path)
        metrics = await _get_latest_sales_metrics_or_none(db)
        if metrics is None:
            today = _today_local()
            filtered_rows = await _query_filtered_snapshots(
                db,
                {},
                date_from=today - timedelta(days=365),
                date_to=today,
                row_limit=100_000,
            )
            if not filtered_rows:
                return {
                    "total_orders": 0, "total_value": 0.0, "open_count": 0,
                    "closed_count": 0, "avg_value": 0.0, "blocked_count": 0,
                    "unblocked_count": 0, "highest_single_order": None,
                    "lowest_nonzero_single_order": None, "top_customer_by_value": None,
                    "top_customer_by_orders": None, "lowest_active_customer": None,
                    "top_material_by_orders": None, "top_material_by_value": None,
                    "monthly_order_count": {}, "monthly_order_value": {},
                    "avg_orders_per_day": {}, "avg_sales_per_day": {},
                    "daily_order_counts": {}, "daily_order_values": {},
                    "daily_dod_pct": {}, "by_status": [], "by_type": [],
                    "top_materials": [], "sales_order_status_table": [],
                    "detailed_view_table": [], "line_item_ac_table": [],
                    "line_item_details": [], "applied_filters": {},
                }

            df = _normalize_sales_df(filtered_rows)
            orders = _build_orders_metrics(df, all_so_rows=filtered_rows)
            orders["applied_filters"] = {}
            return orders

        result = metrics.get("orders", {})
        if result:
            result["applied_filters"] = {}
            return result
        # Aggregate exists but "orders" key is empty — return empty response
        # rather than falling through to load ALL snapshots from DB
        logger.warning("Pre-computed orders aggregate is empty — returning empty result")
        return {
            "total_orders": 0, "total_value": 0.0, "open_count": 0,
            "closed_count": 0, "avg_value": 0.0, "blocked_count": 0,
            "unblocked_count": 0, "highest_single_order": None,
            "lowest_nonzero_single_order": None, "top_customer_by_value": None,
            "top_customer_by_orders": None, "lowest_active_customer": None,
            "top_material_by_orders": None, "top_material_by_value": None,
            "monthly_order_count": {}, "monthly_order_value": {},
            "avg_orders_per_day": {}, "avg_sales_per_day": {},
            "daily_order_counts": {}, "daily_order_values": {},
            "daily_dod_pct": {}, "by_status": [], "by_type": [],
            "top_materials": [], "sales_order_status_table": [],
            "detailed_view_table": [], "line_item_ac_table": [],
            "line_item_details": [], "applied_filters": {},
        }
    # Filters active — re-compute from raw snapshots (DB-level filtering)
    filtered_rows = await _query_filtered_snapshots(db, filters, df_from, df_to)

    if not filtered_rows:
        return {
            "total_orders": 0,
            "total_value": 0.0,
            "open_count": 0,
            "closed_count": 0,
            "avg_value": 0.0,
            "blocked_count": 0,
            "unblocked_count": 0,
            "highest_single_order": None,
            "lowest_nonzero_single_order": None,
            "top_customer_by_value": None,
            "top_customer_by_orders": None,
            "lowest_active_customer": None,
            "top_material_by_orders": None,
            "top_material_by_value": None,
            "monthly_order_count": {},
            "monthly_order_value": {},
            "avg_orders_per_day": {},
            "avg_sales_per_day": {},
            "daily_order_counts": {},
            "daily_order_values": {},
            "daily_dod_pct": {},
            "by_status": [],
            "by_type": [],
            "top_materials": [],
            "sales_order_status_table": [],
            "detailed_view_table": [],
            "line_item_ac_table": [],
            "line_item_details": [],
            "applied_filters": applied_filters,
        }

    df = _normalize_sales_df(filtered_rows)

    orders = _build_orders_metrics(df, all_so_rows=filtered_rows)
    orders["applied_filters"] = applied_filters
    return orders


@app.get("/sales/last-updated")
async def get_last_updated(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(
            select(CuratedAggregate.computed_at)
            .where(
                CuratedAggregate.tenant_id == DEFAULT_TENANT_ID,
                CuratedAggregate.dataset_key == SALES_DATASET_KEY,
            )
            .order_by(CuratedAggregate.computed_at.desc().nullslast())
            .limit(1)
        )
        computed_at = result.scalars().first()
    except Exception as exc:
        logger.error("DB error in /sales/last-updated: %s", exc)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    if not computed_at:
        return {"last_updated": None}
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    # Return a timezone-aware timestamp in dashboard local timezone.
    return {"last_updated": computed_at.astimezone(_LOCAL_TZ).isoformat()}


@app.post("/dashboard/widgets")
async def create_dashboard_widget(
    req: DashboardWidgetCreate,
    db: AsyncSession = Depends(get_db),
):
    widget_type = req.widget_type.strip().lower()
    if widget_type not in {"chart", "table"}:
        raise HTTPException(
            status_code=400, detail="widget_type must be 'chart' or 'table'"
        )

    if not isinstance(req.payload, dict) or not req.payload:
        raise HTTPException(
            status_code=400, detail="payload must be a non-empty object"
        )

    widget = DashboardWidget(
        tenant_id=DEFAULT_TENANT_ID,
        title=req.title.strip() or f"Chatbot {widget_type.title()}",
        widget_type=widget_type,
        payload=req.payload,
        source=req.source.strip() or "chatbot",
        is_active=1,
    )
    db.add(widget)
    await db.commit()
    await db.refresh(widget)

    return {
        "id": str(widget.id),
        "message": "Widget added to dashboard",
    }


@app.get("/dashboard/widgets")
async def list_dashboard_widgets(
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DashboardWidget)
        .where(
            DashboardWidget.tenant_id == DEFAULT_TENANT_ID,
            DashboardWidget.is_active == 1,
        )
        .order_by(DashboardWidget.created_at.desc())
        .limit(100)
    )
    widgets = result.scalars().all()

    return {
        "items": [
            {
                "id": str(w.id),
                "tenant_id": w.tenant_id,
                "title": w.title,
                "widget_type": w.widget_type,
                "payload": w.payload or {},
                "source": w.source,
                "created_at": w.created_at.isoformat() if w.created_at else None,
            }
            for w in widgets
        ]
    }


@app.delete("/dashboard/widgets/{widget_id}")
async def delete_dashboard_widget(
    widget_id: str,
    db: AsyncSession = Depends(get_db),
):
    try:
        widget_uuid = uuid.UUID(widget_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid widget id")

    result = await db.execute(
        select(DashboardWidget).where(
            DashboardWidget.id == widget_uuid,
            DashboardWidget.tenant_id == DEFAULT_TENANT_ID,
            DashboardWidget.is_active == 1,
        )
    )
    widget = result.scalars().first()
    if not widget:
        raise HTTPException(status_code=404, detail="Widget not found")

    widget.is_active = 0
    await db.commit()
    return {"deleted": widget_id}
