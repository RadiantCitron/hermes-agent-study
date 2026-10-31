"""CLI /ctl 어댑터 단위 및 대화 루프 검증 테스트 (로드맵 19 / 구현지시서 050).

REPL 환경에서 /ctl 명령(status, set, enable, disable)의 파싱, 단일 제어판 연동,
턴 경계 반영, 감사 원장(DB v7) 기록, 모델 호출 0회 및 /rollback과의 무간섭 동작을 검증한다.
"""

import io
from pathlib import Path
import sys
from typing import Any, Optional

import pytest

from hermes_core import cli
from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.constants import (
    CLI_CTL_DISABLE_ACCEPTED,
    CLI_CTL_ENABLE_ACCEPTED,
    CLI_CTL_REJECTED,
    CLI_CTL_SET_ACCEPTED,
    CLI_CTL_UNKNOWN_SUBCOMMAND,
    CLI_CTL_USAGE_NOTICE,
    CLI_ROLLBACK_HEADER,
    CLI_ROLLBACK_PROMPT,
    CLI_ROLLBACK_RESULT_SUMMARY,
    OPENROUTER_API_KEY_ENV,
    ROLLBACK_NOTICE_BEGIN,
)
from hermes_core.session_db import SessionDB


FAKE_API_KEY = "sk-or-" + "v1-" + ("a" * 36)


class ScriptableInput:
    """대화형 입력을 시뮬레이션하는 테스트용 입력 핸들러."""

    def __init__(
        self,
        user_inputs: list[str],
        rollback_answers: Optional[list[str]] = None,
    ) -> None:
        self.user_inputs = list(user_inputs)
        self.rollback_answers = list(rollback_answers or [])
        self.recorded_prompts: list[str] = []

    def __call__(self, prompt: str = "", **kwargs: Any) -> str:
        self.recorded_prompts.append(prompt)
        if prompt == CLI_ROLLBACK_PROMPT:
            if self.rollback_answers:
                return self.rollback_answers.pop(0)
            return "n"
        if self.user_inputs:
            return self.user_inputs.pop(0)
        return "exit"


@pytest.fixture
def cli_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI 테스트 환경 픽스처."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, FAKE_API_KEY)
    monkeypatch.chdir(tmp_path)

    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True  # type: ignore[assignment]
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


def _get_latest_session_db(hermes_home: Path) -> tuple[SessionDB, str]:
    db_path = hermes_home / "state.db"
    assert db_path.exists()
    sdb = SessionDB(db_path)
    cur = sdb._conn.execute("SELECT id FROM sessions ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    assert row is not None
    session_id = row[0]
    return sdb, session_id


def test_scenario_1_ctl_status_output_and_zero_llm_calls(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 1: /ctl status 실행 시 스냅샷 출력 및 모델 호출 0회 (ROADMAP §6-5 시나리오 1)."""
    llm_calls = []
    orig_chat_completion = MockLLMClient.chat_completion

    def recorded_chat_completion(self, *args, **kwargs):
        llm_calls.append((args, kwargs))
        return orig_chat_completion(self, *args, **kwargs)

    monkeypatch.setattr(MockLLMClient, "chat_completion", recorded_chat_completion)

    fake_in = ScriptableInput(["/ctl status", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    out = capsys.readouterr().out

    assert "[Settings]" in out
    assert "[Extensions]" in out
    assert "max_iterations" in out
    assert "context_window_tokens" in out
    assert "compression" in out
    assert "loop_guard" in out
    assert len(llm_calls) == 0
    assert "Session saved and closed. Goodbye!" in out


def test_scenario_2_ctl_set_queued_and_applied_on_next_turn(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 2: /ctl set max_iterations 3 접수 안내 및 다음 턴 자동 반영 (ROADMAP §6-5 시나리오 2)."""
    created_agents: list[HermesAgent] = []
    orig_create_agent = cli.create_agent

    def spy_create_agent(*args, **kwargs):
        agent = orig_create_agent(*args, **kwargs)
        created_agents.append(agent)
        return agent

    monkeypatch.setattr(cli, "create_agent", spy_create_agent)

    max_iter_before_hello = None
    orig_run_conversation = HermesAgent.run_conversation

    def spy_run_conversation(self, prompt, **kwargs):
        nonlocal max_iter_before_hello
        if max_iter_before_hello is None:
            max_iter_before_hello = self.max_iterations
        return orig_run_conversation(self, prompt, **kwargs)

    monkeypatch.setattr(HermesAgent, "run_conversation", spy_run_conversation)

    fake_in = ScriptableInput(["/ctl set max_iterations 3", "hello", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    out = capsys.readouterr().out

    expected_msg = CLI_CTL_SET_ACCEPTED.format(key="max_iterations", value=3)
    assert expected_msg in out

    # 첫 턴 실행 직전 apply_pending()에 의해 3으로 반영되었음
    assert max_iter_before_hello == 3
    assert created_agents[0].max_iterations == 3

    # 감사 원장(DB) 검증
    sdb, session_id = _get_latest_session_db(cli_test_env["hermes_home"])
    audits = sdb.list_control_audit(session_id)
    # accepted 1개, applied 1개
    set_audits = [a for a in audits if a["command"] == "set_setting" and a["target"] == "max_iterations"]
    assert len(set_audits) == 2
    assert set_audits[0]["result"] == "accepted"
    assert set_audits[0]["new_value"] == "3"
    assert set_audits[1]["result"] == "applied"
    assert set_audits[1]["new_value"] == "3"


def test_scenario_3_ctl_set_rejected_and_audited(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 3: 보안 locked 설정, startup 전용, 잘못된 값 거부 및 감사 기록 (ROADMAP §6-5 시나리오 3)."""
    fake_in = ScriptableInput([
        "/ctl set approval_enabled false",
        "/ctl set model gpt-4",
        "/ctl set max_iterations -1",
        "exit",
    ])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    out = capsys.readouterr().out

    # 3개 모두 rejected 출력 확인
    assert "Command 'set approval_enabled' rejected" in out
    assert "Command 'set model' rejected" in out
    assert "Command 'set max_iterations' rejected" in out

    # 감사 원장(DB) 검증
    sdb, session_id = _get_latest_session_db(cli_test_env["hermes_home"])
    audits = sdb.list_control_audit(session_id)
    assert len(audits) == 3
    for audit in audits:
        assert audit["result"] == "rejected"
        assert audit["reason"] is not None

    assert audits[0]["target"] == "approval_enabled"
    assert "locked" in audits[0]["reason"]

    assert audits[1]["target"] == "model"
    assert "startup" in audits[1]["reason"]

    assert audits[2]["target"] == "max_iterations"
    assert "bad_value" in audits[2]["reason"]


def test_scenario_4_ctl_extension_toggle_and_turn_boundary(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 4: /ctl disable loop_guard 및 /ctl enable loop_guard 턴 경계 반영 (ROADMAP §6-5 시나리오 4)."""
    created_agents: list[HermesAgent] = []
    orig_create_agent = cli.create_agent

    def spy_create_agent(*args, **kwargs):
        agent = orig_create_agent(*args, **kwargs)
        created_agents.append(agent)
        return agent

    monkeypatch.setattr(cli, "create_agent", spy_create_agent)

    fake_in = ScriptableInput([
        "/ctl disable loop_guard",
        "turn 1",
        "/ctl enable loop_guard",
        "turn 2",
        "exit",
    ])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    out = capsys.readouterr().out

    assert CLI_CTL_DISABLE_ACCEPTED.format(name="loop_guard") in out
    assert CLI_CTL_ENABLE_ACCEPTED.format(name="loop_guard") in out

    # 감사 원장(DB) 검증: disable(accepted, applied), enable(accepted, applied)
    sdb, session_id = _get_latest_session_db(cli_test_env["hermes_home"])
    audits = sdb.list_control_audit(session_id)
    assert len(audits) == 4

    assert audits[0]["command"] == "disable_extension"
    assert audits[0]["result"] == "accepted"
    assert audits[1]["command"] == "disable_extension"
    assert audits[1]["result"] == "applied"

    assert audits[2]["command"] == "enable_extension"
    assert audits[2]["result"] == "accepted"
    assert audits[3]["command"] == "enable_extension"
    assert audits[3]["result"] == "applied"


def test_scenario_5_ctl_usage_and_unknown_commands(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 5: /ctl 단독 및 인자 부족·모르는 하위 명령 사용법 안내 (ROADMAP §6-5 시나리오 5)."""
    fake_in = ScriptableInput([
        "/ctl",
        "/ctl unknown_cmd",
        "/ctl set",
        "/ctl set key",
        "/ctl enable",
        "/ctl disable",
        "exit",
    ])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    out = capsys.readouterr().out

    # /ctl
    assert CLI_CTL_USAGE_NOTICE in out
    # /ctl unknown_cmd
    assert CLI_CTL_UNKNOWN_SUBCOMMAND.format(subcommand="unknown_cmd") in out
    # 크래시 없이 정상 종료
    assert "Session saved and closed. Goodbye!" in out


def test_scenario_6_rollback_and_ctl_coexistence(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 6: /rollback과 /ctl 혼합 실행 및 기존 동작 무변경 (ROADMAP §6-5 시나리오 6)."""
    # 1턴: 도구 호출(write_file)을 수행하여 파일 변경 발생시킴
    # 2턴: /ctl status 조회
    # 3턴: /rollback 실행 (y 확인) -> 파일 롤백
    # 4턴: "after rollback" 전송 -> pending_notices가 모델 메시지로 전달됨
    test_file = cli_test_env["tmp_path"] / "test_artifact.txt"

    tool_call_resp = MockLLMResponse(
        content="Creating file",
        tool_calls=[{
            "id": "call_1",
            "function": {
                "name": "write_file",
                "arguments": f'{{"path": "{test_file.name}", "content": "hello world"}}',
            },
        }],
    )
    final_resp = MockLLMResponse(content="Done writing.")
    after_rollback_resp = MockLLMResponse(content="Understood rollback.")

    llm_responses = [tool_call_resp, final_resp, after_rollback_resp]
    recorded_messages = []

    def mock_chat_completion(self, messages, *args, **kwargs):
        recorded_messages.append([dict(m) for m in messages])
        if llm_responses:
            return llm_responses.pop(0)
        return MockLLMResponse(content="Fallback response")

    monkeypatch.setattr(MockLLMClient, "chat_completion", mock_chat_completion)

    fake_in = ScriptableInput(
        user_inputs=["create a file", "/ctl status", "/rollback", "after rollback", "exit"],
        rollback_answers=["y"],
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    out = capsys.readouterr().out

    # 파일이 생성되었다가 롤백되어 삭제되었는지 확인
    assert not test_file.exists()
    assert CLI_ROLLBACK_RESULT_SUMMARY.format(rolled_back=1, skipped=0) in out
    assert "[Settings]" in out

    # 마지막 턴 메시지에 ROLLBACK_NOTICE_BEGIN이 포함되어 있는지 확인
    assert len(recorded_messages) >= 3
    last_turn_user_msg = recorded_messages[-1][-1]["content"]
    assert ROLLBACK_NOTICE_BEGIN in last_turn_user_msg
    assert "after rollback" in last_turn_user_msg
