import logging
import httpx
from datetime import datetime, time, timezone
from .settings import SAP_BASE_URL, SAP_CLIENT, SAP_USERNAME, SAP_PASSWORD

logger = logging.getLogger(__name__)

# ── Sales Order fields ──────────────────────────────────────────────
# Added: OverallTotalDeliveryStatus, OverallSDDocumentRejectionSts,
#        TotalCreditCheckStatus  → needed for blocked/unblocked detection
SAP_SO_SELECT_FIELDS = ",".join([
    "SalesOrder",
    "SalesOrderType",
    "SalesOrganization",
    "DistributionChannel",
    "SalesGroup",
    "SoldToParty",
    "PurchaseOrderByCustomer",
    "TotalNetAmount",
    "TransactionCurrency",
    "CreationDate",
    "LastChangeDateTime",
    "OverallSDProcessStatus",
    "OverallTotalDeliveryStatus",
    "OverallSDDocumentRejectionSts",
    "TotalCreditCheckStatus",
    # Additional fields for Order Wise Report
    "SalesOffice",
    "SalesDistrict",
    "CustomerGroup",
])

# Line-item level fields (fetched via $expand=to_Item)
SAP_SO_ITEM_SELECT_FIELDS = ",".join([
    "SalesOrder",
    "SalesOrderItem",
    "Material",
    "SalesOrderItemText",
    "RequestedQuantity",
    "RequestedQuantityUnit",
    "NetAmount",
    "TransactionCurrency",
    "MaterialGroup",
    "ProductionPlant",
    "SalesOrderItemCategory",
    "HigherLevelItem",
])


def _build_sales_order_select(expand_items: bool) -> str:
    if not expand_items:
        return SAP_SO_SELECT_FIELDS
    item_fields = [f"to_Item/{field}" for field in SAP_SO_ITEM_SELECT_FIELDS.split(",")]
    return ",".join([SAP_SO_SELECT_FIELDS, *item_fields])

# ── Billing Document fields ─────────────────────────────────────────
SAP_BILLING_SELECT_FIELDS = ",".join([
    "BillingDocument",
    "BillingDocumentType",
    "SoldToParty",
    "SalesOrganization",
    "CompanyCode",
    "BillingDocumentDate",
    "TotalNetAmount",
    "TransactionCurrency",
    "BillingDocumentIsCancelled",
])

BATCH_SIZE = 5000


def _format_odata_datetime(value, end_of_day=False):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt_time = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        dt = datetime.combine(value, dt_time)
    else:
        dt = datetime.fromisoformat(str(value))
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _format_odata_datetimeoffset(value, end_of_day=False):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt_time = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        dt = datetime.combine(value, dt_time)
    else:
        dt = datetime.fromisoformat(str(value))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_MAX_TOTAL_ROWS = 500_000  # safety ceiling to prevent runaway fetches


async def _fetch_odata_paginated(url, headers, auth, params_base, label="OData"):
    """Generic paginated OData v2 fetcher with safety ceiling."""
    all_results = []
    skip = 0
    page_size = int(params_base.get("$top", BATCH_SIZE))

    # Per-request timeout: 30s connect, 10min read (large expanded payloads)
    timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        while True:
            params = {**params_base, "$skip": str(skip)}

            logger.info("%s fetch: skip=%d, accumulated=%d", label, skip, len(all_results))

            try:
                resp = await client.get(url, params=params, headers=headers, auth=auth)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("%s HTTP error %s: %s", label, e.response.status_code, e.response.text[:500])
                raise
            except httpx.RequestError as e:
                logger.error("%s request error: %s", label, e)
                raise

            data = resp.json()
            results = data.get("d", {}).get("results", [])

            if not results:
                break

            all_results.extend(results)

            # Safety ceiling to prevent unbounded memory growth
            if len(all_results) >= _MAX_TOTAL_ROWS:
                logger.warning(
                    "%s hit safety ceiling of %d rows — stopping pagination",
                    label, _MAX_TOTAL_ROWS,
                )
                break

            if len(results) < page_size:
                break

            skip += len(results)

    logger.info("%s fetch completed: %d total rows", label, len(all_results))
    return all_results


async def fetch_sales_orders(date_from, date_to, filter_field="CreationDate", expand_items=False):
    """Fetch sales order headers from SAP, optionally with line items."""
    if not SAP_BASE_URL:
        raise ValueError("SAP_BASE_URL is not configured.")

    url = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder"
    headers = {"Accept": "application/json"}
    if SAP_CLIENT:
        headers["sap-client"] = SAP_CLIENT

    if filter_field == "LastChangeDateTime":
        start_literal = f"datetimeoffset'{_format_odata_datetimeoffset(date_from)}'"
        end_literal = f"datetimeoffset'{_format_odata_datetimeoffset(date_to, end_of_day=True)}'"
    else:
        start_literal = f"datetime'{_format_odata_datetime(date_from)}'"
        end_literal = f"datetime'{_format_odata_datetime(date_to, end_of_day=True)}'"

    params = {
        "$format": "json",
        "$filter": f"{filter_field} ge {start_literal} and {filter_field} le {end_literal}",
        "$select": _build_sales_order_select(expand_items),
        "$orderby": f"{filter_field} asc",
        "$top": str(BATCH_SIZE),
    }

    if expand_items:
        params["$expand"] = "to_Item"
        # Reduce batch size when expanding — responses are much larger
        params["$top"] = str(min(BATCH_SIZE, 2000))

    results = await _fetch_odata_paginated(
        url, headers, (SAP_USERNAME, SAP_PASSWORD), params, label="SalesOrder",
    )

    # Unwrap OData v2 expand wrapper for line items
    if expand_items:
        for row in results:
            items_raw = row.get("to_Item", {})
            if isinstance(items_raw, dict):
                row["to_Item"] = items_raw.get("results", [])
            elif not isinstance(items_raw, list):
                row["to_Item"] = []

    return results


async def fetch_billing_documents(date_from, date_to):
    """Fetch billing documents (invoices) from SAP."""
    if not SAP_BASE_URL:
        raise ValueError("SAP_BASE_URL is not configured.")

    url = f"{SAP_BASE_URL}/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV/A_BillingDocument"
    headers = {"Accept": "application/json"}
    if SAP_CLIENT:
        headers["sap-client"] = SAP_CLIENT

    params = {
        "$format": "json",
        "$filter": (
            f"BillingDocumentDate ge datetime'{_format_odata_datetime(date_from)}' "
            f"and BillingDocumentDate le datetime'{_format_odata_datetime(date_to, end_of_day=True)}'"
        ),
        "$select": SAP_BILLING_SELECT_FIELDS,
        "$orderby": "BillingDocumentDate asc",
        "$top": str(BATCH_SIZE),
    }

    return await _fetch_odata_paginated(
        url, headers, (SAP_USERNAME, SAP_PASSWORD), params, label="BillingDoc",
    )
