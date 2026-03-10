"""SQL query validation, tenant isolation, and row-limit enforcement.

Security layers:
1. Regex pre-scan — reject obviously dangerous keywords.
2. sqlglot AST parse — structural validation (SELECT only, whitelisted tables).
3. Tenant injection — parameterised WHERE tenant_id = $1.
4. Row limit — enforce LIMIT at SQL level.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


# ── Regex guards ──────────────────────────────────────────────────────────────

_FORBIDDEN_KW = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|"
    r"GRANT|REVOKE|COPY|EXECUTE|CALL|SET\s+ROLE|"
    r"COMMIT|ROLLBACK|BEGIN|SAVEPOINT|"
    r"LOCK|VACUUM|REINDEX|CLUSTER|"
    r"NOTIFY|LISTEN|LOAD|SECURITY|"
    r"pg_read_file|pg_write_file|pg_ls_dir|"
    r"pg_terminate_backend|pg_cancel_backend|"
    r"lo_import|lo_export"
    r")\b",
    re.IGNORECASE,
)

_SELECT_START = re.compile(r"^\s*SELECT\b", re.IGNORECASE)


class QueryValidationError(Exception):
    """Raised when a SQL query fails safety checks."""


# ── Internal helpers ─────────────────────────────────────────────────────────

def _collect_selects(node: exp.Expression) -> list[exp.Select]:
    """Collect all SELECT nodes from a parsed expression (handles UNION)."""
    if isinstance(node, exp.Select):
        return [node]
    if isinstance(node, exp.Union):
        left = _collect_selects(node.left)
        right = _collect_selects(node.right)
        return left + right
    return []


def _validate_tables(node: exp.Expression, all_allowed: set[str]) -> None:
    """Validate that all table references are in the allowed set."""
    for table_node in node.find_all(exp.Table):
        name = table_node.name.lower()
        if name.startswith("pg_") or name.startswith("information_schema"):
            raise QueryValidationError(
                f"System table access is not allowed: {name}"
            )
        if name not in all_allowed:
            raise QueryValidationError(
                f"Table '{name}' is not in the allowed list. "
                f"Allowed: {sorted(all_allowed)}"
            )


# ── Public API ────────────────────────────────────────────────────────────────

def validate_query(
    sql: str,
    allowed_tables: list[str],
    allowed_views: list[str],
) -> str:
    """Validate that *sql* is a safe, read-only SELECT on whitelisted objects.

    Supports single SELECT and UNION / UNION ALL queries.
    Returns the cleaned SQL string (trailing semicolons stripped).
    Raises ``QueryValidationError`` on any violation.
    """
    cleaned = sql.strip().rstrip(";").strip()

    if not cleaned:
        raise QueryValidationError("Empty query")

    if len(cleaned) > 10_000:
        raise QueryValidationError("Query exceeds 10 000 character limit")

    # Must start with SELECT
    if not _SELECT_START.match(cleaned):
        raise QueryValidationError("Only SELECT statements are allowed")

    # Forbidden keywords
    match = _FORBIDDEN_KW.search(cleaned)
    if match:
        raise QueryValidationError(f"Forbidden SQL keyword: {match.group(0)}")

    # No multi-statement (embedded semicolons)
    if ";" in cleaned:
        raise QueryValidationError("Multi-statement queries are not allowed")

    # ── Structural validation via sqlglot ─────────────────────────────────
    try:
        parsed = sqlglot.parse_one(cleaned, dialect="postgres")
    except Exception as exc:
        raise QueryValidationError(f"SQL parse error: {exc}") from exc

    # Accept SELECT or UNION/UNION ALL of SELECTs
    if not isinstance(parsed, (exp.Select, exp.Union)):
        raise QueryValidationError("Only SELECT expressions are allowed")

    selects = _collect_selects(parsed)
    if not selects:
        raise QueryValidationError("No SELECT statements found in query")

    # Validate table references across all SELECT branches
    all_allowed = {t.lower() for t in allowed_tables + allowed_views}
    _validate_tables(parsed, all_allowed)

    return cleaned


def inject_tenant_filter(sql: str, tenant_id: str) -> tuple[str, list[str]]:
    """Inject ``WHERE tenant_id = $1`` into *sql*.

    For UNION queries, injects the filter into EVERY SELECT branch.
    Uses ``$1`` parameter binding to prevent SQL injection of the tenant_id
    value itself.  Returns ``(modified_sql, [tenant_id])``.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
        tenant_cond = sqlglot.parse_one("tenant_id = $1", dialect="postgres")

        # Inject tenant filter into every SELECT in the tree
        for select_node in _collect_selects(parsed):
            select_node.where(tenant_cond.copy(), append=True, copy=False)

        return parsed.sql(dialect="postgres"), [tenant_id]
    except Exception:
        # Fallback: simple text injection
        return _fallback_inject(sql), [tenant_id]


def apply_row_limit(sql: str, max_rows: int) -> str:
    """Ensure the query has a LIMIT clause capped at *max_rows*.

    For UNION queries, wraps the entire query in a subquery with LIMIT.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")

        if isinstance(parsed, exp.Union):
            # Wrap UNION in a subquery to apply LIMIT to the combined result
            wrapped = exp.Select().from_(
                parsed.subquery(alias="_union")
            ).select("*").limit(max_rows)
            return wrapped.sql(dialect="postgres")

        existing = parsed.find(exp.Limit)
        if existing:
            try:
                val = int(existing.expression.this)
                if val > max_rows:
                    existing.expression = exp.Literal.number(max_rows)
            except (ValueError, AttributeError):
                pass
        else:
            parsed = parsed.limit(max_rows)
        return parsed.sql(dialect="postgres")
    except Exception:
        if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
            return f"{sql} LIMIT {max_rows}"
        return sql


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fallback_inject(sql: str) -> str:
    """Naive text-based tenant injection when sqlglot fails."""
    upper = sql.upper()
    if " WHERE " in upper:
        return re.sub(r"(?i)\bWHERE\b", "WHERE tenant_id = $1 AND", sql, count=1)
    # Insert before ORDER BY / GROUP BY / HAVING / LIMIT or at end
    m = re.search(r"(?i)\b(ORDER\s+BY|GROUP\s+BY|HAVING|LIMIT|OFFSET)\b", sql)
    if m:
        pos = m.start()
        return f"{sql[:pos]}WHERE tenant_id = $1 {sql[pos:]}"
    return f"{sql} WHERE tenant_id = $1"
