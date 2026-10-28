"""제한 터미널 실행 도구 (Safe Terminal Execution Tool).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/terminal_tool.py
- 핵심 원리:
  - 셸을 거치지 않고 허용목록의 프로그램만 실행 (명령 문자열을 인자 목록으로 나눠 직접 실행).
  - 작업 폴더에서 실행하고, 인자의 경로가 (심볼릭 링크를 풀어) 작업 폴더 밖이면 실행하지 않음.
    명령줄에 적힌 경로만 검사하며, 프로그램이 스스로 따라가는 링크(예: grep -S)는 검사하지 못한다.
  - 서브프로세스 타임아웃(1~30초, 기본 30초) 및 출력 상한선(10,000자) 가드.
  - JSON 규격 반환으로 도구 디스패처에서 일관된 포맷 유지.
"""

import json
import shlex
from pathlib import Path
import subprocess
from typing import Any, Optional

TERMINAL_MAX_OUTPUT_CHARS = 10_000
TERMINAL_MAX_TIMEOUT = 30

# 읽기·출력 중심이고 옵션만으로 다른 프로그램을 실행하거나 파일을 쓰지 못하는 프로그램만 허용한다.
ALLOWED_COMMANDS = ("echo", "pwd", "ls", "cat", "head", "tail", "wc", "grep")

TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": (
            "Run an allowed program without a shell and return its output. "
            f"Allowed programs: {', '.join(ALLOWED_COMMANDS)}. "
            "Pipes, redirection, variables and command substitution are not interpreted. "
            "Paths in arguments must stay inside the working directory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to run: an allowed program name followed by its arguments.",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (1-{TERMINAL_MAX_TIMEOUT}, default {TERMINAL_MAX_TIMEOUT}).",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
}


def _failure(error: str) -> str:
    """실행 전에 거부한 경우의 실패 JSON."""
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def _path_values(arg: str) -> list[str]:
    """인자에서 경로로 검사할 값을 골라낸다. 옵션 자체는 건너뛰고, 옵션에 붙은 값은 검사 대상이다.

    `--opt=값` → 값, `-fVALUE`(길이 3 이상인 짧은 옵션) → VALUE, `--opt`·`-r`·`-` → 없음, 그 밖 → 인자 전체.
    """
    if arg.startswith("--"):
        _, has_value, value = arg.partition("=")
        return [value] if has_value else []
    if arg.startswith("-"):
        return [arg[2:]] if len(arg) > 2 else []
    return [arg]


def _is_inside(value: str, workspace: Path) -> bool:
    """작업 폴더 기준으로 value를 실제 경로로 풀었을 때(심볼릭 링크 포함) 작업 폴더 안인가."""
    try:
        resolved = (workspace / value).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved.is_relative_to(workspace)


def terminal_tool(command: str, timeout: int = TERMINAL_MAX_TIMEOUT, workspace: Optional[Path] = None) -> str:
    """허용목록의 프로그램 하나를 작업 폴더에서 셸 없이 실행하고 JSON 문자열로 결과를 반환.

    workspace가 None이면 호출 시점의 현재 작업 폴더를 쓴다.
    """
    command = command.strip()
    if not command:
        return _failure("Command cannot be empty.")

    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= TERMINAL_MAX_TIMEOUT:
        return _failure(f"Invalid timeout {timeout!r}: must be between 1 and {TERMINAL_MAX_TIMEOUT} seconds.")

    try:
        args = shlex.split(command)
    except ValueError as e:
        return _failure(f"Cannot parse command: {e}")

    if args[0] not in ALLOWED_COMMANDS:
        return _failure(f"Command '{args[0]}' is not allowed. Allowed: {', '.join(ALLOWED_COMMANDS)}")

    base = (Path(workspace) if workspace is not None else Path.cwd()).resolve()
    for arg in args[1:]:
        if not all(_is_inside(value, base) for value in _path_values(arg)):
            return _failure(f"Path '{arg}' is outside the workspace.")

    try:
        proc = subprocess.run(
            args,
            cwd=base,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        stdout = proc.stdout
        stderr = proc.stderr

        # 출력 절단 가드
        if len(stdout) > TERMINAL_MAX_OUTPUT_CHARS:
            stdout = stdout[:TERMINAL_MAX_OUTPUT_CHARS] + "\n...[stdout truncated]..."
        if len(stderr) > TERMINAL_MAX_OUTPUT_CHARS:
            stderr = stderr[:TERMINAL_MAX_OUTPUT_CHARS] + "\n...[stderr truncated]..."

        result = {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        result = {
            "success": False,
            "error": f"Command timed out after {timeout} seconds.",
            "exit_code": -1,
        }
    except Exception as e:
        result = {
            "success": False,
            "error": f"Execution error: {str(e)}",
            "exit_code": -1,
        }

    return json.dumps(result, ensure_ascii=False)
