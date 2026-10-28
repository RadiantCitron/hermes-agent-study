"""작업 폴더 기준 경로 안전 검증 공용 모듈 (Path Guard).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/path_security.py
- 핵심 역할:
  - 작업 폴더 경로를 일관되게 해석 (`resolve_workspace`).
  - 명령줄 인자에서 검사 대상 경로 추출 (`path_values`).
  - 심볼릭 링크를 포함하여 대상 경로가 작업 폴더 내부에 있는지 엄격히 검증 (`is_inside`).
  - 터미널 도구 및 파일 도구(`read_file`, `write_file`, `patch`)가 공유하는 단일 안전 기준선.
"""

from pathlib import Path
from typing import Optional


def resolve_workspace(workspace: Optional[Path] = None) -> Path:
    """작업 폴더 경로를 확인하고 절대 경로로 변환한다.

    workspace가 None이면 호출 시점의 현재 작업 폴더(Path.cwd())를 쓰고,
    지정된 경우 해당 경로를 resolve()하여 반환한다.
    """
    return (Path(workspace) if workspace is not None else Path.cwd()).resolve()


def path_values(arg: str) -> list[str]:
    """인자에서 경로로 검사할 값을 골라낸다. 옵션 자체는 건너뛰고, 옵션에 붙은 값은 검사 대상이다.

    `--opt=값` → 값, `-fVALUE`(길이 3 이상인 짧은 옵션) → VALUE, `--opt`·`-r`·`-` → 없음, 그 밖 → 인자 전체.
    """
    if arg.startswith("--"):
        _, has_value, value = arg.partition("=")
        return [value] if has_value else []
    if arg.startswith("-"):
        return [arg[2:]] if len(arg) > 2 else []
    return [arg]


def is_inside(value: str, workspace: Path) -> bool:
    """작업 폴더 기준으로 value를 실제 경로로 풀었을 때(심볼릭 링크 포함) 작업 폴더 안인가.

    풀 수 없는 경로(OSError, ValueError, RuntimeError)는 거짓을 반환한다.
    """
    try:
        resolved = (workspace / value).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved.is_relative_to(workspace)
