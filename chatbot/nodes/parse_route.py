from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from chatbot.config import settings
from chatbot.llm_client import get_llm_client
from chatbot.state import ChatState
from chatbot.tools.registry import (
    tool_names,
    tools_compact_summary,
    tools_detail_prompt,
    get_tool_by_name,
)

logger = logging.getLogger(__name__)


def _build_expand_catalogue() -> str:
    """Build the expandable data catalogue for LLM prompt."""
    from chatbot.tools.definitions import (
        GET_SALES_ORDERS_SAP,
        GET_PURCHASE_ORDERS,
        GET_PURCHASE_ORDER_ITEMS,
        GET_INVOICES,
        GET_BUSINESS_PARTNERS,
        GET_MATERIALS,
        GET_DELIVERIES,
    )

    tools_with_expands = [
        GET_SALES_ORDERS_SAP,
        GET_PURCHASE_ORDERS,
        GET_PURCHASE_ORDER_ITEMS,
        GET_INVOICES,
        GET_BUSINESS_PARTNERS,
        GET_MATERIALS,
        GET_DELIVERIES,
    ]

    catalogue_lines = []
    for tool in tools_with_expands:
        if not tool.available_expands:
            continue
        catalogue_lines.append(f"**{tool.name}:**")
        for expand_name, expand_desc in tool.available_expands.items():
            default_marker = (
                " *(default)*" if expand_name in tool.default_expands else ""
            )
            catalogue_lines.append(
                f"  - '{expand_name}': {expand_desc}{default_marker}"
            )
        catalogue_lines.append("")
    return "\n".join(catalogue_lines)


# ── Message trimming for routing context ──────────────────────────────────────

_MAX_MSG_CHARS = 300  # max chars per message sent to routing LLM


def _trim_messages_for_routing(messages: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    """Return the last *count* messages, truncating long assistant replies.

    Assistant messages from final_answer can be 2000+ chars with data tables.
    For routing purposes, only the first few sentences matter (what was asked,
    what was answered).  This saves tokens and prevents data bleed into routing.
    """
    trimmed = []
    for msg in messages[-count:]:
        if msg.get("role") == "assistant" and len(msg.get("content", "")) > _MAX_MSG_CHARS:
            content = msg["content"][:_MAX_MSG_CHARS] + "…"
            trimmed.append({"role": "assistant", "content": content})
        else:
            trimmed.append(msg)
    return trimmed


# ── Stage 1: Lightweight tool selection (~500 tokens of tool info) ────────────

STAGE1_PROMPT = """\
You are **MUNTQ**, an intelligent SAP data assistant. Pick the best tool(s) for
the user's question.

## Today's date
{today}  (current year: {year})

## Available tools (one-line summaries)
{compact_tools}

## Selection rules
- **get_sales_aggregates**: PRIMARY for standard sales KPI/report questions
  (today sales, MTD/QTD, sales for a specific month/period, sales trends,
  monthly comparisons). Returns pre-computed summary metrics, NOT individual rows.
- **query_database**: Use for CUSTOM analytics: grouping, ranking, top-N lists,
  comparisons, counts, averages, or time-series that need individual row access.
  Also use when user wants a LIST of specific items sorted/ranked (e.g. "top 5
  sales orders by value", "largest invoices", "customers with most orders").
- **get_sales_orders_db**: Use for listing/looking up specific sales orders
  (e.g. "show order 12345", "list orders for customer X", "orders created today",
  "top 5 orders by value last month"). Returns individual order rows.
  Do NOT use for aggregation — use query_database instead.
- **get_billing_db**: Use for listing/looking up specific billing documents.
  Do NOT use for aggregation — use query_database instead.
- **get_sales_orders**: Live SAP lookup — ONLY when user explicitly asks for real-time
  or live data, or needs expanded navigation properties (items, partners, pricing).
- SAP master-data tools (materials/partners/PO/deliveries/journal): Use when the
  data is NOT in PostgreSQL. DB has: sales_order_snapshots, billing_snapshots,
  curated_aggregates, sales_hourly/daily/monthly/quarterly/yearly, sales_daily_cluster.
- If the request is not about SAP data or SAP business analysis → tool_name="none"
  (strictly refuse off-topic in final response)
- If intent is genuinely ambiguous → tool_name="clarify"
- For multi-entity queries, return a "tools" array (max 3). But prefer a single
  query_database call with JOINs over multi-tool when the data is in PostgreSQL.
- If the previous turn had a pending tool, the user is likely answering it

## Quick decision examples
- "today sales" → get_sales_aggregates (report_type=today)
- "January sales" → get_sales_aggregates (report_type=period)
- "this month sales" → get_sales_aggregates (report_type=mtd)
- "sales trend last 6 months" → get_sales_aggregates (report_type=trends)
- "orders overview / order analysis" → get_sales_aggregates (report_type=orders)
- "show order 12345" → get_sales_orders_db
- "list blocked orders" → get_sales_orders_db
- "top 5 sales orders" / "largest orders" → get_sales_orders_db (top=5, sort_by=value)
- "top 5 orders last month" → get_sales_orders_db (date_from, date_to, top=5, sort_by=value)
- "top 10 customers by revenue" → query_database (GROUP BY + ORDER BY)
- "compare Q1 vs Q2 sales by region" C query_database (custom SQL needed)
- "avg order value per month last year" → query_database
- "which materials appear in the most orders" → query_database (COUNT + GROUP BY)
- "what is SAP?" → none (out of scope; refuse)

**IMPORTANT**: "top N orders/invoices" means the user wants to SEE individual rows
sorted by value — use get_sales_orders_db or query_database, NOT get_sales_aggregates.
get_sales_aggregates returns summary KPIs, not individual records.

## Output — return ONLY valid JSON

Single tool:
{{"tool_name": "<name>", "reasoning": "<brief>"}}

Multi-tool:
{{"tools": [{{"tool_name": "..."}}, {{"tool_name": "..."}}], "reasoning": "..."}}
"""


# ── Stage 2: Full param extraction (only selected tool's details) ─────────────

STAGE2_PROMPT = """\
You are **MUNTQ**. Extract precise parameters for the selected tool.

## Today's date
{today}  (current year: {year})

## Selected tool details
{tool_details}

{expand_section}

## Rules
- Extract every parameter you can infer from the user's question.
- Dates MUST be ISO YYYY-MM-DD.
- Resolve relative dates: "last month" → actual dates, "this year" → {year}-01-01 to {today}.
- **Default date range** ONLY for LIVE SAP transactional tools
  (get_sales_orders, get_purchase_orders, get_purchase_order_items,
  get_invoices, get_deliveries, get_journal_entries): if the user gives NO date,
  set date_from="{year}-01-01", date_to="{today}". Mention in reasoning.
- For DB snapshot tools (get_sales_orders_db, get_billing_db), do NOT auto-add
  date_from/date_to unless the user explicitly requests a date period.
- For master-data tools (materials, business partners) — no date default needed.
- If the user says "all time", set date_from="{five_years_ago}", date_to="{today}".
- **top**: Only set when user specifies a number. Otherwise omit (default applies).
- **expands**: Only include if user needs data BEYOND default expands.
- **query_database**: Write a SELECT query. Do NOT include tenant_id filter (auto-injected).
  Use payload->>'FieldName' for JSONB columns. ALL fields in sales_order_snapshots
  and billing_snapshots are inside payload JSONB — there are NO top-level columns
  except: id, tenant_id, sales_order (or billing_document), payload, last_changed_at,
  billing_date, created_at, updated_at.
  For date filtering use the timestamp columns: last_changed_at (sales), billing_date (billing).
  Do NOT filter on payload->>'CreationDate' (SAP /Date() format is not SQL-comparable).
  For "all time" queries, omit date filters entirely.
  Prefer simple single-SELECT queries. For year-over-year comparisons, use
  EXTRACT(YEAR FROM last_changed_at) or date_trunc with a CASE/GROUP BY
  instead of UNION ALL. UNION ALL is supported but keep queries simple.
- **get_sales_aggregates**:
  - Always set report_type from one of: today|mtd|qtd|trends|orders|period.
  - **today**: Only for "today's sales" (no date params needed).
  - **mtd**: Only for "this month" / "month to date" (no date params needed — auto-computed).
  - **qtd**: Only for "this quarter" / "quarter to date" (no date params needed — auto-computed).
  - **period**: For ANY specific date range or named month/period. Examples:
    "January sales" → period with date_from=YYYY-01-01, date_to=YYYY-01-31.
    "last month sales" → period with actual dates. "sales from March 1 to March 15" → period.
    ALWAYS requires date_from AND date_to.
  - **trends**: For "last N months", "monthly sales", "sales trend", "year-over-year trend".
  - **orders**: For "order overview", "order analysis", "order-wise report" (returns aggregate
    summaries like top materials, status breakdown, blocked counts — NOT individual order rows).
  - Do NOT use mtd/qtd when user specifies explicit dates — use period instead.
- **chart_hint**: Include if the question implies a chart/trend/comparison:
  group_by, metric, agg (sum|count|avg|max|min), time_bucket (month|quarter|year),
  chart_type (bar|line|pie|scatter), top_n, dataset_label. Else null.

## Output — return ONLY valid JSON
{{
  "parameters": {{...}},
  "expands": [],
  "chart_hint": null | {{...}},
  "reasoning": "<brief>"
}}
"""


async def _llm_call(messages: list[dict], tag: str) -> dict | None:
    """Make an LLM call with retry. Returns parsed JSON or None."""
    client = get_llm_client()
    for attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=settings.LLM_MODEL,
                temperature=settings.LLM_TEMPERATURE,
                messages=messages,
                response_format={"type": "json_object"},
                timeout=30,
            )
            raw = (response.choices[0].message.content if response.choices else None) or "{}"
            logger.info("LLM %s (attempt %d): %s", tag, attempt + 1, raw[:500])
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM %s: invalid JSON on attempt %d", tag, attempt + 1)
        except Exception as exc:
            logger.warning("LLM %s failed on attempt %d: %s", tag, attempt + 1, exc)
    return None


async def parse_and_route(state: ChatState) -> dict[str, Any]:
    """LangGraph node – two-stage routing: pick tool, then extract params."""
    user_msg = state["user_message"]
    messages = state.get("messages", [])
    pending_tool = state.get("tool_name", "")
    pending_params = state.get("tool_params", {})

    today = date.today()
    try:
        five_years_ago = today.replace(year=today.year - 5)
    except ValueError:
        five_years_ago = today.replace(year=today.year - 5, day=28)

    updated_messages = [*messages, {"role": "user", "content": user_msg}]

    # Pending-tool context note
    context_note = ""
    if pending_tool and pending_tool not in ("none", "clarify", ""):
        context_note = (
            f"\n[SYSTEM CONTEXT: Previous turn was waiting for user input on "
            f"tool '{pending_tool}' with params {json.dumps(pending_params)}. "
            f"Merge the user's new message with these pending params.]\n"
        )

    # ── STAGE 1: Tool selection (compact prompt) ──────────────────────────

    stage1_system = STAGE1_PROMPT.format(
        today=today.isoformat(),
        year=today.year,
        compact_tools=tools_compact_summary(),
    )

    stage1_msgs: list[dict[str, str]] = [
        {"role": "system", "content": stage1_system},
        *_trim_messages_for_routing(messages, 6),
    ]
    if context_note:
        stage1_msgs.append({"role": "system", "content": context_note})
    stage1_msgs.append({"role": "user", "content": user_msg})

    s1 = await _llm_call(stage1_msgs, "stage1-route")

    if s1 is None:
        return {
            "tool_name": "none",
            "tool_params": {},
            "tool_expands": [],
            "routing_reasoning": "LLM unavailable — please try again.",
            "chart_hint": None,
            "messages": updated_messages,
        }

    valid_names = {*tool_names(), "none", "clarify"}

    # Parse selected tool(s)
    tools_list = s1.get("tools")
    if tools_list and isinstance(tools_list, list) and len(tools_list) > 1:
        # Multi-tool path
        selected_names = [
            t.get("tool_name") for t in tools_list[:3] if t.get("tool_name") in valid_names
        ]
        if not selected_names:
            selected_names = ["none"]
    else:
        raw_name = s1.get("tool_name", "none")
        selected_names = [raw_name if raw_name in valid_names else "none"]

    # If only "none" or "clarify" — skip stage 2
    if selected_names == ["none"]:
        return {
            "tool_name": "none",
            "tool_params": {},
            "tool_expands": [],
            "routing_reasoning": s1.get("reasoning", ""),
            "chart_hint": None,
            "messages": updated_messages,
        }
    if selected_names == ["clarify"]:
        return {
            "tool_name": "clarify",
            "tool_params": {},
            "tool_expands": [],
            "routing_reasoning": s1.get("reasoning", ""),
            "chart_hint": None,
            "messages": updated_messages,
        }

    # ── STAGE 2: Parameter extraction (full detail for selected tools) ────

    # Build expand section only for tools that have expands
    expand_catalogue = _build_expand_catalogue()
    has_expandable = any(
        get_tool_by_name(n) and get_tool_by_name(n).available_expands
        for n in selected_names
    )
    expand_section = ""
    if has_expandable:
        expand_section = (
            "## Expandable Data (Nested Navigation Properties)\n\n"
            + expand_catalogue
            + "\n\n**Expand rules:** Only include expands if user explicitly needs data beyond defaults."
        )

    stage2_system = STAGE2_PROMPT.format(
        today=today.isoformat(),
        year=today.year,
        five_years_ago=five_years_ago.isoformat(),
        tool_details=tools_detail_prompt(selected_names),
        expand_section=expand_section,
    )

    stage2_msgs: list[dict[str, str]] = [
        {"role": "system", "content": stage2_system},
        *_trim_messages_for_routing(messages, 10),
    ]
    if context_note:
        stage2_msgs.append({"role": "system", "content": context_note})
    stage2_msgs.append({"role": "user", "content": user_msg})

    s2 = await _llm_call(stage2_msgs, "stage2-params")

    if s2 is None:
        s2 = {"parameters": {}, "expands": [], "chart_hint": None, "reasoning": ""}

    final_params = s2.get("parameters", {})
    expands = s2.get("expands", [])
    chart_hint = s2.get("chart_hint") or None
    reasoning = s1.get("reasoning", "") + " | " + s2.get("reasoning", "")

    # ── Multi-tool return ─────────────────────────────────────────────────

    if len(selected_names) > 1:
        # For multi-tool, stage 2 may return per-tool params or merged params.
        # If s2 has a "tools" array, use it; else apply same params to all.
        s2_tools = s2.get("tools")
        if s2_tools and isinstance(s2_tools, list):
            valid_tools = []
            for t in s2_tools[:3]:
                t_name = t.get("tool_name", selected_names[0])
                if t_name in valid_names:
                    valid_tools.append({
                        "tool_name": t_name,
                        "parameters": t.get("parameters", {}),
                        "chart_hint": t.get("chart_hint"),
                    })
        else:
            valid_tools = [
                {"tool_name": n, "parameters": final_params, "chart_hint": chart_hint}
                for n in selected_names
            ]

        logger.info("Multi-tool: %s", [t["tool_name"] for t in valid_tools])
        return {
            "tool_name": valid_tools[0]["tool_name"],
            "tool_params": valid_tools[0]["parameters"],
            "tool_expands": expands,
            "routing_reasoning": reasoning,
            "chart_hint": valid_tools[0].get("chart_hint"),
            "messages": updated_messages,
            "tools": valid_tools,
        }

    # ── Single-tool return ────────────────────────────────────────────────

    tool_name = selected_names[0]

    # Validate expands
    if tool_name not in ("none", "clarify") and expands:
        tool = get_tool_by_name(tool_name)
        if tool and tool.available_expands:
            valid_expands = set(tool.available_expands.keys())
            expands = [e for e in expands if e in valid_expands]
        else:
            expands = []

    # Merge pending params if user was providing missing info
    if pending_tool and pending_tool not in ("none", "clarify", ""):
        if tool_name in ("none", "clarify"):
            tool_name = pending_tool
        final_params = {**pending_params, **final_params}

    return {
        "tool_name": tool_name,
        "tool_params": final_params,
        "tool_expands": expands,
        "routing_reasoning": reasoning,
        "chart_hint": chart_hint,
        "messages": updated_messages,
    }

