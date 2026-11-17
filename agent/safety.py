"""Deterministic safety layer. The LLM is never trusted with security:

1. validate_and_scope(): parse the model's SQL (sqlglot AST), allow one read-only SELECT over the 4
   allowed tables, reject any reference to PII columns, then rewrite every table reference into a
   subquery filtered to the user's product scope (row-level security the model cannot bypass).
2. scrub(): regex-mask emails / phone numbers in tool results and in the final answer (defence in depth).
"""
import re

import sqlglot
from sqlglot import exp

from .config import DATASET
from .schema import PII_COLUMNS, TABLES

ALLOWED_TABLES = frozenset(TABLES)
SCOPE_COLUMNS = frozenset({"department", "category", "brand"})
_CATALOG, _DB = DATASET.split(".")
_FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command)


class SQLRejected(ValueError):
    """The query violates policy or does not parse. Message is safe to show the model."""


def _fq(table: str) -> str:
    return f"`{DATASET}.{table}`"


def _scope_predicate(scope: dict) -> str:
    parts = []
    for col, values in scope.items():
        if col not in SCOPE_COLUMNS or not values:
            raise ValueError(f"Bad scope config: {col}={values}")
        literals = ", ".join(exp.Literal.string(str(v)).sql("bigquery") for v in values)
        parts.append(f"p.{col} IN ({literals})")
    return " AND ".join(parts)


def _scoped_source(table: str, scope: dict | None) -> str:
    """SQL that replaces a table reference: the full table for admins, else only rows tied to the user's products."""
    if not scope:
        return f"SELECT * FROM {_fq(table)}"
    pred = _scope_predicate(scope)
    items = f"FROM {_fq('order_items')} AS oi JOIN {_fq('products')} AS p ON oi.product_id = p.id WHERE {pred}"
    return {
        "products": f"SELECT * FROM {_fq('products')} AS p WHERE {pred}",
        "order_items": f"SELECT oi.* {items}",
        "orders": f"SELECT * FROM {_fq('orders')} WHERE order_id IN (SELECT oi.order_id {items})",
        "users": f"SELECT * FROM {_fq('users')} WHERE id IN (SELECT oi.user_id {items})",
    }[table]


def _star_hits_base_table(star: exp.Star, cte_names: set, base_aliases: set) -> bool:
    """`*` is only dangerous when it expands a base table (could include PII columns).
    Over CTEs / subqueries every column was already named explicitly and checked."""
    if isinstance(star.parent, exp.Count):
        return False
    if isinstance(star.parent, exp.Column) and star.parent.table:  # t.*
        return star.parent.table.lower() in base_aliases
    select = star.find_ancestor(exp.Select)
    from_ = select.args.get("from_") or select.args.get("from")  # key name differs across sqlglot versions
    if from_ is None:
        return True  # fail closed: if we cannot see the source, do not allow *
    return any(
        isinstance(src.this, exp.Table) and not (src.this.name.lower() in cte_names and not src.this.db)
        for src in [from_, *(select.args.get("joins") or [])]
    )


def validate_and_scope(sql: str, scope: dict | None) -> str:
    """Return executable SQL or raise SQLRejected with a message the model can act on."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SQLRejected(f"SQL syntax error: {str(e)[:300]}") from e
    if len(statements) != 1:
        raise SQLRejected("Send exactly one SQL statement.")
    tree = statements[0]
    if not isinstance(tree, exp.Query) or any(tree.find_all(*_FORBIDDEN_NODES)):
        raise SQLRejected("Only read-only SELECT queries are allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables, row_aliases = [], set()
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in cte_names and not t.db:
            continue
        if name not in ALLOWED_TABLES or t.db not in ("", _DB) or t.catalog not in ("", _CATALOG):
            raise SQLRejected(f"Table {t.sql('bigquery')} is not allowed. Allowed tables: {', '.join(sorted(ALLOWED_TABLES))}.")
        tables.append(t)
        row_aliases.add(t.alias_or_name.lower())

    for col in tree.find_all(exp.Column):
        if col.name.lower() in PII_COLUMNS:
            raise SQLRejected(f"Column '{col.name}' is personal data (PII) and cannot be queried. Identify customers by user_id.")
        if not col.table and col.name.lower() in row_aliases:
            raise SQLRejected(f"Selecting a whole table row ('{col.name}') is not allowed; list the columns you need.")
    for star in tree.find_all(exp.Star):
        if _star_hits_base_table(star, cte_names, row_aliases):
            raise SQLRejected("SELECT * over a base table is not allowed; list the columns you need explicitly.")

    for t in tables:
        source = sqlglot.parse_one(_scoped_source(t.name.lower(), scope), read="bigquery")
        t.replace(source.subquery(t.alias_or_name))
    return tree.sql(dialect="bigquery")


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.])")


def scrub(text: str) -> tuple[str, int]:
    """Mask emails and phone numbers. Returns (masked_text, number_of_masks)."""
    text, n1 = _EMAIL.subn("[REDACTED_EMAIL]", text)
    text, n2 = _PHONE.subn("[REDACTED_PHONE]", text)
    return text, n1 + n2
