"""CLI 사용자 입력 수신 모듈.

표준 입출력이 모두 터미널이면 prompt_toolkit으로, 그 밖에는 input()으로 읽으며,
유니코드 디코드 예외 발생 시 세션이 비정상 종료되지 않도록 방어한다.
"""

import sys
from typing import Any

from hermes_core.constants import (
    CLI_INPUT_DECODE_MAX_RETRIES,
    CLI_INPUT_DECODE_NOTICE,
    CLI_PROMPT_TOOLKIT_MISSING_NOTICE,
    CLI_USER_PROMPT,
)

# prompt_toolkit 미설치 안내를 이미 출력했는지 (프로세스당 한 번만 출력)
_missing_notice_shown = False


def is_interactive_terminal() -> bool:
    """표준 입력과 표준 출력이 모두 터미널(tty)인지 판정한다.

    호출 시점의 sys.stdin/sys.stdout을 읽는다. isatty가 없거나 예외를 내면 False.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def read_with_prompt_toolkit(prompt: str, *, input: Any = None, output: Any = None) -> str:
    """prompt_toolkit으로 입력을 읽는다 (터미널 경로).

    Enter는 전송, Ctrl+J는 줄바꿈이다. 붙여넣은 여러 줄은 전송 없이 한 입력이 된다.
    Ctrl+C/Ctrl+D는 KeyboardInterrupt/EOFError로 그대로 올라온다.
    prompt_toolkit은 미설치 환경에서도 이 모듈이 import되도록 함수 안에서 불러온다.

    Args:
        prompt: 프롬프트 문자열.
        input: 가짜 입력(시험용). None이면 실제 표준 입력.
        output: 가짜 출력(시험용). None이면 실제 표준 출력.

    Raises:
        ImportError: prompt_toolkit이 설치되어 있지 않을 때.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("c-j")
    def _newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    session = PromptSession(
        prompt,
        multiline=True,
        key_bindings=bindings,
        input=input,
        output=output,
    )
    return session.prompt()


def _read_once(prompt: str) -> str:
    """입력 경로를 골라 한 번 읽는다."""
    global _missing_notice_shown
    if is_interactive_terminal():
        try:
            return read_with_prompt_toolkit(prompt)
        except ImportError:
            if not _missing_notice_shown:
                _missing_notice_shown = True
                print(CLI_PROMPT_TOOLKIT_MISSING_NOTICE)
    return input(prompt)


def read_user_input(prompt: str = CLI_USER_PROMPT) -> str:
    """사용자로부터 입력을 읽어 반환한다.

    입력의 공백이나 개행 문자를 임의로 가공(strip 등)하지 않고 그대로 돌려준다.
    EOFError와 KeyboardInterrupt는 잡지 않고 그대로 상위로 전파한다.
    UnicodeDecodeError 발생 시 안내 문구를 출력하고 동일 프롬프트로 재시도하며,
    연속 실패 횟수가 상한(CLI_INPUT_DECODE_MAX_RETRIES)에 도달하면 EOFError를 발생시킨다.

    Args:
        prompt: 사용자 입력 프롬프트 문자열 (기본값: CLI_USER_PROMPT).

    Returns:
        사용자가 입력한 원본 문자열.

    Raises:
        EOFError: 입력 스트림 종료(EOF) 시 또는 연속 디코드 실패 상한 도달 시.
        KeyboardInterrupt: 사용자 인터럽트(Ctrl+C) 발생 시.
    """
    consecutive_errors = 0
    while True:
        try:
            return _read_once(prompt)
        except UnicodeDecodeError:
            consecutive_errors += 1
            print(CLI_INPUT_DECODE_NOTICE)
            if consecutive_errors >= CLI_INPUT_DECODE_MAX_RETRIES:
                raise EOFError("Exceeded maximum consecutive UnicodeDecodeError retries")
