"""Interactive CLI. Run: python main.py --user ceo"""
import argparse
import json
import sqlite3
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .bq import BigQueryRunner
from .config import API_KEY, DATA_DIR, MODEL, RECURSION_LIMIT, load_user
from .store import Store
from .tracing import Tracer, compute_stats

console = Console()

HELP = """Ask any question about sales, customers, products or trends. Commands:
  /reports        list your saved reports        /show <id>   print a saved report
  /trace [id]     what happened in the last turn /stats       agent metrics from the trace log
  /new            start a new conversation       /exit        quit"""

STEP_LABELS = {
    "run_sql": "Querying BigQuery",
    "save_report": "Saving report",
    "list_saved_reports": "Looking up reports",
    "delete_reports": "Preparing deletion",
    "remember_preference": "Remembering preference",
}


def _callbacks():
    """Optional Langfuse tracing (LANGFUSE_PUBLIC_KEY/SECRET_KEY set + `pip install langfuse`)."""
    import os

    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception as e:  # never let observability break the product
        console.print(f"[yellow]Langfuse disabled: {e}[/]")
        return []


class ChatApp:
    def __init__(self, graph, session):
        self.graph, self.session = graph, session
        self.callbacks = _callbacks()

    @property
    def config(self):
        return {
            # thread id includes the user, so passing someone else's --session never exposes their conversation
            "configurable": {"thread_id": f"{self.session.user.id}:{self.session.session_id}", "session": self.session},
            "recursion_limit": RECURSION_LIMIT,
            "callbacks": self.callbacks,
            "metadata": {"user_id": self.session.user.id, "session_id": self.session.session_id},
        }

    def _stream(self, payload) -> list:
        """Run the graph until it finishes or pauses; print tool steps as they happen. Returns interrupts."""
        interrupts = []
        for update in self.graph.stream(payload, self.config, stream_mode="updates"):
            for node, data in update.items():
                if node == "__interrupt__":
                    interrupts.extend(data)
                elif node == "agent":
                    for call in data["messages"][-1].tool_calls:
                        detail = call["args"].get("purpose") or call["args"].get("title") or ""
                        console.print(f"  [dim]→ {STEP_LABELS.get(call['name'], call['name'])}[/] [dim italic]{detail}[/]")
        return interrupts

    def _confirm(self, payload: dict) -> dict:
        reports = payload["reports"]
        table = Table(title=f"[bold red]Confirm deletion of {len(reports)} report(s)[/]", show_lines=False)
        for col in ("id", "title", "created_at"):
            table.add_column(col)
        for r in reports:
            table.add_row(str(r["id"]), r["title"], r["created_at"])
        console.print(table)
        answer = console.input("[bold red]Type 'yes' to delete, anything else cancels:[/] ").strip().lower()
        return {"approved": answer == "yes", "ids": [r["id"] for r in reports]}

    def ask(self, question: str) -> None:
        self._turn(question, lambda: {"messages": [HumanMessage(question)]})

    def resume_pending(self) -> None:
        """A confirmation left open before a restart/crash is still checkpointed: ask it again."""
        pending = self.graph.get_state(self.config).interrupts
        if pending:
            console.print("[yellow]You have an unfinished action from your previous session:[/]")
            self._turn("(resumed pending action)", lambda: Command(resume=self._confirm(pending[0].value)))

    def _turn(self, question: str, make_payload) -> None:
        trace_id = self.session.new_turn(question)
        self.session.log("turn_start", question=question)
        t0, status, error_type, answer_text = time.perf_counter(), "ok", None, ""
        try:
            payload = make_payload()
            while interrupts := self._stream(payload):
                payload = Command(resume=self._confirm(interrupts[0].value))
            answer_text = self.graph.get_state(self.config).values["messages"][-1].text
            console.print(Panel(Markdown(answer_text or "_(no answer)_"), border_style="cyan", title="Assistant", title_align="left"))
        except GraphRecursionError:
            status, error_type = "error", "step_limit"
            self._repair_history()
            console.print("[yellow]I couldn't finish this analysis within my step budget. Try narrowing the question.[/]")
        except KeyboardInterrupt:
            status, error_type = "error", "user_abort"
            self._repair_history()
            console.print("[yellow]Stopped.[/]")
        except Exception as e:  # LLM outage after retries + fallback, etc. The CLI must never crash.
            status, error_type = "error", type(e).__name__
            self._repair_history()
            self.session.log("error", error_type=error_type, error=str(e)[:500])
            console.print(f"[red]Sorry, something went wrong on my side ({error_type}). Please try again shortly.[/] [dim]trace {trace_id}[/]")
        self.session.log("turn_end", status=status, error_type=error_type, answer=answer_text[:4000],
                         latency_ms=int((time.perf_counter() - t0) * 1000))

    def _repair_history(self):
        """If a turn died between a tool call and its result, close the dangling call so the next turn is valid."""
        try:
            messages = self.graph.get_state(self.config).values.get("messages", [])
            last = messages[-1] if messages else None
            if isinstance(last, AIMessage) and last.tool_calls:
                aborted = [ToolMessage("Aborted.", tool_call_id=c["id"]) for c in last.tool_calls]
                self.graph.update_state(self.config, {"messages": aborted}, as_node="tools")
        except Exception:
            pass

    # ---- slash commands -------------------------------------------------------------------------
    def command(self, line: str) -> bool:
        cmd, _, arg = line.partition(" ")
        s = self.session
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            console.print(HELP)
        elif cmd == "/new":
            s.session_id = uuid.uuid4().hex[:12]
            console.print("[green]New conversation started.[/]")
        elif cmd == "/reports":
            rows = s.store.find_reports(s.user.id)
            console.print("\n".join(f"#{r['id']}  {r['title']}  [dim]{r['created_at']}[/]" for r in rows) or "No saved reports.")
        elif cmd == "/show" and arg.strip().isdigit():
            rows = s.store.find_reports(s.user.id, ids=[int(arg)])
            console.print(Markdown(rows[0]["content"]) if rows else "Not found (or not yours).")
        elif cmd == "/trace":
            self._print_trace(arg.strip() or s.trace_id)
        elif cmd == "/stats":
            console.print_json(json.dumps(compute_stats(s.tracer.read())))
        else:
            console.print(HELP)
        return True

    def _print_trace(self, trace_id: str):
        events = self.session.tracer.trace(trace_id)
        if not events:
            console.print("No trace yet.")
            return
        for e in events:
            extra = {k: v for k, v in e.items() if k not in ("ts", "kind", "trace_id", "session_id", "user_id")}
            sql = extra.pop("sql", None)
            console.print(f"[bold]{e['ts'][11:23]} {e['kind']}[/] {json.dumps(extra, default=str)[:400]}")
            if sql:
                console.print(f"    [dim]{sql}[/]")


def main():
    parser = argparse.ArgumentParser(description="Retail analytics chat agent")
    parser.add_argument("--user", default="ceo", help="user id from config/users.json (simulates login)")
    parser.add_argument("--session", help="resume an earlier conversation (id is shown at startup)")
    args = parser.parse_args()
    if not API_KEY:
        raise SystemExit("Set GEMINI_API_KEY (free: https://aistudio.google.com/apikey) or OPENROUTER_API_KEY")

    from .graph import build_graph, make_model
    from .tools import Session

    user = load_user(args.user)
    session = Session(user=user, store=Store(DATA_DIR / "agent.db"), bq=BigQueryRunner(), tracer=Tracer(DATA_DIR / "traces.jsonl"))
    if args.session:
        session.session_id = args.session
    # Durable checkpoints: conversation state and pending confirmations survive restarts.
    # Same BaseCheckpointSaver interface as PostgresSaver, which production uses.
    checkpointer = SqliteSaver(sqlite3.connect(DATA_DIR / "checkpoints.db", check_same_thread=False))
    app = ChatApp(build_graph(make_model(), checkpointer=checkpointer), session)

    console.print(Panel(f"[bold]Retail Analytics Assistant[/]  ·  {user.name}  ·  model {MODEL}\n"
                        f"Data access: {user.scope or 'all products'}\n"
                        f"Conversation id: {session.session_id} (resume with --session {session.session_id})\n{HELP}",
                        border_style="cyan"))
    app.resume_pending()
    while True:
        try:
            line = console.input("\n[bold green]you ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.startswith("/"):
            if not app.command(line):
                break
            continue
        app.ask(line)
