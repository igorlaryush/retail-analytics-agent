import pytest
import sqlglot

from agent.safety import SQLRejected, scrub, validate_and_scope

OK = "SELECT p.category, SUM(oi.sale_price) AS revenue FROM order_items oi JOIN products p ON oi.product_id = p.id GROUP BY 1"


@pytest.mark.parametrize(
    "sql, needle",
    [
        ("DELETE FROM orders WHERE true", "read-only"),
        ("SELECT 1; DROP TABLE orders", "exactly one"),
        ("SELECT email FROM users", "PII"),
        ("SELECT u.first_name, COUNT(*) FROM users u GROUP BY 1", "PII"),
        ("SELECT * FROM users", "SELECT *"),
        ("SELECT u.* FROM users u", "SELECT *"),
        ("SELECT TO_JSON_STRING(u) FROM users u", "whole table row"),
        ("SELECT id FROM `bigquery-public-data.thelook_ecommerce.events`", "not allowed"),
        ("SELECT id FROM `other-project.thelook_ecommerce.users`", "not allowed"),
        ("SELECT table_name FROM thelook_ecommerce.INFORMATION_SCHEMA.TABLES", "not allowed"),
        ("SELEC id FRM users", "syntax"),
    ],
)
def test_rejects(sql, needle):
    with pytest.raises(SQLRejected, match=needle):
        validate_and_scope(sql, None)


def test_admin_gets_fully_qualified_tables():
    out = validate_and_scope(OK, None)
    assert "`bigquery-public-data.thelook_ecommerce.order_items`" in out
    assert "WHERE p.department" not in out
    sqlglot.parse_one(out, read="bigquery")


def test_scoped_user_cannot_escape_filter():
    out = validate_and_scope("SELECT SUM(sale_price) FROM order_items", {"department": ["Women"]})
    # even a query that never joins products is restricted to the user's products
    assert "p.department IN ('Women')" in out
    out = validate_and_scope("SELECT COUNT(DISTINCT id) FROM users", {"category": ["Jeans", "Outerwear & Coats"]})
    assert "p.category IN ('Jeans', 'Outerwear & Coats')" in out and "WHERE id IN" in out


def test_cte_and_count_star_allowed():
    sql = "WITH m AS (SELECT user_id FROM orders) SELECT COUNT(*) FROM m"
    out = validate_and_scope(sql, {"brand": ["O'Neill"]})
    assert "'O\\'Neill'" in out or "'O''Neill'" in out  # quote escaped


def test_scrub():
    text, n = scrub("contact jane.doe@example.com or (555) 123-4567; revenue 1234567.89, order 98765")
    assert n == 2 and "example.com" not in text and "1234567.89" in text and "98765" in text


def test_star_allowed_over_cte_and_subquery_only():
    ok = [
        "WITH s AS (SELECT category, SUM(sale_price) AS rev FROM order_items oi JOIN products p ON oi.product_id = p.id GROUP BY 1) SELECT * FROM s",
        "WITH s AS (SELECT user_id FROM orders) SELECT i.*, 1 AS x FROM s i",
        "SELECT * FROM (SELECT age, state FROM users)",
        "WITH r AS (SELECT status FROM orders) (SELECT 'a' AS g, * FROM r LIMIT 1) UNION ALL (SELECT 'b' AS g, * FROM r LIMIT 1)",
    ]
    for sql in ok:
        validate_and_scope(sql, {"department": ["Women"]})
    for sql in ["SELECT * FROM orders o JOIN (SELECT 1 AS k) q ON true", "WITH s AS (SELECT 1 AS k) SELECT u.* FROM users u JOIN s ON true"]:
        with pytest.raises(SQLRejected, match=r"\*"):
            validate_and_scope(sql, None)
