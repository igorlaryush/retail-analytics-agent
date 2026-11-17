"""Structured tracing: every turn, LLM call, tool call, SQL attempt and HITL decision is one JSON line.

The JSONL file is the source for `/trace` (deep dive into one turn) and `/stats` (agent-level metrics).
In production the same events go to Cloud Logging -> BigQuery sink + Langfuse (see docs).
"""
import json
import statistics
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


class Tracer:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def event(self, kind: str, **data) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, **data}
        with self._lock, open(self.path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def trace(self, trace_id: str) -> list[dict]:
        return [e for e in self.read() if e.get("trace_id") == trace_id]


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def compute_stats(events: list[dict]) -> dict:
    turns = [e for e in events if e["kind"] == "turn_end"]
    sql = [e for e in events if e["kind"] == "sql"]
    llm = [e for e in events if e["kind"] == "llm_call"]
    hitl = [e for e in events if e["kind"] == "hitl_decision"]

    sql_by_trace = defaultdict(list)
    for e in sql:
        sql_by_trace[e["trace_id"]].append(e["status"])
    # infra outages are not SQL mistakes, so they do not count against self-correction
    had_error = [s for s in sql_by_trace.values() if any(x in ("rejected", "bq_error", "empty") for x in s)]
    recovered = [s for s in had_error if "ok" in s]
    latencies = [e["latency_ms"] for e in turns]

    return {
        "turns": len(turns),
        "turn_success_rate": round(sum(e["status"] == "ok" for e in turns) / len(turns), 3) if turns else 0,
        "turn_latency_p50_ms": round(statistics.median(latencies)) if latencies else 0,
        "turn_latency_p95_ms": round(_pct(latencies, 0.95)),
        "llm_calls": len(llm),
        "llm_fallback_calls": sum(1 for e in llm if e.get("fallback")),
        "tokens_in": sum(e.get("tokens_in", 0) for e in llm),
        "tokens_out": sum(e.get("tokens_out", 0) for e in llm),
        "sql_queries": len(sql),
        "sql_status": dict(sorted({s: sum(e["status"] == s for e in sql) for s in {e["status"] for e in sql}}.items())),
        "sql_self_correction_success_rate": round(len(recovered) / len(had_error), 3) if had_error else None,
        "bq_gb_scanned": round(sum(e.get("bytes", 0) for e in sql) / 1e9, 3),
        "pii_masked_values": sum(e.get("count", 0) for e in events if e["kind"] == "pii_masked"),
        "hitl_approved": sum(1 for e in hitl if e["approved"]),
        "hitl_rejected": sum(1 for e in hitl if not e["approved"]),
        "errors_by_type": dict(
            sorted({t: sum(e.get("error_type") == t for e in turns) for t in {e.get("error_type") for e in turns} if t}.items())
        ),
    }
