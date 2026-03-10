from __future__ import annotations

from pydantic_settings import BaseSettings


class MCPSettings(BaseSettings):
    """Configuration for the MCP PostgreSQL server."""

    # ── Database (read-only pool) ────────────────────────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "qbot_db"
    DB_USER: str = "mcp_readonly"
    DB_PASSWORD: str = "mcp_readonly_pass"

    # ── Admin DB credentials (for bootstrap: create role, audit table, indexes)
    DB_ADMIN_USER: str = "postgres"
    DB_ADMIN_PASSWORD: str = "qbot345qbot"

    # Connection pool
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10

    # ── Query safety ──────────────────────────────────────────────────────
    QUERY_TIMEOUT_SECONDS: int = 30
    MAX_ROWS: int = 1000

    # ── MCP server ────────────────────────────────────────────────────────
    MCP_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8000

    # ── Allowed objects (whitelist) ───────────────────────────────────────
    ALLOWED_TABLES: list[str] = [
        "curated_aggregates",
        "sales_order_snapshots",
        "billing_snapshots",
        "job_runs",
        "dashboard_widgets",
    ]
    ALLOWED_VIEWS: list[str] = [
        "sales_hourly",
        "sales_daily",
        "sales_monthly",
        "sales_quarterly",
        "sales_yearly",
        "sales_daily_cluster",
    ]

    model_config = {"env_file": "mcp_server/.env", "env_file_encoding": "utf-8"}


mcp_settings = MCPSettings()
