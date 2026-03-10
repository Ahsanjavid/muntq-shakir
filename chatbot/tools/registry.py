from __future__ import annotations

from chatbot.models import SAPTool
from chatbot.tools.definitions import (
    GET_BILLING_DB,
    GET_SALES_AGGREGATES,
    GET_SALES_ORDERS_SAP,
    GET_SALES_ORDERS_DB,
    GET_BUSINESS_PARTNERS,
    GET_DELIVERIES,
    GET_INVOICES,
    GET_JOURNAL_ENTRIES,
    GET_MATERIALS,
    GET_PURCHASE_ORDER_ITEMS,
    GET_PURCHASE_ORDERS,
    QUERY_DATABASE,
)

ALL_TOOLS: list[SAPTool] = [
    GET_SALES_AGGREGATES,
    GET_SALES_ORDERS_DB,
    GET_BILLING_DB,
    QUERY_DATABASE,
    GET_SALES_ORDERS_SAP,
    GET_PURCHASE_ORDERS,
    GET_PURCHASE_ORDER_ITEMS,
    GET_INVOICES,
    GET_BUSINESS_PARTNERS,
    GET_MATERIALS,
    GET_DELIVERIES,
    GET_JOURNAL_ENTRIES,
]

TOOL_MAP: dict[str, SAPTool] = {t.name: t for t in ALL_TOOLS}


def get_tool(name: str) -> SAPTool | None:
    """Return the SAPTool for *name*, or None."""
    return TOOL_MAP.get(name)


def get_tool_by_name(name: str) -> SAPTool | None:
    """
    Get tool by name for validation (alias for get_tool).
    Used by parse_route for expand validation.
    """
    return TOOL_MAP.get(name)


def tool_names() -> list[str]:
    return list(TOOL_MAP.keys())


def tools_for_llm_prompt() -> str:
    """Build full tool catalogue (all tools with params). Used in stage-2."""
    lines: list[str] = []
    for tool in ALL_TOOLS:
        lines.append(f"### {tool.name}")
        lines.append(f"Description: {tool.description}")
        lines.append("Parameters:")
        for p in tool.parameters:
            req = "REQUIRED" if p.required else "optional"
            extra = ""
            if p.enum:
                extra = f"  allowed: {p.enum}"
            if p.format_hint:
                extra += f"  format: {p.format_hint}"
            lines.append(
                f"  - {p.name} ({p.type.value}, {req}): {p.description}{extra}"
            )
        lines.append("")
    return "\n".join(lines)


# ── Compact one-liner summaries for stage-1 routing ──────────────────────────

_TOOL_SUMMARIES: dict[str, str] = {
    "get_sales_aggregates": "Curated sales KPIs from dashboard API (today/MTD/QTD/trends/orders/period).",
    "get_sales_orders_db": "Sales orders from PostgreSQL snapshots (fast historical transactional lookup).",
    "get_billing_db": "Billing documents from PostgreSQL snapshots (fast historical invoice lookup).",
    "query_database": "Ad-hoc SQL against PostgreSQL for advanced analytics and custom reports.",
    "get_sales_orders": "Transactional sales orders from SAP: filter by customer, date, org, material, status",
    "get_purchase_orders": "Purchase order headers from SAP: filter by vendor, date, org, type",
    "get_purchase_order_items": "PO line items with GR/IR status: goods receipt, invoice receipt tracking",
    "get_invoices": "Billing documents from SAP: filter by customer, date, billing type",
    "get_business_partners": "Customers/vendors master data: search by name, category, blocked status",
    "get_materials": "Product master data: filter by type, description, product group",
    "get_deliveries": "Outbound deliveries from SAP: filter by customer, date, shipping point, status",
    "get_journal_entries": "Financial GL entries from SAP: filter by company code, account, cost center, date",
}


def tools_compact_summary() -> str:
    """Compact 1-line-per-tool summary for stage-1 routing (~500 tokens)."""
    lines = []
    for name in TOOL_MAP:
        summary = _TOOL_SUMMARIES.get(name, TOOL_MAP[name].description[:80])
        lines.append(f"- **{name}**: {summary}")
    return "\n".join(lines)


def tool_detail_prompt(tool_name: str) -> str:
    """Full detail for a single tool (stage-2 param extraction)."""
    tool = TOOL_MAP.get(tool_name)
    if not tool:
        return ""
    lines = [f"### {tool.name}", f"Description: {tool.description}", "Parameters:"]
    for p in tool.parameters:
        req = "REQUIRED" if p.required else "optional"
        extra = ""
        if p.enum:
            extra = f"  allowed: {p.enum}"
        if p.format_hint:
            extra += f"  format: {p.format_hint}"
        lines.append(f"  - {p.name} ({p.type.value}, {req}): {p.description}{extra}")
    return "\n".join(lines)


def tools_detail_prompt(tool_names_list: list[str]) -> str:
    """Full detail for multiple selected tools (stage-2)."""
    parts = []
    for name in tool_names_list:
        detail = tool_detail_prompt(name)
        if detail:
            parts.append(detail)
    return "\n\n".join(parts)
