FILTER_DIMENSIONS = {
    "sales_order_type": "SalesOrderType",
    "sold_to_party": "SoldToParty",
    "purchase_order_by_customer": "PurchaseOrderByCustomer",
    "sales_organization": "SalesOrganization",
    "distribution_channel": "DistributionChannel",
    "sales_group": "SalesGroup",
}

# Extended dimensions used by Order Wise Report (not in cluster_daily)
EXTENDED_DIMENSIONS = {
    "sales_office": "SalesOffice",
    "sales_district": "SalesDistrict",
    "customer_group": "CustomerGroup",
}

DEFAULT_TENANT_ID = "TENANT_001"
SALES_DATASET_KEY = "sales_orders"
MAX_FILTER_OPTIONS = 200


def normalize_dimension_value(value):
    if value is None:
        return "UNKNOWN"

    value_str = str(value).strip()
    if not value_str or value_str.lower() == "none":
        return "UNKNOWN"

    return value_str
