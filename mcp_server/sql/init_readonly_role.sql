-- =============================================================================
-- MCP PostgreSQL Server — Read-Only Role & Audit Table
-- Run once against the qbot_db database as the postgres superuser.
-- =============================================================================

-- 1. Create the mcp_readonly role (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
        CREATE ROLE mcp_readonly LOGIN PASSWORD 'mcp_readonly_pass';
    END IF;
END $$;

-- 2. Revoke everything first (clean slate)
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mcp_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM mcp_readonly;

-- 3. Grant connection + schema usage
GRANT CONNECT ON DATABASE qbot_db TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;

-- 4. Grant SELECT on allowed tables
DO $$
BEGIN
    IF to_regclass('public.curated_aggregates') IS NOT NULL THEN
        GRANT SELECT ON public.curated_aggregates TO mcp_readonly;
    END IF;
    IF to_regclass('public.sales_order_snapshots') IS NOT NULL THEN
        GRANT SELECT ON public.sales_order_snapshots TO mcp_readonly;
    END IF;
    IF to_regclass('public.billing_snapshots') IS NOT NULL THEN
        GRANT SELECT ON public.billing_snapshots TO mcp_readonly;
    END IF;
    IF to_regclass('public.job_runs') IS NOT NULL THEN
        GRANT SELECT ON public.job_runs TO mcp_readonly;
    END IF;
    IF to_regclass('public.dashboard_widgets') IS NOT NULL THEN
        GRANT SELECT ON public.dashboard_widgets TO mcp_readonly;
    END IF;
END $$;

-- 5. Grant SELECT on materialized views
DO $$
BEGIN
    IF to_regclass('public.sales_hourly') IS NOT NULL THEN
        GRANT SELECT ON public.sales_hourly TO mcp_readonly;
    END IF;
    IF to_regclass('public.sales_daily') IS NOT NULL THEN
        GRANT SELECT ON public.sales_daily TO mcp_readonly;
    END IF;
    IF to_regclass('public.sales_monthly') IS NOT NULL THEN
        GRANT SELECT ON public.sales_monthly TO mcp_readonly;
    END IF;
    IF to_regclass('public.sales_quarterly') IS NOT NULL THEN
        GRANT SELECT ON public.sales_quarterly TO mcp_readonly;
    END IF;
    IF to_regclass('public.sales_yearly') IS NOT NULL THEN
        GRANT SELECT ON public.sales_yearly TO mcp_readonly;
    END IF;
    IF to_regclass('public.sales_daily_cluster') IS NOT NULL THEN
        GRANT SELECT ON public.sales_daily_cluster TO mcp_readonly;
    END IF;
END $$;

-- 6. Ensure future tables created by postgres remain readable for MCP role
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON TABLES TO mcp_readonly;

-- 7. Create audit table
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
);

CREATE INDEX IF NOT EXISTS idx_mcp_query_log_tenant_created
    ON mcp_query_log (tenant_id, created_at DESC);

-- 8. Grant INSERT-only on audit table (so MCP server can write logs)
GRANT INSERT ON mcp_query_log TO mcp_readonly;
GRANT USAGE, SELECT ON SEQUENCE mcp_query_log_id_seq TO mcp_readonly;

-- 9. Also grant SELECT on information_schema.columns (needed for schema introspection)
-- This is granted by default in PostgreSQL, but explicit for clarity.
GRANT SELECT ON ALL TABLES IN SCHEMA information_schema TO mcp_readonly;
