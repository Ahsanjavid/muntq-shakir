from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from chatbot.config import settings

logger = logging.getLogger(__name__)

# ── Persistent HTTP client (connection pooling) ────────────────────────────

_agg_client: httpx.AsyncClient | None = None
_agg_client_lock: asyncio.Lock | None = None

_MAX_RETRIES = 2
_RETRY_BACKOFF = 0.5  # seconds; doubles each retry
_RETRYABLE_STATUS = {502, 503, 504}


def _get_client_lock() -> asyncio.Lock:
    global _agg_client_lock
    if _agg_client_lock is None:
        _agg_client_lock = asyncio.Lock()
    return _agg_client_lock


async def _get_agg_client() -> httpx.AsyncClient:
    """Return a module-level httpx client for dashboard API calls (thread-safe)."""
    global _agg_client
    # Fast path — no lock needed
    if _agg_client is not None and not _agg_client.is_closed:
        return _agg_client
    # Slow path — serialize client creation
    async with _get_client_lock():
        if _agg_client is not None and not _agg_client.is_closed:
            return _agg_client
        _agg_client = httpx.AsyncClient(
            timeout=45.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return _agg_client


async def close_agg_client() -> None:
    """Gracefully close the HTTP client (call on shutdown)."""
    global _agg_client
    if _agg_client is not None and not _agg_client.is_closed:
        await _agg_client.aclose()
        _agg_client = None


_REPORT_ENDPOINTS: dict[str, str] = {
    "today": "/sales/today",
    "mtd": "/sales/mtd",
    "qtd": "/sales/qtd",
    "trends": "/sales/trends",
    "orders": "/sales/orders",
    "period": "/sales/period",
}

_SUPPORTED_FILTERS = {
    "sales_order_type",
    "sold_to_party",
    "purchase_order_by_customer",
    "sales_organization",
    "distribution_channel",
    "sales_group",
    "sales_office",
    "sales_district",
    "customer_group",
    "date_from",
    "date_to",
}


def _rows_from_payload(report_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if report_type == "trends":
        monthly_by_year = payload.get("monthly_by_year")
        if isinstance(monthly_by_year, dict):
            for year, months in monthly_by_year.items():
                if not isinstance(months, dict):
                    continue
                for month, total_sales in months.items():
                    rows.append(
                        {
                            "year": str(year),
                            "month": str(month),
                            "period": f"{year}-{month}",
                            "total_sales": float(total_sales or 0),
                        }
                    )

        if not rows:
            monthly_sales = payload.get("monthly_sales")
            if isinstance(monthly_sales, dict):
                for period, total_sales in monthly_sales.items():
                    rows.append(
                        {
                            "period": str(period),
                            "total_sales": float(total_sales or 0),
                        }
                    )

        if not rows:
            daily_sales = payload.get("daily_sales")
            if isinstance(daily_sales, dict):
                for period, total_sales in daily_sales.items():
                    rows.append(
                        {
                            "period": str(period),
                            "total_sales": float(total_sales or 0),
                        }
                    )
        return rows

    if report_type == "period":
        # Custom date range — same structure as mtd/qtd
        daily_trend = payload.get("daily_trend")
        if isinstance(daily_trend, dict):
            for day, total_sales in daily_trend.items():
                rows.append(
                    {
                        "date": str(day),
                        "total_sales": float(total_sales or 0),
                    }
                )
        for key in ("by_order_type", "by_sales_org", "by_customer"):
            val = payload.get(key)
            if isinstance(val, list):
                rows.extend([item for item in val if isinstance(item, dict)])
        return rows

    if report_type == "orders":
        for key in ("top_materials", "by_status", "by_type"):
            val = payload.get(key)
            if isinstance(val, list):
                rows.extend([item for item in val if isinstance(item, dict)])

        if not rows:
            daily_counts = payload.get("daily_order_counts")
            daily_values = payload.get("daily_order_values")
            if isinstance(daily_counts, dict):
                for day, order_count in daily_counts.items():
                    rows.append(
                        {
                            "date": str(day),
                            "order_count": int(order_count or 0),
                            "total_sales": float((daily_values or {}).get(day, 0) or 0),
                        }
                    )
        return rows

    for key in ("kpis", "summary"):
        val = payload.get(key)
        if isinstance(val, dict):
            rows.append(val)

    return rows


async def call_sales_aggregates(params: dict[str, Any]) -> dict[str, Any]:
    report_type = str(params.get("report_type", "")).strip().lower()
    endpoint = _REPORT_ENDPOINTS.get(report_type)
    if not endpoint:
        return {
            "success": False,
            "count": 0,
            "data": [],
            "raw_payload": {},
            "error": f"Unsupported report_type '{report_type}'. "
            f"Use one of: {', '.join(_REPORT_ENDPOINTS.keys())}",
        }

    query = {
        k: v
        for k, v in params.items()
        if k in _SUPPORTED_FILTERS and v not in (None, "", [])
    }
    url = f"{settings.DASHBOARD_BASE_URL}{endpoint}"

    last_error: str = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            client = await _get_agg_client()
            resp = await client.get(url, params=query)
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "Aggregate API returned %d (attempt %d/%d), retrying",
                    resp.status_code, attempt + 1, _MAX_RETRIES + 1,
                )
                await asyncio.sleep(_RETRY_BACKOFF * (2 ** attempt))
                continue
            resp.raise_for_status()
            payload = resp.json()
            break
        except httpx.HTTPStatusError as exc:
            last_error = f"HTTP {exc.response.status_code}"
            logger.error("Aggregate API HTTP error: %s params=%s", last_error, query)
            return {
                "success": False,
                "count": 0,
                "data": [],
                "raw_payload": {},
                "error": f"Dashboard API returned {last_error}",
            }
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout) as exc:
            last_error = type(exc).__name__
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "Aggregate API %s (attempt %d/%d), retrying",
                    last_error, attempt + 1, _MAX_RETRIES + 1,
                )
                await asyncio.sleep(_RETRY_BACKOFF * (2 ** attempt))
                continue
            logger.exception("Aggregate API call failed after retries: params=%s", query)
            return {
                "success": False,
                "count": 0,
                "data": [],
                "raw_payload": {},
                "error": f"Dashboard API unavailable ({last_error})",
            }
        except Exception:
            logger.exception("Aggregate API unexpected error: params=%s", query)
            return {
                "success": False,
                "count": 0,
                "data": [],
                "raw_payload": {},
                "error": "Dashboard API error — please try again",
            }
    else:
        # All retries exhausted (only hit via retryable status codes)
        return {
            "success": False,
            "count": 0,
            "data": [],
            "raw_payload": {},
            "error": f"Dashboard API unavailable after retries ({last_error})",
        }

    rows = _rows_from_payload(report_type, payload if isinstance(payload, dict) else {})
    return {
        "success": True,
        "count": len(rows),
        "data": rows,
        "raw_payload": payload if isinstance(payload, dict) else {},
        "requested_date_from": query.get("date_from"),
        "requested_date_to": query.get("date_to"),
        "error": None,
    }
