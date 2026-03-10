"""MCP PostgreSQL server — read-only, tenant-isolated database access.

Exposes three tools to the LLM:
  • query_database  — execute a validated SELECT query
  • list_tables     — list available tables/views with column metadata
  • get_table_schema — column details for a single table/view

Transport: SSE on the port configured in MCP_PORT (default 8000).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP

from mcp_server.config import mcp_settings
from mcp_server.query_validator import (
    QueryValidationError,
    apply_row_limit,
    inject_tenant_filter,
    validate_query,
)
from mcp_server.audit import log_query
from mcp_server.bootstrap import run_bootstrap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("mcp_server")

# ── Global connection pool ────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=mcp_settings.DB_HOST,
            port=mcp_settings.DB_PORT,
            database=mcp_settings.DB_NAME,
            user=mcp_settings.DB_USER,
            password=mcp_settings.DB_PASSWORD,
            min_size=mcp_settings.DB_POOL_MIN,
            max_size=mcp_settings.DB_POOL_MAX,
            command_timeout=mcp_settings.QUERY_TIMEOUT_SECONDS,
        )
        logger.info(
            "asyncpg pool created (%s@%s:%s/%s)",
            mcp_settings.DB_USER,
            mcp_settings.DB_HOST,
            mcp_settings.DB_PORT,
            mcp_settings.DB_NAME,
        )
    return _pool


# ── Schema helpers ────────────────────────────────────────────────────────────

async def _fetch_columns(pool: asyncpg.Pool, table_name: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT column_name, data_type, is_nullable,
               column_default, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1
        ORDER BY ordinal_position
        """,
        table_name,
    )
    return [dict(r) for r in rows]


async def _fetch_all_schemas(pool: asyncpg.Pool) -> dict[str, Any]:
    all_names = mcp_settings.ALLOWED_TABLES + mcp_settings.ALLOWED_VIEWS
    schemas: dict[str, Any] = {}
    for name in all_names:
        cols = await _fetch_columns(pool, name)
        if cols:
            obj_type = (
                "table" if name in mcp_settings.ALLOWED_TABLES else "materialized_view"
            )
            schemas[name] = {
                "type": obj_type,
                "columns": [
                    {"name": c["column_name"], "type": c["data_type"]}
                    for c in cols
                ],
            }
    return schemas


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "qbot-mcp-postgres",
    instructions=(
        "Read-only PostgreSQL access for Qbot analytics. "
        "All queries are tenant-isolated and audited. "
        "Use query_database for SQL, list_tables/get_table_schema for schema discovery."
    ),
    host=mcp_settings.MCP_HOST,
    port=mcp_settings.MCP_PORT,
)


@mcp.tool()
async def query_database(
    sql: str,
    tenant_id: str,
    user_id: str = "",
    session_id: str = "",
) -> str:
    """Execute a read-only SQL SELECT against the Qbot PostgreSQL database.

    The query MUST target only allowed tables/views. Tenant isolation is
    enforced automatically — do NOT include a tenant_id filter in your SQL.

    Tables: curated_aggregates, sales_order_snapshots, billing_snapshots,
    job_runs, dashboard_widgets.
    Views: sales_hourly, sales_daily, sales_monthly, sales_quarterly,
    sales_yearly, sales_daily_cluster.

    JSONB columns use: payload->>'FieldName' syntax.
    Maximum 1000 rows. 30-second timeout.

    Args:
        sql: The SQL SELECT query to execute.
        tenant_id: Tenant ID for data isolation (required).
        user_id: User ID for audit logging.
        session_id: Chat session ID for audit logging.
    """
    pool = await _get_pool()
    start = time.monotonic()
    error_msg: str | None = None
    row_count: int | None = None

    try:
        if not tenant_id:
            raise QueryValidationError("tenant_id is required")

        # 1. Validate SQL structure and table references
        cleaned = validate_query(
            sql,
            mcp_settings.ALLOWED_TABLES,
            mcp_settings.ALLOWED_VIEWS,
        )

        # 2. Inject mandatory tenant filter
        tenant_sql, params = inject_tenant_filter(cleaned, tenant_id)

        # 3. Enforce row limit
        limited_sql = apply_row_limit(tenant_sql, mcp_settings.MAX_ROWS)

        # 4. Execute with timeout
        logger.info("MCP query [%s]: %s", tenant_id, limited_sql[:300])
        rows = await asyncio.wait_for(
            pool.fetch(limited_sql, *params),
            timeout=mcp_settings.QUERY_TIMEOUT_SECONDS,
        )

        row_count = len(rows)
        data = [dict(r) for r in rows]

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "MCP query completed: %d rows in %dms [%s]",
            row_count, duration_ms, tenant_id,
        )
        if duration_ms > 5000:
            logger.warning(
                "SLOW MCP query (%dms): %s", duration_ms, limited_sql[:500],
            )
        await log_query(
            pool,
            tenant_id=tenant_id,
            user_id=user_id or None,
            session_id=session_id or None,
            query_text=limited_sql,
            row_count=row_count,
            duration_ms=duration_ms,
        )

        return json.dumps(
            {"success": True, "row_count": row_count, "data": data},
            default=str,
        )

    except QueryValidationError as exc:
        error_msg = f"Validation error: {exc}"
    except asyncio.TimeoutError:
        error_msg = f"Query timed out after {mcp_settings.QUERY_TIMEOUT_SECONDS}s"
    except asyncpg.PostgresError as exc:
        error_msg = f"Database error: {exc}"
    except Exception as exc:
        logger.exception("Unexpected MCP query error")
        error_msg = f"Unexpected error: {exc}"

    duration_ms = int((time.monotonic() - start) * 1000)
    await log_query(
        pool,
        tenant_id=tenant_id,
        user_id=user_id or None,
        session_id=session_id or None,
        query_text=sql[:4000],
        row_count=row_count,
        duration_ms=duration_ms,
        error=error_msg,
    )
    return json.dumps({"success": False, "error": error_msg, "row_count": 0, "data": []})


@mcp.tool()
async def list_tables() -> str:
    """List all database tables and views available for querying.

    Returns a JSON object mapping table/view names to their type and columns.
    Use this to discover what data is available before writing SQL.
    """
    pool = await _get_pool()
    schemas = await _fetch_all_schemas(pool)
    return json.dumps(schemas, indent=2)


@mcp.tool()
async def get_table_schema(table_name: str) -> str:
    """Get detailed column information for a specific table or view.

    Returns column names, data types, nullability, and defaults.

    Args:
        table_name: Name of the table or view to inspect.
    """
    all_allowed = mcp_settings.ALLOWED_TABLES + mcp_settings.ALLOWED_VIEWS
    if table_name not in all_allowed:
        return json.dumps({
            "error": f"'{table_name}' is not an allowed object. Allowed: {all_allowed}"
        })

    pool = await _get_pool()
    columns = await _fetch_columns(pool, table_name)
    return json.dumps(columns, indent=2, default=str)


# ── Resource: static schema context ──────────────────────────────────────────

@mcp.resource("schema://tables")
async def schema_tables() -> str:
    """Complete schema of all available tables and materialized views."""
    pool = await _get_pool()
    schemas = await _fetch_all_schemas(pool)
    return json.dumps(schemas, indent=2, default=str)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Run the MCP server with SSE transport."""
    logger.info(
        "Starting MCP PostgreSQL server on %s:%s",
        mcp_settings.MCP_HOST,
        mcp_settings.MCP_PORT,
    )

    # Bootstrap: ensure mcp_readonly role, audit table, and indexes exist
    import asyncio
    asyncio.run(run_bootstrap())

    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
