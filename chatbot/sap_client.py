from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from chatbot.config import settings
from chatbot.models import SAPTool

logger = logging.getLogger(__name__)

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Return a module-level httpx client for connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=45,
            verify=settings.SAP_VERIFY_TLS,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


_FIELD_MAP: dict[str, dict[str, str]] = {
    "get_sales_orders": {
        "sales_order": "SalesOrder",
        "customer_id": "SoldToParty",
        "sales_org": "SalesOrganization",
        # "material": "Material",  # REMOVED - Material is item-level, filtered client-side
        "distribution_channel": "DistributionChannel",
        "order_type": "SalesOrderType",
        "overall_status": "OverallSDProcessStatus",
        "delivery_status": "OverallTotalDeliveryStatus",
        "date_from": "CreationDate",
        "date_to": "CreationDate",
    },
    "get_purchase_orders": {
        "purchase_order": "PurchaseOrder",
        "vendor_id": "Supplier",
        "purchasing_org": "PurchasingOrganization",
        "plant": "Plant",
        "company_code": "CompanyCode",
        "po_type": "PurchaseOrderType",
        "purchasing_group": "PurchasingGroup",
        "processing_status": "PurchasingProcessingStatus",
        "date_from": "PurchaseOrderDate",
        "date_to": "PurchaseOrderDate",
    },
    "get_purchase_order_items": {
        "purchase_order": "PurchaseOrder",
        "material": "Material",
        "plant": "Plant",
        "company_code": "CompanyCode",
        "supplier": "Supplier",
        "gr_complete": "IsCompletelyDelivered",
        "invoice_complete": "IsFinallyInvoiced",
        "gr_expected": "GoodsReceiptIsExpected",
        "invoice_expected": "InvoiceIsExpected",
    },
    "get_invoices": {
        "billing_document": "BillingDocument",
        "customer_id": "SoldToParty",
        "sales_org": "SalesOrganization",
        "company_code": "CompanyCode",
        "billing_type": "BillingDocumentType",
        "is_cancelled": "BillingDocumentIsCancelled",
        "date_from": "BillingDocumentDate",
        "date_to": "BillingDocumentDate",
    },
    "get_business_partners": {
        "business_partner": "BusinessPartner",
        "name": "BusinessPartnerFullName",
        "category": "BusinessPartnerCategory",
        "is_blocked": "BusinessPartnerIsBlocked",
    },
    "get_materials": {
        "material": "Product",
        "material_type": "ProductType",
        "product_group": "ProductGroup",
        "industry_sector": "IndustrySector",
        # "description" is handled client-side after $expand
    },
    "get_deliveries": {
        "delivery": "DeliveryDocument",
        "customer_id": "ShipToParty",
        "sold_to_party": "SoldToParty",
        "shipping_point": "ShippingPoint",
        "delivery_type": "DeliveryDocumentType",
        "goods_movement_status": "OverallGoodsMovementStatus",
        "date_from": "ActualGoodsMovementDate",
        "date_to": "ActualGoodsMovementDate",
    },
    "get_journal_entries": {
        "company_code": "CompanyCode",
        "fiscal_year": "FiscalYear",
        "gl_account": "GLAccount",
        "cost_center": "CostCenter",
        "profit_center": "ProfitCenter",
        "date_from": "PostingDate",
        "date_to": "PostingDate",
    },
}


def _odata_value(value: Any, param_type: str) -> str:
    """Wrap value in the correct OData literal syntax."""
    if param_type == "date":
        return f"datetime'{value}T00:00:00'"
    if param_type in ("integer", "number"):
        return str(value)
    if param_type == "boolean":
        return "true" if str(value).lower() in ("true", "1", "yes") else "false"
    # Escape single quotes for OData string literals (e.g. O'Brien → O''Brien)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _get_orderby_field(tool: SAPTool) -> str | None:
    """Return the OData field name to order by (date field) for this tool, or None."""
    field_map = _FIELD_MAP.get(tool.name, {})
    date_from_field = field_map.get("date_from")
    if date_from_field:
        return date_from_field
    return None


def build_odata_filter(tool: SAPTool, params: dict[str, Any]) -> str:
    """
    Convert validated param dict → OData $filter string.
    Handles date ranges (date_from / date_to), equality, and substring.

    Note: Some parameters are handled client-side and excluded from OData filter:
    - material (for sales orders) - filtered client-side via to_Item array
    - description (for materials) - filtered client-side after $expand
    """
    field_map = _FIELD_MAP.get(tool.name, {})
    param_index = {p.name: p for p in tool.parameters}
    clauses: list[str] = []

    for key, val in params.items():
        if key == "top" or val is None:
            continue

        # Skip params handled client-side (not filterable in OData)
        if key == "description" and tool.name == "get_materials":
            continue
        if key == "material" and tool.name == "get_sales_orders":
            # Material is item-level for sales orders, filtered client-side
            continue

        odata_field = field_map.get(key, key)
        p_def = param_index.get(key)
        p_type = p_def.type.value if p_def else "string"

        if key == "date_from":
            clauses.append(f"{odata_field} ge {_odata_value(val, 'date')}")
        elif key == "date_to":
            clauses.append(f"{odata_field} le {_odata_value(val, 'date')}")
        elif key in ("name", "description"):
            # substring search (escape single quotes)
            escaped_val = str(val).replace("'", "''")
            clauses.append(f"substringof('{escaped_val}', {odata_field})")
        else:
            clauses.append(f"{odata_field} eq {_odata_value(val, p_type)}")

    return " and ".join(clauses)


def _flatten_expanded(results: list[dict], tool: SAPTool) -> list[dict]:
    """
    LEGACY: Flatten OData $expand navigation properties into the parent row.

    This is kept for backward compatibility with the old single-expand pattern
    (Materials → to_Description). New multi-expand logic uses _preserve_expanded.

    Two strategies:
      - **pick-one** (e.g. Materials → to_Description): merge a single child
        row (prefer English) into the parent.
      - **denormalise** (e.g. Deliveries → to_DeliveryDocumentItem): create
        one output row per child, repeating the parent fields each time.
    """
    expand = tool.odata_expand
    if not expand:
        return results

    # Navigation properties that should pick a single child row
    _PICK_ONE = {"to_Description"}

    flattened = []
    for row in results:
        flat = {k: v for k, v in row.items() if not isinstance(v, dict)}

        nav = row.get(expand)
        if not isinstance(nav, dict):
            flattened.append(flat)
            continue

        inner = nav.get("results", [])
        if not isinstance(inner, list) or not inner:
            flattened.append(flat)
            continue

        if expand in _PICK_ONE:
            # Pick one child row (prefer English for descriptions)
            desc_row = next(
                (d for d in inner if d.get("Language", "").strip().upper() == "EN"),
                inner[0],
            )
            for k, v in desc_row.items():
                if k != "__metadata":
                    flat[k] = v
            flattened.append(flat)
        else:
            # Denormalise: one output row per child item
            for child in inner:
                merged = dict(flat)
                for k, v in child.items():
                    if k != "__metadata":
                        merged[k] = v
                flattened.append(merged)

    return flattened


_SAP_DATE_RE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")


def _format_sap_date_string(value: str) -> str:
    """Convert SAP /Date(ms)/ text to readable UTC date/datetime."""
    text = str(value).strip()
    match = _SAP_DATE_RE.fullmatch(text)
    if not match:
        return value

    ts = int(match.group(1)) / 1000
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _normalize_sap_dates(value: Any) -> Any:
    """Recursively normalize SAP /Date(...)/ strings in dict/list payloads."""
    if isinstance(value, dict):
        return {k: _normalize_sap_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_sap_dates(item) for item in value]
    if isinstance(value, str):
        return _format_sap_date_string(value)
    return value


def _preserve_expanded_data(results: list[dict], expands: list[str]) -> list[dict]:
    """
    NEW: Preserve nested OData navigation properties as arrays.

    Unwraps OData v2's {results: [...]} wrapper but keeps the nested structure
    so the LLM can work with arrays of items, partners, pricing, etc.

    Args:
        results: Raw OData results with nested navigation properties
        expands: List of expand names that were requested

    Returns:
        Results with navigation properties unwrapped from {results: []} but preserved as arrays

    Example transformation:
        Before: {"SalesOrder": "100", "to_Item": {"results": [{"Item": "10"}, {"Item": "20"}]}}
        After:  {"SalesOrder": "100", "to_Item": [{"Item": "10"}, {"Item": "20"}]}
    """
    if not expands:
        return results

    processed = []
    for row in results:
        processed_row = {}

        for key, value in row.items():
            if key in expands and isinstance(value, dict):
                # This is an expanded navigation property
                if "results" in value:
                    # OData v2: {results: [...]}
                    processed_row[key] = value["results"]
                elif "__deferred" in value:
                    # Deferred load (shouldn't happen with $expand, but handle gracefully)
                    processed_row[key] = []
                else:
                    # Single entity (1-to-1 navigation)
                    processed_row[key] = value
            else:
                # Regular field, keep as-is
                processed_row[key] = value

        processed.append(processed_row)

    return processed


def _apply_client_side_filters(
    results: list[dict],
    tool: SAPTool,
    params: dict[str, Any],
    client_filters: dict[str, Any],
) -> list[dict]:
    """
    Apply client-side filters that can't be done in OData.

    Current filters:
    - material (sales orders): Filter orders by material in to_Item array
    - description (materials): Filter materials by description text
    """
    if not client_filters:
        return results

    original_count = len(results)

    # Material filter for sales orders (item-level field)
    if tool.name == "get_sales_orders" and "material" in client_filters:
        material_search = client_filters["material"].upper().strip()
        filtered = []
        for row in results:
            items = row.get("to_Item", [])
            # Check if any item matches the material
            if any(
                item.get("Material", "").upper().strip() == material_search
                for item in items
            ):
                filtered.append(row)
        results = filtered
        logger.info(
            "Client-side material filter '%s': %d → %d orders",
            material_search,
            original_count,
            len(results),
        )

    # Description filter for materials
    if tool.name == "get_materials" and "description" in client_filters:
        desc_search = client_filters["description"].lower()
        filtered = [
            r
            for r in results
            if desc_search in str(r.get("ProductDescription", "")).lower()
        ]
        results = filtered
        logger.info(
            "Client-side description filter '%s': %d → %d materials",
            desc_search,
            original_count,
            len(results),
        )

    return results


_oauth_cache: dict[str, Any] = {}
_OAUTH_TOKEN_BUFFER = 120  # refresh 2 min before expiry
_oauth_lock: asyncio.Lock | None = None


def _get_oauth_lock() -> asyncio.Lock:
    global _oauth_lock
    if _oauth_lock is None:
        _oauth_lock = asyncio.Lock()
    return _oauth_lock


async def _get_oauth_token(client: httpx.AsyncClient) -> str:
    # Fast path — check without lock
    cached = _oauth_cache.get("token")
    expires = _oauth_cache.get("expires_at", 0)
    if cached and time.time() < expires:
        return cached

    # Slow path — serialize refresh to avoid duplicate token requests
    async with _get_oauth_lock():
        # Double-check after acquiring lock
        cached = _oauth_cache.get("token")
        expires = _oauth_cache.get("expires_at", 0)
        if cached and time.time() < expires:
            return cached

        resp = await client.post(
            settings.SAP_OAUTH_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.SAP_OAUTH_CLIENT_ID,
                "client_secret": settings.SAP_OAUTH_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        ttl = int(data.get("expires_in", 3600))
        _oauth_cache["token"] = token
        _oauth_cache["expires_at"] = time.time() + ttl - _OAUTH_TOKEN_BUFFER
        logger.info("OAuth token refreshed, TTL=%ds", ttl)
        return token


def _base_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "sap-client": settings.SAP_CLIENT,
    }


async def call_sap(
    tool: SAPTool,
    params: dict[str, Any],
    skip_expand: bool = False,
    expands: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the OData URL, call SAP, and return a normalised result dict.

    Args:
        tool: The SAP tool definition
        params: Filter parameters
        expands: Additional navigation properties to expand (beyond defaults)

    Returns
    -------
    {
        "success": bool,
        "count": int,
        "data": list[dict],      # rows with nested expands preserved
        "error": str | None,
    }
    """
    # Extract client-side filters BEFORE building OData filter
    client_side_filters = {}
    params_copy = dict(params)  # Don't mutate original

    # Material filter for sales orders (item-level, can't filter in OData)
    if tool.name == "get_sales_orders" and "material" in params_copy:
        client_side_filters["material"] = params_copy.pop("material")
        # Ensure to_Item is expanded for material filtering
        if expands is None:
            expands = []
        if "to_Item" not in expands and "to_Item" not in tool.default_expands:
            expands.append("to_Item")

    # Description filter for materials (handled client-side)
    if tool.name == "get_materials" and "description" in params_copy:
        client_side_filters["description"] = params_copy.pop("description")

    odata_filter = build_odata_filter(tool, params_copy)
    top = params.get("top", tool.top_default)

    # Auto-scale $top for wide date ranges (>90 days) so analytical queries
    # get full month coverage instead of just one month's worth of data.
    _TOP_WIDE_RANGE = 5000          # max headers for wide date range
    _TOP_WIDE_RANGE_EXPAND = 2000   # lower when $expand multiplies rows
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    user_set_top = "top" in params  # user explicitly asked for a specific count
    if date_from and date_to and not user_set_top:
        try:
            df = datetime.strptime(str(date_from), "%Y-%m-%d")
            dt = datetime.strptime(str(date_to), "%Y-%m-%d")
            span_days = (dt - df).days
            if span_days > 90:
                ceiling = _TOP_WIDE_RANGE_EXPAND if tool.odata_expand else _TOP_WIDE_RANGE
                top = max(top, ceiling)
                logger.info("Auto-scaled $top to %d for %d-day date range", top, span_days)
        except ValueError:
            pass

    # Over-fetch when client-side description filter is active so we don't
    # miss matches that fall outside the original $top window.
    original_top = top
    _MAX_OVERFETCH_TOP = 5000
    if client_side_filters:
        # Fetch 5x more to ensure we get enough matches after filtering
        top = min(max(original_top * 5, 2500), _MAX_OVERFETCH_TOP)
        logger.info(
            "Client-side filters detected, over-fetching: top=%d → %d (ceiling=%d)",
            original_top,
            top,
            _MAX_OVERFETCH_TOP,
        )

    # Merge default expands + requested expands
    all_expands = list(set(tool.default_expands + (expands or [])))

    # Legacy support: if tool.odata_expand is set and not in the combined list, add it
    if tool.odata_expand and tool.odata_expand not in all_expands:
        all_expands.append(tool.odata_expand)

    query: dict[str, str] = {"$format": "json", "$top": str(top)}
    if odata_filter:
        query["$filter"] = odata_filter
    if all_expands:
        query["$expand"] = ",".join(all_expands)

    # Order by date field ascending so $top returns earliest records first
    # within the filtered range — ensures chronological coverage
    orderby_field = _get_orderby_field(tool)
    if orderby_field:
        query["$orderby"] = f"{orderby_field} asc"

    url = f"{settings.SAP_BASE_URL}{tool.sap_api_path}"
    headers = _base_headers()

    logger.info(
        "SAP request  → %s  filter=%s  top=%s  expands=%s",
        url,
        odata_filter,
        top,
        all_expands,
    )

    MAX_RETRIES = 2
    last_error = ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = _get_http_client()

            # Authentication
            if settings.SAP_AUTH_TYPE == "oauth2":
                token = await _get_oauth_token(client)
                headers["Authorization"] = f"Bearer {token}"
                resp = await client.get(url, params=query, headers=headers)
            else:
                resp = await client.get(
                    url,
                    params=query,
                    headers=headers,
                    auth=(settings.SAP_USERNAME, settings.SAP_PASSWORD),
                )

            resp.raise_for_status()
            body = resp.json()

            # OData v2 wraps in d/results; v4 uses value
            results = body.get("d", {}).get("results", body.get("value", []))

            # Post-process: preserve expanded navigation properties
            if all_expands and isinstance(results, list):
                results = _preserve_expanded_data(results, all_expands)

            # Legacy: flatten if using old single-expand pattern (Materials only)
            # This maintains backward compatibility while new multi-expand uses preservation
            if (
                tool.odata_expand
                and not tool.available_expands
                and isinstance(results, list)
            ):
                results = _flatten_expanded(results, tool)

            # Apply client-side filters
            if client_side_filters:
                results = _apply_client_side_filters(
                    results, tool, params, client_side_filters
                )
                # Trim back to user's requested $top after filtering
                results = results[:original_top]

            # Trim to declared response_fields (if any) for NON-EXPANDED data only
            # Keep expanded fields intact
            if tool.response_fields and isinstance(results, list):
                trimmed = []
                for row in results:
                    trimmed_row = {k: row.get(k) for k in tool.response_fields}
                    # Preserve all expanded navigation properties
                    for expand in all_expands:
                        if expand in row:
                            trimmed_row[expand] = row[expand]
                    trimmed.append(trimmed_row)
            else:
                trimmed = results

            trimmed = _normalize_sap_dates(trimmed)

            logger.info(
                "SAP response ← %d rows with expands: %s", len(trimmed), all_expands
            )
            return {
                "success": True,
                "count": len(trimmed),
                "data": trimmed,
                "error": None,
            }

        except httpx.TimeoutException as exc:
            last_error = f"SAP timeout (attempt {attempt}/{MAX_RETRIES}): {exc}"
            logger.warning(last_error)
            if attempt < MAX_RETRIES:
                continue
        except httpx.HTTPStatusError as exc:
            last_error = (
                f"SAP HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            )
            logger.error(last_error)
            # Don't retry on 4xx client errors
            if 400 <= exc.response.status_code < 500:
                break
            if attempt < MAX_RETRIES:
                continue
        except Exception as exc:
            last_error = f"SAP connection error: {exc}"
            logger.error(last_error)
            break

    return {"success": False, "count": 0, "data": [], "error": last_error}












