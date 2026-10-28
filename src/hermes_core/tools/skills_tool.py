"""스킬 시스템 도구 및 스킬 발견 모듈.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/skills_tool.py
- 핵심 역할:
  - 파일 시스템 기반 스킬(절차 기억)의 메타데이터 파싱 및 스킬 발견.
  - 외부 라이브러리(PyYAML)에 의존하지 않는 안전한 자체 최소 머리말 파서 제공.
  - 스킬 디렉터리 내 읽기 전용 스캔 및 경로 격리 안전 보장.
"""

from pathlib import Path
from typing import Any, Optional

from hermes_core.constants import (
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_FILE_NAME,
    SKILL_NAME_MAX_CHARS,
)
from hermes_core.tools.path_guard import is_inside, is_protected_path


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """YAML 머리말(frontmatter)을 안전하게 파싱하여 (메타데이터 딕셔너리, 본문) 튜플로 반환한다.

    - 외부 라이브러리(PyYAML)를 사용하지 않는 자체 최소 파서.
    - BOM(utf-8-sig) 및 CRLF(\\r\\n) 줄바꿈을 안전하게 정규화한다.
    - 문서가 '---' 줄로 시작하고 그 뒤 첫 '---' 닫는 줄이 있을 때만 머리말로 파싱한다.
    - 닫는 '---'가 없거나 첫 줄이 '---'가 아니면 머리말 없음({}, content)으로 취급한다.
    - 최상위(들여쓰기 없는) 'key: value' 줄만 읽고, 중첩 구조, 주석(#), 콜론 없는 줄, 리스트 줄(- )은 건너뛴다.
    - 문자열 양끝 따옴표를 벗기며, [a, b] 형태의 인라인 리스트를 지원한다.
    """
    if content.startswith("\ufeff"):
        content = content[1:]

    # 줄바꿈 통일 (CRLF 및 CR을 LF로 정규화)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    if not lines or lines[0].strip() != "---":
        return {}, content

    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break

    if close_idx == -1:
        return {}, content

    frontmatter: dict[str, Any] = {}
    for line in lines[1:close_idx]:
        # 들여쓰기된 줄은 최상위 키가 아니므로 무시
        if line.startswith((" ", "\t")):
            continue

        stripped = line.strip()
        # 주석 줄 및 빈 줄 무시
        if not stripped or stripped.startswith("#"):
            continue
        # 리스트 블록 줄('- ') 무시
        if stripped.startswith("- "):
            continue
        # 콜론 없는 줄 무시
        if ":" not in stripped:
            continue

        key, _, raw_val = stripped.partition(":")
        key = key.strip()
        raw_val = raw_val.strip()

        if not key:
            continue

        # 값 파싱
        if raw_val.startswith("[") and raw_val.endswith("]"):
            # 인라인 리스트 형태: [a, b]
            items = [item.strip().strip("'\"") for item in raw_val[1:-1].split(",") if item.strip()]
            val: Any = items
        elif (raw_val.startswith('"') and raw_val.endswith('"')) or (raw_val.startswith("'") and raw_val.endswith("'")):
            val = raw_val[1:-1]
        else:
            val = raw_val

        frontmatter[key] = val

    body = "\n".join(lines[close_idx + 1:])
    return frontmatter, body


def discover_skills(skills_root: Path) -> list[dict[str, Optional[str]]]:
    """스킬 루트 디렉터리를 스캔하여 발견된 스킬 메타데이터 목록을 반환한다.

    - 스킬 루트 디렉터리가 없거나 디렉터리가 아닌 경우 빈 목록을 반환하며 디렉터리를 생성하지 않는다.
    - 루트 바로 아래의 SKILL.md 파일은 스킬로 인식하지 않는다.
    - 보호 이름(.git, .github, .hub, .env* 등) 및 루트 외부를 가리키는 심볼릭 링크는 제외한다.
    - 파싱 또는 읽기에 실패한 개별 스킬 파일은 무시하고 건너뛴다.
    - 결과는 (category or "", name, path) 기준으로 정렬되어 일관된 순서를 보장한다.
    """
    try:
        resolved_root = Path(skills_root).resolve()
    except (OSError, ValueError, RuntimeError):
        return []

    if not resolved_root.exists() or not resolved_root.is_dir():
        return []

    skills: list[dict[str, Optional[str]]] = []

    for skill_file in resolved_root.rglob(SKILL_FILE_NAME):
        # 1. 루트 바로 아래의 SKILL.md는 제외
        if skill_file.parent == resolved_root:
            continue

        # 2. 상대 경로 계산
        try:
            rel_file = skill_file.relative_to(resolved_root)
        except ValueError:
            continue

        skill_dir = skill_file.parent
        try:
            rel_dir = skill_dir.relative_to(resolved_root)
        except ValueError:
            continue

        # 3. 경로 구성요소 제외 규칙 (.git, .github, .hub, .env* 등)
        skip = False
        for part in rel_dir.parts:
            lowered = part.casefold()
            if part in (".git", ".github", ".hub") or lowered.startswith(".env") or lowered == ".git":
                skip = True
                break
        if skip:
            continue

        # 4. path_guard 안전성 검사 (보호 경로 및 탈출 여부)
        rel_posix = rel_file.as_posix()
        if is_protected_path(rel_posix, resolved_root):
            continue
        if not is_inside(rel_posix, resolved_root):
            continue

        # 5. 파일 읽기 및 머리말 파싱
        try:
            text = skill_file.read_text(encoding="utf-8-sig")
            frontmatter, body = parse_frontmatter(text)
        except Exception:
            continue

        # 6. 필드 추출 및 상한 정규화
        folder_name = skill_dir.name

        # name 추출
        raw_name = frontmatter.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        else:
            name = folder_name

        if len(name) > SKILL_NAME_MAX_CHARS:
            name = name[: SKILL_NAME_MAX_CHARS - 3] + "..."

        # description 추출
        raw_desc = frontmatter.get("description")
        if isinstance(raw_desc, str) and raw_desc.strip():
            desc = raw_desc.strip()
        else:
            desc = ""
            for line in body.split("\n"):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                desc = s
                break

        if len(desc) > SKILL_DESCRIPTION_MAX_CHARS:
            desc = desc[: SKILL_DESCRIPTION_MAX_CHARS - 3] + "..."

        # category 추출 (상대 경로 구성요소가 3개 이상이면 첫 번째 요소)
        parts = rel_file.parts
        if len(parts) >= 3:
            category: Optional[str] = parts[0]
        else:
            category = None

        path_str = rel_dir.as_posix()

        skills.append(
            {
                "name": name,
                "description": desc,
                "category": category,
                "path": path_str,
            }
        )

    # 7. 정렬: (category or "", name or "", path or "")
    skills.sort(key=lambda s: (s["category"] or "", s["name"] or "", s["path"] or ""))
    return skills
