"""CLI 중단·종료 정합 테스트 (로드맵 13 / 구현지시서 010)."""

import io
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Optional

import pytest

from hermes_core import cli
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.constants import (
    CLI_ROLLBACK_PROMPT,
    CLI_TURN_CANCELLED_NOTICE,
    OPENROUTER_API_KEY_ENV,
    get_default_workspace,
)
from hermes_core.llm_client import LLMResponse
from hermes_core.session_db import SessionDB


class DummyMockOpenRouterClient:
    """테스트용 가짜 OpenRouter 클라이언트."""

    call_count = 0

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def chat_completion(self, messages: Any, tools: Any = None) -> LLMResponse:
        DummyMockOpenRouterClient.call_count += 1
        return LLMResponse(content="dummy assistant answer", finish_reason="stop")


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _setup_real_cli_env(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = "test-key-safe-interrupt"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)
    DummyMockOpenRouterClient.call_count = 0


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)


def test_scenario_01_exit_command_records_user_exit(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 1: exit 입력 시 end_reason이 'user_exit'로 기록되고 종료 메시지가 출력된다."""
    _setup_real_cli_env(hermes_home, monkeypatch)
    monkeypatch.setattr(cli, "read_user_input", lambda prompt="": "exit")

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Session saved and closed. Goodbye!" in out

    db = SessionDB(hermes_home / "state.db")
    row = db._conn.execute("SELECT end_reason, ended_at FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "user_exit"
    assert row["ended_at"] is not None
    db.close()


def test_scenario_02_keyboard_interrupt_at_prompt_records_user_interrupt(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 2: 프롬프트 입력 읽기가 KeyboardInterrupt를 내면 'user_interrupt' 기록."""
    _setup_real_cli_env(hermes_home, monkeypatch)

    def raising_input(prompt: str = "") -> str:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "read_user_input", raising_input)

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Exiting session..." in out

    db = SessionDB(hermes_home / "state.db")
    row = db._conn.execute("SELECT end_reason, ended_at FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "user_interrupt"
    assert row["ended_at"] is not None
    db.close()


def test_scenario_03_eof_at_prompt_records_eof(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 3: 프롬프트 입력 읽기가 EOFError를 내면 'eof' 기록."""
    _setup_real_cli_env(hermes_home, monkeypatch)

    def raising_input(prompt: str = "") -> str:
        raise EOFError()

    monkeypatch.setattr(cli, "read_user_input", raising_input)

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Exiting session..." in out

    db = SessionDB(hermes_home / "state.db")
    row = db._conn.execute("SELECT end_reason, ended_at FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "eof"
    assert row["ended_at"] is not None
    db.close()


def test_scenario_04_runtime_error_in_conversation_records_error_and_propagates(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 4: run_conversation에서 예외 발생 시 RuntimeError가 전파되고 'error' 기록, close 1회."""
    _setup_real_cli_env(hermes_home, monkeypatch)
    monkeypatch.setattr(cli, "read_user_input", lambda prompt="": "hello")

    def failing_run(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.HermesAgent, "run_conversation", failing_run)

    close_calls = 0
    orig_close = SessionDB.close

    def tracking_close(self: SessionDB) -> None:
        nonlocal close_calls
        close_calls += 1
        orig_close(self)

    monkeypatch.setattr(SessionDB, "close", tracking_close)

    with pytest.raises(RuntimeError, match="boom"):
        _run_cli(monkeypatch)

    assert close_calls == 1

    db = SessionDB(hermes_home / "state.db")
    row = db._conn.execute("SELECT end_reason, ended_at FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "error"
    assert row["ended_at"] is not None
    db.close()


def test_scenario_05_close_session_does_not_overwrite_existing_reason(tmp_path: Path) -> None:
    """시나리오 5: _close_session 직접 호출 시 이미 기록된 end_reason을 덮어쓰지 않음."""
    db_path = tmp_path / "test_state.db"
    db = SessionDB(db_path)
    session_id = db.create_session("sess_05")
    db.end_session(session_id, "custom")

    before = db.get_session(session_id)
    assert before is not None
    orig_ended_at = before["ended_at"]
    assert before["end_reason"] == "custom"

    cli._close_session(db, session_id, "user_exit")

    after = db.get_session(session_id)
    assert after is not None
    assert after["end_reason"] == "custom"
    assert after["ended_at"] == orig_ended_at
    db.close()


def test_scenario_06_close_session_records_when_empty(tmp_path: Path) -> None:
    """시나리오 6: _close_session 직접 호출 시 아직 안 끝난 세션에 사유 기록."""
    db_path = tmp_path / "test_state.db"
    db = SessionDB(db_path)
    session_id = db.create_session("sess_06")

    cli._close_session(db, session_id, "user_exit")

    after = db.get_session(session_id)
    assert after is not None
    assert after["end_reason"] == "user_exit"
    assert after["ended_at"] is not None
    db.close()


def test_scenario_07_close_session_nonexistent_session_id(tmp_path: Path) -> None:
    """시나리오 7: _close_session 직접 호출 시 존재하지 않는 session_id는 예외 없이 통과하고 행 추가 없음."""
    db_path = tmp_path / "test_state.db"
    db = SessionDB(db_path)
    cli._close_session(db, "nonexistent-id", "user_exit")

    cursor = db._conn.execute("SELECT COUNT(*) as count FROM sessions")
    assert cursor.fetchone()["count"] == 0
    db.close()


def test_scenario_08_end_session_db_error_logged_and_does_not_prevent_close(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 8: SessionDB.end_session 오류 시 경고 로그를 남기고 cli.main이 정상 종료되며 close 1회 호출."""
    _setup_real_cli_env(hermes_home, monkeypatch)
    monkeypatch.setattr(cli, "read_user_input", lambda prompt="": "exit")

    def failing_end_session(self: SessionDB, session_id: str, end_reason: str = "completed") -> None:
        raise RuntimeError("db lock failed")

    monkeypatch.setattr(SessionDB, "end_session", failing_end_session)

    close_calls = 0
    orig_close = SessionDB.close

    def tracking_close(self: SessionDB) -> None:
        nonlocal close_calls
        close_calls += 1
        orig_close(self)

    monkeypatch.setattr(SessionDB, "close", tracking_close)

    import logging

    with caplog.at_level(logging.WARNING):
        _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Session saved and closed. Goodbye!" in out
    assert close_calls == 1
    assert any("Failed to close session" in record.message for record in caplog.records)


def test_scenario_09_mock_mode_non_tty_records_completed(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 9: --mock 단발성 실행 시 end_reason이 'completed'로 기록됨."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    _run_cli(monkeypatch, "--mock")

    out = capsys.readouterr().out
    assert "Mock session finished cleanly." in out

    db = SessionDB(hermes_home / "state.db")
    row = db._conn.execute("SELECT end_reason, ended_at FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "completed"
    assert row["ended_at"] is not None
    db.close()


# =============================================================================
# 로드맵 13 / 구현지시서 020: 처리 중 Ctrl+C 취소 시나리오 1~9
# =============================================================================


class InterruptingClient:
    """테스트용 대본형 클라이언트 (BaseException raise 지원)."""

    created_instances: list["InterruptingClient"] = []
    responses_by_instance: dict[int, list[Any]] = {}

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self.model = model
        self.instance_idx = len(InterruptingClient.created_instances)
        InterruptingClient.created_instances.append(self)
        self.call_history: list[dict[str, Any]] = []
        self.responses = list(
            InterruptingClient.responses_by_instance.get(self.instance_idx, [])
        )
        self._call_recorder = None

    def set_call_recorder(self, recorder: Any) -> None:
        self._call_recorder = recorder

    def chat_completion(
        self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None
    ) -> Any:
        self.call_history.append({"messages": [dict(m) for m in messages], "tools": tools})
        if self._call_recorder:
            self._call_recorder({
                "model": self.model,
                "status_code": 200,
                "input_tokens": 10,
                "output_tokens": 5,
            })
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, BaseException):
                raise resp
            if isinstance(resp, type) and issubclass(resp, BaseException):
                raise resp()
            return resp
        return MockLLMResponse(content="[InterruptingClient] default response")


@pytest.fixture(autouse=True)
def reset_interrupting_client():
    InterruptingClient.created_instances.clear()
    InterruptingClient.responses_by_instance.clear()
    yield
    InterruptingClient.created_instances.clear()
    InterruptingClient.responses_by_instance.clear()


@pytest.fixture
def cli_interrupt_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """020 시나리오용 격리 환경 픽스처."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "test-key-safe-interrupt-env")
    monkeypatch.setattr(cli, "OpenRouterClient", InterruptingClient)
    monkeypatch.chdir(tmp_path)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


class ScriptableInterruptInput:
    """사용자 입력 및 되돌리기 질문 가로채기 핸들러."""

    def __init__(
        self,
        user_inputs: list[Any],
        rollback_answers: Optional[list[Any]] = None,
    ) -> None:
        self.user_inputs = list(user_inputs)
        self.rollback_answers = list(rollback_answers or [])
        self.recorded_prompts: list[str] = []

    def __call__(self, prompt: str = "", **kwargs: Any) -> str:
        self.recorded_prompts.append(prompt)
        if prompt == CLI_ROLLBACK_PROMPT:
            if not self.rollback_answers:
                raise AssertionError(f"Unexpected rollback prompt: {prompt}")
            val = self.rollback_answers.pop(0)
        else:
            if not self.user_inputs:
                raise AssertionError(f"User inputs exhausted: {prompt}")
            val = self.user_inputs.pop(0)

        if isinstance(val, type) and issubclass(val, BaseException):
            raise val()
        if isinstance(val, BaseException):
            raise val
        if callable(val):
            return str(val())
        return str(val)


def make_write_file_response(call_id: str, path: str, content: str) -> MockLLMResponse:
    """write_file 도구 호출 LLM 응답 생성."""
    return MockLLMResponse(
        content=None,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": path, "content": content}),
            },
        }],
    )


def test_scenario_020_01_cancel_on_keyboard_interrupt_in_agent_run(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 1: go -> exit, 모델 호출 중 KeyboardInterrupt 시 안내 1회 출력, 턴 취소, 세션 정상 종료."""
    InterruptingClient.responses_by_instance[0] = [KeyboardInterrupt()]
    fake_in = ScriptableInterruptInput(user_inputs=["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert out.count(CLI_TURN_CANCELLED_NOTICE) == 1
    assert "Traceback" not in out
    client = InterruptingClient.created_instances[0]
    assert len(client.call_history) == 1

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    row = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "user_exit"
    db.close()


def test_scenario_020_02_cancelled_turn_dropped_from_conversation_history(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 2: hello -> go(중단) -> again -> exit 시 3번째 모델 호출에 go가 포함되지 않음."""
    InterruptingClient.responses_by_instance[0] = [
        MockLLMResponse(content="hi"),
        KeyboardInterrupt(),
        MockLLMResponse(content="ok"),
    ]
    fake_in = ScriptableInterruptInput(user_inputs=["hello", "go", "again", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    client = InterruptingClient.created_instances[0]
    assert len(client.call_history) == 3
    third_call_messages = client.call_history[2]["messages"]

    # (역할, 내용) 목록 확인
    assert len(third_call_messages) == 4
    assert third_call_messages[0]["role"] == "system"
    assert (third_call_messages[1]["role"], third_call_messages[1]["content"]) == ("user", "hello")
    assert (third_call_messages[2]["role"], third_call_messages[2]["content"]) == ("assistant", "hi")
    assert (third_call_messages[3]["role"], third_call_messages[3]["content"]) == ("user", "again")

    # go는 세 번째 호출 문맥 어디에도 없음
    assert not any("go" in str(m.get("content")) for m in third_call_messages)


def test_scenario_020_03_cancelled_turn_user_message_preserved_in_db(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 3: 2와 동일 시나리오에서 DB 원장에는 취소된 go user 행이 남아 있음."""
    InterruptingClient.responses_by_instance[0] = [
        MockLLMResponse(content="hi"),
        KeyboardInterrupt(),
        MockLLMResponse(content="ok"),
    ]
    fake_in = ScriptableInterruptInput(user_inputs=["hello", "go", "again", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    rows = db._conn.execute(
        "SELECT content FROM messages WHERE role = 'user' ORDER BY id ASC"
    ).fetchall()
    user_contents = [r["content"] for r in rows]
    assert user_contents == ["hello", "go", "again"]
    db.close()


def test_scenario_020_04_checkpoint_applied_even_if_interrupt_in_next_step(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 4: write_file 수행 후 다음 모델 호출에서 KeyboardInterrupt 시 파일 존재 및 applied 상태."""
    InterruptingClient.responses_by_instance[0] = [
        make_write_file_response("c1", "a.txt", "x"),
        KeyboardInterrupt(),
    ]
    fake_in = ScriptableInterruptInput(user_inputs=["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    ws = get_default_workspace()
    assert (ws / "a.txt").exists()
    assert (ws / "a.txt").read_text(encoding="utf-8") == "x"

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    cp_row = db._conn.execute("SELECT status FROM file_checkpoints WHERE rel_path = 'a.txt'").fetchone()
    assert cp_row is not None
    assert cp_row["status"] == "applied"

    sess_row = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert sess_row["end_reason"] == "user_exit"
    db.close()


def test_scenario_020_05_rollback_can_undo_changes_from_cancelled_turn(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 5: 취소된 턴에서 남은 applied 체크포인트도 /rollback으로 정상 되돌림."""
    InterruptingClient.responses_by_instance[0] = [
        make_write_file_response("c1", "a.txt", "x"),
        KeyboardInterrupt(),
    ]
    fake_in = ScriptableInterruptInput(
        user_inputs=["go", "/rollback", "exit"],
        rollback_answers=["y"],
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    ws = get_default_workspace()
    assert not (ws / "a.txt").exists()

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    cp_row = db._conn.execute("SELECT status FROM file_checkpoints WHERE rel_path = 'a.txt'").fetchone()
    assert cp_row is not None
    assert cp_row["status"] == "rolled_back"
    db.close()


def test_scenario_020_06_rollback_notice_returned_to_pending_on_cancel(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 6: 롤백 후 취소된 턴(read it)에서 소비되지 않고 다음 턴(again)으로 롤백 알림이 전달됨."""
    InterruptingClient.responses_by_instance[0] = [
        make_write_file_response("c1", "new.txt", "hello"),
        MockLLMResponse(content="done"),
        KeyboardInterrupt(),
        MockLLMResponse(content="ok"),
    ]
    fake_in = ScriptableInterruptInput(
        user_inputs=["go", "/rollback", "read it", "again", "exit"],
        rollback_answers=["y"],
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    client = InterruptingClient.created_instances[0]
    assert len(client.call_history) == 4

    # 4번째 호출 (again)의 마지막 user 메시지에 롤백 알림이 실려 있어야 함
    fourth_messages = client.call_history[3]["messages"]
    last_user_msg = fourth_messages[-1]["content"]

    assert last_user_msg.startswith("[Rollback notice - added by the CLI, not typed by the user]")
    assert last_user_msg.endswith("\n\nagain")
    assert not any("read it" in str(m.get("content")) for m in fourth_messages if m["role"] != "system")


def test_scenario_020_07_ctrl_c_in_prompt_after_turn_cancel_exits_cleanly(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 7: 턴 취소 후 프롬프트에서 KeyboardInterrupt 시 user_interrupt로 정상 종료."""
    InterruptingClient.responses_by_instance[0] = [KeyboardInterrupt()]
    fake_in = ScriptableInterruptInput(user_inputs=["go", KeyboardInterrupt()])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert CLI_TURN_CANCELLED_NOTICE in out
    assert "Exiting session..." in out

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    row = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "user_interrupt"
    db.close()


def test_scenario_020_08_cancel_works_with_no_checkpoints(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 8: --no-checkpoints 모드에서도 턴 취소 및 정상 종료 지원."""
    InterruptingClient.responses_by_instance[0] = [KeyboardInterrupt()]
    fake_in = ScriptableInterruptInput(user_inputs=["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    _run_cli(monkeypatch, "--no-checkpoints")

    out = capsys.readouterr().out
    assert CLI_TURN_CANCELLED_NOTICE in out

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    row = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "user_exit"
    db.close()


def test_scenario_020_09_runtime_error_in_model_propagates(
    cli_interrupt_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 9: 모델 클라이언트에서 RuntimeError 발생 시 밖으로 전파되고 end_reason == 'error'."""
    InterruptingClient.responses_by_instance[0] = [RuntimeError("boom")]
    fake_in = ScriptableInterruptInput(user_inputs=["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)

    with pytest.raises(RuntimeError, match="boom"):
        _run_cli(monkeypatch)

    db = SessionDB(cli_interrupt_env["hermes_home"] / "state.db")
    row = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert row is not None
    assert row["end_reason"] == "error"
    db.close()
