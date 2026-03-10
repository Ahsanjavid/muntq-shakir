"""Fire-and-forget audit logging for MCP queries."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger(__name__)


async def log_query(
    pool: asyncpg.Pool,
    *,
    tenant_id: str,
    user_id: str | None = None,
    session_id: str | None = None,
    query_text: str,
    row_count: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Insert an audit record into ``mcp_query_log``.

    Never raises — logs a warning on failure so callers are not affected.
    """
    try:
        await pool.execute(
            """
            INSERT INTO mcp_query_log
                (tenant_id, user_id, session_id, query_text,
                 row_count, duration_ms, error, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            tenant_id,
            user_id,
            session_id,
            query_text[:4000],
            row_count,
            duration_ms,
            error[:2000] if error else None,
            datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.warning("Failed to write MCP audit log: %s", exc)
