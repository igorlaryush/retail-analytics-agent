"""Static description of the 4 allowed tables. Kept in code so schema questions cost no BigQuery call."""

TABLES = {
    "orders": (
        "One row per customer order.",
        {
            "order_id": "INTEGER, primary key",
            "user_id": "INTEGER, -> users.id",
            "status": "STRING: Complete | Cancelled | Returned | Shipped | Processing",
            "gender": "STRING: M | F (customer gender)",
            "created_at": "TIMESTAMP, order placed",
            "returned_at": "TIMESTAMP",
            "shipped_at": "TIMESTAMP",
            "delivered_at": "TIMESTAMP",
            "num_of_item": "INTEGER, items in the order",
        },
    ),
    "order_items": (
        "One row per item sold (the revenue table).",
        {
            "id": "INTEGER, primary key",
            "order_id": "INTEGER, -> orders.order_id",
            "user_id": "INTEGER, -> users.id",
            "product_id": "INTEGER, -> products.id",
            "inventory_item_id": "INTEGER",
            "status": "STRING: Complete | Cancelled | Returned | Shipped | Processing",
            "created_at": "TIMESTAMP",
            "shipped_at": "TIMESTAMP",
            "delivered_at": "TIMESTAMP",
            "returned_at": "TIMESTAMP",
            "sale_price": "FLOAT, price paid in USD",
        },
    ),
    "products": (
        "Product catalog.",
        {
            "id": "INTEGER, primary key",
            "cost": "FLOAT, unit cost in USD",
            "category": "STRING, e.g. Jeans, Tops & Tees, Outerwear & Coats, Swim, Intimates, Accessories",
            "name": "STRING, product name",
            "brand": "STRING",
            "retail_price": "FLOAT, list price in USD",
            "department": "STRING: Men | Women",
            "sku": "STRING",
            "distribution_center_id": "INTEGER",
        },
    ),
    "users": (
        "Customers (demographics).",
        {
            "id": "INTEGER, primary key",
            "age": "INTEGER",
            "gender": "STRING: M | F",
            "state": "STRING, full name, e.g. California, Texas",
            "city": "STRING",
            "country": "STRING, e.g. United States, China, Brasil",
            "traffic_source": "STRING: Search | Organic | Facebook | Email | Display",
            "created_at": "TIMESTAMP, signup time",
            "first_name": "PII - not queryable",
            "last_name": "PII - not queryable",
            "email": "PII - not queryable",
            "street_address": "PII - not queryable",
            "postal_code": "PII - not queryable",
            "latitude": "PII - not queryable",
            "longitude": "PII - not queryable",
            "user_geom": "PII - not queryable",
        },
    ),
}

PII_COLUMNS = frozenset(
    {"first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom"}
)


def schema_prompt() -> str:
    lines = []
    for table, (desc, cols) in TABLES.items():
        lines.append(f"- {table}: {desc}")
        lines += [f"    {c}: {d}" for c, d in cols.items()]
    return "\n".join(lines)
