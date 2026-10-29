"""CLI 사용자 입력 수신 모듈.

터미널 및 비터미널 환경에서 사용자 입력을 안전하게 읽어 들이며,
유니코드 디코드 예외 발생 시 세션이 비정상 종료되지 않도록 방어한다.
"""

from hermes_core.constants import (
    CLI_INPUT_DECODE_MAX_RETRIES,
    CLI_INPUT_DECODE_NOTICE,
    CLI_USER_PROMPT,
)


def read_user_input(prompt: str = CLI_USER_PROMPT) -> str:
    """사용자로부터 한 줄의 입력을 읽어 반환한다.

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
            return input(prompt)
        except UnicodeDecodeError:
            consecutive_errors += 1
            print(CLI_INPUT_DECODE_NOTICE)
            if consecutive_errors >= CLI_INPUT_DECODE_MAX_RETRIES:
                raise EOFError("Exceeded maximum consecutive UnicodeDecodeError retries")
