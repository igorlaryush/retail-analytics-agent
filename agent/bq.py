"""BigQuery access with cost guard (dry run + bytes cap) and a small circuit breaker."""
import time
from dataclasses import dataclass

from google.api_core import exceptions as gexc

from .config import GCP_PROJECT, MAX_BYTES_BILLED, MAX_ROWS_TO_LLM


class QueryError(Exception):
    """The SQL itself is wrong (syntax, unknown column, too expensive). The model can fix it."""


class WarehouseUnavailable(Exception):
    """BigQuery/auth/network is down. Retrying with different SQL will not help."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    total_rows: int
    bytes_processed: int


def _bq_message(e: gexc.GoogleAPICallError) -> str:
    errors = getattr(e, "errors", None) or []
    return (errors[0].get("message") if errors else None) or str(e)


class BigQueryRunner:
    FAILURE_THRESHOLD, COOLDOWN_S = 3, 60

    def __init__(self, project: str | None = GCP_PROJECT):
        self._project = project
        self._client = None
        self._failures = 0
        self._open_until = 0.0

    def _get_client(self):
        if self._client is None:
            from google.cloud import bigquery  # imported lazily so tests/CLI start without GCP creds

            self._bigquery = bigquery
            self._client = bigquery.Client(project=self._project)
        return self._client

    def run(self, sql: str) -> QueryResult:
        if time.monotonic() < self._open_until:
            raise WarehouseUnavailable("BigQuery circuit breaker is open after repeated failures; try again in a minute.")
        try:
            client = self._get_client()
            bq = self._bigquery
            # Dry run is free: catches syntax/schema errors and lets us refuse expensive scans before paying.
            dry = client.query(sql, job_config=bq.QueryJobConfig(dry_run=True, use_query_cache=False))
            if dry.total_bytes_processed > MAX_BYTES_BILLED:
                raise QueryError(
                    f"Query would scan {dry.total_bytes_processed / 1e9:.1f} GB (limit {MAX_BYTES_BILLED / 1e9:.1f} GB). "
                    "Select fewer columns or filter by date."
                )
            job = client.query(
                sql,
                job_config=bq.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED, labels={"app": "retail-agent"}),
            )
            it = job.result(timeout=120, max_results=MAX_ROWS_TO_LLM)
            columns = [f.name for f in it.schema]
            rows = [tuple(r.values()) for r in it]
            result = QueryResult(columns, rows, it.total_rows or 0, job.total_bytes_processed or 0)
        except QueryError:
            raise
        except gexc.BadRequest as e:  # invalid SQL, unknown column, bytes-billed limit exceeded
            raise QueryError(_bq_message(e)[:500]) from e
        except Exception as e:  # network, auth, 5xx after the client's own retries, timeouts
            self._failures += 1
            if self._failures >= self.FAILURE_THRESHOLD:
                self._open_until = time.monotonic() + self.COOLDOWN_S
            raise WarehouseUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e
        self._failures = 0
        return result
