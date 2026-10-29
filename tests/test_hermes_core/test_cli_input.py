"""CLI 입력 함수 분리 및 유니코드 디코드 예외 방어 테스트 (로드맵 09 / 구현지시서 010)."""

import io
from pathlib import Path
import sys
from typing import Any

import pytest

from hermes_core import cli
from hermes_core.cli_input import read_user_input
from hermes_core.constants import (
    CLI_INPUT_DECODE_MAX_RETRIES,
    CLI_INPUT_DECODE_NOTICE,
    CLI_USER_PROMPT,
)
from hermes_core.llm_client import LLMResponse
from hermes_core.session_db import SessionDB


class SequenceInput:
    """줄 문자열 또는 예외를 차례대로 내주는 가짜 stdin 스트림."""

    def __init__(self, sequence: list[Any]) -> None:
        self._sequence = list(sequence)
        self._index = 0

    def readline(self) -> str:
        if self._index >= len(self._sequence):
            return ""
        item = self._sequence[self._index]
        self._index += 1
        if isinstance(item, BaseException):
            raise item
        return item

    def isatty(self) -> bool:
        return False


class AlwaysDecodeErrorInput:
    """항상 UnicodeDecodeError를 발생하는 가짜 stdin 스트림."""

    def readline(self) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    def isatty(self) -> bool:
        return False


class DummyMockOpenRouterClient:
    """테스트용 가짜 OpenRouter 클라이언트."""

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def chat_completion(self, messages: Any, tools: Any = None) -> LLMResponse:
        return LLMResponse(content="dummy assistant answer", finish_reason="stop")


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_scenario_01_simple_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 1: 입력원 'hello\\n' -> read_user_input() == 'hello'."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
    assert read_user_input() == "hello"


def test_scenario_02_preserves_spaces_and_korean(monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 2: 입력원 '  내 이름은 철수야  \\n' -> 공백과 한글 그대로 '  내 이름은 철수야  '."""
    raw_text = "  내 이름은 철수야  \n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw_text))
    assert read_user_input() == "  내 이름은 철수야  "


def test_scenario_03_prompt_output_without_leading_newline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 3: 입력원 'x\\n', 출력 캡처 -> 출력이 정확히 'User > ' (빈 줄은 함수가 내지 않음)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("x\n"))
    res = read_user_input()
    assert res == "x"
    out = capsys.readouterr().out
    assert out == CLI_USER_PROMPT


def test_scenario_04_eof_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 4: 입력원 '' (끝) -> EOFError가 그대로 올라옴."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(EOFError):
        read_user_input()


def test_scenario_05_keyboard_interrupt_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 5: input이 KeyboardInterrupt를 냄 -> KeyboardInterrupt가 그대로 올라옴."""
    monkeypatch.setattr(
        sys,
        "stdin",
        SequenceInput([KeyboardInterrupt()]),
    )
    with pytest.raises(KeyboardInterrupt):
        read_user_input()


def test_scenario_06_terminal_decode_error_recovers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 6: 터미널 모양 [디코드 오류, 'ok\\n'] -> 반환 'ok', 안내 문구 1번, 프롬프트 2번."""
    err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(sys, "stdin", SequenceInput([err, "ok\n"]))
    res = read_user_input()
    assert res == "ok"

    out = capsys.readouterr().out
    assert out.count(CLI_INPUT_DECODE_NOTICE) == 1
    assert out.count(CLI_USER_PROMPT) == 2


def test_scenario_07_pipe_decode_error_ends_in_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 7: 파이프 모양 바이트 b'first\\n\\xed\\x95\\nsecond\\n'
    -> UnicodeDecodeError가 밖으로 나오지 않고 안내 1번 뒤 EOFError로 끝남.
    """
    raw = io.BytesIO(b"first\n\xed\x95\nsecond\n")
    wrapper = io.TextIOWrapper(raw, encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", wrapper)

    with pytest.raises(EOFError):
        read_user_input()

    out = capsys.readouterr().out
    assert out.count(CLI_INPUT_DECODE_NOTICE) == 1


def test_scenario_08_max_retries_exceeded_raises_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 8: 항상 디코드 오류만 내는 입력원 -> 안내가 정확히 상한(5)번 출력된 뒤 EOFError."""
    monkeypatch.setattr(sys, "stdin", AlwaysDecodeErrorInput())

    with pytest.raises(EOFError):
        read_user_input()

    out = capsys.readouterr().out
    assert out.count(CLI_INPUT_DECODE_NOTICE) == CLI_INPUT_DECODE_MAX_RETRIES


def test_scenario_09_consecutive_counter_resets_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 9: [오류, 오류, 'ok\\n', 오류, 'again\\n'] 두 번 호출
    -> 첫 호출 'ok', 둘째 호출 'again' (성공 시 연속 카운터 초기화).
    """
    err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    stream = SequenceInput([err, err, "ok\n", err, "again\n"])
    monkeypatch.setattr(sys, "stdin", stream)

    res1 = read_user_input()
    assert res1 == "ok"

    res2 = read_user_input()
    assert res2 == "again"

    out = capsys.readouterr().out
    assert out.count(CLI_INPUT_DECODE_NOTICE) == 3


def test_scenario_10_cli_main_recovers_from_decode_error(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 10: cli.main() 실제 모드, 입력원 [디코드 오류, 'exit\\n']
    -> 정상 종료, 안내 문구 1번 및 작별 인사, 세션 종료 사유 user_exit, 대화 메시지 0건.
    """
    fake_key = "test-key-safe-scenario10"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)

    err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(sys, "stdin", SequenceInput([err, "exit\n"]))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    captured = capsys.readouterr()
    assert CLI_INPUT_DECODE_NOTICE in captured.out
    assert "Session saved and closed. Goodbye!" in captured.out

    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT end_reason FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row is not None
    assert row["end_reason"] == "user_exit"

    msg_cursor = db._conn.execute("SELECT COUNT(*) as count FROM messages")
    msg_row = msg_cursor.fetchone()
    assert msg_row["count"] == 0
    db.close()


def test_scenario_11_cli_main_prompt_has_leading_newline(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 11: cli.main() 실제 모드, 입력원 StringIO('exit\\n')
    -> 출력에 '\\nUser > '가 그대로 포함됨 (화면 문자열 보존).
    """
    fake_key = "test-key-safe-scenario11"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)

    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    captured = capsys.readouterr()
    assert "\nUser > " in captured.out
