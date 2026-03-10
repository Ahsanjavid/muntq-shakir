-- =============================================================================
-- Performance indexes for MCP queries on JSONB snapshot tables.
-- Run once against qbot_db. All statements are idempotent (IF NOT EXISTS).
-- =============================================================================

-- GIN index on sales_order_snapshots.payload for JSONB operator queries
-- (payload->>'SalesOrder' = ..., payload @> ..., etc.)
CREATE INDEX IF NOT EXISTS ix_so_snap_payload_gin
    ON sales_order_snapshots USING gin (payload);

-- BTREE index on tenant + last_changed_at for date-range filters
CREATE INDEX IF NOT EXISTS ix_so_snap_tenant_changed
    ON sales_order_snapshots (tenant_id, last_changed_at);

-- GIN index on billing_snapshots.payload for JSONB operator queries
CREATE INDEX IF NOT EXISTS ix_bill_snap_payload_gin
    ON billing_snapshots USING gin (payload);

-- BTREE index on tenant + billing_date for date-range filters
CREATE INDEX IF NOT EXISTS ix_bill_snap_tenant_bdate
    ON billing_snapshots (tenant_id, billing_date);
