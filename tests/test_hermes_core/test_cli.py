"""CLI 진입점 테스트: 실제 모델이 없을 때 가짜 모델을 조용히 쓰지 않는다 (로드맵 02 / 구현지시서 030 인수 시나리오 및 040 인수 시나리오)."""

import io
import os
from pathlib import Path
import sys

import pytest

from hermes_core import cli
from hermes_core.constants import DEFAULT_REAL_MODEL, get_hermes_home
from hermes_core.llm_client import LLMError, LLMResponse
from hermes_core.session_db import SessionDB


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    # 표준 입력은 터미널이 아닌 가짜 입력으로 고정한다 (실행 환경에 기대지 않는다)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cli.main()


def test_without_mock_exits_with_code_2(hermes_home, monkeypatch):  # 시나리오 1
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)
    assert exc.value.code == 2


def test_without_mock_message_mentions_mock_flag(hermes_home, monkeypatch, capsys):  # 시나리오 2
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _run(monkeypatch)
    err = capsys.readouterr().err
    assert "--mock" in err
    assert "OPENROUTER_API_KEY" in err


def test_without_mock_creates_no_session_db(hermes_home, monkeypatch):  # 시나리오 3
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _run(monkeypatch)
    assert not (hermes_home / "state.db").exists()


def test_mock_mode_finishes_cleanly(hermes_home, monkeypatch, capsys):  # 시나리오 4 (구현지시서 040 시나리오 1)
    _run(monkeypatch, "--mock")
    assert "Mock session finished cleanly." in capsys.readouterr().out

    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row["model"] == "mock"
    assert row["mode"] == "mock"
    db.close()


def test_mock_mode_keeps_mock_notice(hermes_home, monkeypatch, capsys):  # 시나리오 5
    _run(monkeypatch, "--mock")
    assert "MOCK mode" in capsys.readouterr().out


# =============================================================================
# 로드맵 04: 구현지시서 040 인수 테스트 시나리오 (시나리오 3 ~ 10)
# =============================================================================


def test_040_scenario_03_empty_and_whitespace_keys_exit_2(hermes_home, monkeypatch):
    """시나리오 3: --mock 없이, 키 환경변수가 ''와 '   ' -> 각각 종료 코드 2."""
    for empty_val in ["", "   "]:
        monkeypatch.setenv("OPENROUTER_API_KEY", empty_val)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch)
        assert exc.value.code == 2


class DummyMockOpenRouterClient:
    created_instances: list[Any] = []

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        DummyMockOpenRouterClient.created_instances.append(self)

    def chat_completion(self, messages, tools=None):
        return LLMResponse(content="dummy assistant answer", finish_reason="stop")


def test_040_scenario_04_05_07_real_mode_success_cwd_and_zero_leakage(hermes_home, monkeypatch, capsys):
    """시나리오 4, 5, 7: 키 있음(가짜 값), 가짜 클라이언트, 입력 exit
    -> 종료 코드 0, cwd == HERMES_HOME/workspace, model == DEFAULT_REAL_MODEL, mode == real, 키 출력 없음.
    """
    fake_key = "test-key-safe-12345"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)
    DummyMockOpenRouterClient.created_instances.clear()

    # 입력은 'exit'
    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        # cwd 확인 후 복원
        current_cwd = Path.cwd()
        monkeypatch.chdir(orig_cwd)

    expected_ws = (hermes_home / "workspace").resolve()
    assert current_cwd == expected_ws
    assert expected_ws.is_dir()

    # 시나리오 5: 세션 model == DEFAULT_REAL_MODEL, mode == real
    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row["model"] == DEFAULT_REAL_MODEL
    assert row["mode"] == "real"
    db.close()

    # 시나리오 7: 출력 검사 (키 없음, 모델명/작업 폴더 표시됨)
    captured = capsys.readouterr()
    full_output = captured.out + captured.err
    assert fake_key not in full_output
    assert DEFAULT_REAL_MODEL in full_output
    assert str(expected_ws) in full_output


def test_040_scenario_06_custom_model_flag(hermes_home, monkeypatch):
    """시나리오 6: --model a/b + 실제 모드 조건 -> 세션 model == 'a/b', 클라이언트 a/b로 생성."""
    fake_key = "test-key-safe-model-flag"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)
    DummyMockOpenRouterClient.created_instances.clear()

    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core", "--model", "custom/model-x"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    assert len(DummyMockOpenRouterClient.created_instances) == 1
    assert DummyMockOpenRouterClient.created_instances[0].model == "custom/model-x"

    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row["model"] == "custom/model-x"
    assert row["mode"] == "real"
    db.close()


class ErrorThenExitClient:
    def __init__(self, api_key: str, model: str):
        self.model = model

    def chat_completion(self, messages, tools=None):
        raise LLMError("rate_limited", "Rate limit 429 exceeded")


def test_040_scenario_08_llm_error_recovery_in_repl(hermes_home, monkeypatch, capsys):
    """시나리오 8: LLMError('rate_limited') 발생 후 REPL 끊기지 않고 exit까지 처리."""
    fake_key = "test-key-error-repl"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", ErrorThenExitClient)

    # 1번째 입력 'hi' -> 에러 출력, 2번째 입력 'exit' -> 정상 종료
    monkeypatch.setattr(sys, "stdin", io.StringIO("hi\nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    captured = capsys.readouterr()
    out = captured.out
    assert "Model call failed (rate_limited): Rate limit 429 exceeded" in out
    assert "Session saved and closed. Goodbye!" in out


def test_040_scenario_09_agents_md_not_injected_after_chdir(tmp_path, hermes_home, monkeypatch):
    """시나리오 9: 원래 현재 폴더의 AGENTS.md에 MARKER가 있어도 작업 폴더로 이동하여 프롬프트에 주입되지 않음."""
    start_dir = tmp_path / "original_dir"
    start_dir.mkdir()
    marker = "UNIQUE-REPO-MARKER-9999"
    (start_dir / "AGENTS.md").write_text(f"Rule: {marker}", encoding="utf-8")

    fake_key = "test-key-dir-isolation"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    captured_messages: list[list[dict]] = []

    class CapturingClient:
        def __init__(self, api_key: str, model: str):
            self.model = model

        def chat_completion(self, messages, tools=None):
            captured_messages.append(messages)
            return LLMResponse(content="captured", finish_reason="stop")

    monkeypatch.setattr(cli, "OpenRouterClient", CapturingClient)

    # start_dir에서 실행
    monkeypatch.chdir(start_dir)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    try:
        cli.main()
    finally:
        monkeypatch.chdir(tmp_path)

    # 시스템 프롬프트 확인
    assert len(captured_messages) == 1
    sys_content = captured_messages[0][0]["content"]
    assert marker not in sys_content


def test_040_scenario_10_relative_hermes_home_stays_consistent(tmp_path, monkeypatch):
    """시나리오 10: HERMES_HOME이 상대경로일 때 실행 후에도 get_hermes_home()이 절대경로로 고정되어 DB가 해당 폴더에 생성됨."""
    start_dir = tmp_path / "run_root"
    start_dir.mkdir()
    monkeypatch.chdir(start_dir)

    rel_home_name = "rel_hermes_home"
    monkeypatch.setenv("HERMES_HOME", rel_home_name)

    fake_key = "test-key-rel-home"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)

    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    expected_abs_home = (start_dir / rel_home_name).resolve()

    try:
        cli.main()
    finally:
        monkeypatch.chdir(tmp_path)

    # 실행 후에도 get_hermes_home()이 처음에 resolve된 절대경로와 일치
    assert get_hermes_home() == expected_abs_home
    assert (expected_abs_home / "state.db").exists()
