-- =============================================================================
-- Performance indexes for MCP queries on JSONB snapshot tables.
-- Run once against qbot_db. All statements are idempotent (IF NOT EXISTS).
-- =============================================================================

DO $$
BEGIN
    -- sales_order_snapshots may not exist yet on fresh DB startup.
    IF to_regclass('public.sales_order_snapshots') IS NOT NULL THEN
        -- GIN index on payload for JSONB operator queries.
        CREATE INDEX IF NOT EXISTS ix_so_snap_payload_gin
            ON public.sales_order_snapshots USING gin (payload);

        -- BTREE index on tenant + last_changed_at for date-range filters.
        CREATE INDEX IF NOT EXISTS ix_so_snap_tenant_changed
            ON public.sales_order_snapshots (tenant_id, last_changed_at);
    END IF;

    -- billing_snapshots may not exist yet on fresh DB startup.
    IF to_regclass('public.billing_snapshots') IS NOT NULL THEN
        -- GIN index on payload for JSONB operator queries.
        CREATE INDEX IF NOT EXISTS ix_bill_snap_payload_gin
            ON public.billing_snapshots USING gin (payload);

        -- BTREE index on tenant + billing_date for date-range filters.
        CREATE INDEX IF NOT EXISTS ix_bill_snap_tenant_bdate
            ON public.billing_snapshots (tenant_id, billing_date);
    END IF;
END $$;
