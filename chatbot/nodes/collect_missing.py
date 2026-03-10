from __future__ import annotations

import json
import logging
from typing import Any

from chatbot.config import settings
from chatbot.llm_client import get_llm_client
from chatbot.state import ChatState
from chatbot.tools.registry import get_tool

logger = logging.getLogger(__name__)


def _check_missing_for_tool(tool_name: str, tool_params: dict) -> list[str]:
    """Return list of missing required param names for a single tool."""
    tool = get_tool(tool_name)
    if tool is None:
        return []
    return [p.name for p in tool.parameters if p.required and p.name not in tool_params]


async def _build_clarify_prompt(user_message: str, reasoning: str) -> str:
    """Generate a user-facing clarification question from internal reasoning.

    Instead of exposing the raw LLM routing reasoning (e.g. "ambiguous between
    PO and SO"), this makes a quick LLM call to produce a friendly, specific
    question with options the user can choose from.
    Falls back to a generic prompt if the LLM call fails.
    """
    fallback = (
        "Could you be more specific? For example, are you looking for "
        "sales orders, purchase orders, invoices, materials, deliveries, "
        "billing documents, or something else?"
    )
    try:
        client = get_llm_client()
        resp = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            temperature=0.3,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are MUNTQ, an SAP analytics assistant. The user asked "
                        "an ambiguous question and the system could not determine "
                        "which data source to query. Generate a SHORT, friendly "
                        "follow-up question (2-3 sentences max) that helps the user "
                        "clarify what they need. Offer 2-4 specific options based on "
                        "the context. Return ONLY the question text, no JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"User's question: {user_message}\n"
                        f"Internal routing note: {reasoning}"
                    ),
                },
            ],
            timeout=10,
        )
        result = (resp.choices[0].message.content or "").strip()
        return result if result else fallback
    except Exception as exc:
        logger.warning("Clarify prompt LLM call failed: %s", exc)
        return fallback


async def collect_missing(state: ChatState) -> dict[str, Any]:
    """LangGraph node – only block if genuinely required params are missing."""
    tool_name = state.get("tool_name", "none")
    tool_params = state.get("tool_params", {})
    reasoning = state.get("routing_reasoning", "")
    tools_list = state.get("tools", [])

    if tool_name == "clarify":
        user_msg = state.get("user_message", "")
        prompt = await _build_clarify_prompt(user_msg, reasoning)
        return {
            "missing_params": [],
            "needs_user_input": True,
            "user_input_prompt": prompt,
        }

    # --- Multi-tool: check required params for ALL tools ---
    if tools_list and len(tools_list) > 1:
        all_missing: list[str] = []
        parts: list[str] = []
        for t_entry in tools_list:
            t_name = t_entry["tool_name"]
            missing = _check_missing_for_tool(t_name, t_entry.get("parameters", {}))
            if missing:
                tool = get_tool(t_name)
                for name in missing:
                    all_missing.append(f"{t_name}.{name}")
                    param_def = next((p for p in tool.parameters if p.name == name), None) if tool else None
                    desc = param_def.description if param_def else name
                    hint = f" (format: {param_def.format_hint})" if param_def and param_def.format_hint else ""
                    parts.append(f"• **{t_name} → {name}**: {desc}{hint}")
        if all_missing:
            prompt = (
                "I need a bit more information for this multi-entity query. "
                "Please provide:\n\n" + "\n".join(parts)
            )
            logger.info("Multi-tool missing params: %s", all_missing)
            return {
                "missing_params": all_missing,
                "needs_user_input": True,
                "user_input_prompt": prompt,
            }
        logger.info("Multi-tool params OK for %s", [t["tool_name"] for t in tools_list])
        return {"missing_params": [], "needs_user_input": False, "user_input_prompt": ""}

    # --- Single-tool (default path) ---
    tool = get_tool(tool_name)
    if tool is None:
        if tool_name not in ("none", ""):
            logger.warning("Unknown tool '%s' — skipping param check", tool_name)
        return {
            "missing_params": [],
            "needs_user_input": False,
            "user_input_prompt": "",
        }

    missing: list[str] = []
    for p in tool.parameters:
        if p.required and p.name not in tool_params:
            missing.append(p.name)

    if missing:
        parts_single: list[str] = []
        for name in missing:
            param_def = next((p for p in tool.parameters if p.name == name), None)
            desc = param_def.description if param_def else name
            hint = f" (format: {param_def.format_hint})" if param_def and param_def.format_hint else ""
            parts_single.append(f"• **{name}**: {desc}{hint}")

        prompt = (
            "I need a bit more information to run this query. "
            "Please provide the following:\n\n" + "\n".join(parts_single)
        )
        logger.info("Missing required params for %s: %s", tool_name, missing)
        return {
            "missing_params": missing,
            "needs_user_input": True,
            "user_input_prompt": prompt,
        }

    logger.info("Params OK for %s: %s", tool_name, tool_params)
    return {
        "missing_params": [],
        "needs_user_input": False,
        "user_input_prompt": "",
    }
