"""Golden Knowledge Bucket: expert (question -> SQL -> report) trios used as few-shot examples.

shortcut: lexical overlap retrieval over a local JSONL; production uses embeddings + a vector index
(Vertex AI Vector Search) over the GCS bucket once the bucket outgrows a few hundred trios.
"""
import json
import re
from datetime import datetime, timezone

from .config import DATA_DIR, GOLDEN_FILE

_STOP = frozenset("a an the of in on for to and or by is are was were what why how which who our we me my show give".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


def load_trios() -> list[dict]:
    try:
        return [json.loads(line) for line in GOLDEN_FILE.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def search(question: str, k: int = 2, min_score: float = 0.12) -> list[dict]:
    q = _tokens(question)
    if not q:
        return []
    scored = []
    for trio in load_trios():
        t = _tokens(trio["question"] + " " + " ".join(trio.get("tags", [])))
        score = len(q & t) / len(q | t)
        if score >= min_score:
            scored.append((score, trio))
    return [t for _, t in sorted(scored, key=lambda x: -x[0])[:k]]


def add_candidate(question: str, sqls: list[str], report: str, user_id: str) -> None:
    """A saved report becomes a *candidate* trio; an analyst reviews it before it is promoted to the bucket."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "question": question,
        "sql": sqls,
        "report": report,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pending_review",
    }
    with open(DATA_DIR / "golden_candidates.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
