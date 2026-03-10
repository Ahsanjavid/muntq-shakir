from __future__ import annotations
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"  # ISO-8601  YYYY-MM-DD
    BOOLEAN = "boolean"


class ToolParameter(BaseModel):
    """Single parameter that can be extracted from the user's question."""

    name: str
    type: ParamType
    required: bool = True
    description: str = ""
    default: Any = None
    enum: list[str] | None = None  # allowed values (optional)
    format_hint: str | None = None  # e.g. "YYYY-MM-DD"
    max_value: float | None = None
    min_value: float | None = None


class SAPTool(BaseModel):
    """
    Declarative definition of one SAP OData function the agent can invoke.
    The LLM sees *name* + *description* + *parameters* for routing.
    """

    name: str  # e.g. "get_sales_orders"
    description: str
    parameters: list[ToolParameter]
    sap_entity_set: str  # OData entity-set path
    sap_api_path: str  # full OData service path
    response_fields: list[str] = []  # key fields to extract from response

    # NEW: Expand support for nested data
    available_expands: dict[str, str] = {}  # expand_name: description for LLM
    default_expands: list[str] = []  # Always fetch these expands
    odata_expand: str | None = (
        None  # Legacy: single static expand (DEPRECATED - use available_expands)
    )

    top_default: int = 500


class ToolCall(BaseModel):
    """Structured output the LLM produces after parsing the user question."""

    tool_name: str = Field(..., description="Name of the SAP tool to invoke")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Extracted parameters from the user's question",
    )
    expands: list[str] = Field(
        default_factory=list,
        description="Additional navigation properties to expand (beyond defaults)",
    )
    reasoning: str = Field(
        "",
        description="Brief explanation of why this tool was selected",
    )


class ParamValidationError(BaseModel):
    param: str
    message: str


class ChatRequest(BaseModel):
    tenant_id: str
    user_id: str
    session_id: Optional[str] = None
    message: str = Field(..., max_length=4000)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    needs_input: bool = False  # True when agent asks follow-up
    input_prompt: str | None = None  # what to ask the user
    chart_data: dict[str, Any] | None = None
    raw_sap: dict[str, Any] | None = None  # optional debug payload
    export_url: str | None = None  # Excel download URL when available
