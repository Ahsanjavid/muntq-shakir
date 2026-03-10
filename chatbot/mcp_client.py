"""MCP client wrapper — calls the MCP PostgreSQL server from the chatbot.

Uses the official ``mcp`` SDK's SSE client transport to communicate with
the ``mcp-postgres`` service over HTTP.

Connection strategy:
- A module-level persistent session is lazily created on first use.
- If the session breaks (network error, server restart), it is
  automatically torn down and re-created on the next call.
- An asyncio.Lock serialises reconnection so concurrent callers don't
  each try to rebuild the session at the same time.
- A semaphore caps concurrent in-flight MCP calls to avoid overwhelming
  the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client

from chatbot.config import settings

logger = logging.getLogger(__name__)

_MCP_URL = f"{settings.MCP_SERVER_URL}/sse"

# Maximum concurrent MCP queries (prevents connection storms)
_MAX_CONCURRENT = 10

# Timeout for a single MCP tool call (server has 30s DB timeout internally).
# Set slightly above the server-side limit so the server returns a clean
# error rather than the client cutting the connection prematurely.
_MCP_CALL_TIMEOUT = 40

# ── Persistent session state ────────────────────────────────────────────────

_session: ClientSession | None = None
_exit_stack: AsyncExitStack | None = None
_last_connect_failure: float = 0.0  # monotonic timestamp of last connection failure
_connect_backoff: float = 0.0       # current backoff delay in seconds

# Lazily initialised to avoid creating asyncio primitives before the event loop
_lock: asyncio.Lock | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


async def _ensure_session() -> ClientSession:
    """Return the shared MCP session, creating it if needed.

    Uses a lock so that only one coroutine rebuilds the session at a time;
    all others wait and then reuse the newly-created session.
    Applies exponential backoff (up to 5s) if the server is unreachable.
    """
    global _session, _exit_stack, _last_connect_failure, _connect_backoff

    if _session is not None:
        return _session

    async with _get_lock():
        # Double-check after acquiring lock (another coroutine may have built it)
        if _session is not None:
            return _session

        # Exponential backoff to avoid hammering a down server
        if _connect_backoff > 0:
            logger.info("MCP reconnect backoff: %.1fs", _connect_backoff)
            await asyncio.sleep(_connect_backoff)

        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(
                sse_client(_MCP_URL)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            _exit_stack = stack
            _session = session
            _connect_backoff = 0.0  # Reset on success
            logger.info("MCP persistent session established → %s", _MCP_URL)
            return _session
        except Exception:
            await stack.aclose()
            # Increase backoff: 0.5 → 1 → 2 → 4 → 5 (capped)
            _connect_backoff = min((_connect_backoff or 0.25) * 2, 5.0)
            raise


async def _invalidate_session() -> None:
    """Tear down the current session so the next call creates a fresh one."""
    global _session, _exit_stack

    async with _get_lock():
        if _exit_stack is not None:
            try:
                await _exit_stack.aclose()
            except Exception:
                pass  # best-effort cleanup
        _session = None
        _exit_stack = None
        logger.info("MCP session invalidated — will reconnect on next call")


async def close_mcp_client() -> None:
    """Graceful shutdown — call from app lifespan teardown."""
    await _invalidate_session()


# ── Public helpers ──────────────────────────────────────────────────────────

def _parse_result(result: Any) -> dict[str, Any]:
    """Extract the standard response dict from an MCP tool result."""
    try:
        text = result.content[0].text if result.content else "{}"
    except (AttributeError, IndexError) as exc:
        logger.warning("MCP result has unexpected structure: %s", exc)
        text = "{}"

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("MCP result JSON decode failed: %s — raw: %s", exc, text[:200])
        return {"success": False, "count": 0, "data": [], "error": "MCP returned invalid response"}

    return {
        "success": parsed.get("success", False),
        "count": parsed.get("row_count", 0),
        "data": parsed.get("data", []),
        "error": parsed.get("error"),
    }


async def call_mcp_query(
    sql: str,
    tenant_id: str,
    user_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Execute a read-only SQL query via the MCP PostgreSQL server.

    Returns a dict matching the standard tool response shape:
        {"success": bool, "count": int, "data": list[dict], "error": str | None}
    """
    arguments = {
        "sql": sql,
        "tenant_id": tenant_id,
        "user_id": user_id or "",
        "session_id": session_id or "",
    }

    # Retry once on connection-level failure (session may have gone stale).
    # Timeouts are NOT retried — the query itself is slow, reconnecting won't help.
    for attempt in range(2):
        try:
            async with _get_semaphore():
                session = await _ensure_session()
                result = await asyncio.wait_for(
                    session.call_tool("query_database", arguments=arguments),
                    timeout=_MCP_CALL_TIMEOUT,
                )
            return _parse_result(result)

        except asyncio.TimeoutError:
            # Timeout means the query is too slow — do NOT retry or invalidate session
            logger.error("MCP call_tool timed out after %ds", _MCP_CALL_TIMEOUT)
            return {
                "success": False,
                "count": 0,
                "data": [],
                "error": f"MCP query timed out after {_MCP_CALL_TIMEOUT}s. The query may be too complex — try narrowing your search with more specific filters.",
            }

        except Exception as exc:
            if attempt == 0:
                logger.warning(
                    "MCP call failed (attempt 1), reconnecting: %s", exc,
                )
                await _invalidate_session()
                continue
            # Second attempt also failed — return error
            logger.exception("MCP query failed after retry: %s", exc)
            return {
                "success": False,
                "count": 0,
                "data": [],
                "error": f"MCP connection error: {exc}",
            }

    # Unreachable, but keeps type-checkers happy
    return {"success": False, "count": 0, "data": [], "error": "MCP call failed"}


async def list_mcp_tables() -> dict[str, Any]:
    """Call the list_tables tool to discover available schema."""
    for attempt in range(2):
        try:
            async with _get_semaphore():
                session = await _ensure_session()
                result = await asyncio.wait_for(
                    session.call_tool("list_tables", arguments={}),
                    timeout=_MCP_CALL_TIMEOUT,
                )
            text = result.content[0].text if result.content else "{}"
            return json.loads(text)
        except Exception as exc:
            if attempt == 0:
                logger.warning(
                    "MCP list_tables failed (attempt 1), reconnecting: %s", exc,
                )
                await _invalidate_session()
                continue
            logger.exception("MCP list_tables failed after retry: %s", exc)
            return {"error": str(exc)}

    return {"error": "MCP list_tables failed"}
