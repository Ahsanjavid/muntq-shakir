from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import redis.asyncio as aioredis

from chatbot.config import settings

logger = logging.getLogger(__name__)

# Max messages kept per session (last 5 user+assistant pairs = 10 entries)
MAX_HISTORY = 20

# Session TTL in seconds (24 hours)
SESSION_TTL = 60 * 60 * 24

# Session IDs must be valid UUIDs (hex with dashes)
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _safe_json(raw: str, fallback: Any) -> Any:
    """Decode JSON with a fallback for corrupt data."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupt JSON in session field, using fallback")
        return fallback


class SessionManager:
    """Async Redis session store with enterprise error handling."""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        """Open a connection pool to Redis."""
        self._redis = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("Redis connected → %s", settings.REDIS_URL)

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis disconnected")

    # helpers

    def _key(self, session_id: str) -> str:
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"Invalid session_id format: {session_id!r}")
        return f"chat:session:{session_id}"

    def _ensure_redis(self) -> aioredis.Redis:
        """Return the Redis client or raise a clear error."""
        if self._redis is None:
            raise RuntimeError("Redis not connected — call connect() first")
        return self._redis

    # public API

    async def create_session(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str | None = None,
    ) -> str:
        """Create a new session and return its ID."""
        redis = self._ensure_redis()
        sid = session_id or str(uuid.uuid4())
        data = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "messages": "[]",
            "pending_tool": "",
            "pending_params": "{}",
        }
        try:
            async with redis.pipeline(transaction=True) as pipe:
                await pipe.hset(self._key(sid), mapping=data)
                await pipe.expire(self._key(sid), SESSION_TTL)
                await pipe.execute()
        except Exception as exc:
            logger.error("Redis create_session failed for %s: %s", sid, exc)
            raise
        logger.info("Session created: %s (tenant=%s, user=%s)", sid, tenant_id, user_id)
        return sid

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Load session from Redis. Returns None if not found / expired / error."""
        redis = self._ensure_redis()
        try:
            raw = await redis.hgetall(self._key(session_id))
        except ValueError:
            logger.warning("Invalid session_id rejected: %s", session_id)
            return None
        except Exception as exc:
            logger.error("Redis get_session failed for %s: %s", session_id, exc)
            return None  # Degrade gracefully — caller handles None
        if not raw:
            return None
        return {
            "tenant_id": raw.get("tenant_id", ""),
            "user_id": raw.get("user_id", ""),
            "messages": _safe_json(raw.get("messages", "[]"), []),
            "pending_tool": raw.get("pending_tool", ""),
            "pending_params": _safe_json(raw.get("pending_params", "{}"), {}),
            "extra": _safe_json(raw.get("extra", "{}"), {}),
        }

    async def save_session(
        self,
        session_id: str,
        messages: list[dict[str, str]],
        pending_tool: str = "",
        pending_params: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Persist messages + pending state.  Trims history to MAX_HISTORY."""
        redis = self._ensure_redis()
        trimmed = messages[-MAX_HISTORY:]  # keep last N entries
        data: dict[str, str] = {
            "messages": json.dumps(trimmed, default=str),
            "pending_tool": pending_tool,
            "pending_params": json.dumps(pending_params or {}, default=str),
        }
        if extra is not None:
            data["extra"] = json.dumps(extra, default=str)
        try:
            async with redis.pipeline(transaction=True) as pipe:
                await pipe.hset(self._key(session_id), mapping=data)
                await pipe.expire(self._key(session_id), SESSION_TTL)
                await pipe.execute()
        except Exception as exc:
            # Do NOT re-raise — the user already has their answer
            logger.error("Redis save_session failed for %s: %s", session_id, exc)

    async def delete_session(self, session_id: str) -> None:
        redis = self._ensure_redis()
        try:
            await redis.delete(self._key(session_id))
        except Exception as exc:
            logger.error("Redis delete_session failed for %s: %s", session_id, exc)

    async def session_exists(self, session_id: str) -> bool:
        redis = self._ensure_redis()
        try:
            return bool(await redis.exists(self._key(session_id)))
        except Exception as exc:
            logger.error("Redis session_exists failed for %s: %s", session_id, exc)
            return False



session_manager = SessionManager()
