"""작업 폴더 기준 경로 안전 검증 공용 모듈 (Path Guard).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/path_security.py
- 핵심 역할:
  - 작업 폴더 경로를 일관되게 해석 (`resolve_workspace`).
  - 명령줄 인자에서 검사 대상 경로 추출 (`path_values`).
  - 심볼릭 링크를 포함하여 대상 경로가 작업 폴더 내부에 있는지 엄격히 검증 (`is_inside`).
  - 터미널 도구 및 파일 도구(`read_file`, `write_file`, `patch`)가 공유하는 단일 안전 기준선.
"""

from pathlib import Path, PurePath
from typing import Optional

from hermes_core.constants import (
    INSTRUCTION_FILE_NAMES,
    PROTECTED_NAME_EXACT,
    PROTECTED_NAME_PREFIXES,
    get_hermes_home,
)


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


def _is_protected_component(name: str) -> bool:
    """단일 경로 구성요소가 보호 접두사(.env)로 시작하거나 완전 일치(.git)하는지 검사한다 (대소문자 무시)."""
    lowered = name.casefold()
    if any(lowered.startswith(prefix.casefold()) for prefix in PROTECTED_NAME_PREFIXES):
        return True
    if any(lowered == exact.casefold() for exact in PROTECTED_NAME_EXACT):
        return True
    return False


def is_protected_path(value: str, workspace: Path) -> bool:
    """경로 인자나 실제 풀린 경로에 보호 파일/폴더(.env*, .git)가 포함되어 있는지 검사한다.

    1. PurePath(value).parts ('.' 및 '..' 제외) 중 하나라도 보호 대상이면 참.
    2. 풀린 경로((workspace / value).resolve())에서 작업 폴더를 뺀 상대 경로의 parts 중 하나라도 보호 대상이면 참.
    - 풀 수 없는 경로(OSError, ValueError, RuntimeError)는 거짓이 아니라 참(보수적)으로 본다.
    - 작업 폴더 밖이라 상대 경로를 만들 수 없을 때는 1번 검사만 수행한다.
    """
    # 1. 입력된 경로 자체의 구성요소 검사
    pure_parts = [part for part in PurePath(value).parts if part not in (".", "..")]
    if any(_is_protected_component(part) for part in pure_parts):
        return True

    # 2. 풀린 경로 기준 상대 경로 구성요소 검사
    try:
        resolved = (workspace / value).resolve()
    except (OSError, ValueError, RuntimeError):
        return True

    resolved_workspace = workspace.resolve()
    try:
        rel = resolved.relative_to(resolved_workspace)
        rel_parts = [part for part in rel.parts if part not in (".", "..")]
        if any(_is_protected_component(part) for part in rel_parts):
            return True
    except ValueError:
        pass

    return False


def check_path(path: str, workspace: Path) -> tuple[Optional[Path], Optional[str]]:
    """파일 도구용 경로 검증. 제어문자 및 작업 폴더 이탈, 보호 파일/폴더 여부를 확인한다.

    제어문자가 있으면 (None, "Path contains control characters."),
    작업 폴더 밖이면 (None, f"Path '{path}' is outside the workspace."),
    보호 파일/폴더이면 (None, f"Path '{path}' refers to a protected file or folder (.env*, .git).")을 반환한다.
    통과하면 ((workspace / path).resolve(), None)을 반환하며 ~는 확장하지 않는다.
    """
    for ch in path:
        code = ord(ch)
        if code < 32 or code == 127 or code == 0x85 or ch in ("\u2028", "\u2029"):
            return None, "Path contains control characters."

    if not is_inside(path, workspace):
        return None, f"Path '{path}' is outside the workspace."

    if is_protected_path(path, workspace):
        return None, f"Path '{path}' refers to a protected file or folder (.env*, .git)."

    return (workspace / path).resolve(), None


def check_write_path(path: str, workspace: Path) -> tuple[Optional[Path], Optional[str]]:
    """파일 쓰기·수정용 경로 안전성 검증.

    1. check_path로 제어문자 및 작업 폴더 이탈을 먼저 검증한다.
    2. 파일 이름(대소문자 무시)이 지시문 파일(INSTRUCTION_FILE_NAMES)과 일치하면 거부한다.
    3. 작업 폴더 바로 아래의 지시문 파일 링크가 가리키는 실제 파일인 경우 거부한다.
    4. Hermes 상태 폴더(get_hermes_home().resolve()) 내부 경로인 경우 거부한다.
    5. 모두 통과하면 (풀린 절대 경로, None)을 반환한다.
    """
    target_path, err = check_path(path, workspace)
    if err or target_path is None:
        return None, err

    # 2. 파일 이름 대소문자 무시 검사
    lower_instr_names = {name.casefold() for name in INSTRUCTION_FILE_NAMES}
    if target_path.name.casefold() in lower_instr_names:
        return None, f"Writing to instruction file '{target_path.name}' is not allowed."

    # 3. 작업 폴더 바로 아래 지시문 파일 링크가 가리키는 대상과 동일한지 검사
    for name in INSTRUCTION_FILE_NAMES:
        instr_path = workspace / name
        if instr_path.is_symlink() or instr_path.exists():
            try:
                resolved_instr = instr_path.resolve()
                if resolved_instr == target_path:
                    return None, f"Writing to instruction file '{target_path.name}' is not allowed."
            except (OSError, ValueError, RuntimeError):
                pass

    # 4. Hermes 상태 폴더 내부 검사
    # workspace가 hermes_home의 안쪽(엄격한 하위 디렉터리, !=)인 경우 건너뜀
    hermes_home = get_hermes_home().resolve()
    resolved_workspace = workspace.resolve()
    is_workspace_inside_home = (
        resolved_workspace.is_relative_to(hermes_home)
        and resolved_workspace != hermes_home
    )
    if not is_workspace_inside_home and target_path.is_relative_to(hermes_home):
        return None, "Writing inside the Hermes state folder is not allowed."

    return target_path, None
