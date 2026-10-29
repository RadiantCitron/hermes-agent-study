"""CLI 배너 멀티라인 힌트 및 통합 검증 테스트 (로드맵 09 / 구현지시서 030)."""

import io
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from hermes_core import cli
from hermes_core import cli_input
from hermes_core.cli_input import multiline_input_available
from hermes_core.constants import (
    CLI_MULTILINE_HINT,
    CLI_USER_PROMPT,
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
    fake_key = "test-key-safe-hint"
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


def test_scenario_01_hint_displayed_when_available(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 1: multiline_input_available을 True로 대체, 입력 ['exit']로 cli.main()
    -> 출력에 CLI_MULTILINE_HINT가 정확히 1번, Type 'exit'... 바로 다음 줄, 그 다음 줄은 '=',
    마지막에 'Session saved and closed. Goodbye!'.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)
    monkeypatch.setattr(cli, "multiline_input_available", lambda: True)
    monkeypatch.setattr(cli, "read_user_input", lambda prompt=CLI_USER_PROMPT: "exit")

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert out.count(CLI_MULTILINE_HINT) == 1

    lines = [line.strip() for line in out.splitlines() if line.strip()]
    hint_idx = lines.index(CLI_MULTILINE_HINT)
    assert lines[hint_idx - 1] == "Type 'exit' or 'quit' to terminate."
    assert set(lines[hint_idx + 1]) == {"="}
    assert "Session saved and closed. Goodbye!" in out


def test_scenario_02_hint_not_displayed_in_non_terminal(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 2: 대체 없이(비터미널 StringIO('exit\\n')) cli.main()
    -> 출력에 CLI_MULTILINE_HINT가 없고, 배너 나머지 줄은 그대로 있음.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert CLI_MULTILINE_HINT not in out
    assert "🤖 Hermes Core CLI Started" in out
    assert "🚀 Running in REAL mode" in out
    assert "📁 Working directory:" in out
    assert "Type 'exit' or 'quit' to terminate." in out


def test_scenario_03_hint_independent_of_cli_flags(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 3: multiline_input_available=True 대체, --no-skills-index 및 --delegate 플래그
    -> 두 경우 모두 힌트 1번, 각 플래그의 배너 줄도 보존.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)
    monkeypatch.setattr(cli, "multiline_input_available", lambda: True)
    monkeypatch.setattr(cli, "read_user_input", lambda prompt=CLI_USER_PROMPT: "exit")

    # 1) --no-skills-index
    _run_cli(monkeypatch, "--no-skills-index")
    out1 = capsys.readouterr().out
    assert out1.count(CLI_MULTILINE_HINT) == 1
    assert "Skills index disabled (--no-skills-index)." in out1

    # 2) --delegate
    _run_cli(monkeypatch, "--delegate")
    out2 = capsys.readouterr().out
    assert out2.count(CLI_MULTILINE_HINT) == 1
    assert "Delegation enabled (delegate_task)." in out2


def test_scenario_04_multiline_input_available_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 4: multiline_input_available() 단위 테스트
    ⓐ tty 참 + 라이브러리 설치됨 -> True
    ⓑ tty 참 + sys.modules['prompt_toolkit']=None (미설치 모사) -> False
    ⓒ tty 거짓 -> False.
    """
    # ⓐ tty 참 + 라이브러리 설치됨
    monkeypatch.setattr(cli_input, "is_interactive_terminal", lambda: True)
    assert multiline_input_available() is True

    # ⓑ tty 참 + 미설치 모사
    monkeypatch.setitem(sys.modules, "prompt_toolkit", None)
    assert multiline_input_available() is False

    # ⓒ tty 거짓
    monkeypatch.delitem(sys.modules, "prompt_toolkit", raising=False)
    monkeypatch.setattr(cli_input, "is_interactive_terminal", lambda: False)
    assert multiline_input_available() is False


def test_scenario_05_multiline_input_available_does_not_import_library() -> None:
    """시나리오 5: multiline_input_available() 호출 전후 'prompt_toolkit' in sys.modules
    -> 새 프로세스에서 호출해도 False 유지 (판정이 라이브러리를 import하지 않음).
    """
    code = (
        "import sys\n"
        "from hermes_core.cli_input import multiline_input_available\n"
        "before = 'prompt_toolkit' in sys.modules\n"
        "res = multiline_input_available()\n"
        "after = 'prompt_toolkit' in sys.modules\n"
        "assert not after, f'prompt_toolkit was imported: before={before}, after={after}'\n"
        "print('PASS')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "PASS" in result.stdout


def test_scenario_06_multiline_input_preserves_newlines_in_conversation(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 6: 입력 대체 ['첫째 줄\\n둘째 줄', 'exit']로 cli.main()
    -> 상태 DB에 저장된 user 메시지 내용이 정확히 '첫째 줄\\n둘째 줄'(줄바꿈 보존), 세션 종료 사유 user_exit.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)
    inputs = ["첫째 줄\n둘째 줄", "exit"]

    def fake_input(prompt: str = CLI_USER_PROMPT) -> str:
        return inputs.pop(0)

    monkeypatch.setattr(cli, "read_user_input", fake_input)

    _run_cli(monkeypatch)

    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute(
        "SELECT content, role FROM messages WHERE role = 'user' ORDER BY id ASC LIMIT 1"
    )
    row = cursor.fetchone()
    assert row is not None
    assert row["content"] == "첫째 줄\n둘째 줄"

    session = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert session is not None
    assert session["end_reason"] == "user_exit"
    db.close()


def test_scenario_07_whitespace_and_newline_only_input_skipped(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 7: 입력 대체 ['  \\n  ', 'exit'] (공백·줄바꿈만)
    -> 모델 호출 0회(빈 입력으로 건너뜀), 대화 메시지 0건, 종료 사유 user_exit.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)
    inputs = ["  \n  ", "exit"]

    def fake_input(prompt: str = CLI_USER_PROMPT) -> str:
        return inputs.pop(0)

    monkeypatch.setattr(cli, "read_user_input", fake_input)

    _run_cli(monkeypatch)

    assert DummyMockOpenRouterClient.call_count == 0

    db = SessionDB(hermes_home / "state.db")
    msg_row = db._conn.execute("SELECT COUNT(*) as count FROM messages").fetchone()
    assert msg_row["count"] == 0

    session = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1").fetchone()
    assert session["end_reason"] == "user_exit"
    db.close()


def test_scenario_08_keyboard_interrupt_exits_cleanly(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 8: 입력 대체가 KeyboardInterrupt를 냄
    -> 출력에 'Exiting session...', 오류 없이 종료, DB 닫힘.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)

    def fake_input(prompt: str = CLI_USER_PROMPT) -> str:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "read_user_input", fake_input)

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Exiting session..." in out


def test_scenario_09_eof_error_exits_cleanly(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 9: 입력 대체가 EOFError를 냄
    -> 출력에 'Exiting session...', 오류 없이 종료, DB 닫힘.
    """
    _setup_real_cli_env(hermes_home, monkeypatch)

    def fake_input(prompt: str = CLI_USER_PROMPT) -> str:
        raise EOFError()

    monkeypatch.setattr(cli, "read_user_input", fake_input)

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Exiting session..." in out
