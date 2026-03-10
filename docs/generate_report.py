"""Generate MUNTQ AI Chatbot Progress & Roadmap DOCX Report."""
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT


doc = Document()

# -- Styles --
style = doc.styles["Normal"]
font = style.font
font.name = "Calibri"
font.size = Pt(11)


def add_heading_styled(text, level=1):
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = RGBColor(0x1B, 0x3A, 0x5C)
    return h


def add_bullet(text):
    p = doc.add_paragraph(style="List Bullet")
    p.add_run(text)
    return p


def add_table(headers, rows):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr_cells[i].text = h
        for p in hdr_cells[i].paragraphs:
            for run in p.runs:
                run.bold = True
    for row_data in rows:
        row_cells = table.add_row().cells
        for i, val in enumerate(row_data):
            row_cells[i].text = str(val)
    return table


# ============================================================
# TITLE PAGE
# ============================================================
doc.add_paragraph()
doc.add_paragraph()
title = doc.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = title.add_run("MUNTQ AI Chatbot Program")
run.bold = True
run.font.size = Pt(28)
run.font.color.rgb = RGBColor(0x1B, 0x3A, 0x5C)

subtitle = doc.add_paragraph()
subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = subtitle.add_run("Architecture, Progress & Roadmap Report")
run.font.size = Pt(18)
run.font.color.rgb = RGBColor(0x44, 0x72, 0xC4)

doc.add_paragraph()

meta = doc.add_paragraph()
meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
meta.add_run("Date: March 6, 2026\n").font.size = Pt(12)
meta.add_run("Version: 2.0\n").font.size = Pt(12)
meta.add_run("Status: Active Development").font.size = Pt(12)

doc.add_paragraph()
doc.add_paragraph()

scope = doc.add_paragraph()
scope.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = scope.add_run(
    "Three SAP Business Flows:\n"
    "Order to Cash  |  Procure to Pay  |  Inventory Management"
)
run.font.size = Pt(14)
run.bold = True
run.font.color.rgb = RGBColor(0x44, 0x72, 0xC4)

doc.add_page_break()

# ============================================================
# TABLE OF CONTENTS
# ============================================================
add_heading_styled("Table of Contents", level=1)
toc_items = [
    "1. Executive Summary",
    "2. What Has Been Delivered (3-Week Progress)",
    "3. Current System Architecture",
    "4. Three SAP Business Flows",
    "   4.1 Order to Cash (O2C)",
    "   4.2 Procure to Pay (P2P)",
    "   4.3 Inventory Management",
    "5. Technology Stack",
    "6. What Needs to Be Done",
    "7. Next Phase Roadmap",
    "8. Team Ownership Recommendations",
    "9. Appendix: API & Tool Reference",
]
for item in toc_items:
    p = doc.add_paragraph(item)
    p.paragraph_format.space_after = Pt(2)

doc.add_page_break()

# ============================================================
# 1. EXECUTIVE SUMMARY
# ============================================================
add_heading_styled("1. Executive Summary", level=1)

doc.add_paragraph(
    "Over the last three weeks, we took the MUNTQ AI Chatbot from a tool-based prototype to a "
    "production-ready, multi-service architecture designed for enterprise scale. Each architectural "
    "decision was driven by a real problem we encountered during development:"
)

add_bullet(
    "We started with a chatbot that called SAP APIs directly. It worked, but repeated analytical "
    "questions were slow and expensive because every query hit live SAP."
)
add_bullet(
    "To solve this, we introduced PostgreSQL as a data warehouse layer - aggregating SAP data "
    "so the chatbot could respond faster and more consistently."
)
add_bullet(
    "Since aggregated data and analysis are inherently part of a dashboard, we built the "
    "dashboard service ourselves for quick demonstration and demo purposes - including the cron "
    "jobs, aggregation pipeline, database, and frontend."
)
add_bullet(
    "We then connected the chatbot to the Dashboard API for quick, direct KPI answers, added an "
    "MCP server for complex ad-hoc SQL queries, and kept live SAP APIs for transactional drill-downs."
)
add_bullet(
    "The entire pipeline was built to be adjustable, flexible, and connector-based - designed "
    "with multi-tenancy in mind so it can adapt when the database or infrastructure changes in the future."
)

doc.add_paragraph(
    "The next phase will expand this architecture to cover three core SAP business flows end-to-end:"
)

add_bullet("Order to Cash (O2C)")
add_bullet("Procure to Pay (P2P)")
add_bullet("Inventory Management")

doc.add_paragraph(
    "This document presents what has been completed, the reasoning behind each decision, what "
    "remains to be done, and the roadmap for the three-flow expansion."
)

# ============================================================
# 2. WHAT HAS BEEN DELIVERED
# ============================================================
add_heading_styled("2. What Has Been Delivered (3-Week Progress)", level=1)

add_heading_styled("Week 1 - Chatbot Architecture (Tool-Based Agent + SAP Integration)", level=2)
doc.add_paragraph(
    "We began by building the core chatbot as a tool-based AI agent. The goal was to allow "
    "users to ask natural language questions and get answers directly from SAP, without "
    "needing to know SAP transaction codes or OData query syntax."
)
add_bullet(
    "Designed and implemented a LangGraph-based agent with a 5-node pipeline: intent parsing, "
    "missing parameter collection, validation, execution, and final answer generation."
)
add_bullet(
    "Integrated with SAP OData v2 APIs covering core business entities: Sales Orders, Purchase "
    "Orders, Invoices, Deliveries, Materials, Business Partners, and Journal Entries."
)
add_bullet(
    "Built a declarative tool system where each SAP API is defined as a data model (not code), "
    "making it easy to add new tools without modifying the agent logic."
)
add_bullet(
    "Added Redis-backed session management for multi-turn conversations, allowing follow-up "
    "questions and context retention across turns."
)
add_bullet(
    "Built the chatbot frontend (vanilla JS + Chart.js) for interactive querying and visualization."
)

add_heading_styled("Week 2 - Performance Layer (Aggregated Data + Dashboard)", level=2)
doc.add_paragraph(
    "After the chatbot was functional, we observed that relying solely on live SAP API calls "
    "for every question was too slow and expensive - especially for repeated analytical "
    "questions like \"what are today's sales\" or \"show me MTD performance\". The same SAP "
    "queries were being executed repeatedly with identical results."
)
doc.add_paragraph(
    "To solve this, we introduced PostgreSQL as a warehouse layer where all aggregated data "
    "would be pre-computed and stored. Since aggregated data and analytics are inherently part "
    "of a dashboard, we built the complete dashboard service ourselves - including the ETL "
    "pipeline, cron jobs, aggregation logic, database schema, and frontend - for quick "
    "demonstration and demo purposes."
)
add_bullet(
    "Introduced PostgreSQL as an analytics data warehouse with two layers: raw JSONB snapshots "
    "(preserving full SAP responses) and curated aggregates (pre-computed KPIs)."
)
add_bullet(
    "Built an hourly ETL pipeline: SAP OData extraction -> PostgreSQL snapshot storage -> "
    "pandas-based aggregation -> curated KPI metrics."
)
add_bullet(
    "Created the Dashboard API service with analytics endpoints: today's KPIs, month-to-date, "
    "quarter-to-date, trend series, order summaries, and period breakdowns."
)
add_bullet(
    "Implemented a 9-dimension filter bar with a fast-path (pre-computed aggregate when no "
    "filters) and a slow-path (re-query raw snapshots when filters are applied)."
)
add_bullet(
    "Connected the chatbot to the Dashboard API so it could return quick, direct answers for "
    "standard analytical questions without hitting SAP."
)
add_bullet(
    "Built the interactive dashboard frontend with Chart.js visualizations and configurable widgets."
)

add_heading_styled("Week 3 - Enterprise Architecture (Hybrid Strategy + Multi-Tenant Foundation)", level=2)
doc.add_paragraph(
    "With the dashboard handling standard KPIs efficiently, we identified that some questions "
    "required complex, non-predefined analysis that could not be covered by pre-built dashboard "
    "endpoints. For this, we introduced the MCP (Model Context Protocol) server - a secure SQL "
    "access layer over PostgreSQL that the chatbot can use for ad-hoc analytical queries."
)
doc.add_paragraph(
    "This established the hybrid data strategy that represents the best enterprise approach:"
)
add_bullet(
    "Dashboard API for stable, pre-computed KPIs - the fastest and most efficient path for "
    "standard questions."
)
add_bullet(
    "MCP Server for complex, ad-hoc SQL queries - flexible analysis on snapshot data without "
    "hardcoding every possible question."
)
add_bullet(
    "Live SAP OData APIs for real-time transactional data - used only when current, live "
    "data is specifically needed."
)
doc.add_paragraph(
    "Critically, the entire pipeline was designed to be adjustable and flexible - built as a "
    "connector-based architecture that can adapt to changes. The database schema, ETL pipeline, "
    "and data models were all designed with multi-tenancy in mind, so that when the system "
    "scales to multiple tenants or the underlying database changes, the architecture can adopt "
    "without a rewrite."
)
add_bullet(
    "Refactored ingestion and aggregation into separate, independently scalable worker processes."
)
add_bullet(
    "Added SQL self-correction capability: the agent automatically retries and fixes failed "
    "SQL queries using the LLM, reducing errors without user intervention."
)
add_bullet(
    "Enabled multi-tool execution: the agent can call up to 3 tools in a single request for "
    "comparative or cross-entity questions."
)
add_bullet(
    "Designed all data layers with tenant isolation in mind, preparing for the multi-tenant "
    "evolution that the backend team will build upon."
)

# ============================================================
# 3. CURRENT SYSTEM ARCHITECTURE
# ============================================================
doc.add_page_break()
add_heading_styled("3. Current System Architecture", level=1)

doc.add_paragraph(
    "The system is composed of four main services sharing one PostgreSQL database, "
    "with Redis for chat sessions:"
)

add_table(
    ["Service", "Port", "Responsibility"],
    [
        [
            "Chatbot API (app.py)",
            "5000",
            "LangGraph agent: intent parsing, tool orchestration, session management, chat UI",
        ],
        [
            "Dashboard API (dashboard/)",
            "8001",
            "Analytics endpoints, ETL scheduling, widget management, dashboard UI",
        ],
        [
            "MCP Server (mcp_server/)",
            "8000/3001",
            "Read-only PostgreSQL access via MCP protocol, SQL validation, audit logging",
        ],
        [
            "Background Workers",
            "N/A",
            "Ingestion worker (hourly SAP fetch) + Aggregation worker (KPI computation)",
        ],
    ],
)

doc.add_paragraph()

add_heading_styled("Architecture Diagram", level=2)
arch_text = (
    "User/UI\n"
    "  -> FastAPI Chatbot (5000)\n"
    "     -> LangGraph Agent (5-node pipeline)\n"
    "        -> LLM Provider (OpenAI-compatible)\n"
    "        -> Dashboard API (8001) for curated reports\n"
    "        -> MCP Server (3001 -> 8000 SSE) for SQL analytics\n"
    "        -> SAP OData APIs for live transactional data\n"
    "     -> Redis Session Store\n"
    "\n"
    "Dashboard API (8001)\n"
    "  -> PostgreSQL (snapshots + curated aggregates + materialized views)\n"
    "\n"
    "Ingestion Worker (hourly)\n"
    "  -> SAP OData -> PostgreSQL snapshots\n"
    "\n"
    "Aggregation Worker (every 5 min poll)\n"
    "  -> PostgreSQL snapshots -> pandas aggregates -> curated_aggregates"
)
doc.add_paragraph(arch_text)

add_heading_styled("Agent Pipeline (LangGraph Nodes)", level=2)
add_table(
    ["Node", "Function", "LLM Required"],
    [
        ["1. parse_and_route", "Identify tool(s) + extract parameters from natural language", "Yes"],
        ["2. collect_missing", "Detect missing required params, ask user", "No (deterministic)"],
        ["3. validate", "Type coercion, range checks, business logic validation", "No (deterministic)"],
        ["4. execute", "Dispatch to Dashboard API / MCP / SAP OData", "No"],
        ["5. sql_retry", "Self-correct failed SQL queries (query_database only)", "Yes"],
        ["6. final_answer", "Summarize results, generate chart data, suggest refinements", "Yes"],
    ],
)

add_heading_styled("Data Access Tiers (by priority)", level=2)
add_table(
    ["Tier", "Source", "Speed", "Use Case"],
    [
        ["1. Curated Aggregates", "Dashboard API (PostgreSQL)", "Fastest", "Standard KPIs (today, MTD, QTD, trends)"],
        ["2. Snapshot Queries", "MCP Server (PostgreSQL)", "Fast", "Complex ad-hoc analytics, custom SQL"],
        ["3. Live SAP OData", "SAP S/4HANA APIs", "Slowest", "Real-time transactional drill-down, master data"],
    ],
)

# ============================================================
# 4. THREE SAP BUSINESS FLOWS
# ============================================================
doc.add_page_break()
add_heading_styled("4. Three SAP Business Flows", level=1)

doc.add_paragraph(
    "The platform is being expanded to support three core end-to-end SAP business processes. "
    "Each flow will have dedicated tools, dashboard analytics, and conversational capabilities."
)

# -- 4.1 Order to Cash --
add_heading_styled("4.1 Order to Cash (O2C)", level=2)

doc.add_paragraph(
    "The Order to Cash flow covers the complete sales cycle from customer inquiry through "
    "payment collection. This is the most mature flow, with significant coverage already delivered."
)

add_heading_styled("O2C Process Flow", level=3)
doc.add_paragraph(
    "Customer Inquiry -> Quotation -> Sales Order -> Delivery -> Billing/Invoice -> Payment -> Reconciliation"
)

add_heading_styled("Current Coverage (Delivered)", level=3)
add_table(
    ["Process Step", "SAP Tool", "Status"],
    [
        ["Sales Orders", "get_sales_orders / get_sales_orders_db", "Delivered"],
        ["Billing Documents", "get_invoices / get_billing_db", "Delivered"],
        ["Outbound Deliveries", "get_deliveries", "Delivered"],
        ["Business Partners (Customers)", "get_business_partners", "Delivered"],
        ["Materials/Products", "get_materials", "Delivered"],
        ["Sales Aggregates & KPIs", "get_sales_aggregates", "Delivered"],
        ["Dashboard (Orders, MTD, QTD, Trends)", "Dashboard API endpoints", "Delivered"],
        ["Journal Entries (FI postings)", "get_journal_entries", "Delivered"],
    ],
)

add_heading_styled("Remaining O2C Work", level=3)
add_table(
    ["Process Step", "SAP API/Entity", "Priority"],
    [
        ["Quotations", "API_SALES_QUOTATION_SRV / A_SalesQuotation", "High"],
        ["Credit Memo / Debit Memo", "API_CREDIT_MEMO_REQUEST_SRV", "Medium"],
        ["Customer Payments / AR", "API_CUSTOMER_LINE_ITEMS", "High"],
        ["Returns / Complaints", "API_RETURNS_PROCESS_SRV", "Medium"],
        ["Pricing Conditions", "API_SLSPRICINGCONDITIONRECORD_SRV", "Low"],
        ["Billing KPI Dashboard", "Dashboard aggregation for billing", "High"],
        ["Delivery Tracking Dashboard", "Dashboard aggregation for deliveries", "Medium"],
    ],
)

# -- 4.2 Procure to Pay --
add_heading_styled("4.2 Procure to Pay (P2P)", level=2)

doc.add_paragraph(
    "The Procure to Pay flow covers the purchasing cycle from requisition through vendor payment. "
    "Currently only Purchase Orders and Purchase Order Items are supported via live SAP OData. "
    "This flow needs significant expansion."
)

add_heading_styled("P2P Process Flow", level=3)
doc.add_paragraph(
    "Purchase Requisition -> RFQ/Sourcing -> Purchase Order -> Goods Receipt -> "
    "Invoice Verification -> Vendor Payment"
)

add_heading_styled("Current Coverage", level=3)
add_table(
    ["Process Step", "SAP Tool", "Status"],
    [
        ["Purchase Orders", "get_purchase_orders", "Delivered (Live SAP)"],
        ["Purchase Order Items", "get_purchase_order_items", "Delivered (Live SAP)"],
        ["Business Partners (Vendors)", "get_business_partners (category=2)", "Delivered"],
        ["Materials", "get_materials", "Delivered"],
    ],
)

add_heading_styled("Remaining P2P Work", level=3)
add_table(
    ["Process Step", "SAP API/Entity", "Priority"],
    [
        ["Purchase Requisitions", "API_PURCHASEREQ_PROCESS_SRV / A_PurchaseRequisition", "High"],
        ["Goods Receipt (MIGO)", "API_MATERIAL_DOCUMENT_SRV / A_MaterialDocumentHeader", "High"],
        ["Invoice Verification (MIRO)", "API_SUPPLIERINVOICE_PROCESS_SRV / A_SupplierInvoice", "High"],
        ["Vendor Line Items / AP", "API_VENDOR_LINE_ITEMS", "High"],
        ["Contracts / Scheduling Agreements", "API_PURCHASECONTRACT_PROCESS_SRV", "Medium"],
        ["Source of Supply", "API_PURCHASING_SOURCE_SRV", "Low"],
        ["P2P Snapshot ETL", "PostgreSQL snapshots for PO/GR/IR", "High"],
        ["P2P Dashboard & KPIs", "Dashboard service P2P endpoints", "High"],
        ["P2P Curated Aggregates", "Aggregation pipeline for P2P metrics", "High"],
    ],
)

# -- 4.3 Inventory Management --
add_heading_styled("4.3 Inventory Management", level=2)

doc.add_paragraph(
    "The Inventory Management flow covers stock tracking, goods movements, physical inventory, "
    "and material valuation. Currently only Materials master data is available. "
    "This is a new flow that needs to be built from scratch."
)

add_heading_styled("Inventory Process Flow", level=3)
doc.add_paragraph(
    "Goods Receipt -> Stock Management -> Stock Transfer -> Goods Issue -> "
    "Physical Inventory -> Valuation"
)

add_heading_styled("Current Coverage", level=3)
add_table(
    ["Process Step", "SAP Tool", "Status"],
    [
        ["Materials / Products", "get_materials", "Delivered"],
        ["Material Documents (partial via Journal)", "get_journal_entries", "Partial"],
    ],
)

add_heading_styled("Remaining Inventory Work", level=3)
add_table(
    ["Process Step", "SAP API/Entity", "Priority"],
    [
        ["Material Stock Overview", "API_MATERIAL_STOCK_SRV / A_MatlStkInAcctMod", "High"],
        ["Material Documents (Goods Movements)", "API_MATERIAL_DOCUMENT_SRV / A_MaterialDocumentHeader", "High"],
        ["Warehouse Stock", "API_WAREHOUSE_STOCK_SRV", "Medium"],
        ["Physical Inventory", "API_PHYSICAL_INVENTORY_DOC_SRV / A_PhysInventoryDocHeader", "Medium"],
        ["Reservation Documents", "API_RESERVATION_DOCUMENT_SRV / A_Reservation", "Medium"],
        ["Material Valuation", "API_PRODUCT_VALUATION / A_ProductValuation", "Medium"],
        ["Batch Management", "API_BATCH_SRV / A_Batch", "Low"],
        ["Inventory Snapshot ETL", "PostgreSQL snapshots for stock/movements", "High"],
        ["Inventory Dashboard & KPIs", "Dashboard service inventory endpoints", "High"],
        ["Inventory Curated Aggregates", "Aggregation pipeline for inventory metrics", "High"],
    ],
)

# ============================================================
# 5. TECHNOLOGY STACK
# ============================================================
doc.add_page_break()
add_heading_styled("5. Technology Stack", level=1)

add_table(
    ["Layer", "Technology", "Purpose"],
    [
        ["HTTP Server", "FastAPI >= 0.110", "Async REST API with auto-generated OpenAPI docs"],
        ["Agent Framework", "LangGraph >= 0.2", "Stateful graph-based agent orchestration"],
        ["LLM Client", "OpenAI Python SDK >= 1.12", "Async calls to any OpenAI-compatible endpoint"],
        ["Data Models", "Pydantic v2 >= 2.5", "Type-safe models, validation, serialization"],
        ["HTTP Client", "httpx >= 0.27", "Async SAP OData and inter-service HTTP calls"],
        ["Session Store", "Redis >= 5.0", "Conversation persistence, rate limiting, session TTL"],
        ["Database", "PostgreSQL + asyncpg", "Snapshots, curated aggregates, materialized views"],
        ["Data Analysis", "pandas", "Aggregation worker KPI computation"],
        ["SQL Safety", "sqlglot", "MCP server AST-level SQL validation"],
        ["Containerization", "Docker + docker-compose", "Multi-service deployment"],
        ["Language", "Python 3.11+", "Async/await, type hints, union syntax"],
    ],
)

# ============================================================
# 6. WHAT NEEDS TO BE DONE
# ============================================================
doc.add_page_break()
add_heading_styled("6. What Needs to Be Done - Next Phase", level=1)

doc.add_paragraph(
    "The ETL process, data aggregation, dashboard service, and database management are "
    "fundamentally backend responsibilities - not AI. These are two distinct disciplines. "
    "In phase 1, the AI team handled everything end-to-end (chatbot, ETL, aggregation, "
    "dashboard, database) because no backend team existed yet, and we needed a working demo "
    "to validate the concept."
)
doc.add_paragraph(
    "Now that the backend and frontend teams have been assigned, they will take full ownership "
    "of the data layer and build the production system for the B2B SaaS product. They will use "
    "our implementation as a reference - studying our data flows, schema design, aggregation "
    "logic, and API contracts - to inform their production architecture. The dashboard service "
    "we built in Python will be replaced by the backend team's production service."
)
doc.add_paragraph(
    "The AI team will focus exclusively on the chatbot agent. Once the backend team's "
    "production APIs and database are ready, we will migrate the chatbot from our current "
    "curated APIs to their new production APIs. We will work module by module across the "
    "three SAP business flows, keeping the backend team in the loop throughout."
)

add_heading_styled("6.1 AI Team Focus: Chatbot Modules (One by One)", level=2)

add_heading_styled("Order to Cash (O2C) - Completion", level=3)
add_bullet("Add Quotation and Returns chatbot tools for full O2C conversational coverage.")
add_bullet("Add Customer Payments / Accounts Receivable line items tool.")
add_bullet(
    "Enhance chatbot responses for billing and delivery questions using available data tiers."
)

add_heading_styled("Procure to Pay (P2P) - New Module", level=3)
add_bullet(
    "Add SAP tool definitions for Purchase Requisitions, Goods Receipts, Invoice Verification, "
    "and Vendor Payments."
)
add_bullet("Integrate P2P tools into the agent pipeline with proper routing and validation.")
add_bullet("Enable conversational queries across the full P2P cycle.")

add_heading_styled("Inventory Management - New Module", level=3)
add_bullet(
    "Add SAP tool definitions for Material Stock, Material Documents, Physical Inventory, "
    "and Reservations."
)
add_bullet("Integrate Inventory tools into the agent pipeline with proper routing and validation.")
add_bullet("Enable conversational queries for stock levels, goods movements, and inventory analysis.")

add_heading_styled("Cross-Flow Capabilities", level=3)
add_bullet(
    "Enable multi-flow questions (e.g., correlating purchase orders with sales orders, "
    "inventory impact of sales trends)."
)
add_bullet("Multi-tool coordination across all three business flows.")

add_heading_styled("API Migration", level=3)
add_bullet(
    "Once the backend team delivers production Dashboard APIs, migrate the chatbot from our "
    "current curated APIs to the new production endpoints."
)
add_bullet(
    "The chatbot's connector-based design ensures this migration is a configuration change, "
    "not a rewrite."
)

add_heading_styled("6.2 Backend & Frontend Team: Production System", level=2)

doc.add_paragraph(
    "The backend and frontend teams will build the production-grade system for the whole "
    "B2B SaaS product. Our current implementation gives them a clear reference:"
)

add_bullet(
    "Production database schema with multi-tenant isolation, multi-dimensional data models, "
    "and enterprise-grade design."
)
add_bullet(
    "Production ETL/aggregation pipeline - SAP extraction, data processing, and KPI computation "
    "at scale."
)
add_bullet(
    "Production Dashboard APIs serving curated and aggregated data for all three business flows "
    "and for the entire platform (not just the chatbot)."
)
add_bullet("Production dashboard frontend as part of the B2B SaaS application.")
add_bullet(
    "Multi-tenant infrastructure, observability, alerting, and data quality controls."
)

# ============================================================
# 7. NEXT PHASE ROADMAP
# ============================================================
add_heading_styled("7. Next Phase Roadmap", level=1)

add_heading_styled("AI Team Roadmap (Chatbot Modules)", level=2)
add_table(
    ["Phase", "Focus", "Deliverables"],
    [
        [
            "Phase 1",
            "O2C Completion",
            "Quotation, Returns, Customer Payment tools\n"
            "Full O2C conversational coverage in chatbot",
        ],
        [
            "Phase 2",
            "P2P Module",
            "Purchase Requisition, Goods Receipt, Invoice Verification tools\n"
            "Full P2P conversational coverage in chatbot",
        ],
        [
            "Phase 3",
            "Inventory Module",
            "Material Stock, Material Documents, Physical Inventory tools\n"
            "Full Inventory conversational coverage in chatbot",
        ],
        [
            "Phase 4",
            "Cross-Flow & Migration",
            "Cross-flow analytical capabilities\n"
            "Migrate chatbot from current curated APIs to backend team's production APIs",
        ],
    ],
)

doc.add_paragraph()

add_heading_styled("Backend Team Roadmap (Production System)", level=2)
add_table(
    ["Phase", "Focus", "Deliverables"],
    [
        [
            "Phase 1",
            "Schema & Architecture",
            "Production database schema design (multi-tenant, multi-dimensional)\n"
            "ETL pipeline architecture",
        ],
        [
            "Phase 2",
            "Data Pipeline",
            "Production ETL/aggregation for O2C, P2P, Inventory\n"
            "Dashboard APIs for all three flows",
        ],
        [
            "Phase 3",
            "Dashboard & Frontend",
            "Production dashboard service + frontend\n"
            "Multi-tenant infrastructure",
        ],
    ],
)

doc.add_paragraph(
    "Both teams will work in parallel, with regular alignment to ensure the backend team's "
    "APIs serve the data the chatbot needs, and the AI team's module requirements inform "
    "the backend team's schema and API design."
)

# ============================================================
# 8. TEAM OWNERSHIP
# ============================================================
doc.add_page_break()
add_heading_styled("8. Team Ownership & Collaboration Model", level=1)

doc.add_paragraph(
    "In phase 1, the AI team built the entire stack - chatbot, ETL, aggregation, database, "
    "and dashboard - because no backend or frontend team existed. ETL, data aggregation, "
    "and dashboard development are fundamentally backend and frontend responsibilities, not AI. "
    "We handled them to deliver a working demo and validate the architecture."
)

doc.add_paragraph(
    "Now that the backend and frontend teams are assigned, they will take full ownership of "
    "the production data layer and dashboard for the B2B SaaS product. Our implementation "
    "gives them a complete working reference: the data flows, schema patterns, aggregation "
    "logic, API contracts, and KPI definitions they need to design the production system."
)

add_heading_styled("Backend & Frontend Team", level=2)
add_bullet(
    "Own the production database, ETL pipeline, aggregation, dashboard APIs, and dashboard "
    "frontend for the entire B2B SaaS product."
)
add_bullet(
    "Use our current implementation as a reference to understand data flows, KPI requirements, "
    "and API contracts that the chatbot and platform need."
)
add_bullet(
    "Build multi-tenant, multi-dimensional data models and production-grade infrastructure."
)
add_bullet(
    "Deliver production Dashboard APIs that the AI team will connect to, replacing the "
    "current Python-based demo service."
)

add_heading_styled("AI Team", level=2)
add_bullet(
    "Own the chatbot agent exclusively: intent routing, tool orchestration, response quality, "
    "and conversational UX."
)
add_bullet(
    "Expand chatbot module by module (O2C, P2P, Inventory), keeping the backend team in the "
    "loop on what data and APIs are needed for each flow."
)
add_bullet(
    "Once the backend team delivers production APIs, migrate the chatbot from our current "
    "curated APIs to the new production endpoints."
)
add_bullet(
    "Continue to own prompting, guardrails, chart generation, export, and chatbot frontend."
)

add_heading_styled("Collaboration Model", level=2)
add_bullet(
    "AI team works on chatbot modules one by one, communicating data and API requirements "
    "to the backend team for each flow."
)
add_bullet(
    "Backend team uses our reference implementation to design and build the production "
    "system, aligning API contracts with what the chatbot needs."
)
add_bullet(
    "When backend production APIs are ready, the AI team migrates to them - the chatbot's "
    "connector-based architecture makes this a configuration change, not a rewrite."
)

# ============================================================
# 9. APPENDIX
# ============================================================
add_heading_styled("9. Appendix: Current Tool Reference", level=1)

add_table(
    ["Tool Name", "Category", "SAP Service / Source", "Key Parameters"],
    [
        ["get_sales_aggregates", "Curated", "Dashboard API", "period, date_from, date_to"],
        ["get_sales_orders_db", "Snapshot", "PostgreSQL (MCP)", "customer_id, sales_org, material, dates, top"],
        ["get_billing_db", "Snapshot", "PostgreSQL (MCP)", "customer_id, sales_org, dates, top"],
        ["query_database", "SQL", "PostgreSQL (MCP)", "sql_query (free-form, validated)"],
        ["get_sales_orders", "Live SAP", "API_SALES_ORDER_SRV", "sales_order, customer_id, sales_org, material, dates"],
        ["get_purchase_orders", "Live SAP", "API_PURCHASEORDER_PROCESS_SRV", "purchase_order, vendor_id, purchasing_org, plant, dates"],
        ["get_purchase_order_items", "Live SAP", "API_PURCHASEORDER_PROCESS_SRV", "purchase_order, material, plant"],
        ["get_invoices", "Live SAP", "API_BILLING_DOCUMENT_SRV", "billing_document, customer_id, sales_org, dates"],
        ["get_business_partners", "Live SAP", "API_BUSINESS_PARTNER", "business_partner, name, category"],
        ["get_materials", "Live SAP", "API_PRODUCT_SRV", "material, material_type, description"],
        ["get_deliveries", "Live SAP", "API_OUTBOUND_DELIVERY_SRV", "delivery, customer_id, shipping_point, dates"],
        ["get_journal_entries", "Live SAP", "API_JOURNALENTRYITEMBASIC_SRV", "company_code (required), fiscal_year, gl_account, dates"],
    ],
)

doc.add_paragraph()
add_heading_styled("Database Tables", level=2)
add_table(
    ["Table/View", "Purpose"],
    [
        ["sales_order_snapshots", "Raw JSONB from SAP ETL (sales orders with line items)"],
        ["billing_snapshots", "Raw JSONB from SAP ETL (billing documents)"],
        ["curated_aggregates", "Pre-computed KPI metrics (JSONB), refreshed hourly"],
        ["sales_daily_cluster", "SQL view for filter-dimension aggregation"],
        ["mcp_query_log", "Audit log for MCP queries"],
        ["dashboard_widgets", "Configurable dashboard widget definitions"],
        ["job_runs", "ETL job execution status tracking"],
    ],
)

# ============================================================
# PROGRAM OUTCOME
# ============================================================
doc.add_paragraph()
add_heading_styled("Program Outcome", level=1)

doc.add_paragraph(
    "In phase 1, the AI team delivered the full stack - chatbot, ETL, aggregation, database, "
    "and dashboard - to validate the concept and demonstrate business value. This was necessary "
    "because no backend or frontend team existed yet, and ETL/aggregation/dashboard development, "
    "while not AI work, was required to show a complete working system."
)
doc.add_paragraph(
    "The architecture was intentionally built to be flexible and connector-based. The dashboard, "
    "ETL pipeline, and database we built serve as a validated blueprint for the backend team - "
    "they can see exactly how data flows from SAP through aggregation into dashboard KPIs, and "
    "use this reference to design the production-grade, multi-tenant system for the full B2B "
    "SaaS product."
)
doc.add_paragraph(
    "Going forward, the AI team focuses exclusively on expanding the chatbot across three SAP "
    "business flows (Order to Cash, Procure to Pay, Inventory Management) - one module at a "
    "time, in close collaboration with the backend team. Once the backend team delivers their "
    "production Dashboard APIs, the chatbot will migrate seamlessly from our current curated "
    "APIs to the new production endpoints. The modular, declarative tool architecture ensures "
    "that adding new flows and switching data sources is a configuration change, not a rewrite."
)

# Save
output_path = "docs/MUNTQ_AI_Chatbot_Progress_and_Roadmap.docx"
doc.save(output_path)
print(f"Document saved to: {output_path}")
