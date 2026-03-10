"""One-time database bootstrap for the MCP server.

Runs on startup with admin credentials to ensure:
  1. The mcp_readonly role exists
  2. SELECT grants are in place for allowed tables/views
  3. The mcp_query_log audit table exists
  4. Performance indexes exist on JSONB snapshot tables

All statements are idempotent (IF NOT EXISTS / DO $$ guards).
"""

from __future__ import annotations

import logging

import asyncpg

from mcp_server.config import mcp_settings

logger = logging.getLogger("mcp_server.bootstrap")


async def _ensure_role(conn: asyncpg.Connection) -> None:
    """Create the mcp_readonly role if it doesn't exist."""
    db_user = mcp_settings.DB_USER
    db_password = mcp_settings.DB_PASSWORD
    # Escape single quotes in password for safe SQL embedding
    password_escaped = db_password.replace("'", "''")

    exists = await conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = $1", db_user,
    )
    if not exists:
        await conn.execute(
            f"CREATE ROLE {db_user} LOGIN PASSWORD '{password_escaped}'"
        )
        logger.info("Created role: %s", db_user)
    else:
        logger.info("Role already exists: %s", db_user)


async def _ensure_grants(conn: asyncpg.Connection) -> None:
    """Grant required permissions to the mcp_readonly role."""
    db_user = mcp_settings.DB_USER
    db_name = mcp_settings.DB_NAME
    admin_user = mcp_settings.DB_ADMIN_USER

    grants = [
        f"GRANT CONNECT ON DATABASE {db_name} TO {db_user}",
        f"GRANT USAGE ON SCHEMA public TO {db_user}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {db_user}",
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {admin_user} IN SCHEMA public "
            f"GRANT SELECT ON TABLES TO {db_user}"
        ),
        f"GRANT SELECT ON ALL TABLES IN SCHEMA information_schema TO {db_user}",
    ]
    # Serialize ACL updates across services to avoid transient
    # "tuple concurrently updated/deleted" errors in pg catalog tables.
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(62190411)")
        for sql in grants:
            await conn.execute(sql)


async def _ensure_audit_table(conn: asyncpg.Connection) -> None:
    """Create audit table and grant insert access."""
    db_user = mcp_settings.DB_USER

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_query_log (
            id          BIGSERIAL PRIMARY KEY,
            tenant_id   TEXT        NOT NULL,
            user_id     TEXT,
            session_id  TEXT,
            query_text  TEXT        NOT NULL,
            row_count   INTEGER,
            duration_ms INTEGER,
            error       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mcp_query_log_tenant_created "
        "ON mcp_query_log (tenant_id, created_at DESC)"
    )

    await conn.execute(f"GRANT INSERT ON mcp_query_log TO {db_user}")
    await conn.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE mcp_query_log_id_seq TO {db_user}"
    )


async def _ensure_indexes(conn: asyncpg.Connection) -> None:
    """Create performance indexes on JSONB snapshot tables if they exist."""
    # Sales order snapshots
    exists = await conn.fetchval(
        "SELECT to_regclass('public.sales_order_snapshots')"
    )
    if exists:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_so_snap_payload_gin "
            "ON sales_order_snapshots USING gin (payload)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_so_snap_tenant_changed "
            "ON sales_order_snapshots (tenant_id, last_changed_at)"
        )
        logger.info("Indexes ensured on sales_order_snapshots")

    # Billing snapshots
    exists = await conn.fetchval(
        "SELECT to_regclass('public.billing_snapshots')"
    )
    if exists:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bill_snap_payload_gin "
            "ON billing_snapshots USING gin (payload)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_bill_snap_tenant_bdate "
            "ON billing_snapshots (tenant_id, billing_date)"
        )
        logger.info("Indexes ensured on billing_snapshots")


async def run_bootstrap() -> None:
    """Connect as admin and run all bootstrap steps."""
    try:
        conn = await asyncpg.connect(
            host=mcp_settings.DB_HOST,
            port=mcp_settings.DB_PORT,
            database=mcp_settings.DB_NAME,
            user=mcp_settings.DB_ADMIN_USER,
            password=mcp_settings.DB_ADMIN_PASSWORD,
        )
        try:
            await _ensure_role(conn)
            await _ensure_grants(conn)
            await _ensure_audit_table(conn)
            await _ensure_indexes(conn)
            logger.info("MCP bootstrap completed successfully")
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning(
            "MCP bootstrap failed (non-fatal — may need manual setup): %s", exc,
        )
