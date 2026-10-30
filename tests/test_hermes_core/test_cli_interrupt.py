"""CLI 중단·종료 정합 테스트 (로드맵 13 / 구현지시서 010)."""

import io
from pathlib import Path
import sys
from typing import Any

import pytest

from hermes_core import cli
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
