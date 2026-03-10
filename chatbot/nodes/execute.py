from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from chatbot.aggregate_client import call_sales_aggregates
from chatbot.models import SAPTool
from chatbot.mcp_client import call_mcp_query
from chatbot.sap_client import call_sap
from chatbot.state import ChatState
from chatbot.tools.registry import get_tool

logger = logging.getLogger(__name__)

_MULTI_TOOL_GATHER_TIMEOUT = 90  # max seconds for all parallel SAP calls
_MAX_BP_LOOKUPS = 20              # max concurrent customer name resolutions
_MAX_BP_IDS = 50                  # max unique customer IDs to resolve per call

# ---------------------------------------------------------------------------
# Customer ID → Name auto-resolution
# ---------------------------------------------------------------------------

# (SAP field in response → friendly name field to inject)
# Includes both PascalCase (live SAP OData) and snake_case (DB snapshots)
_CUSTOMER_ID_FIELDS: list[tuple[str, str]] = [
    ("SoldToParty", "_SoldToName"),
    ("ShipToParty", "_ShipToName"),
    ("PayerParty", "_PayerName"),
    # DB snapshot columns (snake_case aliases)
    ("sold_to_party", "_SoldToName"),
]

# Tools that should NOT trigger customer name resolution
_SKIP_RESOLVE_TOOLS = {
    "get_sales_aggregates",
    "query_database",
    "get_sales_orders_db",
    "get_billing_db",
    "get_business_partners",
    "get_materials",
    "get_journal_entries",
}


async def _resolve_customer_names(
    data: list[dict[str, Any]],
    tool_name: str,
) -> list[dict[str, Any]]:
    """
    Enrich SAP response rows with human-readable customer names.

    Scans *data* for known customer-ID fields, collects unique IDs,
    resolves each via get_business_partners, and injects a
    corresponding name field (e.g. _SoldToName) into every row.
    """
    if not data or tool_name in _SKIP_RESOLVE_TOOLS:
        return data

    # 1. Collect unique customer IDs across all rows
    unique_ids: set[str] = set()
    for row in data:
        for src_field, _ in _CUSTOMER_ID_FIELDS:
            val = row.get(src_field)
            if val and str(val).strip():
                unique_ids.add(str(val).strip())

    if not unique_ids:
        return data

    # Cap to avoid SAP fan-out storm
    ids_to_resolve = list(unique_ids)[:_MAX_BP_IDS]
    if len(unique_ids) > _MAX_BP_IDS:
        logger.warning(
            "BP resolution capped at %d of %d unique IDs",
            _MAX_BP_IDS, len(unique_ids),
        )

    logger.info(
        "Resolving %d unique customer ID(s) for %s",
        len(ids_to_resolve),
        tool_name,
    )

    # 2. Parallel lookups via get_business_partners (throttled)
    from chatbot.tools.definitions import GET_BUSINESS_PARTNERS

    sem = asyncio.Semaphore(_MAX_BP_LOOKUPS)

    async def _lookup(bp_id: str) -> tuple[str, str]:
        """Return (id, name) or (id, '') on failure."""
        async with sem:
            try:
                result = await call_sap(
                    GET_BUSINESS_PARTNERS,
                    {"business_partner": bp_id, "top": 1},
                )
                rows = result.get("data", [])
                if rows:
                    name = (
                        rows[0].get("BusinessPartnerFullName")
                        or rows[0].get("OrganizationBPName1")
                        or ""
                    )
                    return bp_id, name
            except Exception as exc:
                logger.warning("BP lookup failed for %s: %s", bp_id, exc)
            return bp_id, ""

    resolved = await asyncio.gather(*[_lookup(bid) for bid in ids_to_resolve])
    name_map: dict[str, str] = {bid: name for bid, name in resolved if name}

    if not name_map:
        logger.info("No customer names resolved")
        return data

    logger.info("Resolved customer names: %s", name_map)

    # 3. Inject name fields into every row
    for row in data:
        for src_field, name_field in _CUSTOMER_ID_FIELDS:
            val = row.get(src_field)
            if val and str(val).strip() in name_map:
                row[name_field] = name_map[str(val).strip()]

    return data


# ---------------------------------------------------------------------------
# Item-level fields added by $expand — these come from child entities,
# not headers.  Keyed by odata_expand value.
# ---------------------------------------------------------------------------

_EXPAND_ITEM_FIELDS: dict[str, set[str]] = {
    "to_Item": {
        "SalesOrderItem",
        "Material",
        "SalesOrderItemText",
        "RequestedQuantity",
        "OrderQuantity",
        "OrderQuantityUnit",
        "NetAmount",
        "MaterialGroup",
        "Plant",
        "StorageLocation",
        "DeliveryStatus",
        "BillingDocumentItem",
        "BillingDocumentItemText",
        "BillingQuantity",
        "BillingQuantityUnit",
        "GrossAmount",
        "SalesDocument",
        "SalesDocumentItem",
    },
    "to_DeliveryDocumentItem": {
        "DeliveryDocumentItem",
        "Material",
        "MaterialDescription",
        "ActualDeliveryQuantity",
        "DeliveryQuantityUnit",
        "ItemGrossWeight",
        "ItemNetWeight",
        "Batch",
        "StorageLocation",
        "Plant",
    },
}


def _get_item_fields(tool: SAPTool) -> set[str]:
    """Return the set of fields that come from $expand (item-level, not header)."""
    if not tool.odata_expand:
        return set()
    return _EXPAND_ITEM_FIELDS.get(tool.odata_expand, set())


def _should_skip_expand(tool: SAPTool, chart_hint: dict | None) -> bool:
    """Check if $expand can be skipped (chart_hint uses only header fields)."""
    if not chart_hint or not tool.odata_expand:
        return False
    hint_fields = {chart_hint.get("group_by"), chart_hint.get("metric")}
    hint_fields.discard(None)
    hint_fields.discard("_count")
    item_fields = _get_item_fields(tool)
    if hint_fields and not hint_fields.intersection(item_fields):
        logger.info("Skipping $expand for aggregate query (header fields only)")
        return True
    return False


def _sql_literal(value: str) -> str:
    """Sanitize a value for safe interpolation into SQL string literals.

    Since MCP's tool protocol passes SQL as a single string (no separate
    parameter binding), we must sanitize at construction time.  Strategy:
    1. Strip to only safe characters (alnum, dash, dot, underscore, space, @).
    2. Escape single quotes for SQL string context.
    3. Reject values that are suspiciously long (>200 chars) or empty.

    Raises ValueError for values that cannot be safely embedded.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("Empty filter value")
    if len(text) > 200:
        raise ValueError(f"Filter value too long ({len(text)} chars, max 200)")

    # Allow only safe characters: alphanumeric, dash, underscore, dot, space,
    # forward slash, @, comma — covers SAP document numbers, dates, org codes,
    # customer names with spaces, email-like values.
    if not re.fullmatch(r"[A-Za-z0-9\s\-_.,/@#()+:]+", text):
        raise ValueError(
            f"Filter value contains disallowed characters: {text!r}"
        )

    return text.replace("'", "''")


def _apply_app_sort(result: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Re-sort and trim results application-side for sort_by=value.

    When there are no filters, the DB query fetches a larger batch sorted by
    date (fast, indexed). This function re-sorts by TotalNetAmount and trims
    to the requested ``top`` count.
    """
    sort_by = str(params.get("sort_by", "recent")).lower()
    if sort_by != "value" or not result.get("success") or not result.get("data"):
        return result

    requested_top = int(params.get("top", 500) or 500)
    requested_top = max(1, min(requested_top, 5000))

    def _parse_amount(row: dict) -> float:
        val = row.get("total_net_amount") or "0"
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    data = sorted(result["data"], key=_parse_amount, reverse=True)
    data = data[:requested_top]
    return {**result, "data": data, "count": len(data)}


def _build_sales_orders_db_sql(params: dict[str, Any]) -> str:
    top = int(params.get("top", 500) or 500)
    top = max(1, min(top, 5000))

    where: list[str] = []
    if params.get("sales_order"):
        where.append(
            f"payload->>'SalesOrder' = '{_sql_literal(str(params['sales_order']))}'"
        )
    if params.get("customer_id"):
        where.append(
            f"payload->>'SoldToParty' = '{_sql_literal(str(params['customer_id']))}'"
        )
    if params.get("sales_org"):
        where.append(
            f"payload->>'SalesOrganization' = '{_sql_literal(str(params['sales_org']))}'"
        )
    if params.get("distribution_channel"):
        where.append(
            f"payload->>'DistributionChannel' = '{_sql_literal(str(params['distribution_channel']))}'"
        )
    if params.get("order_type"):
        where.append(
            f"payload->>'SalesOrderType' = '{_sql_literal(str(params['order_type']))}'"
        )
    if params.get("overall_status"):
        where.append(
            f"payload->>'OverallSDProcessStatus' = '{_sql_literal(str(params['overall_status']))}'"
        )
    if params.get("delivery_status"):
        where.append(
            f"payload->>'OverallTotalDeliveryStatus' = '{_sql_literal(str(params['delivery_status']))}'"
        )
    if params.get("material"):
        material = _sql_literal(str(params["material"]))
        where.append(
            "EXISTS ("
            "SELECT 1 "
            "FROM jsonb_array_elements(COALESCE(payload->'to_Item'->'results','[]'::jsonb)) AS item "
            f"WHERE item->>'Material' = '{material}'"
            ")"
        )
    if params.get("date_from"):
        where.append(
            f"COALESCE(last_changed_at, updated_at, created_at) >= '{_sql_literal(str(params['date_from']))}'::date"
        )
    if params.get("date_to"):
        where.append(
            f"COALESCE(last_changed_at, updated_at, created_at) < ('{_sql_literal(str(params['date_to']))}'::date + INTERVAL '1 day')"
        )

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sort_by = str(params.get("sort_by", "recent")).lower()

    if sort_by == "value" and where:
        # When filters narrow the dataset, it's safe to sort by value in DB
        # because the WHERE clause reduces the scan to a small subset.
        order_by_sql = (
            "ORDER BY CASE "
            "WHEN COALESCE(payload->>'TotalNetAmount','') ~ '^-?\\d+(\\.\\d+)?$' "
            "THEN (payload->>'TotalNetAmount')::numeric "
            "ELSE 0 END DESC "
        )
    elif sort_by == "value":
        # No filters — sorting by JSONB value on full table is too slow.
        # Fetch a larger recent batch; application-side re-sort happens in execute().
        order_by_sql = "ORDER BY COALESCE(last_changed_at, updated_at, created_at) DESC "
        # Over-fetch so the app-side re-sort has enough candidates
        top = max(top, min(top * 10, 5000))
    else:
        order_by_sql = "ORDER BY COALESCE(last_changed_at, updated_at, created_at) DESC "

    return (
        "SELECT "
        "payload->>'SalesOrder' AS sales_order, "
        "payload->>'SalesOrderType' AS sales_order_type, "
        "payload->>'SalesOrganization' AS sales_organization, "
        "payload->>'DistributionChannel' AS distribution_channel, "
        "payload->>'SoldToParty' AS sold_to_party, "
        "payload->>'CreationDate' AS creation_date_raw, "
        "payload->>'TotalNetAmount' AS total_net_amount, "
        "payload->>'TransactionCurrency' AS transaction_currency, "
        "payload->>'OverallSDProcessStatus' AS overall_status, "
        "payload->>'OverallTotalDeliveryStatus' AS delivery_status, "
        "payload->>'TotalCreditCheckStatus' AS credit_status, "
        "last_changed_at, created_at, updated_at "
        "FROM sales_order_snapshots "
        f"{where_sql} "
        f"{order_by_sql}"
        f"LIMIT {top}"
    )


def _build_billing_db_sql(params: dict[str, Any]) -> str:
    top = int(params.get("top", 500) or 500)
    top = max(1, min(top, 5000))

    where: list[str] = []
    if params.get("billing_document"):
        where.append(
            f"payload->>'BillingDocument' = '{_sql_literal(str(params['billing_document']))}'"
        )
    if params.get("customer_id"):
        where.append(
            f"payload->>'SoldToParty' = '{_sql_literal(str(params['customer_id']))}'"
        )
    if params.get("sales_org"):
        where.append(
            f"payload->>'SalesOrganization' = '{_sql_literal(str(params['sales_org']))}'"
        )
    if params.get("company_code"):
        where.append(
            f"payload->>'CompanyCode' = '{_sql_literal(str(params['company_code']))}'"
        )
    if params.get("billing_type"):
        where.append(
            f"payload->>'BillingDocumentType' = '{_sql_literal(str(params['billing_type']))}'"
        )
    if "is_cancelled" in params:
        cancelled = "true" if params.get("is_cancelled") else "false"
        where.append(
            f"LOWER(COALESCE(payload->>'BillingDocumentIsCancelled','false')) = '{cancelled}'"
        )
    if params.get("date_from"):
        where.append(
            f"COALESCE(billing_date, updated_at, created_at) >= '{_sql_literal(str(params['date_from']))}'::date"
        )
    if params.get("date_to"):
        where.append(
            f"COALESCE(billing_date, updated_at, created_at) < ('{_sql_literal(str(params['date_to']))}'::date + INTERVAL '1 day')"
        )

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return (
        "SELECT "
        "payload->>'BillingDocument' AS billing_document, "
        "payload->>'BillingDocumentType' AS billing_document_type, "
        "payload->>'SoldToParty' AS sold_to_party, "
        "payload->>'SalesOrganization' AS sales_organization, "
        "payload->>'CompanyCode' AS company_code, "
        "payload->>'BillingDocumentDate' AS billing_date_raw, "
        "payload->>'TotalNetAmount' AS total_net_amount, "
        "payload->>'TransactionCurrency' AS transaction_currency, "
        "payload->>'BillingDocumentIsCancelled' AS is_cancelled, "
        "billing_date, created_at, updated_at "
        "FROM billing_snapshots "
        f"{where_sql} "
        "ORDER BY COALESCE(billing_date, updated_at, created_at) DESC "
        f"LIMIT {top}"
    )


def _normalize_mcp_error(error: str | None) -> str:
    if not error:
        return ""
    low = error.lower()
    if "permission denied" in low:
        return (
            "Database permission error in MCP read-only role. "
            "Please grant SELECT access to required tables/views "
            "(for example: sales_order_snapshots, billing_snapshots, curated_aggregates)."
        )
    return error


def _normalize_mcp_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result or {})
    normalized_error = _normalize_mcp_error(normalized.get("error"))
    normalized["error"] = normalized_error or None
    if normalized_error:
        normalized["success"] = False
    return normalized


async def execute(state: ChatState) -> dict[str, Any]:
    """LangGraph node – call SAP with validated params."""
    tools_list = state.get("tools", [])

    # --- Multi-tool execution (parallel) ---
    if tools_list and len(tools_list) > 1:

        async def _call_one(t_entry: dict[str, Any]) -> dict[str, Any]:
            t_name = t_entry["tool_name"]
            t_params = t_entry.get("parameters", {})
            _fail = {"success": False, "count": 0, "data": []}
            if t_name == "get_sales_aggregates":
                return await call_sales_aggregates(t_params)
            if t_name == "get_sales_orders_db":
                try:
                    sql = _build_sales_orders_db_sql(t_params)
                except ValueError as exc:
                    return {**_fail, "error": f"Invalid filter value: {exc}"}
                result = await call_mcp_query(
                    sql,
                    state.get("tenant_id", "TENANT_001"),
                    state.get("user_id"),
                    state.get("session_id"),
                )
                return _apply_app_sort(_normalize_mcp_result(result), t_params)
            if t_name == "get_billing_db":
                try:
                    sql = _build_billing_db_sql(t_params)
                except ValueError as exc:
                    return {**_fail, "error": f"Invalid filter value: {exc}"}
                result = await call_mcp_query(
                    sql,
                    state.get("tenant_id", "TENANT_001"),
                    state.get("user_id"),
                    state.get("session_id"),
                )
                return _normalize_mcp_result(result)
            if t_name == "query_database":
                sql = t_params.get("sql", "").strip()
                if not sql:
                    return {**_fail, "error": "No SQL query was generated. Please rephrase your question."}
                result = await call_mcp_query(
                    sql,
                    state.get("tenant_id", "TENANT_001"),
                    state.get("user_id"),
                    state.get("session_id"),
                )
                return _normalize_mcp_result(result)
            tool = get_tool(t_name)
            if tool is None:
                return {**_fail, "error": f"No tool: {t_name}"}
            logger.info("Multi-tool executing (parallel): %s with %s", t_name, t_params)
            skip_expand = _should_skip_expand(tool, t_entry.get("chart_hint"))
            return await call_sap(tool, t_params, skip_expand=skip_expand)

        try:
            raw_responses = await asyncio.wait_for(
                asyncio.gather(*[_call_one(t) for t in tools_list], return_exceptions=True),
                timeout=_MULTI_TOOL_GATHER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("Multi-tool gather timed out after %ds", _MULTI_TOOL_GATHER_TIMEOUT)
            raw_responses = [
                {"success": False, "count": 0, "data": [], "error": "Timeout — SAP did not respond in time"}
                for _ in tools_list
            ]

        # Unwrap exceptions from return_exceptions=True
        sap_responses: list[dict[str, Any]] = []
        for i, resp in enumerate(raw_responses):
            if isinstance(resp, Exception):
                logger.error("Multi-tool call %d failed: %s", i, resp)
                sap_responses.append({"success": False, "count": 0, "data": [], "error": str(resp)})
            else:
                sap_responses.append(resp)

        # Auto-resolve customer IDs → names for each response
        for t_entry, resp in zip(tools_list, sap_responses):
            if resp.get("success") and resp.get("data"):
                resp["data"] = await _resolve_customer_names(
                    resp["data"], t_entry["tool_name"]
                )

        # Collect ALL errors (not just the first)
        errors = []
        for t_entry, r in zip(tools_list, sap_responses):
            if r.get("error"):
                errors.append(f"{t_entry['tool_name']}: {r['error']}")
        execution_error = "; ".join(errors) if errors else ""
        if execution_error:
            logger.warning("Multi-tool partial/full failure: %s", execution_error)

        return {
            "sap_response": sap_responses[0],
            "sap_responses": sap_responses,
            "execution_error": execution_error,
        }

    # --- Single-tool execution (default path) ---
    tool_name = state.get("tool_name", "none")
    validated_params = state.get("validated_params", {})

    tool = get_tool(tool_name)
    if tool_name == "get_sales_aggregates":
        result = await call_sales_aggregates(validated_params)
        return {
            "sap_response": result,
            "execution_error": result.get("error", "") or "",
        }
    if tool_name == "get_sales_orders_db":
        try:
            sql = _build_sales_orders_db_sql(validated_params)
        except ValueError as exc:
            err = f"Invalid filter value: {exc}"
            return {
                "sap_response": {"success": False, "count": 0, "data": [], "error": err},
                "execution_error": err,
            }
        result = await call_mcp_query(
            sql,
            state.get("tenant_id", "TENANT_001"),
            state.get("user_id"),
            state.get("session_id"),
        )
        result = _normalize_mcp_result(result)
        # App-side re-sort for sort_by=value when no filters (DB fetched a
        # larger batch sorted by date to avoid slow full-table JSONB sort).
        result = _apply_app_sort(result, validated_params)
        return {
            "sap_response": result,
            "execution_error": result.get("error", "") or "",
        }
    if tool_name == "get_billing_db":
        try:
            sql = _build_billing_db_sql(validated_params)
        except ValueError as exc:
            err = f"Invalid filter value: {exc}"
            return {
                "sap_response": {"success": False, "count": 0, "data": [], "error": err},
                "execution_error": err,
            }
        result = await call_mcp_query(
            sql,
            state.get("tenant_id", "TENANT_001"),
            state.get("user_id"),
            state.get("session_id"),
        )
        result = _normalize_mcp_result(result)
        return {
            "sap_response": result,
            "execution_error": result.get("error", "") or "",
        }
    if tool_name == "query_database":
        sql = validated_params.get("sql", "").strip()
        if not sql:
            err = "No SQL query was generated. Please rephrase your question."
            return {
                "sap_response": {"success": False, "count": 0, "data": [], "error": err},
                "execution_error": err,
            }
        tenant_id = state.get("tenant_id", "TENANT_001")
        user_id = state.get("user_id")
        session_id = state.get("session_id")
        logger.info("Executing MCP database query: %s", sql[:200])
        result = await call_mcp_query(sql, tenant_id, user_id, session_id)
        result = _normalize_mcp_result(result)
        return {
            "sap_response": result,
            "execution_error": result.get("error", "") or "",
        }

    if tool is None:
        return {
            "sap_response": {
                "success": False,
                "count": 0,
                "data": [],
                "error": "No tool",
            },
            "execution_error": "No tool selected for execution.",
        }

    chart_hint = state.get("chart_hint")
    expands = state.get("tool_expands", [])
    logger.info(
        "Executing SAP call: %s with %s expands=%s",
        tool_name,
        validated_params,
        expands,
    )

    skip_expand = _should_skip_expand(tool, chart_hint)
    result = await call_sap(
        tool, validated_params, skip_expand=skip_expand, expands=expands or None
    )

    # Auto-resolve customer IDs → names
    if result.get("success") and result.get("data"):
        result["data"] = await _resolve_customer_names(result["data"], tool_name)

    return {
        "sap_response": result,
        "execution_error": result.get("error", "") or "",
    }
