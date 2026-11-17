"""Agent tools + per-session context. New capabilities (charts, email, web search) are added here as
one more @tool and listed in TOOLS; the graph does not change."""
import csv
import io
import time
import uuid
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt

from . import golden
from .bq import BigQueryRunner, QueryError, WarehouseUnavailable
from .config import MAX_ROWS_TO_LLM, MAX_SQL_ATTEMPTS, User
from .safety import SQLRejected, scrub, validate_and_scope
from .store import Store
from .tracing import Tracer


@dataclass
class Session:
    """Everything a tool needs about who is asking. Passed via config['configurable']['session']."""

    user: User
    store: Store
    bq: BigQueryRunner
    tracer: Tracer
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trace_id: str = ""
    question: str = ""
    sql_failures: int = 0
    turn_sqls: list[str] = field(default_factory=list)

    def new_turn(self, question: str) -> str:
        self.trace_id, self.question = uuid.uuid4().hex[:12], question
        self.sql_failures, self.turn_sqls = 0, []
        return self.trace_id

    def log(self, kind: str, **data) -> None:
        self.tracer.event(kind, trace_id=self.trace_id, session_id=self.session_id, user_id=self.user.id, **data)


def _session(config: RunnableConfig) -> Session:
    return config["configurable"]["session"]


def _to_csv(columns: list[str], rows: list[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows([[round(v, 4) if isinstance(v, float) else v for v in row] for row in rows])
    return buf.getvalue()


@tool
def run_sql(sql: str, purpose: str, config: RunnableConfig) -> str:
    """Run ONE read-only BigQuery Standard SQL SELECT on the retail dataset and return the result as CSV.

    Args:
        sql: The query. Table names may be bare (orders, order_items, products, users) or fully qualified.
        purpose: One short sentence: what business question this query answers.
    """
    s = _session(config)
    if s.sql_failures >= MAX_SQL_ATTEMPTS:
        s.log("sql", status="budget_exhausted", sql=sql)
        return (
            "STOP: the query budget for this question is used up. Do not call run_sql again. "
            "Tell the user what you tried, what went wrong in plain words, and suggest how to rephrase."
        )
    t0 = time.perf_counter()

    def fail(status: str, message: str) -> str:
        s.sql_failures += 1
        s.log("sql", status=status, sql=sql, error=message, attempt=s.sql_failures, latency_ms=int((time.perf_counter() - t0) * 1000))
        left = MAX_SQL_ATTEMPTS - s.sql_failures
        return f"ERROR ({status}): {message}\nFix the query and try again ({left} attempts left)."

    try:
        result = s.bq.run(validate_and_scope(sql, s.user.scope))
    except SQLRejected as e:
        return fail("rejected", str(e))
    except QueryError as e:
        return fail("bq_error", str(e))
    except WarehouseUnavailable as e:
        s.sql_failures = MAX_SQL_ATTEMPTS  # infra outage: retrying different SQL would only burn tokens
        s.log("sql", status="unavailable", sql=sql, error=str(e))
        return "ERROR: the data warehouse is temporarily unavailable. Do not retry. Apologise and ask the user to try again in a few minutes."

    if result.total_rows == 0:
        return fail(
            "empty",
            "The query returned 0 rows. Check filter values (they are case-sensitive, e.g. 'Jeans', 'Complete', "
            "'California'), date ranges (check MIN/MAX(created_at)) and join keys. If zero is the genuine answer, say so.",
        )

    text, masked = scrub(_to_csv(result.columns, result.rows))
    if masked:
        s.log("pii_masked", where="sql_result", count=masked)
    s.turn_sqls.append(sql)
    s.log("sql", status="ok", sql=sql, purpose=purpose, rows=result.total_rows, bytes=result.bytes_processed,
          latency_ms=int((time.perf_counter() - t0) * 1000))
    shown = f", showing first {len(result.rows)}" if result.total_rows > len(result.rows) else ""
    return f"{result.total_rows} rows{shown} ({result.bytes_processed / 1e6:.0f} MB scanned):\n{text}"


@tool
def save_report(title: str, content_markdown: str, config: RunnableConfig) -> str:
    """Save a finished report to the user's Saved Reports library. Use when the user asks to create/save a report."""
    s = _session(config)
    content, masked = scrub(content_markdown)
    report_id = s.store.save_report(s.user.id, s.session_id, title, content)
    if s.turn_sqls:
        golden.add_candidate(s.question, s.turn_sqls, content, s.user.id)
    s.log("report_saved", report_id=report_id, title=title, pii_masked=masked)
    return f"Saved report #{report_id} '{title}'."


@tool
def list_saved_reports(config: RunnableConfig, mentioning: str | None = None, this_conversation: bool = False) -> str:
    """List the user's saved reports, optionally only those mentioning a text or created in this conversation."""
    s = _session(config)
    rows = s.store.find_reports(s.user.id, s.session_id if this_conversation else None, mentioning)
    if not rows:
        return "No saved reports match."
    return "\n".join(f"#{r['id']} | {r['title']} | {r['created_at']}" for r in rows)


@tool
def delete_reports(
    config: RunnableConfig,
    report_ids: list[int] | None = None,
    mentioning: str | None = None,
    this_conversation: bool = False,
    all_my_reports: bool = False,
) -> str:
    """Delete saved reports of the current user. The system asks the user for confirmation itself;
    call this directly (alone, not in parallel with other tools) and do not ask for confirmation in text.

    Args:
        report_ids: Specific report ids.
        mentioning: Delete reports whose title or content contains this text (e.g. a client or brand name).
        this_conversation: Only reports created in the current conversation.
        all_my_reports: Must be true to delete every report of the user when no other filter is given.
    """
    s = _session(config)
    if not (report_ids or mentioning or this_conversation or all_my_reports):
        return "ERROR: no filter given. Ask the user which reports to delete."
    candidates = s.store.find_reports(s.user.id, s.session_id if this_conversation else None, mentioning, report_ids)
    if not candidates:
        return "No reports owned by this user match; nothing to delete."

    # Graph pauses here (state checkpointed). Only the human's answer, never the model, can resume it.
    decision = interrupt({
        "action": "delete_reports",
        "reports": [{"id": r["id"], "title": r["title"], "created_at": r["created_at"]} for r in candidates],
    })
    approved = isinstance(decision, dict) and decision.get("approved") is True
    s.log("hitl_decision", action="delete_reports", approved=approved, candidate_ids=[r["id"] for r in candidates])
    if not approved:
        return "The user cancelled. Nothing was deleted."
    # Delete exactly what the user saw AND still owns (re-checked after resume).
    ids = sorted(set(decision.get("ids", [])) & {r["id"] for r in candidates})
    n = s.store.delete_reports(s.user.id, ids)
    return f"Deleted {n} report(s): {', '.join(f'#{i}' for i in ids)}."


@tool
def remember_preference(preference: str, config: RunnableConfig) -> str:
    """Store a lasting preference of the user about answers (format, depth, charts vs text, units...).
    Use only when the user expresses a durable preference, e.g. 'I prefer tables' or 'keep it short'."""
    s = _session(config)
    s.store.add_preference(s.user.id, preference)
    s.log("preference_saved", preference=preference)
    return "Preference saved; it will apply to future answers."


TOOLS = [run_sql, save_report, list_saved_reports, delete_reports, remember_preference]
