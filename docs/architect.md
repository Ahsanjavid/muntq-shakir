# QBot Architecture and Workflow Reference (Current Codebase)

Last verified: 2026-03-09  
Repository: `qbot_agentic_ai_chatbot`  
Primary runtime: Python 3.11, FastAPI, LangGraph, Redis, PostgreSQL, SAP OData, MCP SSE

## 1. Purpose and Scope

This document describes the architecture and implemented workflows currently present in code, including:

- Chatbot API request lifecycle and LangGraph branches.
- Dashboard API endpoint and fallback behavior.
- SAP ingestion and aggregate worker pipelines.
- MCP SQL server safety and execution pipeline.
- Data stores, service boundaries, and deployment topology.

This is an implementation-aligned document, not a target-state design document.

## 2. System Overview

QBot is a multi-service analytics platform:

1. Chatbot API (`app.py` + `chatbot/`) for conversational querying.
2. Dashboard API (`dashboard/main.py`) for sales analytics endpoints and widget management.
3. Background workers (`dashboard/ingest_worker.py`, `dashboard/aggregate_worker.py`) for SAP ETL and aggregate refresh.
4. MCP PostgreSQL service (`mcp_server/server.py`) for tenant-scoped, read-only SQL over SSE.

Primary data access tiers used by the chatbot:

1. Curated aggregates through dashboard endpoints (`get_sales_aggregates`).
2. Snapshot/materialized-view SQL through MCP (`query_database`, `get_sales_orders_db`, `get_billing_db`).
3. Live SAP OData APIs for transactional/master data not covered by snapshots.

## 3. High-Level Runtime Topology

```text
Client (frontend or API consumer)
  -> Chatbot FastAPI (:5000)
     -> LangGraph (parse -> collect -> validate -> execute -> sql_retry -> final)
     -> Redis (session state, pending context, rate-limit)
     -> Dashboard API (:8001) for aggregate reports
     -> MCP server (:3001 exposed, :8000 internal SSE) for SQL
     -> SAP OData endpoints for live data

Dashboard FastAPI (:8001)
  -> PostgreSQL (:5432) snapshots, curated_aggregates, views, widgets

Ingest worker (hourly schedule)
  -> SAP OData -> sales_order_snapshots + billing_snapshots + job_runs

Aggregate worker (poll every 5 min)
  -> snapshots -> pandas aggregation -> curated_aggregates + view refresh + job_runs

MCP server
  -> PostgreSQL read-only pool
  -> SQL validation + tenant injection + row cap + audit log
```

## 4. Repository Topology

```text
app.py
chatbot/
  config.py
  models.py
  state.py
  graph.py
  session.py
  llm_client.py
  sap_client.py
  aggregate_client.py
  mcp_client.py
  nodes/
    parse_route.py
    collect_missing.py
    validate.py
    execute.py
    sql_retry.py
    final_answer.py
  tools/
    definitions.py
    registry.py
dashboard/
  main.py
  settings.py
  database.py
  models.py
  sap_extractor.py
  aggregator.py
  scheduler.py
  ingest_worker.py
  aggregate_worker.py
  worker.py
  views.py
mcp_server/
  server.py
  config.py
  query_validator.py
  audit.py
  bootstrap.py
  sql/
frontend/
docs/
  aggregates.md
  architect.md
```

## 5. Data Stores and Core Schema

### 5.1 Redis

Used by chatbot service:

- Session key: `chat:session:{uuid}`
- Rate-limit key: `rate:{tenant_id}`

Session payload includes:

- `tenant_id`, `user_id`
- `messages`
- `pending_tool`, `pending_params`
- `extra` (`last_sap_data`, `sap_data_history`)

### 5.2 PostgreSQL Tables

Defined in `dashboard/models.py` and MCP bootstrap logic:

- `sales_order_snapshots`
- `billing_snapshots`
- `curated_aggregates`
- `job_runs`
- `dashboard_widgets`
- `mcp_query_log`

### 5.3 Materialized Views

Created in `dashboard/views.py`:

- `sales_hourly`
- `sales_daily`
- `sales_monthly`
- `sales_quarterly`
- `sales_yearly`
- `sales_daily_cluster`

Refresh mechanism:

- SQL function `refresh_sales_views()` called after aggregate updates.
- Attempts concurrent refresh per view, falls back to non-concurrent.

## 6. Chatbot Service Architecture

### 6.1 API Routes in `app.py`

- `GET /`: serve chatbot frontend.
- `GET /ui-config`: return dashboard base URL.
- `POST /session/start`: create session.
- `POST /chat`: execute graph and return response.
- `GET /session/{session_id}`: inspect session.
- `DELETE /session/{session_id}`: delete session.
- `GET /tools`: tool catalog.
- `GET /export/{session_id}`: export latest result.
- `GET /health`: liveness.
- `GET /ready`: readiness (Redis ping check).

### 6.2 Request Guards and Runtime Controls

- Optional API key check through `verify_api_key` when `API_SECRET_KEY` is set.
- Per-tenant rate limit: 30 requests per minute in Redis sorted set.
- Per-session lock to prevent simultaneous graph execution for same session.
- Graph timeout: 120 seconds.
- Session lock cache pruned when more than 5000 locks.

### 6.3 Chat Request State Initialization

`/chat` creates state with:

- Correlation identifiers: `request_id`, `tenant_id`, `user_id`, `session_id`
- Input: `user_message`
- Conversation history: `messages`
- Resume context: `tool_name`, `tool_params` from `pending_tool` and `pending_params`
- Previous data context: `last_sap_data`, `sap_data_history`

### 6.4 ChatResponse Contract

Response includes:

- `session_id`
- `reply`
- `needs_input`
- `input_prompt`
- `chart_data`
- `raw_sap` (omitted for needs-input or validation-error paths)
- `export_url`

## 7. LangGraph Node Architecture

Graph defined in `chatbot/graph.py`:

1. `parse_and_route`
2. `collect_missing`
3. `validate`
4. `execute`
5. `sql_retry` (conditional for `query_database`)
6. `final_answer`

Conditional edges:

- `parse_and_route`: `none` -> `final_answer`; otherwise -> `collect_missing`.
- `collect_missing`: if `needs_user_input` -> END; else -> `validate`.
- `validate`: if errors -> END; else -> `execute`.
- `execute`: route to `sql_retry` only for `query_database` failure/empty and retry budget available.
- `sql_retry`: back to `execute` if SQL changed; else `final_answer`.
- `final_answer` -> END.

## 8. Chatbot Node Workflows (Detailed)

### 8.1 `parse_and_route` Workflow (`chatbot/nodes/parse_route.py`)

Two-stage LLM workflow:

1. Stage 1 prompt selects tool intent (`none`, `clarify`, single tool, multi-tool).
2. Stage 2 prompt extracts tool parameters for selected tool(s), optional expands, optional `chart_hint`.

Key logic:

- Uses truncated message history for token control.
- If pending tool exists, injects context note and merges pending params on output.
- Validates additional expands against tool `available_expands`.
- Supports multi-tool return with max 3 tool entries.

### 8.2 `collect_missing` Workflow (`chatbot/nodes/collect_missing.py`)

Branches:

- If `tool_name == clarify`: generates user-friendly clarification question via LLM, sets `needs_user_input=True`.
- Multi-tool mode: checks required params for each tool; prompts with prefixed `tool.param` list.
- Single-tool mode: prompts for missing required fields with descriptions and format hints.
- No missing required fields: proceeds.

### 8.3 `validate` Workflow (`chatbot/nodes/validate.py`)

Per-parameter type validation:

- Date format: strict ISO `YYYY-MM-DD`
- Integer/number coercion, bounds checking
- Enum checks for string params
- Boolean normalization from truthy string values

Date-default behavior:

- Auto-defaults (`date_from` Jan 1 current year, `date_to` today) for transactional SAP tools with date params.
- Skips auto-defaults for:
  - `query_database`
  - `get_sales_aggregates`
  - `get_sales_orders_db`
  - `get_billing_db`

Range checks:

- `date_from <= date_to`
- Max allowed span: 5 years

`export_available` flag:

- True if validated date range exceeds 365 days.

### 8.4 `execute` Workflow (`chatbot/nodes/execute.py`)

Tool routing:

- `get_sales_aggregates` -> dashboard aggregate client.
- `get_sales_orders_db` / `get_billing_db` -> internally generated SQL -> MCP query.
- `query_database` -> LLM-provided SQL -> MCP query.
- SAP tools -> OData client (`call_sap`).

Multi-tool behavior:

- Parallel execution with timeout (90s).
- Response list stored in `sap_responses`.
- First response mirrored into `sap_response`.
- Errors are aggregated as a semicolon-separated `execution_error`.

DB snapshot SQL helper behavior:

- Input sanitization for filter literals.
- For `get_sales_orders_db` `sort_by=value` with no filters:
  - fetches larger recent batch sorted by date in DB,
  - re-sorts application-side by amount to avoid slow global JSON sort.

Customer name enrichment:

- For supported tools, resolves customer IDs to names by calling `get_business_partners`.
- Injects `_SoldToName`, `_ShipToName`, `_PayerName`.

### 8.5 `sql_retry` Workflow (`chatbot/nodes/sql_retry.py`)

Trigger conditions:

- Only for `tool_name == query_database`.
- Executes when SQL run failed OR returned zero rows.
- Retry limit: 2 attempts.

Modes:

- Error-fix mode: repairs invalid SQL.
- Zero-result mode: broadens filters to recover data.

Safety checks on corrected SQL:

- Must start with `SELECT`.
- Must be single-statement.
- Must not include DML/DDL keywords.

On accepted correction:

- Replaces `validated_params["sql"]`.
- Increments retry count.
- Clears execution error for re-execution.

### 8.6 `final_answer` Workflow (`chatbot/nodes/final_answer.py`)

Paths:

- `tool_name in ("none", "clarify")` -> `_general_answer`.
- Multi-tool with responses -> `_multi_tool_answer`.
- Single-tool -> tool-context answer path.

Behavior:

- Builds compact context from result rows + raw payload with char/row budgets.
- Generates structured JSON from LLM: `{"answer": "...", "chart": ...}`.
- Maintains `sap_data_history` with bounded snapshots for follow-up analysis.
- Adds export hint text when `export_available` is true.
- For backend execution error with no data, returns direct deterministic error text without summarization.

## 9. Chatbot Workflow Catalog

### WF-CH-01 Session Start

1. Client calls `POST /session/start`.
2. Service uses provided tenant/user or dev defaults.
3. Redis session key created with empty messages and pending state.
4. Session ID returned.

### WF-CH-02 Chat Happy Path (Single Tool)

1. `/chat` checks rate limit and session existence.
2. Graph runs parse -> collect -> validate -> execute -> final.
3. Session messages saved; pending state cleared.
4. Response returned with optional chart and export URL.

### WF-CH-03 Chat General Conversation

1. Parse selects `tool_name=none`.
2. Graph skips execute path.
3. `_general_answer` uses conversation + prior SAP snapshots for follow-up charting.
4. Response returned with optional chart.

### WF-CH-04 Clarification Path

1. Parse selects `tool_name=clarify`.
2. `collect_missing` asks a clarifying question.
3. Graph ends with `needs_user_input=True`.
4. Pending state is saved for continuation.

### WF-CH-05 Missing Required Parameter Path

1. Tool selected with missing required params.
2. `collect_missing` builds parameter prompt.
3. Graph ends with `needs_user_input=True`.
4. `pending_tool` and partial params saved.

### WF-CH-06 Resume Pending Query

1. User sends follow-up answer in same session.
2. State initializes from `pending_tool` and `pending_params`.
3. `parse_and_route` merges new params with pending ones.
4. Query proceeds through validate/execute/final.

### WF-CH-07 Validation Error Path

1. `validate` detects parameter issues.
2. Graph ends before execute.
3. API formats bullet list of errors.
4. Pending tool/params are preserved for correction retry.

### WF-CH-08 SQL Error Recovery Path

1. `query_database` fails in execute.
2. `sql_retry` requests corrected SQL from LLM.
3. Safe corrected SQL is re-executed.
4. Continues until success or retry budget exhausted.

### WF-CH-09 SQL Zero-Result Recovery Path

1. `query_database` returns zero rows.
2. `sql_retry` attempts query broadening.
3. Re-execute if SQL changed and safe.
4. Otherwise final answer reports no data.

### WF-CH-10 Multi-Tool Query Path

1. Parse returns `tools[]`.
2. Missing-param and validation checks run across tool set.
3. Execute runs all tools in parallel.
4. Final answer fuses datasets into single response.

### WF-CH-11 Chat Rate Limit Rejection

1. Tenant exceeds 30 RPM.
2. `/chat` returns HTTP 429.
3. Graph is not invoked.

### WF-CH-12 Session Double-Submit Rejection

1. Concurrent `/chat` request for same session detects held lock.
2. Returns HTTP 429 "request already in progress".
3. Existing request continues.

### WF-CH-13 Graph Timeout

1. Graph execution exceeds 120s.
2. `/chat` returns HTTP 504.
3. No successful result persisted.

### WF-CH-14 Chat Internal Error

1. Unhandled exception inside graph execution path.
2. `/chat` returns HTTP 500 with request reference ID.

### WF-CH-15 Export Path

1. Client calls `GET /export/{session_id}`.
2. Service loads `extra.last_sap_data` from session.
3. Nested arrays are flattened to tabular rows.
4. Returns XLSX (or CSV if `openpyxl` unavailable).

## 10. Tooling and Data Access Layer

### 10.1 Tool Catalog

Current tool list in registry:

1. `get_sales_aggregates`
2. `get_sales_orders_db`
3. `get_billing_db`
4. `query_database`
5. `get_sales_orders`
6. `get_purchase_orders`
7. `get_purchase_order_items`
8. `get_invoices`
9. `get_business_partners`
10. `get_materials`
11. `get_deliveries`
12. `get_journal_entries`

### 10.2 Aggregate Client Workflow (`chatbot/aggregate_client.py`)

1. Maps `report_type` to endpoint:
   - `today`, `mtd`, `qtd`, `trends`, `orders`, `period`.
2. Passes only supported query filters.
3. Calls dashboard API with pooled `httpx.AsyncClient`.
4. Retries transient failures (502/503/504 and connect/read/pool timeout).
5. Normalizes response:
   - `raw_payload`
   - flattened `data` rows for LLM context
   - `requested_date_from`, `requested_date_to`

### 10.3 MCP Client Workflow (`chatbot/mcp_client.py`)

1. Lazily establishes persistent SSE session to MCP server.
2. Uses reconnect lock to avoid connection storms.
3. Applies bounded concurrency via semaphore.
4. `call_mcp_query` behavior:
   - one retry on connection-level failure with session invalidation,
   - no retry on timeout (returns explicit timeout guidance).

### 10.4 SAP Client Workflow (`chatbot/sap_client.py`)

1. Maps tool params to OData field names.
2. Builds OData filter string from validated params.
3. Merges default expands and LLM-requested expands.
4. Applies tool-specific logic:
   - client-side material filter for sales orders (`to_Item`),
   - client-side description filter for materials,
   - over-fetch when client-side filtering is required.
5. Auto scales `$top` for wide date ranges if user did not set `top`.
6. Uses either basic auth or OAuth token flow.
7. Retries network/5xx failures.
8. Normalizes SAP `/Date(...)` strings.
9. Preserves expanded navigation data as nested arrays.

## 11. Dashboard API Architecture

### 11.1 Lifespan Workflow (`dashboard/main.py`)

Startup:

1. Configure logging.
2. Ensure DB tables exist.
3. Recreate materialized views.
4. Attempt MCP grant repair for `mcp_readonly`.
5. Optionally schedule startup sync job.
6. Optionally start internal scheduler.

Shutdown:

1. Stop scheduler if enabled.

### 11.2 Dashboard Routes

Health/UI:

- `GET /health`
- `GET /ready`
- `GET /`
- `GET /ui-config`

Sales analytics:

- `GET /sales/filters`
- `GET /sales/graph/daily`
- `GET /sales/graph/breakdown`
- `GET /sales/today`
- `GET /sales/mtd`
- `GET /sales/qtd`
- `GET /sales/period`
- `GET /sales/trends`
- `GET /sales/orders`
- `GET /sales/last-updated`

Widget APIs:

- `POST /dashboard/widgets`
- `GET /dashboard/widgets`
- `DELETE /dashboard/widgets/{widget_id}`

### 11.3 Endpoint Workflow Patterns

Pattern A: Unfiltered request with cache-first response

1. Load latest `curated_aggregates`.
2. If required metric keys exist and cache not stale, return cached section.
3. Else recompute from snapshots.

Pattern B: Filtered or explicit date range request

1. Build filter map from query params.
2. Query snapshots with DB-level JSONB predicates.
3. Apply precise date filtering in Python using parsed SAP date values.
4. Recompute metrics from filtered dataframe.
5. Include `applied_filters` in response.

### 11.4 Per-Endpoint Workflow Notes

`/sales/filters`:

- Cached `filter_options` and totals if available.
- Else computes from up to 50k snapshot rows over recent 730 days.

`/sales/graph/daily`:

- Uses cached `cluster_daily` when present.
- If cache absent, recomputes daily from filtered snapshots.
- If cache exists and filters applied but no cluster rows match, returns zeroed result.

`/sales/graph/breakdown`:

- Validates requested dimension.
- Uses `cluster_daily` when available.
- Else computes grouped totals from filtered snapshots.

`/sales/today`, `/sales/mtd`, `/sales/qtd`:

- Unfiltered path: cached metrics with staleness check.
- Fallback path: recompute from snapshot dataframe plus billing metrics.
- Filtered path: recompute and return `applied_filters`.

`/sales/period`:

- Requires `date_from` and `date_to`.
- Always recomputes using filtered snapshots and billing metrics helper.

`/sales/trends`:

- Unfiltered path returns cached trends when present.
- Otherwise recomputes from snapshots.

`/sales/orders`:

- Unfiltered path prefers cached orders metrics.
- Fallback to recompute from up to 100k rows.
- Filtered path recomputes from filtered snapshots.

`/sales/last-updated`:

- Returns latest aggregate timestamp in dashboard timezone.

Widget APIs:

- Create validates `widget_type` (`chart` or `table`) and non-empty payload.
- List returns latest active widgets (up to 100).
- Delete soft-deactivates widget (`is_active=0`).

### 11.5 Snapshot Query and Billing Helper Workflows

`_query_filtered_snapshots`:

1. Start with tenant condition.
2. Add JSONB dimension filters using `payload[field].astext`.
3. Add coarse lower-bound by `last_changed_at` when `date_from` exists.
4. Execute SQL and collect payload rows.
5. Apply exact `CreationDate` filtering in Python (`_filter_rows_by_date`).

`_query_billing_metrics`:

1. Try fast path using cached aggregate fields (`today`, `mtd`, `qtd`) when compatible with reference date.
2. If unavailable, load up to 100k billing snapshots.
3. Compute billing metrics via aggregator helper.

## 12. Worker and Scheduler Architecture

### 12.1 Scheduler Controls (`dashboard/scheduler.py`)

Main locks:

- `_job_lock` for combined run
- `_ingest_lock` for ingestion
- `_aggregate_lock` for aggregation

Retry helper:

- `_fetch_with_retry` uses up to 3 attempts with backoffs 5s, 15s, 45s.

### 12.2 Ingestion Workflow (`run_ingestion_job`)

1. Skip if ingestion lock is already held.
2. Set DB statement timeout to 600s.
3. Determine mode per dataset:
   - incremental mode when snapshot data exists,
   - backfill mode when empty.
4. Sales order fetch:
   - incremental by `LastChangeDateTime` with lookback,
   - backfill by `CreationDate` for `MAX_YEARS`.
5. Billing fetch:
   - incremental by latest billing date with lookback,
   - backfill for `MAX_YEARS`.
6. Upsert snapshot tables in batches.
7. Record `job_runs` success/failed.

### 12.3 Aggregation Workflow (`run_aggregation_job`)

1. Skip if aggregate lock is already held.
2. Set DB statement timeout to 600s.
3. Stream all snapshot payloads in 5000-row chunks.
4. Run `calculate_aggregates` in worker thread with 300s timeout.
5. Acquire advisory lock (`pg_advisory_xact_lock(42)`).
6. Upsert/update single current `curated_aggregates` record by fingerprint.
7. Delete stale extra aggregate rows if any.
8. Record `job_runs`.
9. Refresh materialized views if data changed.

### 12.4 Aggregate-If-Pending Workflow (`run_aggregation_if_pending`)

1. Read latest successful ingest and aggregate completion timestamps.
2. If no successful ingest: skip.
3. If aggregate is not older than ingest: skip.
4. Else run aggregation.

### 12.5 Worker Processes

`dashboard/ingest_worker.py`:

- Optional startup sync ingestion.
- Schedules hourly ingestion if enabled.

`dashboard/aggregate_worker.py`:

- Optional startup pending-check aggregation.
- Polls every 5 minutes for pending aggregate.

`dashboard/worker.py`:

- Legacy combined mode using `run_hourly_job`.

## 13. Aggregate Computation Workflow (`dashboard/aggregator.py`)

`calculate_aggregates(so_rows, billing_rows)`:

1. Build dataframe from sales-order payload.
2. Normalize dimensions and status fields.
3. Parse SAP dates and numeric values.
4. Build billing metrics.
5. Compute:
   - `hourly`, `daily`, `monthly`, `quarterly`, `yearly`
   - `totals`
   - `filter_options`
   - `cluster_daily`
   - `today`, `mtd`, `qtd`, `trends`, `orders`
6. Compute fingerprint hash of metrics JSON.

Formula-level detail and dashboard placement map are maintained in `docs/aggregates.md`.

## 14. MCP Server Architecture and Workflows

### 14.1 Server Surface (`mcp_server/server.py`)

Tools:

- `query_database(sql, tenant_id, user_id, session_id)`
- `list_tables()`
- `get_table_schema(table_name)`

Resource:

- `schema://tables`

Transport:

- SSE via FastMCP.

### 14.2 Query Workflow (`query_database`)

1. Validate `tenant_id` exists.
2. Validate SQL (`validate_query`):
   - SELECT-only checks,
   - forbidden keyword scan,
   - no multi-statement,
   - AST parse with `sqlglot`,
   - allowed table/view allowlist enforcement.
3. Inject tenant filter (`tenant_id = $1`) into all SELECT branches.
4. Apply row limit cap (`MAX_ROWS`, default 1000).
5. Execute with asyncpg and timeout (`QUERY_TIMEOUT_SECONDS`, default 30).
6. Log query duration and row count to audit table.
7. Return JSON with success flag, row count, and rows.

Error path:

- Validation, timeout, DB, and unexpected errors are normalized in response.
- Failed attempts are still audit-logged with error text.

### 14.3 Schema Discovery Workflows

`list_tables`:

- Returns allowed tables and views with type and columns.

`get_table_schema(table_name)`:

- Returns column metadata for one allowed object.
- Rejects non-allowlisted object names.

### 14.4 Bootstrap Workflow (`mcp_server/bootstrap.py`)

Run on startup before server loop:

1. Ensure read-only role exists.
2. Ensure DB/schema/table grants and default privileges.
3. Ensure `mcp_query_log` table and index.
4. Ensure snapshot indexes for JSON/date access.

Bootstrap failures are non-fatal (logged as warnings).

## 15. End-to-End Workflow Sequences

### 15.1 Data Refresh Pipeline

```text
SAP OData
  -> Ingest Worker (hourly)
     -> sales_order_snapshots / billing_snapshots
     -> job_runs (ingest success/failure)
  -> Aggregate Worker (poll 5 min)
     -> run_aggregation_if_pending
     -> curated_aggregates (single latest row by fingerprint)
     -> materialized view refresh
     -> job_runs (aggregate success/failure)
```

### 15.2 Chatbot Query via Curated Aggregate

```text
User -> /chat
  -> parse_and_route selects get_sales_aggregates
  -> validate params (report_type etc)
  -> aggregate_client calls dashboard /sales/*
  -> final_answer summarizes raw_payload + rows
  -> session saved + optional chart + optional export_url
```

### 15.3 Chatbot Query via MCP SQL

```text
User -> /chat
  -> parse_and_route selects query_database or DB snapshot tool
  -> execute calls mcp_client
  -> MCP validates SQL + injects tenant filter + limits rows + executes
  -> result returned to chatbot
  -> optional sql_retry path if failure/empty for query_database
  -> final_answer
```

### 15.4 Chatbot Query via Live SAP

```text
User -> /chat
  -> parse_and_route selects SAP tool
  -> validate (type, date, defaults where applicable)
  -> execute -> call_sap (filter, auth, expand, retry)
  -> optional customer-name enrichment via get_business_partners
  -> final_answer
```

### 15.5 Pending Follow-Up Query Continuation

```text
Turn 1: Missing required params
  -> needs_user_input=True
  -> pending_tool/pending_params saved in Redis

Turn 2: User provides missing details
  -> /chat loads pending context
  -> parse merges pending + new parameters
  -> validate/execute/final complete original intent
```

## 16. Configuration Surfaces

### 16.1 Chatbot (`chatbot/config.py`)

Key groups:

- LLM: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TEMPERATURE`
- SAP: `SAP_BASE_URL`, `SAP_AUTH_TYPE`, `SAP_USERNAME`, `SAP_PASSWORD`, `SAP_CLIENT`, `SAP_OAUTH_*`, `SAP_VERIFY_TLS`
- Service runtime: `APP_HOST`, `APP_PORT`, `CORS_ORIGINS`
- Integrations: `REDIS_URL`, `DASHBOARD_BASE_URL`, `MCP_SERVER_URL`
- Auth/dev: `API_SECRET_KEY`, `DEV_TENANT_ID`, `DEV_USER_ID`, `DEV_USER_ROLE`

### 16.2 Dashboard (`dashboard/settings.py`)

Key groups:

- DB: `DATABASE_URL`
- SAP extractor credentials: `SAP_BASE_URL`, `SAP_CLIENT`, `SAP_USERNAME`, `SAP_PASSWORD`
- Worker behavior: `MAX_YEARS`, `INCREMENTAL_LOOKBACK_HOURS`, `BATCH_UPSERT_SIZE`
- Scheduler flags: `RUN_SCHEDULER`, `RUN_STARTUP_SYNC`
- UI/timezone: `CHATBOT_WIDGET_URL`, `SERVE_LOCAL_CHATBOT_UI`, `DASHBOARD_TIMEZONE`

### 16.3 MCP (`mcp_server/config.py`)

Key groups:

- Read-only DB connection + pool
- Admin bootstrap credentials
- Query safety limits (`QUERY_TIMEOUT_SECONDS`, `MAX_ROWS`)
- SSE host/port
- Allowed tables and views

## 17. Deployment Architecture

### 17.1 Compose Services (`docker-compose.yml`)

- `postgres`
- `redis`
- `mcp-postgres`
- `qbot`
- `dashboard`
- `dashboard-ingest-worker`
- `dashboard-aggregate-worker`

### 17.2 Current Build-File Mismatch

Compose references:

- `Dockerfile.qbot`
- `Dockerfile.dashboard`
- `Dockerfile.dashboard-ingest`
- `Dockerfile.dashboard-aggregate`

These Dockerfiles are not present in the current repository snapshot. Existing Dockerfiles found:

- `Dockerfile` (chatbot)
- `mcp_server/Dockerfile` (MCP)

Deployments should align compose build definitions to available Dockerfiles in this branch.

## 18. Security and Reliability Controls (Implemented)

Chatbot:

- Optional API key guard.
- Redis-backed rate limiting.
- Per-session lock for concurrency safety.
- Explicit graph timeout.

MCP:

- SELECT-only + forbidden keyword validation.
- AST parsing with object allowlist.
- Mandatory tenant filter injection.
- Row cap and query timeout.
- Audit logging for success and failure.

Dashboard/Workers:

- DB statement timeout for ingest/aggregate jobs.
- Retry with backoff for SAP extraction.
- Locking to prevent overlapping jobs.
- Fingerprint-based aggregate update to avoid unnecessary refresh work.

## 19. Known Behavior and Edge Cases

1. `ChatRequest` model requires tenant and user fields, but handler still applies dev defaults when values are empty strings.
2. Dashboard `CORS` is currently wide-open (`*`).
3. `/sales/period` uses `_build_custom_period_metrics`, which delegates billing fields through `mtd` key prefix logic.
4. Compose file references Dockerfiles not present in repository snapshot.
5. `final_answer` appends export hint text for multi-year data if not already mentioned.
6. Liveness and readiness probes are intentionally separated:
   - liveness avoids external dependency checks,
   - readiness checks required dependencies (Redis or DB/scheduler depending on service).

## 20. Extension Workflows

### 20.1 Add a New Chatbot Tool

1. Define tool in `chatbot/tools/definitions.py`.
2. Register in `chatbot/tools/registry.py`.
3. Add mapping/handling:
   - SAP path: update field mapping in `chatbot/sap_client.py`.
   - DB path: add execute branch and SQL builder where needed.
4. Validate routing prompt coverage in `parse_route`.

### 20.2 Add a New Dashboard Metric

1. Extend `dashboard/aggregator.py`.
2. Ensure value is included in `metrics` payload.
3. Expose via endpoint in `dashboard/main.py` if needed.
4. If needed for SQL consumers, update materialized-view strategy in `dashboard/views.py`.

### 20.3 Add a New MCP-Queryable Object

1. Add table/view to MCP allowlist in `mcp_server/config.py`.
2. Ensure read grants exist (dashboard grant path or MCP bootstrap).
3. Confirm schema discovery output includes object.

### 20.4 Split or Add New Worker Pipelines

1. Add job function in `dashboard/scheduler.py`.
2. Create dedicated process entrypoint under `dashboard/`.
3. Add container definition and scheduling policy.
