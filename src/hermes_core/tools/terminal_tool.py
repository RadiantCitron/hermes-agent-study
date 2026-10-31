"""제한 터미널 실행 도구 (Safe Terminal Execution Tool).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/terminal_tool.py
- 핵심 원리:
  - 셸을 거치지 않고 허용목록의 프로그램만 실행 (명령 문자열을 인자 목록으로 나눠 직접 실행).
  - 작업 폴더에서 실행하고, 인자의 경로가 (심볼릭 링크를 풀어) 작업 폴더 밖이면 실행하지 않음(판정은 path_guard).
    명령줄에 적힌 경로만 검사하며, 프로그램이 스스로 따라가는 링크(예: grep -S)는 검사하지 못한다.
  - 서브프로세스 타임아웃(1~30초, 기본 30초) 및 출력 상한선(10,000자) 가드.
  - JSON 규격 반환으로 도구 디스패처에서 일관된 포맷 유지.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Optional

from hermes_core.constants import TERMINAL_STRIPPED_ENV_NAMES
from hermes_core.tools.path_guard import (
    is_inside,
    is_protected_path,
    path_values,
    resolve_workspace,
)
from hermes_core.tools.ports import ApproverPort, CommandExecutorPort

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


def _child_env() -> dict[str, str]:
    """호출 시점 os.environ 사본에서 TERMINAL_STRIPPED_ENV_NAMES를 뺀 환경."""
    env = dict(os.environ)
    for name in TERMINAL_STRIPPED_ENV_NAMES:
        env.pop(name, None)
    return env


def _execute(args: list[str], cwd: Path, timeout: int) -> str:
    """하위 프로세스를 실행하고 출력 상한 및 예외 처리를 거쳐 JSON 문자열로 반환."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
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


def _run_command(
    args: list[str],
    cwd: Path,
    timeout: int,
    command_executor: Optional[CommandExecutorPort] = None,
) -> str:
    """명령 실행을 command_executor 또는 기본 _execute에 위임."""
    if command_executor is not None:
        try:
            return command_executor(args, cwd, timeout)
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Execution error: {str(e)}",
                    "exit_code": -1,
                },
                ensure_ascii=False,
            )
    return _execute(args, cwd, timeout)


def terminal_tool(
    command: str,
    timeout: int = TERMINAL_MAX_TIMEOUT,
    workspace: Optional[Path] = None,
    approver: Optional[ApproverPort] = None,
    command_executor: Optional[CommandExecutorPort] = None,
) -> str:
    """허용목록의 프로그램 또는 승인된 명령을 작업 폴더에서 셸 없이 실행하고 JSON 문자열로 결과를 반환.

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

    base = resolve_workspace(workspace)

    if args[0] in ALLOWED_COMMANDS:
        for arg in args[1:]:
            values = path_values(arg)
            if not all(is_inside(value, base) for value in values):
                return _failure(f"Path '{arg}' is outside the workspace.")
            if any(is_protected_path(value, base) for value in values):
                return _failure(f"Path '{arg}' refers to a protected file or folder (.env*, .git).")
        return _run_command(args, base, timeout, command_executor)

    if approver is None:
        return _failure(f"Command '{args[0]}' is not allowed. Allowed: {', '.join(ALLOWED_COMMANDS)}")

    for arg in args:
        values = path_values(arg)
        if any(is_protected_path(value, base) for value in values):
            return _failure(f"Path '{arg}' refers to a protected file or folder (.env*, .git).")

    outcome = approver.request(args)
    if not outcome.approved:
        return _failure(outcome.message)

    return _run_command(args, base, timeout, command_executor)
