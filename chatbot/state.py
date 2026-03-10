from __future__ import annotations

from typing import Any, TypedDict


class _ChatStateRequired(TypedDict):
    """Fields that must always be present when the graph is invoked."""
    request_id: str  # unique per /chat call, for log correlation
    tenant_id: str
    user_id: str
    session_id: str
    user_message: str
    messages: list[dict[str, str]]  # [{role, content}, ...]


class ChatState(_ChatStateRequired, total=False):
    """All other fields are optional (set by graph nodes during execution)."""

    tool_name: str  # selected SAP tool (or "none")
    tool_params: dict[str, Any]  # extracted parameters
    tool_expands: list[str]  # navigation properties to expand
    routing_reasoning: str

    missing_params: list[str]  # params still needed
    needs_user_input: bool  # True → return and wait for user
    user_input_prompt: str

    validation_errors: list[dict[str, str]]  # [{param, message}, ...]
    validated_params: dict[str, Any]

    chart_hint: dict[str, Any] | None     # LLM-decided aggregation plan from parse_route

    # Multi-tool support (when LLM detects cross-entity query)
    tools: list[dict[str, Any]]           # [{tool_name, parameters, chart_hint}, ...] (1-3)
    sap_responses: list[dict[str, Any]]   # one SAP result per tool in tools[]

    sap_response: dict[str, Any]          # normalised SAP result (single-tool / first tool)
    execution_error: str
    last_sap_data: list[dict[str, Any]]   # previous successful SAP rows from session
    sap_data_history: list[list[dict[str, Any]]]  # last N SAP query snapshots (max 4)

    # Error recovery / self-correction
    retry_count: int                      # how many times execute has been retried (max 2)
    original_sql: str                     # the SQL that failed (for self-correction context)

    final_response: str
    chart_data: dict[str, Any] | None
    export_available: bool  # True if multi-year → Excel export

    # WebSocket streaming support
    stream_ws: bool                       # True when called via WebSocket
    _ws_token_queue: Any                  # asyncio.Queue for streaming LLM tokens to WS handler
