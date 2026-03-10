import pandas as pd
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .utils import compute_hash
from .sales_filters import FILTER_DIMENSIONS, EXTENDED_DIMENSIONS, MAX_FILTER_OPTIONS, normalize_dimension_value
from .settings import get_tenant_timezone

CLUSTER_MAX_DAYS = 730
CLUSTER_KEEP_DAYS = 180
CLUSTER_MAX_ROWS = 20000

try:
    _LOCAL_TZ = ZoneInfo(get_tenant_timezone())
except Exception:
    _LOCAL_TZ = timezone.utc


def _today_local():
    return datetime.now(_LOCAL_TZ).date()


def _max_df_date(df):
    if df is None or df.empty or "date" not in df.columns:
        return None
    try:
        max_date = df["date"].max()
    except Exception:
        return None
    if pd.isna(max_date):
        return None
    if isinstance(max_date, pd.Timestamp):
        return max_date.date()
    return max_date


def _resolve_reference_date(df, preferred=None):
    today = _today_local()
    if preferred is None:
        preferred = today

    max_date = _max_df_date(df)
    if max_date is None:
        return preferred if preferred <= today else today

    capped_preferred = preferred if preferred <= today else today
    return min(capped_preferred, max_date)


# ── Date parsing ────────────────────────────────────────────────────

def _parse_creation_dates(series):
    raw = series.astype(str).str.strip()
    millis = raw.str.extract(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")[0]
    millis = pd.to_numeric(millis, errors="coerce")

    parsed_millis = pd.to_datetime(millis, unit="ms", errors="coerce", utc=True)
    sap_mask = raw.str.match(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")
    invalid_mask = raw.str.lower().isin({"", "none", "nan", "nat"})

    def _parse_iso(value):
        text = str(value).strip()
        if not text:
            return pd.NaT
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return pd.NaT
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return pd.Timestamp(dt)

    standard_candidates = raw.mask(sap_mask | invalid_mask)
    parsed_standard = standard_candidates.map(_parse_iso)
    parsed = parsed_millis.fillna(parsed_standard)
    # Convert to local timezone so .dt.date extracts the correct local date
    # (e.g. 23:00 UTC = 02:00+03 next day in Asia/Riyadh)
    return parsed.dt.tz_convert(_LOCAL_TZ).dt.tz_localize(None)


def _parse_billing_dates(series):
    """Parse BillingDocumentDate — same SAP /Date()/ or ISO format."""
    return _parse_creation_dates(series)


# ── Blocked order detection ─────────────────────────────────────────

def _is_blocked(row):
    """
    An order is considered blocked if:
    - Credit check status is 'B' (blocked) or 'C' (rejected)
    - OR rejection status is not empty/space
    """
    credit = str(row.get("TotalCreditCheckStatus", "")).strip()
    rejection = str(row.get("OverallSDDocumentRejectionSts", "")).strip()

    if credit in ("B", "C"):
        return True
    if rejection and rejection not in ("", " ", "A"):
        return True
    return False


def _vectorized_is_blocked(df):
    """Vectorized version of _is_blocked for large DataFrames."""
    credit = df["TotalCreditCheckStatus"].astype(str).str.strip()
    rejection = df["OverallSDDocumentRejectionSts"].astype(str).str.strip()
    return credit.isin(["B", "C"]) | (~rejection.isin(["", " ", "A"]) & (rejection != ""))


# ── Shared helpers ──────────────────────────────────────────────────

def _pct_change(current, previous):
    if previous == 0:
        return 0.0 if current == 0 else 100.0
    return round((current - previous) / abs(previous) * 100, 2)


def _build_breakdown(df, column, top_n=20):
    if df.empty:
        return []
    grouped = (
        df.groupby(column, dropna=False)
        .agg(total_sales=("TotalNetAmount", "sum"), order_count=("SalesOrder", "nunique"))
        .reset_index()
        .sort_values("total_sales", ascending=False)
        .head(top_n)
    )
    records = grouped.to_dict(orient="records")
    return [
        {"value": str(r[column]), "total_sales": float(r["total_sales"]), "order_count": int(r["order_count"])}
        for r in records
    ]


def _safe_last_year(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def _build_filter_options(df):
    options = {}
    # Include both regular filter dimensions AND extended dimensions in options
    all_dims = {**FILTER_DIMENSIONS, **EXTENDED_DIMENSIONS}
    for dim_key in all_dims:
        if dim_key not in df.columns:
            options[dim_key] = []
            continue
        grouped = (
            df.groupby(dim_key, dropna=False)
            .agg(
                total_sales=("TotalNetAmount", "sum"),
                order_count=("SalesOrder", "nunique"),
            )
            .reset_index()
            .sort_values(["total_sales", dim_key], ascending=[False, True])
        )

        records = grouped.head(MAX_FILTER_OPTIONS).to_dict(orient="records")
        options[dim_key] = [
            {
                "value": str(r[dim_key]),
                "total_sales": float(r["total_sales"]),
                "order_count": int(r["order_count"]),
            }
            for r in records
        ]

    return options


def _build_cluster_daily(df):
    cutoff = _today_local() - timedelta(days=CLUSTER_KEEP_DAYS)
    df_capped = df[df["date"] >= cutoff]
    if df_capped.empty:
        return []

    group_columns = ["date"] + list(FILTER_DIMENSIONS.keys())
    grouped = (
        df_capped.groupby(group_columns, dropna=False)
        .agg(
            total_sales=("TotalNetAmount", "sum"),
            order_count=("SalesOrder", "nunique"),
        )
        .reset_index()
        .sort_values(["date", "total_sales"], ascending=[True, False])
    )

    if len(grouped) > CLUSTER_MAX_ROWS:
        grouped = grouped.head(CLUSTER_MAX_ROWS)

    records = grouped.to_dict(orient="records")
    rows = []
    for r in records:
        item = {
            "date": str(r["date"]),
            "total_sales": float(r["total_sales"]),
            "order_count": int(r["order_count"]),
        }
        for dim_key in FILTER_DIMENSIONS:
            item[dim_key] = str(r[dim_key])
        rows.append(item)

    return rows


# ── Billing data helpers ────────────────────────────────────────────

def _build_billing_metrics(billing_rows, reference_date=None):
    """Build billing (actual sales) metrics aligned with sales order dates."""
    _empty_billing = {
        "today_actual_sales": 0.0,
        "today_actual_sales_ly": 0.0,
        "mtd_actual_sales": 0.0,
        "mtd_actual_sales_ly": 0.0,
        "qtd_actual_sales": 0.0,
        "qtd_actual_sales_ly": 0.0,
        "today_billings": [],
        "mtd_billings": [],
        "qtd_billings": [],
        "billing_by_org": [],
    }

    if not billing_rows:
        return _empty_billing

    bdf = pd.DataFrame(billing_rows)

    if "BillingDocumentDate" not in bdf.columns or "TotalNetAmount" not in bdf.columns:
        return _empty_billing

    bdf["BillingDocumentDate"] = _parse_billing_dates(bdf["BillingDocumentDate"])
    bdf["TotalNetAmount"] = pd.to_numeric(bdf["TotalNetAmount"], errors="coerce").fillna(0)
    bdf = bdf[bdf["BillingDocumentDate"].notna()]

    if bdf.empty:
        return _empty_billing

    bdf["date"] = bdf["BillingDocumentDate"].dt.date

    # Filter out cancelled
    if "BillingDocumentIsCancelled" in bdf.columns:
        bdf = bdf[bdf["BillingDocumentIsCancelled"].astype(str).str.strip().isin(["", "false", "False", " "])]

    latest_billing_date = bdf["date"].max()
    today = reference_date or _today_local()
    if latest_billing_date is not None and today > latest_billing_date:
        today = latest_billing_date
    month_start = today.replace(day=1)
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    quarter_start = today.replace(month=quarter_month, day=1)

    bdf_today = bdf[bdf["date"] == today]
    bdf_mtd = bdf[(bdf["date"] >= month_start) & (bdf["date"] <= today)]
    bdf_qtd = bdf[(bdf["date"] >= quarter_start) & (bdf["date"] <= today)]

    # LY comparisons
    ly_today = _safe_last_year(today)
    ly_month_start = _safe_last_year(month_start)
    ly_quarter_start = _safe_last_year(quarter_start)
    ly_same_day = _safe_last_year(today)

    bdf_today_ly = bdf[bdf["date"] == ly_today]
    bdf_mtd_ly = bdf[(bdf["date"] >= ly_month_start) & (bdf["date"] <= ly_same_day)]
    bdf_qtd_ly = bdf[(bdf["date"] >= ly_quarter_start) & (bdf["date"] <= ly_same_day)]

    def _billing_table(subset, limit=20):
        if subset.empty:
            return []
        cols = ["BillingDocument", "BillingDocumentType", "SalesOrganization", "SoldToParty", "TotalNetAmount"]
        available = [c for c in cols if c in subset.columns]
        rows = subset.sort_values("TotalNetAmount", ascending=False).head(limit)
        records = rows[available].to_dict(orient="records")
        return [
            {c: (float(r[c]) if c == "TotalNetAmount" else str(r.get(c, ""))) for c in available}
            for r in records
        ]

    # Billing by org
    billing_by_org = []
    if "SalesOrganization" in bdf_mtd.columns and not bdf_mtd.empty:
        org_grouped = bdf_mtd.groupby("SalesOrganization")["TotalNetAmount"].sum().reset_index()
        org_grouped = org_grouped.sort_values("TotalNetAmount", ascending=False).head(10)
        org_records = org_grouped.to_dict(orient="records")
        billing_by_org = [
            {"value": str(r["SalesOrganization"]), "total_sales": float(r["TotalNetAmount"])}
            for r in org_records
        ]

    return {
        "today_actual_sales": float(bdf_today["TotalNetAmount"].sum()),
        "today_actual_sales_ly": float(bdf_today_ly["TotalNetAmount"].sum()),
        "mtd_actual_sales": float(bdf_mtd["TotalNetAmount"].sum()),
        "mtd_actual_sales_ly": float(bdf_mtd_ly["TotalNetAmount"].sum()),
        "qtd_actual_sales": float(bdf_qtd["TotalNetAmount"].sum()),
        "qtd_actual_sales_ly": float(bdf_qtd_ly["TotalNetAmount"].sum()),
        "today_billings": _billing_table(bdf_today),
        "mtd_billings": _billing_table(bdf_mtd),
        "qtd_billings": _billing_table(bdf_qtd),
        "billing_by_org": billing_by_org,
    }


# ── Page-specific metrics ──────────────────────────────────────────

def _build_today_metrics(df, billing, reference_date=None):
    today = _resolve_reference_date(df, preferred=reference_date)
    ly_same_day = _safe_last_year(today)

    df_today = df[df["date"] == today]
    df_ly = df[df["date"] == ly_same_day]

    today_sales = float(df_today["TotalNetAmount"].sum()) if not df_today.empty else 0.0
    today_orders = int(df_today["SalesOrder"].nunique()) if not df_today.empty else 0
    ly_sales = float(df_ly["TotalNetAmount"].sum()) if not df_ly.empty else 0.0

    # Blocked / Unblocked
    blocked_count = 0
    unblocked_count = 0
    blocked_value = 0.0
    unblocked_value = 0.0
    blocked_orders_list = []
    if not df_today.empty:
        df_today_dedup = df_today.drop_duplicates(subset="SalesOrder")
        df_today_dedup = df_today_dedup.copy()
        df_today_dedup["is_blocked"] = _vectorized_is_blocked(df_today_dedup)
        blocked_mask = df_today_dedup["is_blocked"]
        blocked_count = int(blocked_mask.sum())
        unblocked_count = today_orders - blocked_count
        blocked_value = float(df_today_dedup.loc[blocked_mask, "TotalNetAmount"].sum())
        unblocked_value = today_sales - blocked_value

        # Build blocked/unblocked table
        bo_records = df_today_dedup.to_dict(orient="records")
        for r in bo_records:
            blocked_orders_list.append({
                "sales_org": str(r.get("sales_organization", "")),
                "sales_order": str(r.get("SalesOrder", "")),
                "status": "Blocked" if r.get("is_blocked") else "Unblocked",
                "order_value": float(r.get("TotalNetAmount", 0)),
            })

    # Last 4 days
    last_4_days = {}
    for i in range(3, -1, -1):
        d = today - timedelta(days=i)
        day_df = df[df["date"] == d]
        last_4_days[str(d)] = float(day_df["TotalNetAmount"].sum()) if not day_df.empty else 0.0

    actual_sales = billing.get("today_actual_sales", 0.0)
    actual_sales_ly = billing.get("today_actual_sales_ly", 0.0)

    return {
        "date": str(today),
        "sales_order_count": today_orders,
        "sales_order_value": today_sales,
        "same_day_ly": ly_sales,
        "pct_change_ly": _pct_change(today_sales, ly_sales),
        "actual_sales": actual_sales,
        "actual_sales_ly": actual_sales_ly,
        "blocked_count": blocked_count,
        "unblocked_count": unblocked_count,
        "blocked_value": blocked_value,
        "unblocked_value": unblocked_value,
        "blocked_orders_list": blocked_orders_list[:50],
        "by_sales_org": _build_breakdown(df_today, "sales_organization", 10),
        "by_sales_group": _build_breakdown(df_today, "sales_group", 10),
        "by_order_type": _build_breakdown(df_today, "sales_order_type", 10),
        "by_customer": _build_breakdown(df_today, "sold_to_party", 15),
        "last_4_days": last_4_days,
        "today_billings": billing.get("today_billings", []),
        "billing_by_org": billing.get("billing_by_org", []),
    }


def _build_period_metrics(df, period_start, period_end, ly_period_start, ly_period_end, billing_key_prefix, billing):
    df_period = df[(df["date"] >= period_start) & (df["date"] <= period_end)]
    df_ly = df[(df["date"] >= ly_period_start) & (df["date"] <= ly_period_end)]

    sales = float(df_period["TotalNetAmount"].sum()) if not df_period.empty else 0.0
    ly_sales = float(df_ly["TotalNetAmount"].sum()) if not df_ly.empty else 0.0
    orders = int(df_period["SalesOrder"].nunique()) if not df_period.empty else 0

    # Blocked / Unblocked for period
    blocked_count = 0
    unblocked_count = 0
    blocked_value = 0.0
    unblocked_value = 0.0
    blocked_orders_list = []
    if not df_period.empty:
        df_dedup = df_period.drop_duplicates(subset="SalesOrder").copy()
        df_dedup["is_blocked"] = _vectorized_is_blocked(df_dedup)
        blocked_mask = df_dedup["is_blocked"]
        blocked_count = int(blocked_mask.sum())
        unblocked_count = orders - blocked_count
        blocked_value = float(df_dedup.loc[blocked_mask, "TotalNetAmount"].sum())
        unblocked_value = sales - blocked_value

        bp_records = df_dedup.to_dict(orient="records")
        for r in bp_records:
            blocked_orders_list.append({
                "sales_org": str(r.get("sales_organization", "")),
                "sales_order": str(r.get("SalesOrder", "")),
                "status": "Blocked" if r.get("is_blocked") else "Unblocked",
                "order_value": float(r.get("TotalNetAmount", 0)),
            })

    daily_trend = {}
    if not df_period.empty:
        daily = df_period.groupby("date")["TotalNetAmount"].sum().sort_index()
        daily_trend = {str(k): float(v) for k, v in daily.items()}

    actual_sales = billing.get(f"{billing_key_prefix}_actual_sales", 0.0)
    actual_sales_ly = billing.get(f"{billing_key_prefix}_actual_sales_ly", 0.0)

    return {
        "period_start": str(period_start),
        "sales_total": sales,
        "sales_ly": ly_sales,
        "pct_change_ly": _pct_change(sales, ly_sales),
        "order_count": orders,
        "actual_sales": actual_sales,
        "actual_sales_ly": actual_sales_ly,
        "pct_change_actual_ly": _pct_change(actual_sales, actual_sales_ly),
        "blocked_count": blocked_count,
        "unblocked_count": unblocked_count,
        "blocked_value": blocked_value,
        "unblocked_value": unblocked_value,
        "blocked_orders_list": blocked_orders_list[:50],
        "daily_trend": daily_trend,
        "by_order_type": _build_breakdown(df_period, "sales_order_type", 10),
        "by_sales_org": _build_breakdown(df_period, "sales_organization", 10),
        "by_sales_group": _build_breakdown(df_period, "sales_group", 10),
        "by_customer": _build_breakdown(df_period, "sold_to_party", 15),
        "billings": billing.get(f"{billing_key_prefix}_billings", []),
    }


def _build_mtd_metrics(df, billing, reference_date=None):
    today = _resolve_reference_date(df, preferred=reference_date)
    month_start = today.replace(day=1)
    ly_month_start = _safe_last_year(month_start)
    ly_same_day = _safe_last_year(today)
    return _build_period_metrics(df, month_start, today, ly_month_start, ly_same_day, "mtd", billing)


def _build_qtd_metrics(df, billing, reference_date=None):
    today = _resolve_reference_date(df, preferred=reference_date)
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    quarter_start = today.replace(month=quarter_month, day=1)
    ly_quarter_start = _safe_last_year(quarter_start)
    ly_same_day = _safe_last_year(today)
    return _build_period_metrics(df, quarter_start, today, ly_quarter_start, ly_same_day, "qtd", billing)


def _build_custom_period_metrics(df, period_start, period_end, billing):
    """Build metrics for an arbitrary user-specified date range with LY comparison."""
    ly_start = _safe_last_year(period_start)
    ly_end = _safe_last_year(period_end)
    result = _build_period_metrics(df, period_start, period_end, ly_start, ly_end, "mtd", billing)
    result["period_start"] = str(period_start)
    result["period_end"] = str(period_end)
    return result


def _build_trends_metrics(df):
    today = _today_local()
    cutoff = today - timedelta(days=365)
    df_year = df[df["date"] >= cutoff]

    total_sales = float(df_year["TotalNetAmount"].sum()) if not df_year.empty else 0.0
    total_orders = int(df_year["SalesOrder"].nunique()) if not df_year.empty else 0
    total_customers = int(df_year["sold_to_party"].nunique()) if not df_year.empty else 0

    daily_sales = {}
    if not df_year.empty:
        daily = df_year.groupby("date")["TotalNetAmount"].sum().sort_index()
        daily_sales = {str(k): float(v) for k, v in daily.items()}

    daily_dod_pct = {}
    sorted_days = sorted(daily_sales.keys())
    for i, day in enumerate(sorted_days):
        if i == 0:
            daily_dod_pct[day] = 0.0
        else:
            prev = daily_sales.get(sorted_days[i - 1], 0)
            curr = daily_sales.get(day, 0)
            daily_dod_pct[day] = _pct_change(curr, prev)

    monthly_by_year = {}
    if not df.empty:
        df_recent = df[df["date"] >= (today - timedelta(days=730))]
        if not df_recent.empty:
            df_tmp = df_recent.copy()
            df_tmp["year"] = df_tmp["date"].apply(lambda d: str(d.year))
            df_tmp["month"] = df_tmp["date"].apply(lambda d: str(d.month).zfill(2))
            monthly = df_tmp.groupby(["year", "month"])["TotalNetAmount"].sum().reset_index()
            for r in monthly.to_dict(orient="records"):
                yr = r["year"]
                if yr not in monthly_by_year:
                    monthly_by_year[yr] = {}
                monthly_by_year[yr][r["month"]] = float(r["TotalNetAmount"])

    highest_daily_sales = {"date": None, "total_sales": 0.0}
    lowest_daily_sales = {"date": None, "total_sales": 0.0}
    if daily_sales:
        max_day, max_val = max(daily_sales.items(), key=lambda kv: kv[1])
        min_day, min_val = min(daily_sales.items(), key=lambda kv: kv[1])
        highest_daily_sales = {"date": str(max_day), "total_sales": float(max_val)}
        lowest_daily_sales = {"date": str(min_day), "total_sales": float(min_val)}

    monthly_totals_flat = []
    for year_key, months in monthly_by_year.items():
        for month_key, total in months.items():
            monthly_totals_flat.append((f"{year_key}-{month_key}", float(total)))

    highest_monthly_sales = {"period": None, "total_sales": 0.0}
    lowest_monthly_sales = {"period": None, "total_sales": 0.0}
    if monthly_totals_flat:
        max_month, max_month_val = max(monthly_totals_flat, key=lambda kv: kv[1])
        min_month, min_month_val = min(monthly_totals_flat, key=lambda kv: kv[1])
        highest_monthly_sales = {"period": max_month, "total_sales": float(max_month_val)}
        lowest_monthly_sales = {"period": min_month, "total_sales": float(min_month_val)}

    max_customer_year = None
    min_customer_year = None
    if not df_year.empty:
        cust_df = (
            df_year[df_year["sold_to_party"].astype(str).str.strip() != ""]
            .groupby("sold_to_party", dropna=False)
            .agg(total_sales=("TotalNetAmount", "sum"), order_count=("SalesOrder", "nunique"))
            .reset_index()
        )
        if not cust_df.empty:
            cust_sorted = cust_df.sort_values(["total_sales", "order_count"], ascending=[False, False])
            top_row = cust_sorted.iloc[0]
            min_row = cust_df.sort_values(["total_sales", "order_count"], ascending=[True, True]).iloc[0]
            max_customer_year = {
                "value": str(top_row["sold_to_party"]),
                "total_sales": float(top_row["total_sales"]),
                "order_count": int(top_row["order_count"]),
            }
            min_customer_year = {
                "value": str(min_row["sold_to_party"]),
                "total_sales": float(min_row["total_sales"]),
                "order_count": int(min_row["order_count"]),
            }

    return {
        "total_sales": total_sales,
        "total_orders": total_orders,
        "total_customers": total_customers,
        "daily_sales": daily_sales,
        "daily_dod_pct": daily_dod_pct,
        "monthly_by_year": monthly_by_year,
        "top_product_categories": _build_breakdown(df_year, "sales_order_type", 10),
        "top_customers": _build_breakdown(df_year, "sold_to_party", 15),
        "highest_daily_sales": highest_daily_sales,
        "lowest_daily_sales": lowest_daily_sales,
        "highest_monthly_sales": highest_monthly_sales,
        "lowest_monthly_sales": lowest_monthly_sales,
        "max_customer_year": max_customer_year,
        "min_customer_year": min_customer_year,
    }


def _build_orders_metrics(df, all_so_rows=None):
    today = _today_local()
    cutoff = today - timedelta(days=365)
    df_year = df[df["date"] >= cutoff].copy()

    total_orders = int(df_year["SalesOrder"].nunique()) if not df_year.empty else 0
    total_value = float(df_year["TotalNetAmount"].sum()) if not df_year.empty else 0.0
    avg_value = round(total_value / total_orders, 2) if total_orders > 0 else 0.0
    highest_single_order = None
    lowest_nonzero_single_order = None
    top_customer_by_value = None
    top_customer_by_orders = None
    lowest_active_customer = None
    top_material_by_orders = None
    top_material_by_value = None

    if not df_year.empty:
        order_df = (
            df_year[df_year["SalesOrder"].astype(str).str.strip() != ""]
            .groupby("SalesOrder", dropna=False)
            .agg(
                total_value=("TotalNetAmount", "sum"),
                sold_to_party=("sold_to_party", "first"),
                date=("date", "first"),
            )
            .reset_index()
        )
        if not order_df.empty:
            high = order_df.sort_values("total_value", ascending=False).iloc[0]
            highest_single_order = {
                "sales_order": str(high["SalesOrder"]),
                "total_value": float(high["total_value"]),
                "customer": str(high.get("sold_to_party", "")),
                "date": str(high.get("date", "")),
            }

            nonzero = order_df[order_df["total_value"] > 0]
            if not nonzero.empty:
                low = nonzero.sort_values("total_value", ascending=True).iloc[0]
                lowest_nonzero_single_order = {
                    "sales_order": str(low["SalesOrder"]),
                    "total_value": float(low["total_value"]),
                    "customer": str(low.get("sold_to_party", "")),
                    "date": str(low.get("date", "")),
                }

        customer_df = (
            df_year[df_year["sold_to_party"].astype(str).str.strip() != ""]
            .groupby("sold_to_party", dropna=False)
            .agg(total_sales=("TotalNetAmount", "sum"), order_count=("SalesOrder", "nunique"))
            .reset_index()
        )
        if not customer_df.empty:
            top_val = customer_df.sort_values(["total_sales", "order_count"], ascending=[False, False]).iloc[0]
            top_ord = customer_df.sort_values(["order_count", "total_sales"], ascending=[False, False]).iloc[0]
            low_act = customer_df.sort_values(["total_sales", "order_count"], ascending=[True, True]).iloc[0]
            top_customer_by_value = {
                "value": str(top_val["sold_to_party"]),
                "total_sales": float(top_val["total_sales"]),
                "order_count": int(top_val["order_count"]),
            }
            top_customer_by_orders = {
                "value": str(top_ord["sold_to_party"]),
                "total_sales": float(top_ord["total_sales"]),
                "order_count": int(top_ord["order_count"]),
            }
            lowest_active_customer = {
                "value": str(low_act["sold_to_party"]),
                "total_sales": float(low_act["total_sales"]),
                "order_count": int(low_act["order_count"]),
            }

    # ── Status breakdown ─────────────────────────────────────────────
    open_count = 0
    closed_count = 0
    by_status = []
    status_map = {"C": "Completed", "A": "Open", "B": "Partially Processed", "": "Not Processed"}
    if not df_year.empty and "OverallSDProcessStatus" in df_year.columns:
        df_year["status_label"] = df_year["OverallSDProcessStatus"].map(
            lambda s: status_map.get(s, s or "Not Processed")
        )
        status_counts = df_year.groupby("status_label")["SalesOrder"].nunique().reset_index()
        status_counts.columns = ["status", "count"]
        by_status = [{"value": r["status"], "count": int(r["count"])} for r in status_counts.to_dict(orient="records")]
        closed_count = int(df_year[df_year["OverallSDProcessStatus"] == "C"]["SalesOrder"].nunique())
        open_count = total_orders - closed_count

    # ── Monthly order count ──────────────────────────────────────────
    monthly_order_count = {}
    monthly_order_value = {}
    if not df_year.empty:
        df_tmp = df_year.copy()
        df_tmp["month_key"] = df_tmp["date"].apply(lambda d: d.strftime("%Y-%m"))
        monthly_cnt = df_tmp.groupby("month_key")["SalesOrder"].nunique().sort_index()
        monthly_order_count = {k: int(v) for k, v in monthly_cnt.items()}
        monthly_val = df_tmp.groupby("month_key")["TotalNetAmount"].sum().sort_index()
        monthly_order_value = {k: float(v) for k, v in monthly_val.items()}

    # ── Avg orders per day and avg value per month ───────────────────
    avg_orders_per_day = {}
    avg_sales_per_day = {}
    if not df_year.empty:
        df_tmp2 = df_year.copy()
        df_tmp2["month_key"] = df_tmp2["date"].apply(lambda d: d.strftime("%Y-%m"))
        for mk in sorted(monthly_order_count.keys()):
            month_df = df_tmp2[df_tmp2["month_key"] == mk]
            n_days = month_df["date"].nunique()
            if n_days > 0:
                avg_orders_per_day[mk] = round(monthly_order_count.get(mk, 0) / n_days, 1)
                avg_sales_per_day[mk] = round(monthly_order_value.get(mk, 0) / n_days, 2)
            else:
                avg_orders_per_day[mk] = 0
                avg_sales_per_day[mk] = 0.0

    # ── Daily order counts + value + DoD% ────────────────────────────
    daily_order_counts = {}
    daily_order_values = {}
    daily_dod_pct = {}
    if not df_year.empty:
        daily_cnt = df_year.groupby("date")["SalesOrder"].nunique().sort_index()
        daily_order_counts = {str(k): int(v) for k, v in daily_cnt.items()}
        daily_val = df_year.groupby("date")["TotalNetAmount"].sum().sort_index()
        daily_order_values = {str(k): float(v) for k, v in daily_val.items()}
        sorted_days = sorted(daily_order_counts.keys())
        for i, day in enumerate(sorted_days):
            if i == 0:
                daily_dod_pct[day] = 0.0
            else:
                prev = daily_order_counts.get(sorted_days[i - 1], 0)
                curr = daily_order_counts.get(day, 0)
                daily_dod_pct[day] = _pct_change(curr, prev)

    # ── By type breakdown ────────────────────────────────────────────
    by_type = _build_breakdown(df_year, "sales_order_type", 10)

    # ── Blocked detection for orders ─────────────────────────────────
    blocked_count = 0
    unblocked_count = 0
    if not df_year.empty:
        df_dedup = df_year.drop_duplicates(subset="SalesOrder").copy()
        df_dedup["is_blocked"] = _vectorized_is_blocked(df_dedup)
        blocked_count = int(df_dedup["is_blocked"].sum())
        unblocked_count = total_orders - blocked_count

    # ── Build per-order material count map (from line items) ─────────
    order_material_count = {}  # sales_order → distinct material count
    if all_so_rows:
        for so_row in all_so_rows:
            so_num = str(so_row.get("SalesOrder", ""))
            items = so_row.get("to_Item", [])
            if isinstance(items, dict):
                items = items.get("results", [])
            if not isinstance(items, list):
                items = []
            materials = {str(item.get("Material", "")).strip() for item in items if str(item.get("Material", "")).strip()}
            order_material_count[so_num] = len(materials)

    # ── Top materials by order count (from line items) ───────────────
    top_materials = []
    line_item_details = []
    if all_so_rows:
        item_rows = []
        for so_row in all_so_rows:
            so_number = str(so_row.get("SalesOrder", ""))
            creation_raw = so_row.get("CreationDate", "")
            items = so_row.get("to_Item", [])
            if not isinstance(items, list):
                items_dict = items if isinstance(items, dict) else {}
                items = items_dict.get("results", [])
            for item in items:
                item_rows.append({
                    "SalesOrder": so_number,
                    "SalesOrderItem": str(item.get("SalesOrderItem", "")),
                    "Material": str(item.get("Material", "")).strip(),
                    "MaterialGroup": str(item.get("MaterialGroup", "")).strip(),
                    "SalesOrderItemText": str(item.get("SalesOrderItemText", "")),
                    "OrderQuantity": float(
                        item.get("RequestedQuantity", item.get("OrderQuantity", 0)) or 0
                    ),
                    "OrderQuantityUnit": str(
                        item.get("RequestedQuantityUnit", item.get("OrderQuantityUnit", ""))
                    ),
                    "NetAmount": float(item.get("NetAmount", 0) or 0),
                    "Division": str(item.get("Division", "")),
                    "ProductionPlant": str(item.get("ProductionPlant", "")),
                    "SalesOrderItemCategory": str(item.get("SalesOrderItemCategory", "")),
                    "CreationDate": creation_raw,
                })

        if item_rows:
            idf = pd.DataFrame(item_rows)
            # Filter to last year
            idf["CreationDate"] = _parse_creation_dates(idf["CreationDate"])
            idf = idf[idf["CreationDate"].notna()]
            idf["date"] = idf["CreationDate"].dt.date
            idf_year = idf[idf["date"] >= cutoff]

            if not idf_year.empty:
                # Top materials by number of distinct sales orders
                mat_grouped = (
                    idf_year[idf_year["Material"] != ""]
                    .groupby("Material")
                    .agg(order_count=("SalesOrder", "nunique"), total_value=("NetAmount", "sum"))
                    .reset_index()
                    .sort_values("order_count", ascending=False)
                    .head(15)
                )
                top_materials = [
                    {
                        "material": r["Material"],
                        "order_count": int(r["order_count"]),
                        "total_value": float(r["total_value"]),
                    }
                    for r in mat_grouped.to_dict(orient="records")
                ]
                if top_materials:
                    top_material_by_orders = max(top_materials, key=lambda x: x.get("order_count", 0))
                    top_material_by_value = max(top_materials, key=lambda x: x.get("total_value", 0))

                # Line item detail table (most recent 200)
                idf_sorted = idf_year.sort_values("date", ascending=False).head(200)
                line_item_details = [
                    {
                        "sales_order": r["SalesOrder"],
                        "item": r["SalesOrderItem"],
                        "material": r["Material"],
                        "description": r["SalesOrderItemText"],
                        "quantity": float(r["OrderQuantity"]),
                        "uom": r["OrderQuantityUnit"],
                        "net_amount": float(r["NetAmount"]),
                        "division": r["Division"],
                        "plant": r["ProductionPlant"],
                    }
                    for r in idf_sorted.to_dict(orient="records")
                ]

    # ── Sales Order Status table ─────────────────────────────────────
    sales_order_status_table = []
    if not df_year.empty:
        df_dedup_status = df_year.drop_duplicates(subset="SalesOrder").copy()
        df_dedup_status["is_blocked"] = _vectorized_is_blocked(df_dedup_status)
        df_dedup_status["status_label"] = df_dedup_status["OverallSDProcessStatus"].map(
            lambda s: status_map.get(s, s or "Not Processed")
        )
        df_dedup_status["block_status"] = df_dedup_status["is_blocked"].map(
            lambda b: "Blocked" if b else "Unblocked"
        )
        # Sort by most recent, take top 100
        status_sorted = df_dedup_status.sort_values("date", ascending=False).head(100)
        for r in status_sorted.to_dict(orient="records"):
            so_num = str(r["SalesOrder"])
            mat_count = order_material_count.get(so_num, "")
            sales_order_status_table.append({
                "sales_order": so_num,
                "no_of_material_type": mat_count if mat_count != "" else "",
                "order_type": str(r.get("sales_order_type", r.get("SalesOrderType", ""))),
                "customer": str(r.get("sold_to_party", "")),
                "salesman": str(r.get("sales_group", "")),
                "order_status": r["status_label"],
                "order_aging_days": (today - r["date"]).days if r["date"] else 0,
                "block_status": r["block_status"],
            })

    # ── Detailed View table (header-level) ───────────────────────────
    detailed_view_table = []
    if not df_year.empty:
        df_detail = df_year.drop_duplicates(subset="SalesOrder").copy()
        df_detail = df_detail.sort_values("date", ascending=False).head(100)
        for r in df_detail.to_dict(orient="records"):
            detailed_view_table.append({
                "sales_org": str(r.get("sales_organization", "")),
                "sales_office": str(r.get("sales_office", r.get("SalesOffice", ""))),
                "distribution_channel": str(r.get("distribution_channel", "")),
                "material": "",
                "product_category": str(r.get("sales_order_type", "")),
                "region": str(r.get("sales_district", r.get("SalesDistrict", ""))),
                "customer_group": str(r.get("customer_group", r.get("CustomerGroup", ""))),
                "customer_name": str(r.get("sold_to_party", "")),
                "salesman_name": str(r.get("sales_group", "")),
                "no_of_sales_order": str(r.get("SalesOrder", "")),
                "total_value": float(r.get("TotalNetAmount", 0)),
            })

    # ── Line item detailed view (A/C line item) ─────────────────────
    line_item_ac_table = []
    if all_so_rows:
        # Build from the raw rows with items
        seen = 0
        for so_row in sorted(all_so_rows, key=lambda r: str(r.get("CreationDate", "")), reverse=True):
            if seen >= 50:
                break
            so_number = str(so_row.get("SalesOrder", ""))
            items = so_row.get("to_Item", [])
            if not isinstance(items, list):
                items_dict = items if isinstance(items, dict) else {}
                items = items_dict.get("results", [])
            if not items:
                continue
            for item in items[:10]:
                line_item_ac_table.append({
                    "sales_order": so_number,
                    "item_no": str(item.get("SalesOrderItem", "")),
                    "material": str(item.get("Material", "")),
                    "description": str(item.get("SalesOrderItemText", "")),
                    "quantity": float(
                        item.get("RequestedQuantity", item.get("OrderQuantity", 0)) or 0
                    ),
                    "uom": str(
                        item.get("RequestedQuantityUnit", item.get("OrderQuantityUnit", ""))
                    ),
                    "net_amount": float(item.get("NetAmount", 0) or 0),
                    "currency": str(item.get("TransactionCurrency", "")),
                })
            seen += 1

    return {
        "total_orders": total_orders,
        "total_value": total_value,
        "open_count": open_count,
        "closed_count": closed_count,
        "avg_value": avg_value,
        "blocked_count": blocked_count,
        "unblocked_count": unblocked_count,
        "highest_single_order": highest_single_order,
        "lowest_nonzero_single_order": lowest_nonzero_single_order,
        "top_customer_by_value": top_customer_by_value,
        "top_customer_by_orders": top_customer_by_orders,
        "lowest_active_customer": lowest_active_customer,
        "top_material_by_orders": top_material_by_orders,
        "top_material_by_value": top_material_by_value,
        "monthly_order_count": monthly_order_count,
        "monthly_order_value": monthly_order_value,
        "avg_orders_per_day": avg_orders_per_day,
        "avg_sales_per_day": avg_sales_per_day,
        "daily_order_counts": daily_order_counts,
        "daily_order_values": daily_order_values,
        "daily_dod_pct": daily_dod_pct,
        "by_status": by_status,
        "by_type": by_type,
        "top_materials": top_materials,
        "sales_order_status_table": sales_order_status_table,
        "detailed_view_table": detailed_view_table,
        "line_item_ac_table": line_item_ac_table,
        "line_item_details": line_item_details,
    }


# ── Main entry point ───────────────────────────────────────────────

def calculate_aggregates(so_rows, billing_rows=None):
    """
    Build all dashboard metrics from sales order snapshots + billing snapshots.
    Returns (metrics_dict, fingerprint_hash).
    """
    empty_metrics = {
        "hourly": {},
        "daily": {},
        "monthly": {},
        "quarterly": {},
        "yearly": {},
        "filter_options": {key: [] for key in FILTER_DIMENSIONS},
        "cluster_daily": [],
        "totals": {"total_sales": 0.0, "order_count": 0},
        "today": {},
        "mtd": {},
        "qtd": {},
        "trends": {},
        "orders": {},
    }

    if not so_rows:
        return empty_metrics, compute_hash(empty_metrics)

    df = pd.DataFrame(so_rows)
    if "CreationDate" not in df.columns or "TotalNetAmount" not in df.columns:
        return empty_metrics, compute_hash(empty_metrics)
    if "SalesOrder" not in df.columns:
        df["SalesOrder"] = ""

    for dim_key, sap_field in FILTER_DIMENSIONS.items():
        if sap_field not in df.columns:
            df[sap_field] = None
        df[dim_key] = df[sap_field].apply(normalize_dimension_value)

    # Normalize extended dimensions for Order Wise Report
    for dim_key, sap_field in EXTENDED_DIMENSIONS.items():
        if sap_field not in df.columns:
            df[sap_field] = None
        df[dim_key] = df[sap_field].apply(normalize_dimension_value)

    if "OverallSDProcessStatus" not in df.columns:
        df["OverallSDProcessStatus"] = ""
    df["OverallSDProcessStatus"] = df["OverallSDProcessStatus"].fillna("").astype(str)

    # Ensure blocked-detection fields exist
    for col in ("TotalCreditCheckStatus", "OverallSDDocumentRejectionSts", "OverallTotalDeliveryStatus"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    df["CreationDate"] = _parse_creation_dates(df["CreationDate"])
    df["TotalNetAmount"] = pd.to_numeric(df["TotalNetAmount"], errors="coerce").fillna(0)
    df = df[df["CreationDate"].notna()]
    if df.empty:
        return empty_metrics, compute_hash(empty_metrics)
    df["date"] = df["CreationDate"].dt.date

    # Build billing metrics
    billing = _build_billing_metrics(billing_rows or [])

    metrics = {}

    # Time-series aggregations
    hourly = df.groupby(df["CreationDate"].dt.floor("h"))["TotalNetAmount"].sum().to_dict()
    metrics["hourly"] = {
        key.isoformat(sep=" ", timespec="seconds"): float(value)
        for key, value in hourly.items()
    }

    daily = df.groupby(df["CreationDate"].dt.date)["TotalNetAmount"].sum().to_dict()
    metrics["daily"] = {str(key): float(value) for key, value in daily.items()}

    monthly = df.groupby(df["CreationDate"].dt.to_period("M"))["TotalNetAmount"].sum().to_dict()
    metrics["monthly"] = {str(key): float(value) for key, value in monthly.items()}

    quarterly = df.groupby(df["CreationDate"].dt.to_period("Q"))["TotalNetAmount"].sum().to_dict()
    metrics["quarterly"] = {str(key): float(value) for key, value in quarterly.items()}

    yearly = df.groupby(df["CreationDate"].dt.year)["TotalNetAmount"].sum().to_dict()
    metrics["yearly"] = {str(int(key)): float(value) for key, value in yearly.items()}

    metrics["filter_options"] = _build_filter_options(df)
    metrics["cluster_daily"] = _build_cluster_daily(df)
    metrics["totals"] = {
        "total_sales": float(df["TotalNetAmount"].sum()),
        "order_count": int(df["SalesOrder"].nunique()),
    }

    # SPG page-specific metrics
    metrics["today"] = _build_today_metrics(df, billing)
    metrics["mtd"] = _build_mtd_metrics(df, billing)
    metrics["qtd"] = _build_qtd_metrics(df, billing)
    metrics["trends"] = _build_trends_metrics(df)
    metrics["orders"] = _build_orders_metrics(df, all_so_rows=so_rows)

    fingerprint = compute_hash(metrics)

    return metrics, fingerprint
