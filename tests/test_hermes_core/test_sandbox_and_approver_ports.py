"""실행환경(CommandExecutorPort) 및 승인·감사 포트 확장 검증 (ADR-0021 D-04, D-05).

로드맵 21의 030 단계 구현 검증을 위한 10대 독립 인수 테스트 모듈.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from hermes_core.session_db import SessionDB
from hermes_core.tools import terminal_tool as terminal_module
from hermes_core.tools.approval import ApprovalOutcome, Approver
from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.ports import (
    ApproverPort,
    CommandExecutorPort,
    ControlAuditPort,
)
from hermes_core.tools.terminal_tool import terminal_tool


# ============================================================================
# 시나리오 01: ApproverPort 런타임 검사
# ============================================================================
def test_scenario_01_approver_port_runtime_check(tmp_path: Path) -> None:
    """Approver 인스턴스 및 request(argv)를 갖춘 객체가 ApproverPort 프로토콜을 통과하는지 검증."""
    db_path = tmp_path / "test.db"
    session_db = SessionDB(str(db_path))
    session_db.create_session("s1")

    approver = Approver(
        session_db=session_db,
        session_id="s1",
        ask=lambda q: "y",
        is_interactive=lambda: True,
    )
    assert isinstance(approver, ApproverPort) is True

    class CustomApprover:
        def request(self, argv: list[str]) -> ApprovalOutcome:
            return ApprovalOutcome(approved=True, decision="approved", message="")

    assert isinstance(CustomApprover(), ApproverPort) is True

    class InvalidApprover:
        def ask(self, argv: list[str]) -> bool:
            return True

    assert isinstance(InvalidApprover(), ApproverPort) is False


# ============================================================================
# 시나리오 02: CommandExecutorPort 런타임 검사
# ============================================================================
def test_scenario_02_command_executor_port_runtime_check() -> None:
    """__call__(args, cwd, timeout) 함수 및 호출 가능 객체가 CommandExecutorPort를 통과하는지 검증."""
    def fake_exec_func(args: list[str], cwd: Path, timeout: int) -> str:
        return json.dumps({"success": True})

    assert isinstance(fake_exec_func, CommandExecutorPort) is True

    class ClassExecutor:
        def __call__(self, args: list[str], cwd: Path, timeout: int) -> str:
            return json.dumps({"success": True})

    assert isinstance(ClassExecutor(), CommandExecutorPort) is True

    class NonCallableObject:
        pass

    assert isinstance(NonCallableObject(), CommandExecutorPort) is False


# ============================================================================
# 시나리오 03: ControlAuditPort 런타임 검사
# ============================================================================
def test_scenario_03_control_audit_port_runtime_check(tmp_path: Path) -> None:
    """SessionDB 인스턴스 및 필수 감사 메서드를 갖춘 객체가 ControlAuditPort를 통과하는지 검증."""
    db_path = tmp_path / "test.db"
    session_db = SessionDB(str(db_path))
    assert isinstance(session_db, ControlAuditPort) is True

    class CustomAuditStore:
        def record_control_audit(
            self,
            session_id: str,
            *,
            source: str,
            command: str,
            target: str,
            old_value: Optional[str] = None,
            new_value: Optional[str] = None,
            result: str,
            reason: Optional[str] = None,
        ) -> int:
            return 1

        def list_control_audit(self, session_id: str) -> list[dict[str, Any]]:
            return []

    assert isinstance(CustomAuditStore(), ControlAuditPort) is True

    class IncompleteAuditStore:
        def record_control_audit(
            self,
            session_id: str,
            *,
            source: str,
            command: str,
            target: str,
            old_value: Optional[str] = None,
            new_value: Optional[str] = None,
            result: str,
            reason: Optional[str] = None,
        ) -> int:
            return 1

    assert isinstance(IncompleteAuditStore(), ControlAuditPort) is False


# ============================================================================
# 시나리오 04: terminal_tool 허용 명령 실행기 위임
# ============================================================================
def test_scenario_04_terminal_tool_delegates_allowed_command_to_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """허용 명령(ls) 실행 시 주입된 command_executor가 호출되고 subprocess.run은 호출되지 않음."""
    calls: list[tuple[list[str], Path, int]] = []

    def mock_executor(args: list[str], cwd: Path, timeout: int) -> str:
        calls.append((args, cwd, timeout))
        return json.dumps({
            "success": True,
            "exit_code": 0,
            "stdout": "custom_file.txt\n",
            "stderr": "",
        })

    def forbid_subprocess(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be called when command_executor is provided")

    monkeypatch.setattr(terminal_module.subprocess, "run", forbid_subprocess)

    res_raw = terminal_tool(
        command="ls",
        workspace=tmp_path,
        command_executor=mock_executor,
    )
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["exit_code"] == 0
    assert res["stdout"] == "custom_file.txt\n"
    assert len(calls) == 1
    assert calls[0] == (["ls"], tmp_path.resolve(), 30)


# ============================================================================
# 시나리오 05: terminal_tool 미허용 명령 승인 후 실행기 위임
# ============================================================================
def test_scenario_05_terminal_tool_approves_then_delegates_to_executor(tmp_path: Path) -> None:
    """미허용 명령(pytest)이 승인되면 approver 호출 후 command_executor가 정상 실행됨."""
    approver_calls: list[list[str]] = []
    executor_calls: list[tuple[list[str], Path, int]] = []

    class MockApprover:
        def request(self, argv: list[str]) -> ApprovalOutcome:
            approver_calls.append(argv)
            return ApprovalOutcome(approved=True, decision="approved", message="")

    def mock_executor(args: list[str], cwd: Path, timeout: int) -> str:
        executor_calls.append((args, cwd, timeout))
        return json.dumps({
            "success": True,
            "exit_code": 0,
            "stdout": "5 passed in 0.1s",
            "stderr": "",
        })

    res_raw = terminal_tool(
        command="pytest tests/",
        workspace=tmp_path,
        approver=MockApprover(),
        command_executor=mock_executor,
    )
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["stdout"] == "5 passed in 0.1s"
    assert approver_calls == [["pytest", "tests/"]]
    assert len(executor_calls) == 1
    assert executor_calls[0][0] == ["pytest", "tests/"]


# ============================================================================
# 시나리오 06: terminal_tool 미허용 명령 승인 거부 시 차단
# ============================================================================
def test_scenario_06_terminal_tool_denied_command_blocks_executor(tmp_path: Path) -> None:
    """미허용 명령 승인이 거부되면 executor는 전혀 호출되지 않고 거부 메시지가 반환됨."""
    executor_called = False

    class MockApprover:
        def request(self, argv: list[str]) -> ApprovalOutcome:
            return ApprovalOutcome(
                approved=False,
                decision="denied",
                message="User denied execution of pytest",
            )

    def mock_executor(args: list[str], cwd: Path, timeout: int) -> str:
        nonlocal executor_called
        executor_called = True
        return json.dumps({"success": True})

    res_raw = terminal_tool(
        command="pytest",
        workspace=tmp_path,
        approver=MockApprover(),
        command_executor=mock_executor,
    )
    res = json.loads(res_raw)

    assert res["success"] is False
    assert res["error"] == "User denied execution of pytest"
    assert executor_called is False


# ============================================================================
# 시나리오 07: terminal_tool 보호 경로 차단 선행
# ============================================================================
def test_scenario_07_terminal_tool_protected_path_blocks_before_executor(tmp_path: Path) -> None:
    """보호 경로(.env) 접근 시 실행기 호출 전에 차단되어 실행기가 불리지 않음."""
    executor_called = False

    def mock_executor(args: list[str], cwd: Path, timeout: int) -> str:
        nonlocal executor_called
        executor_called = True
        return json.dumps({"success": True})

    res_raw = terminal_tool(
        command="ls .env",
        workspace=tmp_path,
        command_executor=mock_executor,
    )
    res = json.loads(res_raw)

    assert res["success"] is False
    assert "refers to a protected file or folder (.env*, .git)" in res["error"]
    assert executor_called is False


# ============================================================================
# 시나리오 08: terminal_tool 실행기 예외 방어
# ============================================================================
def test_scenario_08_terminal_tool_executor_exception_handling(tmp_path: Path) -> None:
    """주입된 command_executor에서 예외가 발생해도 실패 JSON으로 안전하게 감싸서 반환."""
    def broken_executor(args: list[str], cwd: Path, timeout: int) -> str:
        raise RuntimeError("Docker daemon socket connection refused")

    res_raw = terminal_tool(
        command="ls",
        workspace=tmp_path,
        command_executor=broken_executor,
    )
    res = json.loads(res_raw)

    assert res["success"] is False
    assert res["exit_code"] == -1
    assert res["error"] == "Execution error: Docker daemon socket connection refused"


# ============================================================================
# 시나리오 09: terminal_tool 기본 실행기(None) 로컬 폴백 호환성
# ============================================================================
def test_scenario_09_terminal_tool_default_fallback_monkeypatch_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """command_executor가 None이면 기존 subprocess.run monkeypatching이 100% 호환 동작."""
    subprocess_calls: list[list[str]] = []

    def mock_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="hello from local subprocess\n",
            stderr="",
        )

    monkeypatch.setattr(terminal_module.subprocess, "run", mock_run)

    res_raw = terminal_tool(
        command="echo hello",
        workspace=tmp_path,
        command_executor=None,
    )
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["stdout"] == "hello from local subprocess\n"
    assert subprocess_calls == [["echo", "hello"]]


# ============================================================================
# 시나리오 10: Lv 4 Docker 샌드박스 통합 E2E 시뮬레이션
# ============================================================================
def test_scenario_10_docker_sandbox_integration_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_default_dispatcher에 가상 Docker 샌드박스 실행기를 주입하여 dispatch E2E 검증."""
    monkeypatch.chdir(tmp_path)

    class MockDockerSandboxExecutor:
        def __init__(self, container_id: str = "c7f93a12") -> None:
            self.container_id = container_id
            self.executed_commands: list[dict[str, Any]] = []

        def __call__(self, args: list[str], cwd: Path, timeout: int) -> str:
            self.executed_commands.append({"args": args, "cwd": str(cwd), "timeout": timeout})
            return json.dumps({
                "success": True,
                "exit_code": 0,
                "stdout": f"[docker:{self.container_id}] executed {args[0]} inside container\n",
                "stderr": "",
            })

    sandbox_executor = MockDockerSandboxExecutor(container_id="hermes-sandbox-e2e")
    dispatcher = create_default_dispatcher(command_executor=sandbox_executor)

    # 1. read_file 도구는 로컬 작업공간에서 정상 실행
    test_file = tmp_path / "greeting.txt"
    test_file.write_text("Hello Docker")

    read_res_raw = dispatcher.handle_function_call(
        "read_file",
        {"path": "greeting.txt"},
    )
    read_payload = json.loads(read_res_raw)
    assert read_payload["success"] is True
    assert "Hello Docker" in read_payload["content"]

    # 2. terminal 도구는 Docker 샌드박스 격리 실행기로 투명하게 라우팅
    term_res_raw = dispatcher.handle_function_call(
        "terminal",
        {"command": "cat greeting.txt"},
    )
    term_payload = json.loads(term_res_raw)
    assert term_payload["success"] is True
    assert "[docker:hermes-sandbox-e2e] executed cat inside container" in term_payload["stdout"]
    assert len(sandbox_executor.executed_commands) == 1
    assert sandbox_executor.executed_commands[0]["args"] == ["cat", "greeting.txt"]
