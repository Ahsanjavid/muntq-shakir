from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from chatbot.models import ParamType, SAPTool
from chatbot.state import ChatState
from chatbot.tools.registry import get_tool

logger = logging.getLogger(__name__)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str) -> date | None:
    if _ISO_DATE.match(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _validate_tool_params(
    tool: SAPTool,
    raw_params: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any], bool]:
    """
    Validate and coerce params for a single tool.
    Returns (errors, validated_params, export_available).
    """
    errors: list[dict[str, str]] = []
    validated: dict[str, Any] = {}

    for p in tool.parameters:
        val = raw_params.get(p.name)

        if val is None:
            if p.required and p.default is None:
                errors.append({"param": p.name, "message": "This field is required."})
            if p.default is not None:
                validated[p.name] = p.default
            continue

        if p.required and isinstance(val, str) and not val.strip():
            errors.append({"param": p.name, "message": "This field is required."})
            continue

        if p.type == ParamType.DATE:
            d = _parse_date(str(val))
            if d is None:
                errors.append({
                    "param": p.name,
                    "message": f"'{val}' is not a valid date. Expected YYYY-MM-DD.",
                })
            else:
                validated[p.name] = d.isoformat()

        elif p.type == ParamType.INTEGER:
            try:
                int_val = int(val)
                if int_val == 0 and p.default is not None:
                    validated[p.name] = p.default
                elif p.min_value is not None and int_val < p.min_value:
                    errors.append({"param": p.name, "message": f"Must be ≥ {p.min_value}"})
                elif p.max_value is not None and int_val > p.max_value:
                    errors.append({"param": p.name, "message": f"Must be ≤ {p.max_value}"})
                else:
                    validated[p.name] = int_val
            except (ValueError, TypeError):
                errors.append({"param": p.name, "message": f"'{val}' is not a valid integer."})

        elif p.type == ParamType.NUMBER:
            try:
                validated[p.name] = float(val)
            except (ValueError, TypeError):
                errors.append({"param": p.name, "message": f"'{val}' is not a valid number."})

        elif p.type == ParamType.BOOLEAN:
            validated[p.name] = str(val).lower() in ("true", "1", "yes")

        else:
            str_val = str(val)
            if p.enum and str_val not in p.enum:
                errors.append({
                    "param": p.name,
                    "message": f"'{str_val}' not in allowed values {p.enum}.",
                })
            else:
                validated[p.name] = str_val

    # Date defaults and range checks
    d_from = validated.get("date_from")
    d_to = validated.get("date_to")
    today = date.today()
    current_year = today.year

    # Auto-inject date defaults for transactional tools only.
    # query_database uses SQL and does not take date_from/date_to params directly.
    _SKIP_DATE_DEFAULTS = {
        "query_database",
        "get_sales_aggregates",
        # DB snapshot tools should not force current-year filters.
        "get_sales_orders_db",
        "get_billing_db",
    }
    date_param_names = {p.name for p in tool.parameters if p.type == ParamType.DATE}
    if tool.name not in _SKIP_DATE_DEFAULTS:
        if "date_from" in date_param_names and not d_from:
            d_from = f"{current_year}-01-01"
            validated["date_from"] = d_from
        if "date_to" in date_param_names and not d_to:
            d_to = today.isoformat()
            validated["date_to"] = d_to

    if d_from and d_to:
        try:
            df = datetime.strptime(d_from, "%Y-%m-%d").date()
            dt = datetime.strptime(d_to, "%Y-%m-%d").date()
            if df > dt:
                errors.append({
                    "param": "date_from",
                    "message": "date_from must be before date_to.",
                })
            elif (dt - df) > timedelta(days=366 * 5):
                errors.append({
                    "param": "date_from",
                    "message": "Date range exceeds 5 years. Maximum is 5 years.",
                })
        except ValueError:
            pass

    export_available = False
    if d_from and d_to:
        try:
            df = datetime.strptime(d_from, "%Y-%m-%d").date()
            dt = datetime.strptime(d_to, "%Y-%m-%d").date()
            if (dt - df) > timedelta(days=365):
                export_available = True
        except ValueError:
            pass

    return errors, validated, export_available


async def validate(state: ChatState) -> dict[str, Any]:
    """LangGraph node – validate and coerce parameters."""
    tool_name = state.get("tool_name", "none")
    tools_list = state.get("tools", [])

    # --- Multi-tool validation ---
    if tools_list and len(tools_list) > 1:
        all_errors: list[dict[str, str]] = []
        validated_tools: list[dict[str, Any]] = []
        any_export = False

        for t_entry in tools_list:
            t_name = t_entry["tool_name"]
            tool = get_tool(t_name)
            if tool is None:
                validated_tools.append(t_entry)
                continue
            errors, validated, export_avail = _validate_tool_params(
                tool, dict(t_entry.get("parameters", {})),
            )
            if errors:
                # Prefix errors with tool name for clarity
                for e in errors:
                    e["param"] = f"{t_name}.{e['param']}"
                all_errors.extend(errors)
            any_export = any_export or export_avail
            validated_tools.append({
                **t_entry,
                "parameters": validated,
            })

        logger.info("Multi-tool validation: errors=%s export=%s", all_errors, any_export)
        return {
            "validation_errors": all_errors,
            "validated_params": validated_tools[0].get("parameters", {}),
            "tools": validated_tools,
            "export_available": any_export,
        }

    # --- Single-tool validation (default path) ---
    raw_params = dict(state.get("tool_params", {}))
    tool = get_tool(tool_name)
    if tool is None:
        return {"validation_errors": [], "validated_params": raw_params}

    errors, validated, export_available = _validate_tool_params(tool, raw_params)
    logger.info("Validation for %s: errors=%s export=%s", tool_name, errors, export_available)

    return {
        "validation_errors": errors,
        "validated_params": validated,
        "export_available": export_available,
    }
