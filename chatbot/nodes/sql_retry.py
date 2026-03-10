"""LangGraph node — SQL self-correction for query_database errors.

When `execute` produces a SQL error for `query_database`, this node feeds
the original SQL + error message back to the LLM and asks it to fix the
query.  The corrected SQL is written back into `validated_params` so
`execute` can re-run it.

Also handles zero-result recovery: when a query returns 0 rows and the
user asked a specific question, the LLM is asked to broaden the query
(e.g. remove overly restrictive filters, widen date ranges).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from chatbot.config import settings
from chatbot.llm_client import get_llm_client
from chatbot.state import ChatState

logger = logging.getLogger(__name__)

MAX_SQL_RETRIES = 2  # exported — also used by graph.py
_MAX_RETRIES = MAX_SQL_RETRIES

_MAX_ERROR_MSG_CHARS = 500
_MAX_SQL_CHARS = 2000

# Safety: reject LLM-corrected SQL containing DML/DDL
_DANGEROUS_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_MULTI_STMT_RE = re.compile(r";\s*\S")  # semicolon followed by more SQL


def _validate_corrected_sql(sql: str) -> str | None:
    """Return the SQL if safe for re-execution, else None."""
    if _DANGEROUS_SQL_RE.search(sql):
        logger.warning("LLM-corrected SQL contains DML/DDL — rejecting: %s", sql[:200])
        return None
    if _MULTI_STMT_RE.search(sql):
        logger.warning("LLM-corrected SQL contains multiple statements — rejecting: %s", sql[:200])
        return None
    if not sql.upper().strip().startswith("SELECT"):
        logger.warning("LLM-corrected SQL does not start with SELECT — rejecting: %s", sql[:200])
        return None
    return sql

SQL_FIX_PROMPT = """\
You are an expert PostgreSQL query debugger for the MUNTQ analytics platform.

The following SQL query was executed against the database and FAILED.
Your job: fix the SQL so it succeeds.

## Database schema reminder
- sales_order_snapshots: id, tenant_id, sales_order (text), payload (JSONB), \
last_changed_at (timestamp), created_at, updated_at
- billing_snapshots: id, tenant_id, billing_document (text), payload (JSONB), \
billing_date (timestamp), created_at, updated_at
- Materialized views: sales_daily(period, total_sales), \
sales_monthly(period, total_sales), sales_quarterly(period, total_sales), \
sales_yearly(year, total_sales), sales_daily_cluster(period, \
sales_order_type, sold_to_party, purchase_order_by_customer, \
sales_organization, distribution_channel, sales_group, total_sales, order_count)
- ALL data fields inside JSONB use payload->>'FieldName' syntax
- Cast numerics: (payload->>'TotalNetAmount')::numeric
- Do NOT include tenant_id filter (auto-injected)
- SAP dates in payload are in /Date(ms)/ format — NOT SQL-comparable. \
Use last_changed_at or billing_date columns for date filtering instead.
- Today's date: {today}

## Failed query
```sql
{original_sql}
```

## Error message
{error_message}

## User's original question
{user_question}

## Rules
1. Fix ONLY the SQL error — do not change the query intent.
2. If a column doesn't exist, check the schema above and use the correct name.
3. For JSONB fields, ensure proper casting and quoting.
4. Return ONLY valid JSON: {{"fixed_sql": "<the corrected SQL>", "explanation": "<what you fixed>"}}
"""

ZERO_RESULT_PROMPT = """\
You are an expert PostgreSQL query optimizer for the MUNTQ analytics platform.

The following SQL query executed successfully but returned 0 rows.
The user is expecting data.  Your job: broaden the query to find matching data.

## Database schema reminder
- sales_order_snapshots payload keys: SalesOrder, SalesOrderType, SoldToParty, \
SalesOrganization, DistributionChannel, TotalNetAmount, TransactionCurrency, \
OverallSDProcessStatus, OverallTotalDeliveryStatus, CreationDate, SalesGroup
- billing_snapshots payload keys: BillingDocument, BillingDocumentType, \
SoldToParty, SalesOrganization, TotalNetAmount, TransactionCurrency, \
BillingDocumentDate, BillingDocumentIsCancelled
- Date filtering: use last_changed_at / billing_date columns, NOT payload dates
- Today's date: {today}

## Query that returned 0 rows
```sql
{original_sql}
```

## User's original question
{user_question}

## Recovery strategies (pick one or more)
1. If date range is too narrow, widen it (e.g. current year → all time)
2. If filtering on a specific value that may not exist, remove that filter
3. If using exact match, try ILIKE for partial matching
4. If querying wrong table, switch to the correct one
5. If the query looks correct and data simply doesn't exist, return the \
original SQL unchanged

## Rules
- Return ONLY valid JSON: {{"fixed_sql": "<broadened SQL or original if no fix>", \
"explanation": "<what you changed or 'no change — data does not exist'>"}}
- Do NOT change what columns are selected — only relax WHERE filters
"""


async def sql_retry(state: ChatState) -> dict[str, Any]:
    """LangGraph node — attempt to fix a failed or empty SQL query."""
    tool_name = state.get("tool_name", "")
    execution_error = state.get("execution_error", "")
    sap_response = state.get("sap_response", {})
    validated_params = dict(state.get("validated_params", {}))
    retry_count = state.get("retry_count", 0)
    user_msg = state.get("user_message", "")

    # Only applies to query_database
    if tool_name != "query_database":
        return {}

    # Don't retry more than _MAX_RETRIES times
    if retry_count >= _MAX_RETRIES:
        logger.info("SQL retry limit reached (%d), proceeding to final_answer", retry_count)
        return {}

    original_sql = validated_params.get("sql", "")
    today = date.today().isoformat()

    # Determine if this is an error fix or a zero-result broadening
    is_error = bool(execution_error and not sap_response.get("success", False))
    is_empty = (
        sap_response.get("success", False)
        and sap_response.get("count", 0) == 0
        and not execution_error
    )

    if not is_error and not is_empty:
        # Success with data — nothing to fix
        return {}

    # Truncate inputs to prevent prompt blowout
    truncated_sql = original_sql[:_MAX_SQL_CHARS]
    if len(original_sql) > _MAX_SQL_CHARS:
        truncated_sql += f"\n... (truncated, total {len(original_sql)} chars)"
    truncated_error = execution_error[:_MAX_ERROR_MSG_CHARS]
    if len(execution_error) > _MAX_ERROR_MSG_CHARS:
        truncated_error += f"... (truncated, total {len(execution_error)} chars)"
    truncated_msg = user_msg[:500]

    if is_error:
        prompt = SQL_FIX_PROMPT.format(
            today=today,
            original_sql=truncated_sql,
            error_message=truncated_error,
            user_question=truncated_msg,
        )
        tag = "sql-fix"
    else:
        prompt = ZERO_RESULT_PROMPT.format(
            today=today,
            original_sql=truncated_sql,
            user_question=truncated_msg,
        )
        tag = "sql-broaden"

    logger.info("SQL %s attempt %d for: %s", tag, retry_count + 1, original_sql[:200])

    client = get_llm_client()
    try:
        resp = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            temperature=0.0,
            messages=[
                {"role": "system", "content": prompt},
            ],
            response_format={"type": "json_object"},
            timeout=20,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning("SQL %s LLM call failed: %s", tag, exc)
        return {"retry_count": retry_count + 1}

    fixed_sql = parsed.get("fixed_sql", "").strip()
    explanation = parsed.get("explanation", "")
    logger.info("SQL %s result: %s | explanation: %s", tag, fixed_sql[:200], explanation)

    if not fixed_sql or fixed_sql == original_sql:
        # LLM couldn't fix it or returned the same query — don't loop
        logger.info("SQL %s returned unchanged query, proceeding", tag)
        return {"retry_count": retry_count + 1}

    # Validate corrected SQL before re-execution (defense-in-depth)
    safe_sql = _validate_corrected_sql(fixed_sql)
    if safe_sql is None:
        logger.info("SQL correction rejected as unsafe, proceeding to final_answer")
        return {"retry_count": retry_count + 1}

    # Write the fixed SQL back into validated_params so execute re-runs it
    validated_params["sql"] = safe_sql
    return {
        "validated_params": validated_params,
        "original_sql": original_sql if retry_count == 0 else state.get("original_sql", original_sql),
        "retry_count": retry_count + 1,
        # Clear the error so execute runs fresh
        "execution_error": "",
        "sap_response": {},
    }
