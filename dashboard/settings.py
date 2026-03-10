import logging
import os
from pathlib import Path
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:qbot345qbot@localhost:5433/qbot_db")

SAP_BASE_URL = os.getenv("SAP_BASE_URL", "")
SAP_CLIENT = os.getenv("SAP_CLIENT", "120")
SAP_USERNAME = os.getenv("SAP_USERNAME", "")
SAP_PASSWORD = os.getenv("SAP_PASSWORD", "")

MAX_YEARS = 2
INCREMENTAL_LOOKBACK_HOURS = int(os.getenv("INCREMENTAL_LOOKBACK_HOURS", "4"))
CHATBOT_WIDGET_URL = os.getenv("CHATBOT_WIDGET_URL", "/chatbot-ui/")
SERVE_LOCAL_CHATBOT_UI = os.getenv("SERVE_LOCAL_CHATBOT_UI", "true").strip().lower() in {"1", "true", "yes", "on"}
DASHBOARD_TIMEZONE = os.getenv("DASHBOARD_TIMEZONE", "Asia/Riyadh")

# ── Per-tenant timezone (future: read from DB) ─────────────────────
# Maps tenant_id → IANA timezone.  Falls back to DASHBOARD_TIMEZONE.
_TENANT_TIMEZONES: dict[str, str] = {}


def get_tenant_timezone(tenant_id: str = "") -> str:
    """Return the IANA timezone string for a tenant.

    Currently reads from DASHBOARD_TIMEZONE env var (single-tenant).
    When multi-tenant support is needed, populate _TENANT_TIMEZONES
    from a DB table or config file.
    """
    return _TENANT_TIMEZONES.get(tenant_id, DASHBOARD_TIMEZONE)
RUN_STARTUP_SYNC = os.getenv("RUN_STARTUP_SYNC", "true").strip().lower() in {"1", "true", "yes", "on"}
RUN_SCHEDULER = os.getenv("RUN_SCHEDULER", "true").strip().lower() in {"1", "true", "yes", "on"}
BATCH_UPSERT_SIZE = int(os.getenv("BATCH_UPSERT_SIZE", "5000"))


# ── Startup validation ──────────────────────────────────────────────
def _validate_settings():
    """Fail fast if critical env vars are missing."""
    errors = []
    if not SAP_BASE_URL:
        errors.append("SAP_BASE_URL is not set — scheduler cannot fetch data from SAP")
    if not SAP_USERNAME or not SAP_PASSWORD:
        errors.append("SAP_USERNAME / SAP_PASSWORD not set — SAP auth will fail")
    if "localhost" in DATABASE_URL and os.getenv("DATABASE_URL") is None:
        logger.warning(
            "DATABASE_URL is using default localhost — set it explicitly in production"
        )
    if errors:
        for err in errors:
            logger.error("CONFIG ERROR: %s", err)
        # Don't exit — allow dashboard to start for health checks, but log prominently
        logger.error(
            "Dashboard started with %d configuration error(s). "
            "Scheduler jobs will likely fail.",
            len(errors),
        )


_validate_settings()

