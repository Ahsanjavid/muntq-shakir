from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text

SQL_STATEMENTS = [
    # Drop views
    "DROP MATERIALIZED VIEW IF EXISTS sales_hourly CASCADE;",
    "DROP MATERIALIZED VIEW IF EXISTS sales_daily CASCADE;",
    "DROP MATERIALIZED VIEW IF EXISTS sales_monthly CASCADE;",
    "DROP MATERIALIZED VIEW IF EXISTS sales_quarterly CASCADE;",
    "DROP MATERIALIZED VIEW IF EXISTS sales_yearly CASCADE;",
    "DROP MATERIALIZED VIEW IF EXISTS sales_daily_cluster CASCADE;",

    # Hourly
    """
    CREATE MATERIALIZED VIEW sales_hourly AS
    SELECT tenant_id,
           key::timestamp AS period,
           value::numeric AS total_sales
    FROM curated_aggregates,
         jsonb_each_text((metrics::jsonb)->'hourly')
    WHERE dataset_key = 'sales_orders';
    """,

    "CREATE UNIQUE INDEX idx_sales_hourly_uniq ON sales_hourly (tenant_id, period);",

    # Daily
    """
    CREATE MATERIALIZED VIEW sales_daily AS
    SELECT tenant_id,
           key::date AS period,
           value::numeric AS total_sales
    FROM curated_aggregates,
         jsonb_each_text((metrics::jsonb)->'daily')
    WHERE dataset_key = 'sales_orders';
    """,

    "CREATE UNIQUE INDEX idx_sales_daily_uniq ON sales_daily (tenant_id, period);",

    # Monthly
    """
    CREATE MATERIALIZED VIEW sales_monthly AS
    SELECT tenant_id,
           key AS period,
           value::numeric AS total_sales
    FROM curated_aggregates,
         jsonb_each_text((metrics::jsonb)->'monthly')
    WHERE dataset_key = 'sales_orders';
    """,

    "CREATE UNIQUE INDEX idx_sales_monthly_uniq ON sales_monthly (tenant_id, period);",

    # Quarterly
    """
    CREATE MATERIALIZED VIEW sales_quarterly AS
    SELECT tenant_id,
           key AS period,
           value::numeric AS total_sales
    FROM curated_aggregates,
         jsonb_each_text((metrics::jsonb)->'quarterly')
    WHERE dataset_key = 'sales_orders';
    """,

    "CREATE UNIQUE INDEX idx_sales_quarterly_uniq ON sales_quarterly (tenant_id, period);",

    # Yearly
    """
    CREATE MATERIALIZED VIEW sales_yearly AS
    SELECT tenant_id,
           key::int AS year,
           value::numeric AS total_sales
    FROM curated_aggregates,
         jsonb_each_text((metrics::jsonb)->'yearly')
    WHERE dataset_key = 'sales_orders';
    """,

    "CREATE UNIQUE INDEX idx_sales_yearly_uniq ON sales_yearly (tenant_id, year);",

    # Clustered daily
    """
    CREATE MATERIALIZED VIEW sales_daily_cluster AS
    SELECT tenant_id,
           (elem->>'date')::date AS period,
           elem->>'sales_order_type' AS sales_order_type,
           elem->>'sold_to_party' AS sold_to_party,
           elem->>'purchase_order_by_customer' AS purchase_order_by_customer,
           elem->>'sales_organization' AS sales_organization,
           elem->>'distribution_channel' AS distribution_channel,
           elem->>'sales_group' AS sales_group,
           (elem->>'total_sales')::numeric AS total_sales,
           (elem->>'order_count')::int AS order_count
    FROM curated_aggregates
         CROSS JOIN LATERAL jsonb_array_elements(
             COALESCE((metrics::jsonb)->'cluster_daily', '[]'::jsonb)
         ) AS elem
    WHERE dataset_key = 'sales_orders';
    """,

    """
    CREATE UNIQUE INDEX idx_sales_daily_cluster_uniq
    ON sales_daily_cluster (
        tenant_id,
        period,
        COALESCE(sales_order_type, ''),
        COALESCE(sold_to_party, ''),
        COALESCE(purchase_order_by_customer, ''),
        COALESCE(sales_organization, ''),
        COALESCE(distribution_channel, ''),
        COALESCE(sales_group, '')
    );
    """,

    # Non-blocking refresh function.
    # Uses CONCURRENTLY so reads are not blocked during refresh.
    # Falls back to plain REFRESH on first run (empty view has no rows
    # for CONCURRENTLY to diff against).
    """
    CREATE OR REPLACE FUNCTION refresh_sales_views()
    RETURNS void AS $$
    DECLARE
        v_name text;
    BEGIN
        FOREACH v_name IN ARRAY ARRAY[
            'sales_hourly', 'sales_daily', 'sales_monthly',
            'sales_quarterly', 'sales_yearly', 'sales_daily_cluster'
        ] LOOP
            BEGIN
                EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I', v_name);
            EXCEPTION WHEN OTHERS THEN
                EXECUTE format('REFRESH MATERIALIZED VIEW %I', v_name);
            END;
        END LOOP;
    END;
    $$ LANGUAGE plpgsql;
    """
]


async def create_views(engine: AsyncEngine):
    async with engine.begin() as conn:
        for statement in SQL_STATEMENTS:
            await conn.execute(text(statement))
