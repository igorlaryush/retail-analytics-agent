"""End-to-end graph tests with a scripted LLM and a fake BigQuery: self-correction, PII masking, HITL deletion."""
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from agent.bq import QueryError, QueryResult, WarehouseUnavailable
from agent.config import User
from agent.graph import build_graph
from agent.store import Store
from agent.tools import Session
from agent.tracing import Tracer, compute_stats


class ScriptedLLM(BaseChatModel):
    script: list
    seen: list = []

    @property
    def _llm_type(self):
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


class FakeBQ:
    def __init__(self, outcomes):
        self.outcomes, self.sqls = list(outcomes), []

    def run(self, sql):
        self.sqls.append(sql)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}_{len(args)}"}])


def make(tmp_path, script, bq_outcomes=(), user="ceo", scope=None):
    store = Store(tmp_path / "t.db")
    session = Session(User(user, user, "analyst", scope), store, FakeBQ(bq_outcomes), Tracer(tmp_path / "tr.jsonl"))
    llm = ScriptedLLM(script=list(script), seen=[])
    graph = build_graph(llm)
    cfg = {"configurable": {"thread_id": session.session_id, "session": session}}
    return graph, session, cfg, llm


def ask(graph, session, cfg, text):
    session.new_turn(text)
    return graph.invoke({"messages": [HumanMessage(text)]}, cfg)


def test_self_correction_then_pii_masked_answer(tmp_path):
    rows = QueryResult(["user_id", "spend"], [(1, 10.5)], 1, 1000)
    graph, session, cfg, llm = make(
        tmp_path,
        [
            call("run_sql", sql="SELECT email FROM users", purpose="bad"),  # rejected by guard
            call("run_sql", sql="SELECT user_id, SUM(sale_price) spend FROM order_items GROUP BY 1", purpose="x"),  # BQ error
            call("run_sql", sql="SELECT user_id, SUM(sale_price) AS spend FROM order_items GROUP BY 1", purpose="ok"),
            AIMessage(content="Top customer is user 1 (reach them at vip@example.com)."),
        ],
        [QueryError("Unrecognized name: x"), rows],
    )
    out = ask(graph, session, cfg, "top customers?")
    tool_msgs = [m.content for m in out["messages"] if isinstance(m, ToolMessage)]
    assert "PII" in tool_msgs[0] and "Unrecognized name" in tool_msgs[1] and "user_id,spend" in tool_msgs[2]
    assert "example.com" not in out["messages"][-1].text and "[REDACTED_EMAIL]" in out["messages"][-1].text
    assert len(session.bq.sqls) == 2  # the PII query never reached BigQuery
    stats = compute_stats(session.tracer.read())
    assert stats["sql_status"] == {"bq_error": 1, "ok": 1, "rejected": 1}
    assert stats["sql_self_correction_success_rate"] == 1.0


def test_retry_budget_and_outage(tmp_path):
    graph, session, cfg, _ = make(
        tmp_path,
        [call("run_sql", sql="SELECT 1 AS a", purpose="p"), AIMessage(content="BigQuery is down, sorry.")],
        [WarehouseUnavailable("503")],
    )
    out = ask(graph, session, cfg, "revenue?")
    assert "temporarily unavailable" in out["messages"][-2].content
    assert session.sql_failures == 3  # further run_sql calls in this turn are refused without hitting BQ


def test_delete_requires_confirmation_and_respects_ownership(tmp_path):
    graph, session, cfg, _ = make(
        tmp_path,
        [
            call("delete_reports", this_conversation=True),
            AIMessage(content="Cancelled, nothing deleted."),
            call("delete_reports", mentioning="Acme"),
            AIMessage(content="Done."),
        ],
    )
    store = session.store
    mine = store.save_report("ceo", session.session_id, "Acme Q1", "about Acme")
    older = store.save_report("ceo", "old-session", "Acme 2023", "Acme again")
    theirs = store.save_report("alice", session.session_id, "Acme for Alice", "Acme")

    # 1) user rejects -> nothing deleted
    out = ask(graph, session, cfg, "delete all the reports we made in this conversation")
    payload = out["__interrupt__"][0].value
    assert [r["id"] for r in payload["reports"]] == [mine]
    graph.invoke(Command(resume={"approved": False, "ids": [mine]}), cfg)
    assert len(store.find_reports("ceo")) == 2

    # 2) user approves -> only own reports deleted, even if resume payload tries to sneak another id in
    out = ask(graph, session, cfg, "delete all reports mentioning Acme")
    assert {r["id"] for r in out["__interrupt__"][0].value["reports"]} == {mine, older}
    out = graph.invoke(Command(resume={"approved": True, "ids": [mine, older, theirs]}), cfg)
    assert store.find_reports("ceo") == []
    assert [r["id"] for r in store.find_reports("alice")] == [theirs]
    assert out["messages"][-1].text == "Done."


def test_empty_reply_is_retried_and_old_tool_traffic_compacted(tmp_path):
    rows = QueryResult(["n"], [(5,)], 1, 10)
    graph, session, cfg, llm = make(
        tmp_path,
        [
            call("run_sql", sql="SELECT COUNT(*) AS n FROM orders", purpose="count"),
            AIMessage(content="We have 5 orders."),
            AIMessage(content=""),  # thinking model returns nothing on the follow-up turn
            AIMessage(content="Revenue is up."),
        ],
        [rows],
    )
    ask(graph, session, cfg, "how many orders?")
    out = ask(graph, session, cfg, "and revenue?")
    assert out["messages"][-1].text == "Revenue is up."
    retry_prompt = llm.seen[-1]
    assert not any(isinstance(m, ToolMessage) for m in retry_prompt)  # previous turn's tool calls not replayed
    assert "We have 5 orders." in [m.text for m in retry_prompt]
    assert "empty" in retry_prompt[-1].text


def test_pending_confirmation_survives_restart(tmp_path):
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    def boot(script):
        """Fresh process: new graph, new DB connection, same files and conversation id."""
        saver = SqliteSaver(sqlite3.connect(tmp_path / "ckpt.db", check_same_thread=False))
        llm = ScriptedLLM(script=list(script), seen=[])
        session = Session(User("ceo", "ceo", "admin", None), Store(tmp_path / "t.db"), FakeBQ([]), Tracer(tmp_path / "tr.jsonl"),
                          session_id="conv1")
        cfg = {"configurable": {"thread_id": "ceo:conv1", "session": session}}
        return build_graph(llm, checkpointer=saver), session, cfg

    graph, session, cfg = boot([call("delete_reports", this_conversation=True)])
    rid = session.store.save_report("ceo", "conv1", "Old", "x")
    out = ask(graph, session, cfg, "delete this conversation's reports")
    assert out["__interrupt__"]
    del graph  # process dies while waiting for the user's answer

    graph, session, cfg = boot([AIMessage(content="Deleted.")])
    pending = graph.get_state(cfg).interrupts
    assert [r["id"] for r in pending[0].value["reports"]] == [rid]
    out = graph.invoke(Command(resume={"approved": True, "ids": [rid]}), cfg)
    assert out["messages"][-1].text == "Deleted." and session.store.find_reports("ceo") == []
