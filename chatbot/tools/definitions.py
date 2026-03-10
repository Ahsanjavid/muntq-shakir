from chatbot.models import ParamType, SAPTool, ToolParameter

# ── MCP Database Query Tool (PRIMARY for sales/billing analytics) ─────────────
# All sales orders and billing docs are ETL'd into PostgreSQL hourly.
# The LLM generates SQL directly — no dashboard API middleman.

QUERY_DATABASE = SAPTool(
    name="query_database",
    description=(
        "Query the MUNTQ PostgreSQL database via SQL. This is the PRIMARY tool for "
        "all sales analytics, sales order lookups, and billing/invoice queries — "
        "the data is synced from SAP hourly.\n\n"
        "TABLES:\n"
        "- sales_order_snapshots: JSONB 'payload' with SAP fields. "
        "Key fields: payload->>'SalesOrder', payload->>'SalesOrderType', "
        "payload->>'SoldToParty', payload->>'CreationDate', "
        "payload->>'TotalNetAmount', payload->>'SalesOrganization', "
        "payload->>'DistributionChannel', payload->>'OverallSDProcessStatus', "
        "payload->>'TotalCreditCheckStatus', payload->>'SalesGroup'. "
        "Has 'last_changed_at' timestamp column.\n"
        "- billing_snapshots: JSONB 'payload' with: payload->>'BillingDocument', "
        "payload->>'BillingDocumentDate', payload->>'SoldToParty', "
        "payload->>'TotalNetAmount', payload->>'BillingDocumentIsCancelled'. "
        "Has 'billing_date' timestamp column.\n"
        "- curated_aggregates: Pre-computed JSONB 'metrics' blob with keys: "
        "hourly, daily, monthly, quarterly, yearly, totals, today, mtd, qtd, "
        "trends, orders, filter_options, cluster_daily.\n\n"
        "MATERIALIZED VIEWS (pre-aggregated, fast):\n"
        "- sales_daily(period date, total_sales numeric)\n"
        "- sales_monthly(period text, total_sales numeric)\n"
        "- sales_quarterly(period text, total_sales numeric)\n"
        "- sales_yearly(year int, total_sales numeric)\n"
        "- sales_hourly(period timestamp, total_sales numeric)\n"
        "- sales_daily_cluster(period, sales_order_type, sold_to_party, "
        "purchase_order_by_customer, sales_organization, distribution_channel, "
        "sales_group, total_sales, order_count)\n\n"
        "RULES: Write SELECT only. Do NOT include tenant_id (auto-injected). "
        "Use payload->>'Field' for JSONB. Cast numerics: (payload->>'TotalNetAmount')::numeric. "
        "Max 1000 rows. 30s timeout."
    ),
    parameters=[
        ToolParameter(
            name="sql",
            type=ParamType.STRING,
            required=True,
            description=(
                "SQL SELECT query. Use payload->>'FieldName' for JSONB columns. "
                "Do NOT include tenant_id filter — it is injected automatically."
            ),
        ),
    ],
    sap_entity_set="postgres_query",
    sap_api_path="/mcp/query",
    response_fields=[],
)

GET_SALES_AGGREGATES = SAPTool(
    name="get_sales_aggregates",
    description=(
        "Retrieve curated sales KPI aggregates from the dashboard service. "
        "Use this as PRIMARY for standard sales analytics questions: today's sales, "
        "MTD/QTD, sales trends, monthly comparisons, top materials/customers, "
        "highest/lowest order summaries. Faster and more reliable than ad-hoc SQL "
        "for known KPI/report requests."
    ),
    parameters=[
        ToolParameter(
            name="report_type",
            type=ParamType.STRING,
            required=True,
            description="Aggregate report to fetch",
            enum=["today", "mtd", "qtd", "trends", "orders", "period"],
        ),
        ToolParameter(
            name="date_from",
            type=ParamType.DATE,
            required=False,
            description="Start date (YYYY-MM-DD)",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="date_to",
            type=ParamType.DATE,
            required=False,
            description="End date (YYYY-MM-DD)",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="sales_order_type",
            type=ParamType.STRING,
            required=False,
            description="Filter by Sales Order Type",
        ),
        ToolParameter(
            name="sold_to_party",
            type=ParamType.STRING,
            required=False,
            description="Filter by Sold-to Party",
        ),
        ToolParameter(
            name="purchase_order_by_customer",
            type=ParamType.STRING,
            required=False,
            description="Filter by Customer PO reference",
        ),
        ToolParameter(
            name="sales_organization",
            type=ParamType.STRING,
            required=False,
            description="Filter by Sales Organization",
        ),
        ToolParameter(
            name="distribution_channel",
            type=ParamType.STRING,
            required=False,
            description="Filter by Distribution Channel",
        ),
        ToolParameter(
            name="sales_group",
            type=ParamType.STRING,
            required=False,
            description="Filter by Sales Group",
        ),
        ToolParameter(
            name="sales_office",
            type=ParamType.STRING,
            required=False,
            description="Filter by Sales Office",
        ),
        ToolParameter(
            name="sales_district",
            type=ParamType.STRING,
            required=False,
            description="Filter by Sales District",
        ),
        ToolParameter(
            name="customer_group",
            type=ParamType.STRING,
            required=False,
            description="Filter by Customer Group",
        ),
    ],
    sap_entity_set="sales_aggregates",
    sap_api_path="/sales/*",
    response_fields=[],
)

GET_SALES_ORDERS_DB = SAPTool(
    name="get_sales_orders_db",
    description=(
        "Query sales orders from PostgreSQL snapshots (not live SAP). "
        "Use for fast historical lookups and tenant-scoped transactional queries."
    ),
    parameters=[
        ToolParameter(name="sales_order", type=ParamType.STRING, required=False, description="Specific sales order"),
        ToolParameter(name="customer_id", type=ParamType.STRING, required=False, description="Sold-to party"),
        ToolParameter(name="sales_org", type=ParamType.STRING, required=False, description="Sales organization"),
        ToolParameter(name="material", type=ParamType.STRING, required=False, description="Material number in any item"),
        ToolParameter(name="distribution_channel", type=ParamType.STRING, required=False, description="Distribution channel"),
        ToolParameter(name="order_type", type=ParamType.STRING, required=False, description="Sales order type"),
        ToolParameter(name="overall_status", type=ParamType.STRING, required=False, description="Overall processing status"),
        ToolParameter(name="delivery_status", type=ParamType.STRING, required=False, description="Overall delivery status"),
        ToolParameter(name="date_from", type=ParamType.DATE, required=False, description="Change-date start (YYYY-MM-DD)", format_hint="YYYY-MM-DD"),
        ToolParameter(name="date_to", type=ParamType.DATE, required=False, description="Change-date end (YYYY-MM-DD)", format_hint="YYYY-MM-DD"),
        ToolParameter(name="top", type=ParamType.INTEGER, required=False, description="Max records", default=500, min_value=1, max_value=5000),
        ToolParameter(name="sort_by", type=ParamType.STRING, required=False, description="Sort order: 'recent' (default, by date) or 'value' (by TotalNetAmount descending)", default="recent"),
    ],
    sap_entity_set="sales_order_snapshots",
    sap_api_path="/mcp/query",
    response_fields=[
        "SalesOrder", "SalesOrderType", "SalesOrganization", "DistributionChannel",
        "SoldToParty", "CreationDate", "TotalNetAmount", "TransactionCurrency",
        "OverallSDProcessStatus", "OverallTotalDeliveryStatus", "TotalCreditCheckStatus",
    ],
)

GET_BILLING_DB = SAPTool(
    name="get_billing_db",
    description=(
        "Query billing documents from PostgreSQL snapshots (not live SAP). "
        "Use for fast historical invoice and billing lookups."
    ),
    parameters=[
        ToolParameter(name="billing_document", type=ParamType.STRING, required=False, description="Billing document number"),
        ToolParameter(name="customer_id", type=ParamType.STRING, required=False, description="Sold-to party / payer"),
        ToolParameter(name="sales_org", type=ParamType.STRING, required=False, description="Sales organization"),
        ToolParameter(name="company_code", type=ParamType.STRING, required=False, description="Company code"),
        ToolParameter(name="billing_type", type=ParamType.STRING, required=False, description="Billing type"),
        ToolParameter(name="is_cancelled", type=ParamType.BOOLEAN, required=False, description="Cancelled only true/false"),
        ToolParameter(name="date_from", type=ParamType.DATE, required=False, description="Billing-date start (YYYY-MM-DD)", format_hint="YYYY-MM-DD"),
        ToolParameter(name="date_to", type=ParamType.DATE, required=False, description="Billing-date end (YYYY-MM-DD)", format_hint="YYYY-MM-DD"),
        ToolParameter(name="top", type=ParamType.INTEGER, required=False, description="Max records", default=500, min_value=1, max_value=5000),
    ],
    sap_entity_set="billing_snapshots",
    sap_api_path="/mcp/query",
    response_fields=[
        "BillingDocument", "BillingDocumentType", "SoldToParty", "SalesOrganization",
        "CompanyCode", "BillingDocumentDate", "TotalNetAmount", "TransactionCurrency",
        "BillingDocumentIsCancelled",
    ],
)

# ── SAP OData Tools (for data NOT in PostgreSQL) ─────────────────────────────

GET_SALES_ORDERS_SAP = SAPTool(
    name="get_sales_orders",
    description=(
        "Retrieve sales orders LIVE from SAP OData. "
        "Use ONLY when you need real-time data not yet in the database, "
        "or need expanded navigation properties (line items, partners, pricing). "
        "For analytics/aggregations, use query_database instead."
    ),
    parameters=[
        ToolParameter(name="sales_order", type=ParamType.STRING, required=False, description="Specific sales-order number"),
        ToolParameter(name="customer_id", type=ParamType.STRING, required=False, description="SAP customer / sold-to party number"),
        ToolParameter(name="sales_org", type=ParamType.STRING, required=False, description="Sales organisation code"),
        ToolParameter(name="material", type=ParamType.STRING, required=False, description="Material number"),
        ToolParameter(name="distribution_channel", type=ParamType.STRING, required=False, description="Distribution channel code"),
        ToolParameter(name="order_type", type=ParamType.STRING, required=False, description="Sales order type (e.g. OR, SO)"),
        ToolParameter(name="overall_status", type=ParamType.STRING, required=False, description="Overall processing status (A/B/C)"),
        ToolParameter(name="delivery_status", type=ParamType.STRING, required=False, description="Overall delivery status (A/B/C)"),
        ToolParameter(name="date_from", type=ParamType.DATE, required=False, description="Start date (YYYY-MM-DD)", format_hint="YYYY-MM-DD"),
        ToolParameter(name="date_to", type=ParamType.DATE, required=False, description="End date (YYYY-MM-DD)", format_hint="YYYY-MM-DD"),
        ToolParameter(name="top", type=ParamType.INTEGER, required=False, description="Max records", default=500, min_value=1, max_value=5000),
    ],
    sap_entity_set="A_SalesOrder",
    sap_api_path="/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder",
    response_fields=[
        "SalesOrder", "SalesOrderType", "SalesOrganization", "DistributionChannel",
        "SoldToParty", "CreationDate", "TotalNetAmount", "TransactionCurrency",
        "OverallSDProcessStatus", "OverallTotalDeliveryStatus",
        "OverallSDDocumentRejectionSts", "TotalCreditCheckStatus",
        "SalesOrderItem", "Material", "SalesOrderItemText",
        "RequestedQuantity", "OrderQuantity", "NetAmount", "MaterialGroup", "Plant",
    ],
    available_expands={
        "to_Item": "Line items: material, quantity, price, delivery status, item text",
        "to_Partner": "Partner functions: sold-to, ship-to, bill-to, payer details",
        "to_PricingElement": "Pricing breakdown: discounts, surcharges, freight, taxes",
        "to_Text": "Header-level text notes and comments",
        "to_PaymentPlanItemDetails": "Payment schedule and installment terms",
    },
    default_expands=["to_Item"],
)

GET_PURCHASE_ORDERS = SAPTool(
    name="get_purchase_orders",
    description=(
        "Retrieve purchase order HEADERS from SAP. "
        "Can filter by vendor, date range, purchasing org, plant, PO number, "
        "company code, PO type, or purchasing group. "
        "NOTE: GR/IR status lives at ITEM level — use get_purchase_order_items for that."
    ),
    parameters=[
        ToolParameter(
            name="purchase_order",
            type=ParamType.STRING,
            required=False,
            description="Specific purchase-order number",
        ),
        ToolParameter(
            name="vendor_id",
            type=ParamType.STRING,
            required=False,
            description="SAP vendor / supplier number",
        ),
        ToolParameter(
            name="purchasing_org",
            type=ParamType.STRING,
            required=False,
            description="Purchasing organisation code",
        ),
        ToolParameter(
            name="plant",
            type=ParamType.STRING,
            required=False,
            description="Plant code",
        ),
        ToolParameter(
            name="company_code",
            type=ParamType.STRING,
            required=False,
            description="Company code",
        ),
        ToolParameter(
            name="po_type",
            type=ParamType.STRING,
            required=False,
            description="Purchase order type (e.g. NB=standard, FO=framework)",
        ),
        ToolParameter(
            name="purchasing_group",
            type=ParamType.STRING,
            required=False,
            description="Purchasing group code",
        ),
        ToolParameter(
            name="processing_status",
            type=ParamType.STRING,
            required=False,
            description="Purchasing processing status",
        ),
        ToolParameter(
            name="date_from",
            type=ParamType.DATE,
            required=False,
            description="Start date (YYYY-MM-DD)",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="date_to",
            type=ParamType.DATE,
            required=False,
            description="End date (YYYY-MM-DD)",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=500,
            min_value=1,
            max_value=500,
        ),
    ],
    sap_entity_set="A_PurchaseOrder",
    sap_api_path="/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder",
    response_fields=[
        "PurchaseOrder",
        "PurchaseOrderType",
        "CompanyCode",
        "Supplier",
        "PurchasingOrganization",
        "PurchasingGroup",
        "PurchaseOrderDate",
        "CreationDate",
        "PurchasingProcessingStatus",
        "PurchaseOrderNetAmount",
        "DocumentCurrency",
        "AddressName",
    ],
    available_expands={
        "to_PurchaseOrderItem": "Line items: material, quantity, price, delivery dates",
        "to_PurOrdAccountAssignment": "G/L account assignments for items",
        "to_PurchaseOrderNote": "Header-level notes and comments",
    },
    default_expands=["to_PurchaseOrderItem"],
)

GET_PURCHASE_ORDER_ITEMS = SAPTool(
    name="get_purchase_order_items",
    description=(
        "Retrieve purchase order ITEMS from SAP with GR/IR status. "
        "Use this tool when the user asks about goods receipt (GR) status, "
        "invoice receipt (IR) status, delivery completion, or item-level PO details. "
        "Can filter by PO number, material, plant, GR status (IsCompletelyDelivered), "
        "or invoice status (IsFinallyInvoiced)."
    ),
    parameters=[
        ToolParameter(
            name="purchase_order",
            type=ParamType.STRING,
            required=False,
            description="Purchase-order number to get items for",
        ),
        ToolParameter(
            name="material",
            type=ParamType.STRING,
            required=False,
            description="Material number",
        ),
        ToolParameter(
            name="plant",
            type=ParamType.STRING,
            required=False,
            description="Plant code",
        ),
        ToolParameter(
            name="company_code",
            type=ParamType.STRING,
            required=False,
            description="Company code",
        ),
        ToolParameter(
            name="supplier",
            type=ParamType.STRING,
            required=False,
            description="Supplier / vendor number",
        ),
        ToolParameter(
            name="gr_complete",
            type=ParamType.BOOLEAN,
            required=False,
            description="Goods receipt complete? true=fully delivered, false=not fully delivered. Maps to IsCompletelyDelivered.",
        ),
        ToolParameter(
            name="invoice_complete",
            type=ParamType.BOOLEAN,
            required=False,
            description="Invoice complete? true=fully invoiced, false=not fully invoiced. Maps to IsFinallyInvoiced.",
        ),
        ToolParameter(
            name="gr_expected",
            type=ParamType.BOOLEAN,
            required=False,
            description="Is goods receipt expected? true/false. Maps to GoodsReceiptIsExpected.",
        ),
        ToolParameter(
            name="invoice_expected",
            type=ParamType.BOOLEAN,
            required=False,
            description="Is invoice expected? true/false. Maps to InvoiceIsExpected.",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=500,
            min_value=1,
            max_value=500,
        ),
    ],
    sap_entity_set="A_PurchaseOrderItem",
    sap_api_path="/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrderItem",
    response_fields=[
        "PurchaseOrder",
        "PurchaseOrderItem",
        "PurchaseOrderItemText",
        "Material",
        "Plant",
        "OrderQuantity",
        "PurchaseOrderQuantityUnit",
        "NetPriceAmount",
        "DocumentCurrency",
        "IsCompletelyDelivered",
        "IsFinallyInvoiced",
        "GoodsReceiptIsExpected",
        "InvoiceIsExpected",
        "AccountAssignmentCategory",
        "StorageLocation",
        "MaterialGroup",
        "Supplier",
    ],
    available_expands={
        "to_AccountAssignment": "G/L account, cost center, WBS element assignments",
        "to_ScheduleLine": "Delivery schedule lines with quantities and dates",
    },
)

GET_INVOICES = SAPTool(
    name="get_invoices",
    description=(
        "Retrieve billing documents (invoices) from SAP. "
        "Filter by customer, billing date range, sales org, document number, "
        "company code, billing type, or posting status."
    ),
    parameters=[
        ToolParameter(
            name="billing_document",
            type=ParamType.STRING,
            required=False,
            description="Billing document number",
        ),
        ToolParameter(
            name="customer_id",
            type=ParamType.STRING,
            required=False,
            description="Sold-to party / payer",
        ),
        ToolParameter(
            name="sales_org",
            type=ParamType.STRING,
            required=False,
            description="Sales organisation",
        ),
        ToolParameter(
            name="company_code",
            type=ParamType.STRING,
            required=False,
            description="Company code",
        ),
        ToolParameter(
            name="billing_type",
            type=ParamType.STRING,
            required=False,
            description="Billing document type (e.g. F2=invoice, RE=credit memo)",
        ),
        ToolParameter(
            name="is_cancelled",
            type=ParamType.BOOLEAN,
            required=False,
            description="Filter for cancelled invoices (true/false)",
        ),
        ToolParameter(
            name="date_from",
            type=ParamType.DATE,
            required=False,
            description="Billing date from (YYYY-MM-DD)",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="date_to",
            type=ParamType.DATE,
            required=False,
            description="Billing date to (YYYY-MM-DD)",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=500,
            min_value=1,
            max_value=500,
        ),
    ],
    sap_entity_set="A_BillingDocument",
    sap_api_path="/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV/A_BillingDocument",
    response_fields=[
        "BillingDocument",
        "BillingDocumentType",
        "SoldToParty",
        "SalesOrganization",
        "CompanyCode",
        "BillingDocumentDate",
        "TotalNetAmount",
        "TaxAmount",
        "TotalGrossAmount",
        "TransactionCurrency",
        "OverallBillingStatus",
        "AccountingPostingStatus",
        "AccountingTransferStatus",
        "BillingDocumentIsCancelled",
        "CustomerPaymentTerms",
        "PayerParty",
        # Item fields (from to_Item expand)
        "BillingDocumentItem", "Material", "BillingDocumentItemText",
        "BillingQuantity", "BillingQuantityUnit",
        "NetAmount", "GrossAmount",
        "Plant", "SalesDocument", "SalesDocumentItem",
    ],
    available_expands={
        "to_Item": "Line items: material, quantity, net value, reference documents",
        "to_Partner": "Partner functions: payer, bill-to, sold-to details",
        "to_PricingElement": "Pricing conditions applied to the invoice",
    },
    default_expands=["to_Item"],
)

GET_BUSINESS_PARTNERS = SAPTool(
    name="get_business_partners",
    description=(
        "Retrieve business partners (customers or vendors) from SAP. "
        "Filter by partner number, name, category, role, or blocked status."
    ),
    parameters=[
        ToolParameter(
            name="business_partner",
            type=ParamType.STRING,
            required=False,
            description="Business-partner number",
        ),
        ToolParameter(
            name="name",
            type=ParamType.STRING,
            required=False,
            description="Full or partial partner name (search term)",
        ),
        ToolParameter(
            name="category",
            type=ParamType.STRING,
            required=False,
            description="Partner category: 1=Person, 2=Organisation",
            enum=["1", "2"],
        ),
        ToolParameter(
            name="is_blocked",
            type=ParamType.BOOLEAN,
            required=False,
            description="Filter blocked partners (true/false)",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=500,
            min_value=1,
            max_value=500,
        ),
    ],
    sap_entity_set="A_BusinessPartner",
    sap_api_path="/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner",
    response_fields=[
        "BusinessPartner",
        "BusinessPartnerFullName",
        "BusinessPartnerCategory",
        "BusinessPartnerIsBlocked",
        "OrganizationBPName1",
        "SearchTerm1",
        "Customer",
        "Supplier",
        "CreationDate",
        "Industry",
        "LegalForm",
    ],
    available_expands={
        "to_BusinessPartnerAddress": "Addresses: street, city, postal code, country",
        "to_BusinessPartnerBank": "Bank account details",
        "to_Customer": "Customer-specific data and credit info",
        "to_Supplier": "Vendor-specific data and payment terms",
    },
)

GET_MATERIALS = SAPTool(
    name="get_materials",
    description=(
        "Retrieve materials / products from SAP. "
        "Filter by material number, material type, description keyword, "
        "product group, or industry sector."
    ),
    parameters=[
        ToolParameter(
            name="material",
            type=ParamType.STRING,
            required=False,
            description="Material / product number",
        ),
        ToolParameter(
            name="material_type",
            type=ParamType.STRING,
            required=False,
            description="Material type code (e.g. FERT, HAWA, ROH)",
        ),
        ToolParameter(
            name="description",
            type=ParamType.STRING,
            required=False,
            description="Material description search term (searched after retrieval)",
        ),
        ToolParameter(
            name="product_group",
            type=ParamType.STRING,
            required=False,
            description="Product / material group code",
        ),
        ToolParameter(
            name="industry_sector",
            type=ParamType.STRING,
            required=False,
            description="Industry sector code",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=500,
            min_value=1,
            max_value=500,
        ),
    ],
    sap_entity_set="A_Product",
    sap_api_path="/sap/opu/odata/sap/API_PRODUCT_SRV/A_Product",
    response_fields=[
        "Product",
        "ProductType",
        "ProductDescription",
        "BaseUnit",
        "CreationDate",
        "ProductGroup",
        "GrossWeight",
        "WeightUnit",
        "IndustrySector",
        "Division",
        "IsMarkedForDeletion",
        "Brand",
    ],
    # Legacy single expand - keep for backward compatibility
    odata_expand="to_Description",
    available_expands={
        "to_Description": "Product descriptions in multiple languages",
        "to_Plant": "Plant-specific data: MRP type, lot size, procurement",
        "to_Valuation": "Valuation data: standard price, price control, moving average",
    },
    default_expands=["to_Description"],
)

GET_DELIVERIES = SAPTool(
    name="get_deliveries",
    description=(
        "Retrieve outbound delivery documents from SAP. "
        "Filter by delivery number, ship-to/sold-to customer, date range, "
        "shipping point, delivery type, or goods movement status."
    ),
    parameters=[
        ToolParameter(
            name="delivery",
            type=ParamType.STRING,
            required=False,
            description="Delivery document number",
        ),
        ToolParameter(
            name="customer_id",
            type=ParamType.STRING,
            required=False,
            description="Ship-to party number",
        ),
        ToolParameter(
            name="sold_to_party",
            type=ParamType.STRING,
            required=False,
            description="Sold-to party number",
        ),
        ToolParameter(
            name="shipping_point",
            type=ParamType.STRING,
            required=False,
            description="SAP shipping point",
        ),
        ToolParameter(
            name="delivery_type",
            type=ParamType.STRING,
            required=False,
            description="Delivery document type (e.g. LF=delivery)",
        ),
        ToolParameter(
            name="goods_movement_status",
            type=ParamType.STRING,
            required=False,
            description="Overall goods movement status (space=not yet started, A=not yet processed, B=partially processed, C=completely processed)",
        ),
        ToolParameter(
            name="date_from",
            type=ParamType.DATE,
            required=False,
            description="Goods-movement date from",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="date_to",
            type=ParamType.DATE,
            required=False,
            description="Goods-movement date to",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=500,
            min_value=1,
            max_value=500,
        ),
    ],
    sap_entity_set="A_OutbDeliveryHeader",
    sap_api_path="/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV;v=0002/A_OutbDeliveryHeader",
    response_fields=[
        "DeliveryDocument",
        "DeliveryDocumentType",
        "ShipToParty",
        "SoldToParty",
        "ShippingPoint",
        "SalesOrganization",
        "ActualGoodsMovementDate",
        "OverallSDProcessStatus",
        "OverallGoodsMovementStatus",
        "OverallDelivReltdBillgStatus",
        "OverallPickingStatus",
        "HeaderGrossWeight",
        "HeaderNetWeight",
        "TransactionCurrency",
        # Item fields (from to_DeliveryDocumentItem expand)
        "DeliveryDocumentItem", "Material", "MaterialDescription",
        "ActualDeliveryQuantity", "DeliveryQuantityUnit",
        "ItemGrossWeight", "ItemNetWeight", "Batch",
        "StorageLocation", "Plant",
    ],
    available_expands={
        "to_DeliveryDocumentItem": "Delivery line items: material, quantity, batch, picking status",
        "to_DeliveryDocumentPartner": "Partner functions: ship-to, sold-to, forwarding agent",
    },
    default_expands=["to_DeliveryDocumentItem"],
)

GET_JOURNAL_ENTRIES = SAPTool(
    name="get_journal_entries",
    description=(
        "Retrieve financial journal entries / line items from SAP. "
        "Filter by company code, fiscal year, G/L account, cost center, "
        "profit center, or posting date range."
    ),
    parameters=[
        ToolParameter(
            name="company_code",
            type=ParamType.STRING,
            required=True,
            description="Company code (mandatory for finance queries)",
        ),
        ToolParameter(
            name="fiscal_year",
            type=ParamType.STRING,
            required=False,
            description="Fiscal year (e.g. 2026)",
        ),
        ToolParameter(
            name="gl_account",
            type=ParamType.STRING,
            required=False,
            description="G/L account number",
        ),
        ToolParameter(
            name="cost_center",
            type=ParamType.STRING,
            required=False,
            description="Cost center",
        ),
        ToolParameter(
            name="profit_center",
            type=ParamType.STRING,
            required=False,
            description="Profit center",
        ),
        ToolParameter(
            name="date_from",
            type=ParamType.DATE,
            required=False,
            description="Posting date from",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="date_to",
            type=ParamType.DATE,
            required=False,
            description="Posting date to",
            format_hint="YYYY-MM-DD",
        ),
        ToolParameter(
            name="top",
            type=ParamType.INTEGER,
            required=False,
            description="Max records",
            default=100,
            min_value=1,
            max_value=1000,
        ),
    ],
    sap_entity_set="A_JournalEntryItemBasic",
    sap_api_path="/sap/opu/odata/sap/API_JOURNALENTRYITEMBASIC_SRV/A_JournalEntryItemBasic",
    response_fields=[
        "CompanyCode",
        "CompanyCodeName",
        "FiscalYear",
        "FiscalPeriod",
        "AccountingDocument",
        "GLAccount",
        "GLAccountName",
        "CostCenter",
        "CostCenterName",
        "ProfitCenter",
        "ProfitCenterName",
        "AmountInCompanyCodeCurrency",
        "CompanyCodeCurrency",
        "AmountInTransactionCurrency",
        "TransactionCurrency",
        "Customer",
        "CustomerName",
        "Plant",
        "PlantName",
    ],
    # Journal entries typically don't have expandable navigation properties
    available_expands={},
)


