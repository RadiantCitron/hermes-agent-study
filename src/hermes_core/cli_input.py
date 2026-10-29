"""CLI 사용자 입력 수신 모듈.

표준 입출력이 모두 터미널이면 prompt_toolkit으로, 그 밖에는 input()으로 읽으며,
유니코드 디코드 예외 발생 시 세션이 비정상 종료되지 않도록 방어한다.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import importlib.util
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


def multiline_input_available() -> bool:
    """줄바꿈 입력 안내(CLI_MULTILINE_HINT)를 출력할 수 있는 환경인지 판정한다.

    표준 입출력이 모두 터미널이고 prompt_toolkit이 설치되어 있으면 True.
    라이브러리를 실제로 import하지 않고 사양(spec) 존재 여부로 판정한다.
    """
    if not is_interactive_terminal():
        return False
    try:
        return importlib.util.find_spec("prompt_toolkit") is not None
    except (ImportError, ValueError, AttributeError):
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


def discard_pending_input() -> None:
    """질문을 띄우기 전에 터미널에 미리 들어와 있던 입력을 모두 버린다.

    커널 입력 버퍼와 prompt_toolkit이 다음 프롬프트로 넘기려고 보관한 키(typeahead)를 함께 비운다.
    터미널이 아니면(파이프로 넣은 답은 의도된 입력이므로) 아무것도 하지 않는다.
    버리기에 실패해도 예외를 내지 않는다 — 질문은 여전히 사람의 Enter를 기다린다.
    """
    if not is_interactive_terminal():
        return
    try:
        import termios
    except ImportError:
        pass
    else:
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (termios.error, OSError, ValueError, AttributeError):
            pass
    try:
        from prompt_toolkit.input import create_input
        from prompt_toolkit.input.typeahead import clear_typeahead
    except ImportError:
        return
    try:
        clear_typeahead(create_input())
    except Exception:
        pass


@contextmanager
def hold_typeahead() -> Iterator[None]:
    """프롬프트 사이(모델 처리 중)에 사용자가 미리 친 키를 원래 바이트 그대로 보존한다.

    prompt_toolkit 프롬프트가 끝나면 터미널은 줄 입력 모드(cooked)로 돌아가, 그동안 친 키가 화면에 찍히고
    Enter(\\r)가 \\n(다음 프롬프트에서는 Ctrl+J 줄바꿈)으로 바뀐다. 이 구간 동안 ICANON·ECHO·ICRNL을 꺼서
    다음 프롬프트가 미리 친 키를 정상 키로 읽게 한다. ISIG는 켜 두어 처리 중 Ctrl+C는 그대로 동작한다.
    빠져나갈 때는 예외가 나도 들어올 때의 상태로 복원한다.
    터미널이 아니거나 prompt_toolkit이 없으면(input() 폴백은 줄 입력 모드가 필요) 아무것도 하지 않는다.
    """
    saved: Any = None
    termios: Any = None
    fd = -1
    if multiline_input_available():
        try:
            import termios

            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~termios.ICRNL
            attrs[3] &= ~(termios.ICANON | termios.ECHO)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            saved = None
    try:
        yield
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except Exception:
                pass


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


def read_user_input(prompt: str = CLI_USER_PROMPT, *, discard_typeahead: bool = False) -> str:
    """사용자로부터 입력을 읽어 반환한다.

    입력의 공백이나 개행 문자를 임의로 가공(strip 등)하지 않고 그대로 돌려준다.
    EOFError와 KeyboardInterrupt는 잡지 않고 그대로 상위로 전파한다.
    UnicodeDecodeError 발생 시 안내 문구를 출력하고 동일 프롬프트로 재시도하며,
    연속 실패 횟수가 상한(CLI_INPUT_DECODE_MAX_RETRIES)에 도달하면 EOFError를 발생시킨다.

    Args:
        prompt: 사용자 입력 프롬프트 문자열 (기본값: CLI_USER_PROMPT).
        discard_typeahead: True면 읽기 전에 미리 들어와 있던 입력을 버린다(discard_pending_input).
            승인처럼 질문을 보기 전에 친 키가 답이 되면 안 되는 질문에 쓴다.

    Returns:
        사용자가 입력한 원본 문자열.

    Raises:
        EOFError: 입력 스트림 종료(EOF) 시 또는 연속 디코드 실패 상한 도달 시.
        KeyboardInterrupt: 사용자 인터럽트(Ctrl+C) 발생 시.
    """
    if discard_typeahead:
        discard_pending_input()
    consecutive_errors = 0
    while True:
        try:
            return _read_once(prompt)
        except UnicodeDecodeError:
            consecutive_errors += 1
            print(CLI_INPUT_DECODE_NOTICE)
            if consecutive_errors >= CLI_INPUT_DECODE_MAX_RETRIES:
                raise EOFError("Exceeded maximum consecutive UnicodeDecodeError retries")
