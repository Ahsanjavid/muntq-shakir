from __future__ import annotations

from langgraph.graph import END, StateGraph

from chatbot.state import ChatState

from chatbot.nodes.parse_route import parse_and_route
from chatbot.nodes.collect_missing import collect_missing
from chatbot.nodes.validate import validate
from chatbot.nodes.execute import execute
from chatbot.nodes.sql_retry import MAX_SQL_RETRIES, sql_retry
from chatbot.nodes.final_answer import final_answer


def after_parse(state: ChatState) -> str:
    """If no tool selected → skip straight to final_answer (general chat).
       If clarify → go to collect_missing (which handles clarification prompts)."""
    tool_name = state.get("tool_name", "none")
    if tool_name == "none":
        return "final_answer"
    return "collect_missing"


def after_collect(state: ChatState) -> str:
    """If we still need user input → stop the graph and return a prompt."""
    if state.get("needs_user_input"):
        return END
    return "validate"


def after_validate(state: ChatState) -> str:

    errors = state.get("validation_errors", [])
    if errors:
        return END
    return "execute"


def after_execute(state: ChatState) -> str:
    """After execute, decide whether to attempt SQL self-correction.

    Routes to sql_retry when:
    - The tool is query_database AND
    - There's a SQL error OR the result is empty (0 rows) AND
    - We haven't exceeded the retry limit
    """
    tool_name = state.get("tool_name", "")
    execution_error = state.get("execution_error", "")
    sap_response = state.get("sap_response", {})
    retry_count = state.get("retry_count", 0)

    if tool_name != "query_database" or retry_count >= MAX_SQL_RETRIES:
        return "final_answer"

    is_error = bool(execution_error and not sap_response.get("success", False))
    is_empty = (
        sap_response.get("success", False)
        and sap_response.get("count", 0) == 0
        and not execution_error
    )

    if is_error or is_empty:
        return "sql_retry"

    return "final_answer"


def after_sql_retry(state: ChatState) -> str:
    """After sql_retry, check if there's a corrected SQL to re-execute."""
    validated_params = state.get("validated_params", {})
    original_sql = state.get("original_sql", "")
    current_sql = validated_params.get("sql", "")

    # If the SQL was actually changed, re-execute
    if current_sql and current_sql != original_sql and state.get("retry_count", 0) <= MAX_SQL_RETRIES:
        return "execute"

    # No fix available — proceed to final_answer with whatever we have
    return "final_answer"


def build_graph() -> StateGraph:
    g = StateGraph(ChatState)

    # -- Nodes --
    g.add_node("parse_and_route", parse_and_route)
    g.add_node("collect_missing", collect_missing)
    g.add_node("validate", validate)
    g.add_node("execute", execute)
    g.add_node("sql_retry", sql_retry)
    g.add_node("final_answer", final_answer)

    # -- Edges --
    g.set_entry_point("parse_and_route")

    g.add_conditional_edges("parse_and_route", after_parse, {
        "collect_missing": "collect_missing",
        "final_answer": "final_answer",
    })

    g.add_conditional_edges("collect_missing", after_collect, {
        "validate": "validate",
        END: END,
    })

    g.add_conditional_edges("validate", after_validate, {
        "execute": "execute",
        END: END,
    })

    g.add_conditional_edges("execute", after_execute, {
        "sql_retry": "sql_retry",
        "final_answer": "final_answer",
    })

    g.add_conditional_edges("sql_retry", after_sql_retry, {
        "execute": "execute",
        "final_answer": "final_answer",
    })

    g.add_edge("final_answer", END)

    return g



agent_graph = build_graph().compile()
