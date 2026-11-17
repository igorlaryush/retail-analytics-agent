"""Runtime settings. Everything tunable comes from env vars or files in config/ (no redeploy needed)."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = "bigquery-public-data.thelook_ecommerce"

# Provider: OpenRouter if its key is set (one key, any vendor, cross-vendor fallback), else Gemini via AI Studio.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PROVIDER = "openrouter" if OPENROUTER_API_KEY else "gemini"
if PROVIDER == "openrouter":
    MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-4-hydrogen")
    FALLBACK_MODEL = os.getenv("OPENROUTER_FALLBACK_MODEL", "anthropic/claude-haiku-5.5")
    API_KEY = OPENROUTER_API_KEY
else:
    MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")
    FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
    API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")  # billing project for BigQuery jobs; None = ADC default

MAX_BYTES_BILLED = int(os.getenv("MAX_BYTES_BILLED", 2 * 10**9))  # hard cap per query (2 GB)
MAX_SQL_ATTEMPTS = int(os.getenv("MAX_SQL_ATTEMPTS", 3))  # failed/empty queries allowed per user turn
MAX_ROWS_TO_LLM = 100
HISTORY_MESSAGES = 40  # conversation window sent to the LLM
RECURSION_LIMIT = 30  # max graph steps per turn

DATA_DIR = Path(os.getenv("AGENT_DATA_DIR", ROOT / "data"))
PERSONA_FILE = ROOT / "config" / "persona.md"
USERS_FILE = ROOT / "config" / "users.json"
GOLDEN_FILE = ROOT / "golden" / "trios.jsonl"


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str
    scope: dict | None  # product filter, e.g. {"department": ["Women"]}; None = all products


def load_user(user_id: str) -> User:
    users = json.loads(USERS_FILE.read_text())
    if user_id not in users:
        raise SystemExit(f"Unknown user '{user_id}'. Known users: {', '.join(users)}")
    u = users[user_id]
    return User(id=user_id, name=u["name"], role=u.get("role", "analyst"), scope=u.get("scope"))


def load_persona() -> str:
    """Re-read on every turn so a non-developer can edit config/persona.md while the agent runs."""
    try:
        return PERSONA_FILE.read_text().strip()
    except OSError:
        return ""
