"""포트 적합성 시험 (시나리오 28 및 R7 도구 포트)."""

import inspect
import json
from pathlib import Path
from typing import Any, Optional

from hermes_core.agent.agent import MockLLMClient
from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.ports import LLMPort, ToolExecutor, TranscriptStore
from hermes_core.kernel.tool_call import ToolCall
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import Approver
from hermes_core.tools.checkpoint import Checkpointer
from hermes_core.tools.dispatcher import ToolDispatcher
from hermes_core.tools.ports import (
    ApprovalStorePort,
    ApproverPort,
    CheckpointStorePort,
    CommandExecutorPort,
    ControlAuditPort,
    DelegateStorePort,
    SessionSearchPort,
)
from hermes_core.tools.session_search_tool import session_search_tool


def test_scenario_28_ports_conformance() -> None:
    """시나리오 28: 포트 적합성 및 실제 호출 바인딩 검증."""
    # 1. issubclass (runtime_checkable)
    assert issubclass(SessionDB, TranscriptStore), "SessionDB must satisfy TranscriptStore"
    assert issubclass(ToolDispatcher, ToolExecutor), "ToolDispatcher must satisfy ToolExecutor"
    assert issubclass(MockLLMClient, LLMPort), "MockLLMClient must satisfy LLMPort"
    assert issubclass(OpenRouterClient, LLMPort), "OpenRouterClient must satisfy LLMPort"

    # 2. inspect.signature().bind() 호출 형태 검증
    # append_message
    sig_append = inspect.signature(SessionDB.append_message)
    # (session_id, role, content)
    sig_append.bind(None, "s1", "user", "hi")
    # (session_id, role, content, tool_calls)
    sig_append.bind(None, "s1", "assistant", content=None, tool_calls=[])
    # (session_id, role, content, tool_call_id, tool_name)
    sig_append.bind(None, "s1", "tool", content="ok", tool_call_id="c1", tool_name="echo")

    # archive_and_compact
    sig_archive = inspect.signature(SessionDB.archive_and_compact)
    sig_archive.bind(None, "s1", [], summary_index=0)

    # update_token_counts
    sig_tokens = inspect.signature(SessionDB.update_token_counts)
    sig_tokens.bind(None, "s1", input_tokens=10, output_tokens=5)

    # chat_completion
    sig_mock_chat = inspect.signature(MockLLMClient.chat_completion)
    sig_mock_chat.bind(None, messages=[], tools=[])

    sig_openrouter_chat = inspect.signature(OpenRouterClient.chat_completion)
    sig_openrouter_chat.bind(None, messages=[], tools=[])

    # dispatch
    sig_dispatch = inspect.signature(ToolDispatcher.dispatch)
    fake_call = ToolCall(name="test", args={}, task_id="t1", scope=TurnScope(session_id="s1"))
    sig_dispatch.bind(None, fake_call)


def test_scenario_r7_tools_ports_conformance() -> None:
    """시나리오 R7-1: 도구 계층 포트 4종 적합성 및 SessionDB 시그니처 바인딩 검증."""
    # 1. issubclass (runtime_checkable)
    assert issubclass(SessionDB, ApprovalStorePort), "SessionDB must satisfy ApprovalStorePort"
    assert issubclass(SessionDB, CheckpointStorePort), "SessionDB must satisfy CheckpointStorePort"
    assert issubclass(SessionDB, DelegateStorePort), "SessionDB must satisfy DelegateStorePort"
    assert issubclass(SessionDB, SessionSearchPort), "SessionDB must satisfy SessionSearchPort"

    # 2. inspect.signature().bind() 바인딩 검증
    # ApprovalStorePort
    sig_record_appr = inspect.signature(SessionDB.record_approval_request)
    sig_record_appr.bind(None, "s1", ["rm", "-rf", "/"])
    sig_record_appr.bind(None, "s1", ["git", "status"], tool_name="terminal")

    sig_resolve_appr = inspect.signature(SessionDB.resolve_approval_request)
    sig_resolve_appr.bind(None, 1, "approved")
    sig_resolve_appr.bind(None, 1, "denied", reason="user said no")

    # CheckpointStorePort
    sig_record_chk = inspect.signature(SessionDB.record_file_checkpoint)
    sig_record_chk.bind(None, "s1", 1, "write_file", "/tmp", "a.txt", True)
    sig_record_chk.bind(None, "s1", 1, "write_file", "/tmp", "a.txt", True, before_content="hello")

    sig_update_chk = inspect.signature(SessionDB.update_file_checkpoint)
    sig_update_chk.bind(None, 1, "applied")
    sig_update_chk.bind(None, 1, "applied", after_sha256="abc", reason="ok")

    sig_get_chk = inspect.signature(SessionDB.get_file_checkpoints)
    sig_get_chk.bind(None, "s1")
    sig_get_chk.bind(None, "s1", turn_index=1)

    sig_get_appr = inspect.signature(SessionDB.get_approval_requests)
    sig_get_appr.bind(None, "s1")

    sig_count_children = inspect.signature(SessionDB.count_child_sessions)
    sig_count_children.bind(None, "s1")
    sig_count_children.bind(None, "s1", started_at_min=100.0)
    sig_count_children.bind(None, "s1", started_at_min=100.0, started_at_max=200.0)

    # DelegateStorePort
    sig_end_sess = inspect.signature(SessionDB.end_session)
    sig_end_sess.bind(None, "s1")
    sig_end_sess.bind(None, "s1", end_reason="completed")

    sig_get_sess = inspect.signature(SessionDB.get_session)
    sig_get_sess.bind(None, "s1")

    # SessionSearchPort
    sig_search = inspect.signature(SessionDB.search_messages)
    sig_search.bind(None, "query")
    sig_search.bind(None, "query", source_filter=["cli"], role_filter=["user"], limit=10, offset=0)


def test_scenario_r7_count_child_sessions_and_checkpoint(tmp_path: Path) -> None:
    """시나리오 R7-2~4: SessionDB.count_child_sessions 3개 분기 및 Checkpointer 연동 검증."""
    db = SessionDB(str(tmp_path / "test.db"))
    db.create_session("p1", source="cli")
    db.create_session("p2", source="cli")

    t1 = 1000.0
    t2 = 2000.0
    db._conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at) VALUES (?, ?, ?, ?)",
        ("c1", "subagent", "p1", t1),
    )
    db._conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at) VALUES (?, ?, ?, ?)",
        ("c2", "subagent", "p1", t2),
    )
    db._conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at) VALUES (?, ?, ?, ?)",
        ("other", "subagent", "p2", t1),
    )
    db._conn.commit()

    # 시나리오 2: started_at_min=None
    assert db.count_child_sessions("p1") == 2
    assert db.count_child_sessions("none") == 0

    # 시나리오 3: 구간 조회
    assert db.count_child_sessions("p1", started_at_min=t2) == 1
    assert db.count_child_sessions("p1", started_at_min=t1, started_at_max=t2) == 1
    assert db.count_child_sessions("p1", started_at_min=t1, started_at_max=t2 + 100) == 2

    # 시나리오 4: Checkpointer.plan_rollback 연동
    chk_turn_db = SessionDB(str(tmp_path / "chk_test.db"))
    chk_turn_db.create_session("p_chk", source="cli")
    checkpointer = Checkpointer(db=chk_turn_db, session_id="p_chk", workspace=tmp_path)
    checkpointer.begin_turn(1)
    cid = chk_turn_db.record_file_checkpoint(
        session_id="p_chk",
        turn_index=1,
        tool_name="write_file",
        workspace=str(tmp_path),
        rel_path="a.txt",
        before_exists=False,
    )
    chk_turn_db.update_file_checkpoint(cid, status="applied", after_sha256="a" * 64)
    chk_turn_db._conn.execute(
        "INSERT INTO sessions (id, source, parent_session_id, started_at) VALUES (?, ?, ?, ?)",
        ("c_chk", "subagent", "p_chk", checkpointer.turn_started_at + 1.0),
    )
    chk_turn_db._conn.commit()

    plan = checkpointer.plan_rollback()
    assert plan.subagent_count == 1
    assert plan.has_warnings is True


def test_scenario_r7_fake_stores_standalone_without_session_db() -> None:
    """시나리오 R7-5~6: 순수 인메모리 Fake 포트로 Approver 및 session_search_tool 구동 검증."""
    # 시나리오 5: Approver with FakeApprovalStore
    class FakeApprovalStore:
        def __init__(self) -> None:
            self.records: list[tuple[str, list[str], str]] = []
            self.resolutions: list[tuple[int, str, Optional[str]]] = []

        def record_approval_request(
            self, session_id: str, argv: list[str], tool_name: str = "terminal"
        ) -> int:
            self.records.append((session_id, argv, tool_name))
            return 99

        def resolve_approval_request(
            self, request_id: int, decision: str, reason: Optional[str] = None
        ) -> bool:
            self.resolutions.append((request_id, decision, reason))
            return True

    fake_appr_store = FakeApprovalStore()
    approver = Approver(
        session_db=fake_appr_store,
        session_id="fake_s1",
        ask=lambda p: "y",
        is_interactive=lambda: True,
    )
    outcome = approver.request(["rm", "-rf", "test"])
    assert outcome.approved is True
    assert len(fake_appr_store.records) == 1
    assert fake_appr_store.records[0] == ("fake_s1", ["rm", "-rf", "test"], "terminal")
    assert len(fake_appr_store.resolutions) == 1
    assert fake_appr_store.resolutions[0][0] == 99
    assert fake_appr_store.resolutions[0][1] == "approved"

    # 시나리오 6: session_search_tool with FakeSearchStore
    class FakeSearchStore:
        def search_messages(
            self,
            query: str,
            source_filter: Optional[list[str]] = None,
            role_filter: Optional[list[str]] = None,
            limit: int = 20,
            offset: int = 0,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "session_id": "fake_search_s1",
                    "snippet": f"found query={query}",
                    "role": "user",
                }
            ]

    fake_search_store = FakeSearchStore()
    result_json = session_search_tool("keyword", db=fake_search_store)
    result = json.loads(result_json)
    assert result["success"] is True
    assert result["count"] == 1
    assert "found query=keyword" in result["results"][0]["snippet"]


def test_scenario_r20_020_child_runner_port_conformance() -> None:
    """시나리오 20-020-2: ChildRunnerPort 적합성 및 run_child_session 바인딩 검증."""
    from hermes_core.assembly import create_child_runner, run_child_session
    from hermes_core.tools.ports import ChildRunnerPort

    assert isinstance(run_child_session, ChildRunnerPort), "run_child_session must satisfy ChildRunnerPort"
    assert create_child_runner() is run_child_session, "create_child_runner() must return run_child_session"

    sig_runner = inspect.signature(run_child_session)
    sig_runner.bind(
        "goal",
        context="ctx",
        session_db=None,
        parent_session_id="p1",
        llm_client=None,
        max_iterations=10,
        dispatcher=None,
    )


def test_scenario_r21_ports_conformance() -> None:
    """시나리오 R21-040 (시나리오 5~7): ApproverPort, ControlAuditPort, CommandExecutorPort 적합성 및 바인딩 검증."""
    # 시나리오 5: ApproverPort 런타임 적합성 및 바인딩
    assert issubclass(Approver, ApproverPort), "Approver must satisfy ApproverPort"
    approver_instance = Approver(
        session_db=None,  # type: ignore[arg-type]
        session_id="s1",
        ask=lambda p: "y",
        is_interactive=lambda: False,
    )
    assert isinstance(approver_instance, ApproverPort), "Approver instance must satisfy ApproverPort"

    sig_appr = inspect.signature(Approver.request)
    sig_appr.bind(None, ["rm", "-rf", "/"])

    # 시나리오 6: ControlAuditPort 런타임 적합성 및 바인딩
    assert issubclass(SessionDB, ControlAuditPort), "SessionDB must satisfy ControlAuditPort"

    sig_rec = inspect.signature(SessionDB.record_control_audit)
    sig_rec.bind(
        None, "s1", source="control_panel", command="pause", target="agent", result="success"
    )
    sig_rec.bind(
        None,
        "s1",
        source="control_panel",
        command="budget",
        target="tokens",
        old_value="10",
        new_value="20",
        result="success",
        reason="user",
    )

    sig_list = inspect.signature(SessionDB.list_control_audit)
    sig_list.bind(None, "s1")

    # 시나리오 7: CommandExecutorPort 콜러블 적합성 및 바인딩
    def dummy_exec(args: list[str], cwd: Path, timeout: int) -> str:
        return "{}"

    class DummyExecutor:
        def __call__(self, args: list[str], cwd: Path, timeout: int) -> str:
            return "{}"

    assert isinstance(dummy_exec, CommandExecutorPort), "Function must satisfy CommandExecutorPort"
    assert isinstance(DummyExecutor(), CommandExecutorPort), "Callable instance must satisfy CommandExecutorPort"
    assert issubclass(DummyExecutor, CommandExecutorPort), "Callable class must satisfy CommandExecutorPort"

    sig_exec = inspect.signature(CommandExecutorPort.__call__)
    sig_exec.bind(None, ["echo", "1"], Path("/tmp"), 10)


