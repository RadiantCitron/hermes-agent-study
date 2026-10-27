"""제한 터미널 실행 도구 (Safe Terminal Execution Tool).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/terminal_tool.py
- 핵심 원리:
  - 쉘 명령어 실행 시 서브프로세스 타임아웃(기본 30초) 및 출력 상한선(10,000자) 가드.
  - JSON 규격 반환으로 도구 디스패처에서 일관된 포맷 유지.
"""

import json
import subprocess
from typing import Any

TERMINAL_MAX_OUTPUT_CHARS = 10_000

TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Execute a shell command in the local environment with a timeout and return output.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30).",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
}


def terminal_tool(command: str, timeout: int = 30) -> str:
    """단일 쉘 명령어를 실행하고 JSON 문자열로 결과를 반환."""
    command = command.strip()
    if not command:
        return json.dumps({"success": False, "error": "Command cannot be empty."}, ensure_ascii=False)

    try:
        proc = subprocess.run(
            command,
            shell=True,
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
