"""CLI 위임 플래그 (`--delegate`) 연동 테스트 (로드맵 07 / 구현지시서 045 인수 시나리오)."""

import io
import json
from pathlib import Path

import pytest

from hermes_core import cli
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.constants import DEFAULT_REAL_MODEL
from hermes_core.llm_client import LLMError
from hermes_core.session_db import SessionDB


class ScriptableClient:
    created_instances = []
    responses_by_instance = {}

    def __init__(self, api_key: str, model: str, **kwargs):
        self.api_key = api_key
        self.model = model
        self.instance_idx = len(ScriptableClient.created_instances)
        ScriptableClient.created_instances.append(self)
        self.call_history = []
        self.responses = list(ScriptableClient.responses_by_instance.get(self.instance_idx, []))
        self._call_recorder = None

    def set_call_recorder(self, recorder):
        self._call_recorder = recorder

    def chat_completion(self, messages, tools=None):
        self.call_history.append({"messages": messages, "tools": tools})
        if self._call_recorder:
            self._call_recorder({"model": self.model, "status_code": 200, "input_tokens": 10, "output_tokens": 5})
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-safe-delegate")
    monkeypatch.setattr(cli, "OpenRouterClient", ScriptableClient)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


def test_scenario_01_real_mode_without_delegate_flag(cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 1: --delegate 없이 실행 시 클라이언트 1개, 기본 8종 도구, delegate_task 미포함, 안내문 미출력 검증."""
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(content="hello from parent"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert len(ScriptableClient.created_instances) == 1
    parent_client = ScriptableClient.created_instances[0]
    first_call = parent_client.call_history[0]

    tool_names = {t["function"]["name"] for t in first_call["tools"]}
    expected_8 = {"memory", "session_search", "terminal", "read_file", "write_file", "patch", "search_files", "skills_list", "skill_view"}
    assert tool_names == expected_8

    sys_msg = first_call["messages"][0]["content"]
    assert "delegate_task" not in sys_msg
    assert "Delegation enabled" not in captured.out


def test_scenario_02_real_mode_with_delegate_flag(cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 2: --delegate 지정 시 9종 도구 전달, 프롬프트 내 delegate_task 포함, 배너 안내 1회 출력 검증."""
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(content="hello with delegation"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert len(ScriptableClient.created_instances) == 1
    parent_client = ScriptableClient.created_instances[0]
    first_call = parent_client.call_history[0]

    tool_names = {t["function"]["name"] for t in first_call["tools"]}
    expected_8 = {"memory", "session_search", "terminal", "read_file", "write_file", "patch", "search_files", "skills_list", "skill_view"}
    assert tool_names == expected_8 | {"delegate_task"}

    sys_msg = first_call["messages"][0]["content"]
    assert "delegate_task" in sys_msg
    assert captured.out.count("Delegation enabled (delegate_task).") == 1


def test_scenario_03_delegation_execution_cycle(cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 3: --delegate 실행 시 부모 0번과 자식 1번 클라이언트 생성, 위임 완료, 세션 DB 상태 검증."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])
    monkeypatch.setattr("sys.stdin", io.StringIO("go\nexit\n"))

    # 부모 0번: 1차 delegate_task 호출, 2차 최종 parent done
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            tool_calls=[
                {
                    "id": "p_call_1",
                    "type": "function",
                    "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "G1"})},
                }
            ]
        ),
        MockLLMResponse(content="parent done"),
    ]
    # 자식 1번: 최종 child summary
    ScriptableClient.responses_by_instance[1] = [
        MockLLMResponse(content="child summary"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert "parent done" in captured.out
    assert len(ScriptableClient.created_instances) == 2
    assert ScriptableClient.created_instances[0].model == DEFAULT_REAL_MODEL
    assert ScriptableClient.created_instances[1].model == DEFAULT_REAL_MODEL

    # 세션 DB 검증
    db = SessionDB(hermes_home / "state.db")
    rows = db._conn.execute("SELECT id, source, parent_session_id, ended_at, end_reason FROM sessions").fetchall()
    assert len(rows) == 2

    child_row = next(r for r in rows if r["source"] == "subagent")
    parent_row = next(r for r in rows if r["source"] == "cli")

    assert child_row["parent_session_id"] == parent_row["id"]
    assert child_row["ended_at"] is not None
    assert child_row["end_reason"] == "completed"

    assert parent_row["ended_at"] is not None
    assert parent_row["end_reason"] == "user_exit"


def test_scenario_04_custom_model_passed_to_child(cli_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 4: --model 지정 시 부모와 자식 클라이언트 모두 해당 모델 사용 검증."""
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate", "--model", "custom/model-x"])
    monkeypatch.setattr("sys.stdin", io.StringIO("go\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            tool_calls=[
                {
                    "id": "p_call_1",
                    "type": "function",
                    "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "G1"})},
                }
            ]
        ),
        MockLLMResponse(content="parent done"),
    ]
    ScriptableClient.responses_by_instance[1] = [
        MockLLMResponse(content="child summary"),
    ]

    cli.main()

    assert len(ScriptableClient.created_instances) == 2
    assert ScriptableClient.created_instances[0].model == "custom/model-x"
    assert ScriptableClient.created_instances[1].model == "custom/model-x"


def test_scenario_05_delegate_with_mock_rejected(cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 5: --delegate --mock 동시 사용 시 종료 코드 2 및 DB/클라이언트 미생성 검증."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate", "--mock"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "--delegate" in captured.err
    assert "--mock" in captured.err
    assert not (hermes_home / "state.db").exists()
    assert len(ScriptableClient.created_instances) == 0


def test_scenario_06_delegate_without_api_key_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 6: 키 환경변수 부재 시 종료 코드 2 및 키 안내 메시지 검증."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "OPENROUTER_API_KEY environment variable is not set" in captured.err
    assert "--mock" in captured.err
    assert not (hermes_home / "state.db").exists()


def test_scenario_07_zero_leakage_and_key_parity(cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 7: 자식 클라이언트의 api_key가 부모와 같고, 화면/로그/DB 전체 덤프에 미노출(Zero Leakage) 검증."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])
    monkeypatch.setattr("sys.stdin", io.StringIO("go\nexit\n"))

    fake_key = "test-key-safe-delegate"

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            tool_calls=[
                {
                    "id": "p_call_1",
                    "type": "function",
                    "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "G1"})},
                }
            ]
        ),
        MockLLMResponse(content="parent done"),
    ]
    ScriptableClient.responses_by_instance[1] = [
        MockLLMResponse(content="child summary"),
    ]

    cli.main()
    captured = capsys.readouterr()

    # 1. 자식 클라이언트 api_key는 부모와 동일
    assert ScriptableClient.created_instances[0].api_key == fake_key
    assert ScriptableClient.created_instances[1].api_key == fake_key

    # 2. 콘솔 출력 미노출
    assert fake_key not in captured.out
    assert fake_key not in captured.err

    # 3. SQLite DB 덤프 미노출
    db = SessionDB(hermes_home / "state.db")
    dump_text = "\n".join(db._conn.iterdump())
    assert fake_key not in dump_text


def test_scenario_08_child_llm_error_recovery(cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 8: 자식 클라이언트가 LLMError 발생 시 정상 종료 코드 0 및 부모 복구 검증."""
    hermes_home = cli_test_env["hermes_home"]
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])
    monkeypatch.setattr("sys.stdin", io.StringIO("go\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            tool_calls=[
                {
                    "id": "p_call_1",
                    "type": "function",
                    "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "G1"})},
                }
            ]
        ),
        MockLLMResponse(content="parent done after child error"),
    ]
    # 자식에게 LLMError 주입
    ScriptableClient.responses_by_instance[1] = [
        LLMError(kind="server", message="upstream server error"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert "parent done after child error" in captured.out

    db = SessionDB(hermes_home / "state.db")
    child_row = db._conn.execute("SELECT end_reason FROM sessions WHERE source = 'subagent'").fetchone()
    assert child_row["end_reason"] == "llm_error"


def test_scenario_09_cli_help(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """시나리오 9: --help 실행 시 종료 코드 0 및 --delegate 설명 포함 검증."""
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "--delegate" in captured.out
