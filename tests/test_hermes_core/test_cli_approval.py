"""CLI 승인 흐름 연동 테스트 (로드맵 10 / 구현지시서 040 인수 시나리오 1~17)."""

import io
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Optional

import pytest

from hermes_core import cli
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.constants import (
    APPROVAL_DENIED_MESSAGE,
    APPROVAL_HEADER,
    APPROVAL_PROMPT,
    CLI_APPROVAL_DISABLED_NOTICE,
    CLI_APPROVAL_ENABLED_NOTICE,
    CLI_USER_PROMPT,
    OPENROUTER_API_KEY_ENV,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import format_command_for_display

FAKE_API_KEY = "FAKE-NOT-A-REAL-KEY-0002"


class ScriptableClient:
    """테스트용 대본형 가짜 OpenRouter 클라이언트."""

    created_instances: list["ScriptableClient"] = []
    responses_by_instance: dict[int, list[Any]] = {}

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self.model = model
        self.instance_idx = len(ScriptableClient.created_instances)
        ScriptableClient.created_instances.append(self)
        self.call_history: list[dict[str, Any]] = []
        self.responses = list(ScriptableClient.responses_by_instance.get(self.instance_idx, []))
        self._call_recorder = None

    def set_call_recorder(self, recorder: Any) -> None:
        self._call_recorder = recorder

    def chat_completion(
        self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None
    ) -> Any:
        self.call_history.append({"messages": messages, "tools": tools})
        if self._call_recorder:
            self._call_recorder({
                "model": self.model,
                "status_code": 200,
                "input_tokens": 10,
                "output_tokens": 5,
            })
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return MockLLMResponse(content="[Scriptable] default response")


@pytest.fixture(autouse=True)
def reset_scriptable_client():
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()
    yield
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()


@pytest.fixture
def cli_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI 테스트 환경 픽스처."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_API_KEY)
    monkeypatch.setattr(cli, "OpenRouterClient", ScriptableClient)
    monkeypatch.chdir(tmp_path)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


class FakeInput:
    """사용자 입력 및 승인 프롬프트 입력을 가로채는 대본형 가짜 입력 핸들러."""

    def __init__(self, user_inputs: list[Any], approval_answers: list[Any] = None) -> None:
        self.user_inputs = list(user_inputs)
        self.approval_answers = list(approval_answers or [])
        self.recorded_prompts: list[str] = []

    def __call__(self, prompt: str = CLI_USER_PROMPT, **kwargs: Any) -> str:
        self.recorded_prompts.append(prompt)
        if prompt == APPROVAL_PROMPT:
            if not self.approval_answers:
                raise AssertionError(f"Unexpected approval prompt received: {prompt}")
            val = self.approval_answers.pop(0)
            if isinstance(val, type) and issubclass(val, BaseException):
                raise val()
            if isinstance(val, BaseException):
                raise val
            return val
        else:
            if not self.user_inputs:
                raise AssertionError(f"User inputs exhausted when prompted with: {prompt}")
            val = self.user_inputs.pop(0)
            if isinstance(val, type) and issubclass(val, BaseException):
                raise val()
            if isinstance(val, BaseException):
                raise val
            return val


def make_terminal_call_response(call_id: str, command: str) -> MockLLMResponse:
    """terminal 도구를 호출하는 LLM 응답 생성."""
    return MockLLMResponse(
        content=None,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": command}),
            },
        }],
    )


# =============================================================================
# 3-1. 켜짐·꺼짐 판정과 배너 (가정 A)
# =============================================================================


def test_scenario_01_terminal_default_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 1: 터미널 참, 인자 없음 -> CLI_APPROVAL_ENABLED_NOTICE 정확히 1회, 배너 위치 정합."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)
    fake_in = FakeInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_APPROVAL_ENABLED_NOTICE) == 1
    assert CLI_APPROVAL_DISABLED_NOTICE not in captured.out

    lines = [line.strip() for line in captured.out.splitlines() if line.strip()]
    appr_idx = -1
    exit_idx = -1
    for i, line in enumerate(lines):
        if CLI_APPROVAL_ENABLED_NOTICE in line:
            appr_idx = i
        if "Type 'exit' or 'quit' to terminate." in line:
            exit_idx = i

    assert appr_idx != -1 and exit_idx != -1
    assert appr_idx < exit_idx


def test_scenario_02_terminal_no_approval_flag_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 2: 터미널 참, --no-approval -> CLI_APPROVAL_DISABLED_NOTICE 1회, 켜짐 안내 없음."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)
    fake_in = FakeInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-approval"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_APPROVAL_DISABLED_NOTICE) == 1
    assert CLI_APPROVAL_ENABLED_NOTICE not in captured.out


def test_scenario_03_non_terminal_no_flags_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 3: 터미널 거짓(비터미널), 인자 없음 -> 두 안내 문구 모두 출력에 없음, 기본 배너 유지."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: False)
    fake_in = FakeInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert CLI_APPROVAL_ENABLED_NOTICE not in captured.out
    assert CLI_APPROVAL_DISABLED_NOTICE not in captured.out
    assert "Hermes Core CLI Started" in captured.out
    assert "Type 'exit' or 'quit' to terminate." in captured.out


def test_scenario_04_non_terminal_no_approval_flag_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 4: 터미널 거짓, --no-approval -> 꺼짐 안내만 1회 (플래그 우선)."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: False)
    fake_in = FakeInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-approval"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_APPROVAL_DISABLED_NOTICE) == 1
    assert CLI_APPROVAL_ENABLED_NOTICE not in captured.out


def test_scenario_05_mock_mode_terminal_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 5: 터미널 참, 모의 모드(--mock) -> 켜짐 안내 1회 (모드 무관 동일 규칙)."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)
    fake_in = FakeInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_APPROVAL_ENABLED_NOTICE) == 1
    assert CLI_APPROVAL_DISABLED_NOTICE not in captured.out


def test_scenario_06_help_and_flag_combinations(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 6: --help에 --no-approval 명시 및 --mock / --delegate 조합 정상 파싱."""
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "--no-approval" in captured.out

    # --mock --no-approval 조합 실행 가능 확인
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)
    fake_in = FakeInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock", "--no-approval"])
    cli.main()
    captured2 = capsys.readouterr()
    assert CLI_APPROVAL_DISABLED_NOTICE in captured2.out

    # --delegate --no-approval 조합 실행 가능 확인
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate", "--no-approval"])
    cli.main()
    captured3 = capsys.readouterr()
    assert CLI_APPROVAL_DISABLED_NOTICE in captured3.out
    assert "Delegation enabled (delegate_task)." in captured3.out


# =============================================================================
# 3-2. 승인 흐름이 CLI에서 이어진다 (가정 B·C)
# =============================================================================


def test_scenario_07_approval_flow_approved(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 7: 터미널 참, 인자 없음, 모델 허용목록 밖 명령 요청, 승인 'y' -> 실행 및 원장 approved."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_argv = [sys.executable, "-c", "print('hi')"]
    py_cmd = shlex.join(py_argv)

    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_hi", py_cmd),
        MockLLMResponse(content="Executed hi cleanly."),
    ]

    fake_in = FakeInput(user_inputs=["go", "exit"], approval_answers=["y"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 프롬프트 열 검증: 질문은 REPL 프롬프트와 구분됨
    assert fake_in.recorded_prompts == [CLI_USER_PROMPT, APPROVAL_PROMPT, CLI_USER_PROMPT]

    # 출력에 헤더 및 표시 정제 명령 확인
    assert APPROVAL_HEADER in captured.out
    assert format_command_for_display(py_argv) in captured.out

    # 모델이 받은 2번째 호출 메시지에 tool_call 결과 "hi\n" 확인
    parent_client = ScriptableClient.created_instances[0]
    assert len(parent_client.call_history) == 2
    second_messages = parent_client.call_history[1]["messages"]
    tool_msg = next(m for m in second_messages if m.get("role") == "tool")
    tool_res = json.loads(tool_msg["content"])
    assert tool_res["success"] is True
    assert tool_res["stdout"] == "hi\n"

    # 상태 DB 원장 확인
    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    assert session_id[:8] in captured.out

    reqs = db.get_approval_requests(session_id)
    assert len(reqs) == 1
    assert reqs[0]["decision"] == "approved"
    assert json.loads(reqs[0]["argv_json"]) == py_argv


def test_scenario_08_approval_flow_denied(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 8: 7과 같으나 승인 'n' -> 도구 실패 반환, 원장 denied, REPL 다음 턴 지속 후 정상 종료."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_argv = [sys.executable, "-c", "print('hi')"]
    py_cmd = shlex.join(py_argv)

    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_hi", py_cmd),
        MockLLMResponse(content="Understood denial."),
    ]

    fake_in = FakeInput(user_inputs=["go", "exit"], approval_answers=["n"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 도구 결과 검증: 거부 메시지 포함, 실행 없음(출력에 hi 없음)
    parent_client = ScriptableClient.created_instances[0]
    second_messages = parent_client.call_history[1]["messages"]
    tool_msg = next(m for m in second_messages if m.get("role") == "tool")
    tool_res = json.loads(tool_msg["content"])
    assert tool_res["success"] is False
    assert APPROVAL_DENIED_MESSAGE in tool_res["error"]
    assert "hi\n" not in captured.out

    # 정상 종료 안내
    assert "Session saved and closed. Goodbye!" in captured.out

    # 상태 DB 원장 검증
    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    session_id = sessions[0]["id"]
    reqs = db.get_approval_requests(session_id)
    assert len(reqs) == 1
    assert reqs[0]["decision"] == "denied"


def test_scenario_09_approval_flow_cancelled_keyboard_interrupt(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 9a: 승인 질문에서 KeyboardInterrupt -> 원장 cancelled, REPL 지속 후 exit 정상 종료."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_argv = [sys.executable, "-c", "print('hi')"]
    py_cmd = shlex.join(py_argv)

    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_hi", py_cmd),
        MockLLMResponse(content="Understood cancel."),
    ]

    fake_in = FakeInput(user_inputs=["go", "exit"], approval_answers=[KeyboardInterrupt])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # REPL이 끝나지 않고 마지막 exit 시점에 정상 종료됨
    assert "Session saved and closed. Goodbye!" in captured.out

    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    session_id = sessions[0]["id"]
    reqs = db.get_approval_requests(session_id)
    assert len(reqs) == 1
    assert reqs[0]["decision"] == "cancelled"


def test_scenario_09_approval_flow_cancelled_eof_error(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 9b: 승인 질문에서 EOFError -> 원장 cancelled, REPL 지속 후 exit 정상 종료."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_argv = [sys.executable, "-c", "print('hi')"]
    py_cmd = shlex.join(py_argv)

    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_hi", py_cmd),
        MockLLMResponse(content="Understood cancel."),
    ]

    fake_in = FakeInput(user_inputs=["go", "exit"], approval_answers=[EOFError])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert "Session saved and closed. Goodbye!" in captured.out

    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    session_id = sessions[0]["id"]
    reqs = db.get_approval_requests(session_id)
    assert len(reqs) == 1
    assert reqs[0]["decision"] == "cancelled"


def test_scenario_10_multiple_approvals_same_session(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 10: 같은 REPL 세션 2턴 연속 승인 요청 및 승인 -> 질문 2회, 원장 2개 approved, 세션 동일."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_cmd1 = shlex.join([sys.executable, "-c", "print('turn1')"])
    py_cmd2 = shlex.join([sys.executable, "-c", "print('turn2')"])

    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_1", py_cmd1),
        MockLLMResponse(content="turn 1 done"),
        make_terminal_call_response("call_2", py_cmd2),
        MockLLMResponse(content="turn 2 done"),
    ]

    fake_in = FakeInput(user_inputs=["turn1", "turn2", "exit"], approval_answers=["y", "y"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()

    assert fake_in.recorded_prompts.count(APPROVAL_PROMPT) == 2

    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    reqs = db.get_approval_requests(session_id)
    assert len(reqs) == 2
    assert reqs[0]["decision"] == "approved"
    assert reqs[1]["decision"] == "approved"


def test_scenario_11_approved_command_stripped_env(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 11: 승인된 명령 실행 시 키 환경변수 차단 (<unset>), DB 및 어디에도 키 미노출 (Zero Leakage)."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_argv = [
        sys.executable,
        "-c",
        f"import os; print(os.environ.get('{OPENROUTER_API_KEY_ENV}', '<unset>'))",
    ]
    py_cmd = shlex.join(py_argv)

    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_env", py_cmd),
        MockLLMResponse(content="done env inspection"),
    ]

    fake_in = FakeInput(user_inputs=["check_env", "exit"], approval_answers=["y"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 도구 결과 stdout == "<unset>\n" 확인
    parent_client = ScriptableClient.created_instances[0]
    second_messages = parent_client.call_history[1]["messages"]
    tool_msg = next(m for m in second_messages if m.get("role") == "tool")
    tool_res = json.loads(tool_msg["content"])
    assert tool_res["success"] is True
    assert tool_res["stdout"] == "<unset>\n"

    # 가짜 키 문자열 미노출 검증
    assert FAKE_API_KEY not in captured.out
    for msg in second_messages:
        assert FAKE_API_KEY not in str(msg)

    # 상태 DB 전체 덤프 검증
    db_path = hermes_home / "state.db"
    assert db_path.exists()
    db = SessionDB(db_path)
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    session_id = sessions[0]["id"]
    reqs = db.get_approval_requests(session_id)
    assert len(reqs) == 1
    assert FAKE_API_KEY not in str(reqs[0]["argv_json"])

    raw_dump = db_path.read_bytes()
    assert FAKE_API_KEY.encode("utf-8") not in raw_dump


# =============================================================================
# 3-3. 승인기가 없는 구성은 지금처럼 거부한다 (가정 A, 위험 1·8·9)
# =============================================================================


def test_scenario_12_no_approval_flag_denies_without_asking(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 12: 터미널 참, --no-approval -> 승인 질문 0회, 기존 거부(is not allowed), 원장 행 0."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    py_cmd = shlex.join([sys.executable, "-c", "print('hi')"])
    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_hi", py_cmd),
        MockLLMResponse(content="done"),
    ]

    fake_in = FakeInput(user_inputs=["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-approval"])

    cli.main()

    # 승인 프롬프트 호출 없음
    assert APPROVAL_PROMPT not in fake_in.recorded_prompts

    # 기존 거부 메시지 확인
    parent_client = ScriptableClient.created_instances[0]
    second_messages = parent_client.call_history[1]["messages"]
    tool_msg = next(m for m in second_messages if m.get("role") == "tool")
    tool_res = json.loads(tool_msg["content"])
    assert tool_res["success"] is False
    assert "is not allowed" in tool_res["error"]

    # 원장 행 0
    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    session_id = sessions[0]["id"]
    assert len(db.get_approval_requests(session_id)) == 0


def test_scenario_13_non_terminal_denies_without_asking(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 13: 터미널 거짓(비터미널), 인자 없음 -> 승인 질문 0회, 기존 거부, 원장 행 0."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: False)

    py_cmd = shlex.join([sys.executable, "-c", "print('hi')"])
    ScriptableClient.responses_by_instance[0] = [
        make_terminal_call_response("call_hi", py_cmd),
        MockLLMResponse(content="done"),
    ]

    fake_in = FakeInput(user_inputs=["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()

    assert APPROVAL_PROMPT not in fake_in.recorded_prompts

    parent_client = ScriptableClient.created_instances[0]
    second_messages = parent_client.call_history[1]["messages"]
    tool_msg = next(m for m in second_messages if m.get("role") == "tool")
    tool_res = json.loads(tool_msg["content"])
    assert tool_res["success"] is False
    assert "is not allowed" in tool_res["error"]

    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions ORDER BY started_at ASC").fetchall()
    session_id = sessions[0]["id"]
    assert len(db.get_approval_requests(session_id)) == 0


def test_scenario_14_subagent_denies_without_approval(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 14: --delegate 자식 모델이 terminal 요청 시 승인 질문 0회, 기존 거부, 자식 원장 행 0."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    child_py_cmd = shlex.join([sys.executable, "-c", "print(1)"])

    # 부모: delegate_task 호출 후 최종 응답
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            content=None,
            tool_calls=[{
                "id": "call_delegate",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "sub task"}),
                },
            }],
        ),
        MockLLMResponse(content="Parent finished after delegation."),
    ]

    # 자식: terminal 호출 후 종료
    ScriptableClient.responses_by_instance[1] = [
        make_terminal_call_response("call_child_term", child_py_cmd),
        MockLLMResponse(content="Child finished."),
    ]

    fake_in = FakeInput(user_inputs=["start", "exit"], approval_answers=[])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])

    cli.main()

    # 승인 질문 0회
    assert APPROVAL_PROMPT not in fake_in.recorded_prompts

    # 자식 클라이언트의 도구 결과 검증
    child_client = ScriptableClient.created_instances[1]
    child_second_msgs = child_client.call_history[1]["messages"]
    child_tool_msg = next(m for m in child_second_msgs if m.get("role") == "tool")
    child_tool_res = json.loads(child_tool_msg["content"])
    assert child_tool_res["success"] is False
    assert "is not allowed" in child_tool_res["error"]

    # 원장 행 0
    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id, source FROM sessions").fetchall()
    child_session = next(s["id"] for s in sessions if s["source"] == "subagent")
    assert len(db.get_approval_requests(child_session)) == 0


def test_scenario_15_parent_approves_after_delegation(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 15: --delegate 환경에서 부모가 위임 후 직접 terminal 요청 -> 승인 질문 1회, 원장 approved."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    parent_py_cmd = shlex.join([sys.executable, "-c", "print(2)"])

    # 부모: 1) delegate_task 호출 -> 2) 위임 완료 후 terminal 호출 -> 3) 최종 응답
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            content=None,
            tool_calls=[{
                "id": "call_delegate",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "sub task"}),
                },
            }],
        ),
        make_terminal_call_response("call_parent_term", parent_py_cmd),
        MockLLMResponse(content="Parent fully done."),
    ]

    # 자식: 간단한 텍스트 응답으로 완료
    ScriptableClient.responses_by_instance[1] = [
        MockLLMResponse(content="Child finished clean."),
    ]

    fake_in = FakeInput(user_inputs=["run_delegate_and_term", "exit"], approval_answers=["y"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])

    cli.main()

    # 승인 질문 1회 확인
    assert fake_in.recorded_prompts.count(APPROVAL_PROMPT) == 1

    # 부모의 terminal 도구 결과 확인
    parent_client = ScriptableClient.created_instances[0]
    third_msgs = parent_client.call_history[2]["messages"]
    parent_term_msg = next(m for m in third_msgs if m.get("tool_call_id") == "call_parent_term")
    parent_term_res = json.loads(parent_term_msg["content"])
    assert parent_term_res["success"] is True
    assert parent_term_res["stdout"] == "2\n"

    # 원장 행 1개 approved
    db = SessionDB(hermes_home / "state.db")
    sessions = db._conn.execute("SELECT id, source FROM sessions").fetchall()
    parent_session = next(s["id"] for s in sessions if s["source"] == "cli")
    reqs = db.get_approval_requests(parent_session)
    assert len(reqs) == 1
    assert reqs[0]["decision"] == "approved"


# =============================================================================
# 3-4. 기존 동작 불변 (가정 I)
# =============================================================================


def test_scenario_16_non_terminal_tools_and_prompt_unchanged(
    cli_test_env, monkeypatch: pytest.MonkeyPatch
):
    """시나리오 16: 인자 없음, 터미널 거짓 -> 도구 8종 목록 및 시스템 프롬프트 구성 이전과 불변."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: False)

    fake_in = FakeInput(user_inputs=["hi", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()

    parent_client = ScriptableClient.created_instances[0]
    first_call = parent_client.call_history[0]

    tool_names = {t["function"]["name"] for t in first_call["tools"]}
    expected_8 = {
        "memory",
        "session_search",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "skills_list",
        "skill_view",
    }
    assert tool_names == expected_8

    sys_msg = first_call["messages"][0]["content"]
    assert "delegate_task" not in sys_msg
