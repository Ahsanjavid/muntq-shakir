from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from chatbot.config import settings
from chatbot.llm_client import get_llm_client
from chatbot.state import ChatState

logger = logging.getLogger(__name__)

# Regex to extract "answer" value from partial JSON stream
_ANSWER_START_RE = re.compile(r'"answer"\s*:\s*"', re.DOTALL)

_MAX_SAP_SNAPSHOTS = 4  # keep last N SAP data snapshots in session
_MAX_CONTEXT_ROWS = 50  # max rows sent to LLM for context
_MAX_CONTEXT_CHARS = 30000  # hard limit on total context chars sent to LLM (~7500 tokens)
_MULTI_TOOL_TOTAL_BUDGET = 45000  # total char budget across all tools in multi-tool path

_LLM_FALLBACK_TOOL = '{"answer":"I encountered a temporary issue generating a response. The data was retrieved successfully — please try again.","chart":null}'
_LLM_FALLBACK_GENERAL = '{"answer":"I encountered a temporary issue. Please try again.","chart":null}'


async def _streaming_llm_call(
    messages: list[dict],
    token_queue: asyncio.Queue | None,
    fallback: str,
    temperature: float = 0.2,
) -> str:
    """Call LLM with streaming. Push answer tokens to queue as they arrive.

    Returns the full raw JSON response string.
    Streams only the 'answer' field content to the queue (not chart JSON).
    Uses a simple state machine on the accumulated text to find the "answer"
    JSON string value and forward its contents character-by-character.
    """
    client = get_llm_client()
    full_text = ""
    # Track how far we've scanned in full_text for answer extraction
    _scan_pos = 0
    _in_answer = False
    _escaped = False  # True when previous char was an unescaped backslash
    _unicode_buf = None  # None = inactive; "" = collecting hex digits after \u

    try:
        stream = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            temperature=temperature,
            messages=messages,
            response_format={"type": "json_object"},
            timeout=45,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta or not delta.content:
                continue

            token = delta.content
            full_text += token

            if token_queue is None:
                continue

            # Process newly added characters from _scan_pos to end of full_text
            to_send = ""
            while _scan_pos < len(full_text):
                ch = full_text[_scan_pos]
                _scan_pos += 1

                if not _in_answer:
                    # Look for the pattern "answer" : " in full_text
                    # We check if the accumulated text up to _scan_pos ends with
                    # the answer field opening. Use regex on the tail.
                    match = _ANSWER_START_RE.search(full_text[:_scan_pos])
                    if match and match.end() == _scan_pos:
                        _in_answer = True
                        _escaped = False
                    continue

                # We're inside the answer string value

                # Accumulating \uXXXX hex digits
                if _unicode_buf is not None:
                    _unicode_buf += ch
                    if len(_unicode_buf) == 4:
                        try:
                            to_send += chr(int(_unicode_buf, 16))
                        except ValueError:
                            to_send += "\\u" + _unicode_buf
                        _unicode_buf = None  # back to inactive state
                    continue

                if _escaped:
                    # Previous char was backslash — this is an escaped char
                    _escaped = False
                    # Convert JSON escapes to display chars
                    if ch == "u":
                        _unicode_buf = ""  # start collecting 4 hex digits
                    elif ch == "n":
                        to_send += "\n"
                    elif ch == "t":
                        to_send += "\t"
                    elif ch == "\\":
                        to_send += "\\"
                    elif ch == '"':
                        to_send += '"'
                    elif ch == "/":
                        to_send += "/"
                    else:
                        to_send += ch
                    continue

                if ch == "\\":
                    _escaped = True
                    continue

                if ch == '"':
                    # End of answer string value
                    _in_answer = False
                    continue

                to_send += ch

            if to_send:
                await token_queue.put(to_send)

    except Exception as exc:
        logger.error("Streaming LLM call failed: %s", exc)
        full_text = fallback

    # NOTE: do NOT send a sentinel (None) here — the caller (_run_graph in
    # app.py) always pushes a sentinel in its `finally` block, which covers
    # all exit paths (success, early exit, exception).  A double sentinel
    # would leave a stale item on the queue.

    return full_text or fallback


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

TOOL_ANSWER_SYSTEM = """\
You are **MUNTQ**, an SAP enterprise analytics assistant.

The system has already queried SAP / the dashboard API and returned structured
data below.  Your job: interpret the data and answer the user's question.

## Rules
1. **Trust the data** — all numbers are pre-computed. Do NOT recalculate or invent.
2. State the exact record count when relevant.
3. Format currency values with commas.  Use human-readable dates.
4. If data is missing or empty, say so clearly.
5. If `raw_payload` contains KPI fields (total_sales, order_count, etc.),
   use those as the authoritative summary — the `rows` are the detail breakdown.
6. Keep your answer concise and professional.
7. All monetary values must be in SAR, never $
8. When `_SoldToName`, `_ShipToName`, or `_PayerName` fields are present,
   use the human-readable NAME instead of the numeric ID in your answer.
   Always prefer showing the customer/partner name over showing IDs like SoldToParty.

## Chart rules
- If the user asks for a chart/graph/trend/comparison, return a chart object.
- Pick the best chart type: **line** for time series, **bar** for comparisons,
  **pie** for proportions, **scatter** for correlations.
- Build chart labels/values from the provided rows or raw_payload.
- If no chart is needed, set "chart": null.

## Response format
Return valid JSON (no markdown fences):
{
  "answer": "<markdown text>",
  "chart": null | { "type": "bar|line|pie|scatter", "labels": [...], "values": [...], "dataset_label": "..." }
}
"""

GENERAL_SYSTEM = """\
You are **MUNTQ**, a strict SAP analytics assistant.

The conversation history may contain previous SAP data snapshots (structured
JSON from earlier queries).  Treat them as source of truth for follow-up
analysis, charting, or summarisation.

## Strict scope guardrail
- ONLY handle SAP data queries, SAP business analysis, or analysis of previously
  retrieved SAP datasets.
- If the user asks anything outside SAP data/analysis (small talk, emotions,
  personal advice, general knowledge, tests, jokes, unrelated topics), refuse.
- Refusal must be short and direct:
  "I can only help with SAP data queries and SAP business analysis."

## Chart rules
- If the user asks for a chart of previously retrieved data, build it from
  the SAP data snapshots.  Use EVERY record — do not skip any.
- Pick the best chart type for the data.
- State the total record count so the user can verify.

## Response format
Return valid JSON (no markdown fences):
{
  "answer": "<markdown text>",
  "chart": null | { "type": "bar|line|pie|scatter", "labels": [...], "values": [...], "dataset_label": "..." }
}
"""

MULTI_TOOL_SYSTEM = (
    TOOL_ANSWER_SYSTEM
    + """
## Multi-entity context
Multiple SAP tools were called.  Each dataset is labelled separately below.
Cross-reference the datasets when answering.  Clearly label which entity each
insight comes from.
"""
)


# ---------------------------------------------------------------------------
# Context builders (lightweight — no local aggregation)
# ---------------------------------------------------------------------------


def _build_tool_context(
    tool_name: str,
    sap: dict[str, Any],
    data_rows: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> str:
    """Build compact LLM context from tool output. No local aggregation.

    Enforces _MAX_CONTEXT_CHARS to prevent token blowout on large SQL results
    or aggregate payloads with hundreds of daily entries.
    """
    total_count = int(sap.get("count", 0) or 0)
    raw_payload = (
        sap.get("raw_payload", {}) if isinstance(sap.get("raw_payload"), dict) else {}
    )
    err = (extra or {}).get("error", "")

    parts = [
        f"Tool: {tool_name}",
        f"Record count: {total_count}",
    ]
    if err:
        parts.append(f"Error: {err}")

    # Include user's requested date range (aggregate API returns full data;
    # LLM should focus on the requested period).
    req_from = sap.get("requested_date_from")
    req_to = sap.get("requested_date_to")
    if req_from or req_to:
        parts.append(
            f"User requested date range: {req_from or 'earliest'} to {req_to or 'latest'}"
        )
        parts.append(
            "IMPORTANT: The data below contains ALL periods. Filter your answer to only cover the requested date range."
        )

    # Track running size to enforce hard limit
    current_size = sum(len(p) for p in parts)

    # Include raw_payload KPIs (dashboard endpoints return summary fields)
    if raw_payload:
        # Filter out large nested structures for the summary line
        scalar_kpis = {
            k: v for k, v in raw_payload.items() if not isinstance(v, (dict, list))
        }
        if scalar_kpis:
            kpi_str = f"Summary KPIs: {json.dumps(scalar_kpis, default=str)}"
            parts.append(kpi_str)
            current_size += len(kpi_str)

        # Include small nested dicts (e.g. monthly_by_year, daily_sales)
        # but cap large dicts to avoid token blowout
        for k, v in raw_payload.items():
            if current_size >= _MAX_CONTEXT_CHARS:
                parts.append(f"... (context truncated at {_MAX_CONTEXT_CHARS} chars)")
                break
            if isinstance(v, dict) and len(v) <= 30:
                chunk = f"{k}: {json.dumps(v, default=str)}"
                if current_size + len(chunk) <= _MAX_CONTEXT_CHARS:
                    parts.append(chunk)
                    current_size += len(chunk)
            elif isinstance(v, list) and len(v) <= 20:
                chunk = f"{k} ({len(v)} items): {json.dumps(v[:15], default=str)}"
                if current_size + len(chunk) <= _MAX_CONTEXT_CHARS:
                    parts.append(chunk)
                    current_size += len(chunk)

    # Include data rows (capped by count AND total size)
    if data_rows and current_size < _MAX_CONTEXT_CHARS:
        remaining_budget = _MAX_CONTEXT_CHARS - current_size - 200  # leave room for header
        capped = data_rows[:_MAX_CONTEXT_ROWS]
        rows_json = json.dumps(capped, default=str)

        if len(rows_json) > remaining_budget:
            # Progressively reduce rows until they fit
            for limit in (30, 20, 10, 5):
                rows_json = json.dumps(data_rows[:limit], default=str)
                if len(rows_json) <= remaining_budget:
                    capped = data_rows[:limit]
                    break
            else:
                capped = data_rows[:3]
                rows_json = json.dumps(capped, default=str)

        parts.append(f"Data rows (first {len(capped)} of {total_count}):")
        parts.append(rows_json)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


async def final_answer(state: ChatState) -> dict[str, Any]:
    """LangGraph node — produce the user-facing response."""
    tool_name = state.get("tool_name", "none")
    sap = state.get("sap_response", {})
    user_msg = state.get("user_message", "")
    err = state.get("execution_error", "")
    export_available = state.get("export_available", False)
    tools_list = state.get("tools", [])
    sap_responses = state.get("sap_responses", [])

    # --- No tool / clarify → general conversation ---
    if tool_name in ("none", "clarify"):
        return await _general_answer(state)

    # --- Multi-tool path ---
    if tools_list and len(tools_list) > 1 and sap_responses:
        return await _multi_tool_answer(state, tools_list, sap_responses)

    # --- Single tool path (aggregate or direct SAP) ---
    data_rows = sap.get("data", [])
    tool_params = (
        state.get("validated_params", {}) or state.get("tool_params", {}) or {}
    )

    # Do not ask LLM to "summarize" when backend execution failed with no data.
    if err and not data_rows and not sap.get("success", False):
        answer = f"I could not retrieve data due to: {err}"
        messages = list(state.get("messages", []))
        messages.append({"role": "assistant", "content": answer})
        return {
            "final_response": answer,
            "chart_data": None,
            "messages": messages,
            "sap_data_history": list(state.get("sap_data_history", []))[-_MAX_SAP_SNAPSHOTS:],
            "export_available": export_available,
        }

    context = _build_tool_context(tool_name, sap, data_rows, {"error": err})

    # Add tool params for context
    if tool_params:
        context = f"Params: {json.dumps(tool_params, default=str)}\n{context}"

    token_queue = state.get("_ws_token_queue")
    llm_messages = [
        {"role": "system", "content": TOOL_ANSWER_SYSTEM},
        {"role": "user", "content": f"User question: {user_msg}\n\n{context}"},
    ]

    raw = await _streaming_llm_call(llm_messages, token_queue, _LLM_FALLBACK_TOOL)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"answer": raw, "chart": None}

    answer = parsed.get("answer", "") or "Report generated."
    chart = parsed.get("chart")

    # Add export note if multi-year data available
    if (
        export_available
        and "excel" not in answer.lower()
        and "export" not in answer.lower()
    ):
        answer += "\n\n📥 *Multi-year data is available — use the Export button to download the full dataset as Excel.*"

    # Update conversation history
    messages = list(state.get("messages", []))
    messages.append({"role": "assistant", "content": answer})

    # Update SAP data history (for follow-up questions)
    sap_data_history = list(state.get("sap_data_history", []))
    if data_rows:
        sap_data_history.append(data_rows[:100])
    sap_data_history = sap_data_history[-_MAX_SAP_SNAPSHOTS:]

    return {
        "final_response": answer,
        "chart_data": chart,
        "messages": messages,
        "sap_data_history": sap_data_history,
        "export_available": export_available,
    }


async def _multi_tool_answer(
    state: ChatState,
    tools_list: list[dict[str, Any]],
    sap_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Handle final answer when multiple SAP tools were called."""
    user_msg = state.get("user_message", "")
    export_available = state.get("export_available", False)

    context_parts = [f"Multi-entity query: {len(tools_list)} tools called"]
    all_data_for_history: list[list[dict[str, Any]]] = []
    remaining_budget = _MULTI_TOOL_TOTAL_BUDGET
    per_tool_budget = remaining_budget // max(len(tools_list), 1)

    for i, (t_entry, sap_resp) in enumerate(zip(tools_list, sap_responses)):
        t_name = t_entry["tool_name"]
        data_rows = sap_resp.get("data", [])

        chunk = _build_tool_context(t_name, sap_resp, data_rows)
        if len(chunk) > per_tool_budget:
            chunk = chunk[:per_tool_budget] + f"\n... (truncated at {per_tool_budget} chars)"

        context_parts.append("")
        context_parts.append(f"=== Dataset {i + 1}: {t_name} ===")
        context_parts.append(chunk)
        remaining_budget -= len(chunk)

        if data_rows:
            all_data_for_history.append(data_rows[:100])

        if remaining_budget <= 0:
            context_parts.append("... (additional datasets omitted, budget exhausted)")
            break

    context = "\n".join(context_parts)

    token_queue = state.get("_ws_token_queue")
    llm_messages = [
        {"role": "system", "content": MULTI_TOOL_SYSTEM},
        {"role": "user", "content": f"User question: {user_msg}\n\n{context}"},
    ]

    raw = await _streaming_llm_call(llm_messages, token_queue, _LLM_FALLBACK_TOOL)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"answer": raw, "chart": None}

    answer = parsed.get("answer", "")
    chart = parsed.get("chart")

    messages = list(state.get("messages", []))
    messages.append({"role": "assistant", "content": answer})

    sap_data_history = list(state.get("sap_data_history", []))
    sap_data_history.extend(all_data_for_history)
    sap_data_history = sap_data_history[-_MAX_SAP_SNAPSHOTS:]

    return {
        "final_response": answer,
        "chart_data": chart,
        "messages": messages,
        "sap_data_history": sap_data_history,
        "export_available": export_available,
    }


async def _general_answer(state: ChatState) -> dict[str, Any]:
    """Handle non-tool conversational messages (including follow-up charts)."""
    user_msg = state.get("user_message", "")
    messages = state.get("messages", [])
    sap_data_history = state.get("sap_data_history", [])
    sap_data_history = sap_data_history[-_MAX_SAP_SNAPSHOTS:]

    def _is_sap_analysis_request(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        keywords = (
            "sap", "sales", "order", "orders", "billing", "invoice", "customer",
            "material", "delivery", "purchase", "journal", "kpi", "mtd", "qtd",
            "today", "trend", "analysis", "analyze", "compare", "report",
            "chart", "graph", "dashboard", "revenue", "profit", "sql",
        )
        return any(k in t for k in keywords)

    # Hard guardrail: no off-topic chatting when there is no SAP dataset context.
    if not sap_data_history and not _is_sap_analysis_request(user_msg):
        refusal = "I can only help with SAP data queries and SAP business analysis."
        updated = [*messages, {"role": "assistant", "content": refusal}]
        return {
            "final_response": refusal,
            "chart_data": None,
            "messages": updated,
        }

    data_context = ""
    if sap_data_history:
        context_budget = _MAX_CONTEXT_CHARS
        for i, snapshot in enumerate(sap_data_history, 1):
            # Cap each snapshot to avoid token blowout on follow-ups
            capped = snapshot[:_MAX_CONTEXT_ROWS]
            snippet = json.dumps(capped, default=str, separators=(",", ":"))
            if len(snippet) > context_budget:
                # Reduce rows progressively
                for limit in (20, 10, 5):
                    snippet = json.dumps(snapshot[:limit], default=str, separators=(",", ":"))
                    if len(snippet) <= context_budget:
                        capped = snapshot[:limit]
                        break
                else:
                    snippet = json.dumps(snapshot[:3], default=str, separators=(",", ":"))
                    capped = snapshot[:3]
            data_context += (
                f"\n--- SAP Query {i}: {len(capped)} of {len(snapshot)} records ---\n{snippet}\n"
            )
            context_budget -= len(snippet)
            if context_budget <= 0:
                break

    llm_messages = [
        {"role": "system", "content": GENERAL_SYSTEM},
        *messages[-10:],
    ]

    if data_context:
        llm_messages.append(
            {
                "role": "system",
                "content": f"Previous SAP query results:\n{data_context}",
            }
        )

    llm_messages.append({"role": "user", "content": user_msg})

    token_queue = state.get("_ws_token_queue")

    raw = await _streaming_llm_call(llm_messages, token_queue, _LLM_FALLBACK_GENERAL, temperature=0.4)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"answer": raw, "chart": None}

    answer = parsed.get("answer", "")
    chart = parsed.get("chart")

    updated = [*messages, {"role": "assistant", "content": answer}]

    return {
        "final_response": answer,
        "chart_data": chart,
        "messages": updated,
    }

