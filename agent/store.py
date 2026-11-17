"""Local persistence (SQLite): saved reports library + learned user preferences.
Production equivalent: Cloud SQL / Firestore (see docs/ARCHITECTURE.md)."""
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT NOT NULL,
    preference TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Tools may run in LangGraph's thread pool, hence the shared connection + lock.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def save_report(self, owner: str, session_id: str, title: str, content: str) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO reports (owner, session_id, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (owner, session_id, title, content, _now()),
            )
            return cur.lastrowid

    def find_reports(
        self, owner: str, session_id: str | None = None, mentioning: str | None = None, ids: list[int] | None = None
    ) -> list[sqlite3.Row]:
        """Live reports of `owner` only. Filters are ANDed."""
        sql, args = "SELECT * FROM reports WHERE owner = ? AND deleted_at IS NULL", [owner]
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id)
        if mentioning:
            sql += " AND (title LIKE ? OR content LIKE ?)"
            args += [f"%{mentioning}%"] * 2
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            args += list(ids)
        with self._lock:
            return self._db.execute(sql + " ORDER BY id", args).fetchall()

    def delete_reports(self, owner: str, ids: list[int]) -> int:
        """Soft delete (recoverable by an admin); the owner filter makes deleting others' reports impossible."""
        if not ids:
            return 0
        with self._lock, self._db:
            cur = self._db.execute(
                f"UPDATE reports SET deleted_at = ? WHERE owner = ? AND deleted_at IS NULL AND id IN ({','.join('?' * len(ids))})",
                [_now(), owner, *ids],
            )
            return cur.rowcount

    def add_preference(self, user_id: str, preference: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO preferences VALUES (?, ?, ?)", (user_id, preference, _now()))

    def preferences(self, user_id: str, limit: int = 10) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT preference FROM preferences WHERE user_id = ? ORDER BY rowid DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        return [r[0] for r in reversed(rows)]
