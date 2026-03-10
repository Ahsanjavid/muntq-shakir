# Current Aggregate Behavior

Last verified against code on: 2026-03-07

## Scope
This document explains how these files currently work together:

1. `dashboard/aggregator.py`
2. `chatbot/aggregate_client.py`
3. `dashboard/aggregate_worker.py`

Supporting files used for tracing formulas and UI placement:
- `dashboard/scheduler.py`
- `dashboard/sap_extractor.py`
- `dashboard/sales_filters.py`
- `dashboard/main.py`
- `dashboard/static/app.js`

## End-to-end flow
1. `aggregate_worker.py` starts an async scheduler (every 5 minutes) and calls `run_aggregation_if_pending()`.
2. `run_aggregation_if_pending()` (in `scheduler.py`) checks `job_runs`:
   - If no successful ingest run exists, it skips.
   - If latest aggregate is newer than or equal to latest ingest, it skips.
   - Otherwise it runs `run_aggregation_job()`.
3. `run_aggregation_job()` loads all `SalesOrderSnapshot.payload` and `BillingSnapshot.payload` rows, then calls:
   - `calculate_aggregates(all_so_rows, all_bill_rows)` from `aggregator.py`
4. `calculate_aggregates()` computes one metrics JSON blob (hourly/daily/monthly/quarterly/yearly/today/mtd/qtd/trends/orders/filter options/cluster).
5. The blob is stored in `curated_aggregates.metrics` with a fingerprint hash.
6. Dashboard API endpoints (`/sales/today`, `/sales/mtd`, `/sales/qtd`, `/sales/trends`, `/sales/orders`, `/sales/period`) read from this blob when possible.
7. `chatbot/aggregate_client.py` calls those same endpoints and flattens payloads into `data` rows for chatbot responses.

---

## Q1) Formulas and SAP fields used for aggregation

## SAP fields fetched from SAP and used downstream

### Sales order header fields (`API_SALES_ORDER_SRV/A_SalesOrder`)
- `SalesOrder`
- `SalesOrderType`
- `SalesOrganization`
- `DistributionChannel`
- `SalesGroup`
- `SoldToParty`
- `PurchaseOrderByCustomer`
- `TotalNetAmount`
- `TransactionCurrency`
- `CreationDate`
- `LastChangeDateTime`
- `OverallSDProcessStatus`
- `OverallTotalDeliveryStatus`
- `OverallSDDocumentRejectionSts`
- `TotalCreditCheckStatus`
- `SalesOffice`
- `SalesDistrict`
- `CustomerGroup`

### Sales order item fields (`$expand=to_Item`)
- `SalesOrderItem`
- `Material`
- `SalesOrderItemText`
- `RequestedQuantity`
- `RequestedQuantityUnit`
- `NetAmount`
- `MaterialGroup`
- `ProductionPlant`
- `SalesOrderItemCategory`
- `HigherLevelItem`

### Billing fields (`API_BILLING_DOCUMENT_SRV/A_BillingDocument`)
- `BillingDocument`
- `BillingDocumentType`
- `SoldToParty`
- `SalesOrganization`
- `CompanyCode`
- `BillingDocumentDate`
- `TotalNetAmount`
- `TransactionCurrency`
- `BillingDocumentIsCancelled`

## Dimension aliases used in metrics
- `sales_order_type` <- `SalesOrderType`
- `sold_to_party` <- `SoldToParty`
- `purchase_order_by_customer` <- `PurchaseOrderByCustomer`
- `sales_organization` <- `SalesOrganization`
- `distribution_channel` <- `DistributionChannel`
- `sales_group` <- `SalesGroup`
- `sales_office` <- `SalesOffice`
- `sales_district` <- `SalesDistrict`
- `customer_group` <- `CustomerGroup`

## Exact SAP field usage per formula input
These are the exact SAP OData property names used in calculations (stored in snapshot JSON payloads as-is).

- Date bucketing for sales aggregates (`hourly/daily/monthly/quarterly/yearly`, today/mtd/qtd/trends/orders windows):
  - `CreationDate`
- Date bucketing for billing/actual-sales aggregates:
  - `BillingDocumentDate`
- Sales value sums (order value):
  - `TotalNetAmount` (sales order header)
- Billing value sums (actual sales):
  - `TotalNetAmount` (billing document)
- Distinct order counts:
  - `SalesOrder`
- Grouping by order type/product category:
  - `SalesOrderType`
- Grouping by customer:
  - `SoldToParty`
- Grouping by customer PO:
  - `PurchaseOrderByCustomer`
- Grouping by sales org:
  - `SalesOrganization`
- Grouping/filter by distribution channel:
  - `DistributionChannel`
- Grouping/filter by sales group:
  - `SalesGroup`
- Grouping/filter by extended order report dimensions:
  - `SalesOffice`
  - `SalesDistrict`
  - `CustomerGroup`
- Blocked/unblocked detection:
  - `TotalCreditCheckStatus`
  - `OverallSDDocumentRejectionSts`
- Open/closed status and status breakdown:
  - `OverallSDProcessStatus`
- Line-item material metrics (`top_materials`, line-item tables):
  - `to_Item/Material`
  - `to_Item/NetAmount`
  - `to_Item/RequestedQuantity`
  - `to_Item/RequestedQuantityUnit`
  - `to_Item/SalesOrderItemText`
  - `to_Item/ProductionPlant`
  - `to_Item/SalesOrderItemCategory`
  - `to_Item/SalesOrderItem`
- Billing document tables and cancellation filtering:
  - `BillingDocument`
  - `BillingDocumentType`
  - `SoldToParty`
  - `SalesOrganization`
  - `BillingDocumentDate`
  - `TotalNetAmount`
  - `BillingDocumentIsCancelled`

## Core formula rules

### Shared formulas
- Percent change: `((current - previous) / abs(previous)) * 100`, rounded to 2 decimals.
- If previous is 0:
  - returns `0.0` when current is 0
  - returns `100.0` when current is non-zero

### Blocked order logic
An order is blocked when either condition is true:
- `TotalCreditCheckStatus` in `{"B", "C"}`
- `OverallSDDocumentRejectionSts` is non-empty and not `"A"`

### Time basis
- Sales order metrics are based on `CreationDate` (converted to dashboard timezone).
- Billing metrics are based on `BillingDocumentDate` (converted to dashboard timezone).

## Aggregate formulas by metric group

### Global time-series and totals (`metrics.hourly/daily/monthly/quarterly/yearly/totals`)
- `hourly[hour] = SUM(TotalNetAmount)` grouped by `CreationDate` floored to hour
- `daily[date] = SUM(TotalNetAmount)` grouped by `CreationDate.date`
- `monthly[YYYY-MM] = SUM(TotalNetAmount)` grouped by month period
- `quarterly[YYYYQn] = SUM(TotalNetAmount)` grouped by quarter period
- `yearly[YYYY] = SUM(TotalNetAmount)` grouped by year
- `totals.total_sales = SUM(TotalNetAmount)`
- `totals.order_count = COUNT(DISTINCT SalesOrder)`

### Filter option and cluster metrics
- `filter_options[dim]`:
  - grouped by each dimension alias
  - `total_sales = SUM(TotalNetAmount)`
  - `order_count = COUNT(DISTINCT SalesOrder)`
  - sorted by sales desc
- `cluster_daily` (last 180 days, capped):
  - group by `date + FILTER_DIMENSIONS`
  - `total_sales = SUM(TotalNetAmount)`
  - `order_count = COUNT(DISTINCT SalesOrder)`

### Billing metrics object (used by today/mtd/qtd)
- Excludes cancelled docs: `BillingDocumentIsCancelled` must be one of `["", "false", "False", " "]`
- `today_actual_sales = SUM(TotalNetAmount)` where billing date = reference day
- `today_actual_sales_ly = SUM(TotalNetAmount)` on same day last year
- `mtd_actual_sales = SUM(TotalNetAmount)` from month start to reference day
- `mtd_actual_sales_ly = SUM(TotalNetAmount)` same month-to-date last year
- `qtd_actual_sales = SUM(TotalNetAmount)` from quarter start to reference day
- `qtd_actual_sales_ly = SUM(TotalNetAmount)` same quarter-to-date last year
- `billing_by_org = SUM(TotalNetAmount)` grouped by `SalesOrganization` (MTD, top 10)
- Billing tables (`today_billings`, `mtd_billings`, `qtd_billings`) are top docs by `TotalNetAmount`

### Today metrics (`metrics.today`)
- `sales_order_count = COUNT(DISTINCT SalesOrder)` for reference date
- `sales_order_value = SUM(TotalNetAmount)` for reference date
- `same_day_ly = SUM(TotalNetAmount)` for same date last year
- `pct_change_ly = pct_change(sales_order_value, same_day_ly)`
- `actual_sales`, `actual_sales_ly` from billing metrics
- `blocked_count/unblocked_count` from deduped daily orders + blocked logic
- `blocked_value = SUM(TotalNetAmount)` for blocked deduped orders
- `unblocked_value = sales_order_value - blocked_value`
- `last_4_days[date] = SUM(TotalNetAmount)` for each of last 4 dates
- Breakdowns (`by_sales_org`, `by_sales_group`, `by_order_type`, `by_customer`):
  - grouped totals with `SUM(TotalNetAmount)` and `COUNT(DISTINCT SalesOrder)`

### MTD / QTD metrics (`metrics.mtd`, `metrics.qtd`)
- For selected period:
  - `sales_total = SUM(TotalNetAmount)`
  - `sales_ly = SUM(TotalNetAmount)` on LY equivalent date window
  - `pct_change_ly = pct_change(sales_total, sales_ly)`
  - `order_count = COUNT(DISTINCT SalesOrder)`
  - `actual_sales`, `actual_sales_ly` from billing object (`mtd_*` or `qtd_*`)
  - `pct_change_actual_ly = pct_change(actual_sales, actual_sales_ly)`
  - blocked/unblocked metrics same as Today logic but over period
  - `daily_trend[date] = SUM(TotalNetAmount)` per day in period
  - breakdowns by order type/sales org/sales group/customer

### Custom period metrics (`/sales/period`)
- Sales and order metrics use requested `date_from/date_to`.
- LY comparison uses same dates shifted by one year.
- Current behavior detail: billing keys are read using `"mtd"` prefix in `_build_custom_period_metrics`.
  - So `actual_sales`, `actual_sales_ly`, and `billings` in period payload follow MTD billing keys, not a true custom-period billing slice.

### Trends metrics (`metrics.trends`)
- `total_sales = SUM(TotalNetAmount)` for last 365 days
- `total_orders = COUNT(DISTINCT SalesOrder)` for last 365 days
- `total_customers = COUNT(DISTINCT sold_to_party)` for last 365 days
- `daily_sales[date] = SUM(TotalNetAmount)` for last 365 days
- `daily_dod_pct[date] = pct_change(daily_sales[date], daily_sales[prev_date])`
- `monthly_by_year[year][month] = SUM(TotalNetAmount)` for last ~730 days
- `highest_daily_sales` / `lowest_daily_sales` from `daily_sales`
- `highest_monthly_sales` / `lowest_monthly_sales` from flattened `monthly_by_year`
- `top_product_categories` and `top_customers` via grouped sales/order counts
- `max_customer_year` and `min_customer_year` from customer grouped totals/order counts

### Orders metrics (`metrics.orders`)
All on last 365 days:
- `total_orders = COUNT(DISTINCT SalesOrder)`
- `total_value = SUM(TotalNetAmount)`
- `avg_value = total_value / total_orders` (0 when no orders)
- `highest_single_order` from max order total
- `lowest_nonzero_single_order` from min order total > 0
- `top_customer_by_value`, `top_customer_by_orders`, `lowest_active_customer` from customer grouped totals/counts
- Status:
  - status source field: `OverallSDProcessStatus`
  - `by_status = COUNT(DISTINCT SalesOrder)` by mapped status label
  - `closed_count = COUNT(DISTINCT SalesOrder where status == "C")`
  - `open_count = total_orders - closed_count`
- Monthly:
  - `monthly_order_count[YYYY-MM] = COUNT(DISTINCT SalesOrder)`
  - `monthly_order_value[YYYY-MM] = SUM(TotalNetAmount)`
  - `avg_orders_per_day[month] = monthly_order_count / distinct_days_with_data`
  - `avg_sales_per_day[month] = monthly_order_value / distinct_days_with_data`
- Daily:
  - `daily_order_counts[date] = COUNT(DISTINCT SalesOrder)`
  - `daily_order_values[date] = SUM(TotalNetAmount)`
  - `daily_dod_pct[date] = pct_change(daily_order_counts[date], daily_order_counts[prev_date])`
- `by_type` uses grouped order type totals/counts
- `blocked_count` / `unblocked_count` from deduped order-level blocked logic
- Line-item-driven metrics:
  - `top_materials`: group by `Material`, with
    - `order_count = COUNT(DISTINCT SalesOrder)`
    - `total_value = SUM(NetAmount)` from item rows
  - `top_material_by_orders` and `top_material_by_value` from `top_materials`

---

## Q2) Where these aggregates are shown on the dashboard (charts/graphs)

## Today tab (`/sales/today`)
- KPI cards:
  - `sales_order_count`, `sales_order_value`, `pct_change_ly`, `actual_sales`, `same_day_ly`
  - `blocked_count`, `blocked_value`, `unblocked_count`, `unblocked_value`
- Charts:
  - `last_4_days` -> "Last 4 Days Actual Sales"
  - `by_order_type.total_sales` -> "Daily Sales by Product Category"
  - `actual_sales` vs `sales_order_value` -> gauge "Sale Order Net Value VS Actual Sales (Today)"
  - `by_sales_group.total_sales` -> "Today's Sales by Sales Office"
  - `by_sales_org.total_sales` + `billing_by_org.total_sales` -> "Actual Sales VS Sale Order Value A/C Sales Org"
- Tables:
  - `blocked_orders_list` -> "Today's Sales Order Blocked/Unblocked"
  - `today_billings` -> "Today Billings"

## MTD and QTD tabs (`/sales/mtd`, `/sales/qtd`)
- KPI cards:
  - `order_count`, `sales_total`, `pct_change_ly`, `actual_sales`, `actual_sales_ly`
  - blocked/unblocked counts and values
- Charts:
  - `actual_sales` vs `sales_total` -> period gauge
  - `daily_trend` -> "`{MTD|QTD}` Actual Sales"
  - `by_order_type.total_sales` -> "Sales `{MTD|QTD}` Current Month by Product Category"
  - `by_sales_group.total_sales` -> "`{MTD|QTD}` Sales by Sales Office"
  - `by_sales_org.total_sales` -> "`{MTD|QTD}` Actual VS `{MTD|QTD}` LY Sales A/C Sales Org"
- Tables:
  - `blocked_orders_list` -> period blocked/unblocked table
  - `billings` -> "`{MTD|QTD}` Billings"

## Trends tab (`/sales/trends`)
- KPI cards:
  - `total_sales`, `total_orders`, `total_customers`
  - `highest_daily_sales`, `lowest_daily_sales`
  - `highest_monthly_sales`, `lowest_monthly_sales`
  - `max_customer_year`, `min_customer_year`
- Charts:
  - `daily_sales` -> "Daily Sales"
  - `daily_dod_pct` -> "Day-over-Day % Change"
  - `monthly_by_year` -> "Monthly Totals by Year"
  - derived max/min from `monthly_by_year` -> "Monthly Max vs Min Sales"
  - `top_product_categories.total_sales` -> "Top Product Categories"
- Table:
  - `top_customers` -> "Top Customers"

## Orders tab (`/sales/orders`)
- KPI cards:
  - `total_orders`, `total_value`, `open_count`, `closed_count`, `avg_value`
  - `highest_single_order`, `lowest_nonzero_single_order`
  - `top_customer_by_value`, `top_customer_by_orders`
  - `top_material_by_value`, `top_material_by_orders`
- Charts:
  - `monthly_order_count` -> "No. Of Sale Order (Monthly)"
  - `daily_dod_pct` -> "Day-over-Day Count Of Orders Change (%)"
  - `avg_orders_per_day` + `avg_sales_per_day` -> "Avg No. Of Order Per Month VS Avg Order Value Per Month"
  - `top_materials` -> "Top Materials Having Most No. Of Sales Order"
  - `by_status` -> "No. Of Sales Order By Status"
  - `by_type` -> "No. Of Sales Order By Order Type"
  - `daily_order_counts` + `daily_order_values` -> "No. Of Sale Order VS Total Order Value (Daily)"
- Tables:
  - `sales_order_status_table` -> "Sales Order Status"
  - `line_item_ac_table` -> "Detailed View Of Sales Order A/C To Line Item"
  - `detailed_view_table` -> "Detailed View"

---

## What `aggregate_client.py` specifically does
- Receives chatbot params with `report_type`.
- Routes to dashboard endpoints:
  - `today -> /sales/today`
  - `mtd -> /sales/mtd`
  - `qtd -> /sales/qtd`
  - `trends -> /sales/trends`
  - `orders -> /sales/orders`
  - `period -> /sales/period`
- Passes only supported filters (`sales_order_type`, `sold_to_party`, `purchase_order_by_customer`, `sales_organization`, `distribution_channel`, `sales_group`, `sales_office`, `sales_district`, `customer_group`, `date_from`, `date_to`).
- Uses retry for transient HTTP statuses/timeouts.
- Returns:
  - `raw_payload` (full endpoint response)
  - `data` (flattened rows for LLM/chat usage)
  - `count`, `requested_date_from`, `requested_date_to`

---

## Implementation notes to keep in mind
- Chart title mismatch:
  - "Sales by Sales Office" currently uses `by_sales_group` data (mapped from `SalesGroup`), not `SalesOffice`.
- MTD/QTD org chart title says "VS LY", but payload currently plots only one "Current" dataset from `by_sales_org`.
- `/sales/period` currently reuses MTD billing keys for `actual_sales` and `billings` fields.
