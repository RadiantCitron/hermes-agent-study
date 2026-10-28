"""스킬 시스템 도구 및 스킬 발견 모듈.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/skills_tool.py
- 핵심 역할:
  - 파일 시스템 기반 스킬(절차 기억)의 메타데이터 파싱 및 스킬 발견.
  - 외부 라이브러리(PyYAML)에 의존하지 않는 안전한 자체 최소 머리말 파서 제공.
  - 스킬 디렉터리 내 읽기 전용 스캔 및 경로 격리 안전 보장.
"""

import json
from pathlib import Path
from typing import Any, Optional

from hermes_core.constants import (
    FILE_MAX_BYTES,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_FILE_NAME,
    SKILL_LINKED_DIRS,
    SKILL_LINKED_FILES_MAX_PER_KIND,
    SKILL_NAME_MAX_CHARS,
    SKILL_VIEW_MAX_CHARS,
    get_skills_dir,
)
from hermes_core.tools.path_guard import check_path, is_inside, is_protected_path


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


def skills_list_tool(
    category: Optional[str] = None,
    skills_root: Optional[Path] = None,
) -> str:
    """사용 가능한 스킬 목록을 JSON 문자열로 반환한다.

    - skills_root가 None이면 호출 시점의 get_skills_dir()를 사용한다.
    - 스킬 폴더를 생성하지 않는다 (읽기 전용).
    - category 필터가 주어지면(공백 제외) 해당 분류의 스킬만 반환한다.
    - 본문은 포함하지 않고 메타데이터(name, description, category, path)만 반환한다.
    """
    if skills_root is None:
        target_root = get_skills_dir().resolve()
    else:
        target_root = Path(skills_root).resolve()

    all_skills = discover_skills(target_root)

    # category 필터링 (빈 문자열·공백뿐이면 필터 없음)
    filter_cat = category.strip() if isinstance(category, str) and category.strip() else None
    if filter_cat:
        filtered_skills = [s for s in all_skills if s.get("category") == filter_cat]
    else:
        filtered_skills = all_skills

    # categories는 필터링 결과 기준 고유 분류 목록 정렬
    categories = sorted(
        list({s["category"] for s in filtered_skills if s.get("category") is not None})
    )

    if not filtered_skills:
        res: dict[str, Any] = {
            "success": True,
            "skills": [],
            "categories": [],
            "count": 0,
            "message": "No skills found.",
        }
    else:
        res = {
            "success": True,
            "skills": filtered_skills,
            "categories": categories,
            "count": len(filtered_skills),
        }

    return json.dumps(res, ensure_ascii=False)


# =============================================================================
# OpenAI 규격 함수 호출 스키마
# =============================================================================

SKILLS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_list",
        "description": (
            "List available skills (procedural memories). Returns metadata only (name, description, category, path). "
            "To read the full skill content or linked files, use skill_view."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category to filter skills by. If omitted or empty, lists all skills.",
                },
            },
        },
    },
}


def _read_skill_text_file(target_file: Path) -> tuple[Optional[str], bool, int, Optional[str]]:
    """스킬 본문 및 부속 파일을 읽는 공통 절차.

    - 파일 크기 상한(FILE_MAX_BYTES) 초과 검사
    - NUL 바이트 존재 시 이진 파일 거부
    - utf-8-sig 디코딩 (BOM 제거)
    - SKILL_VIEW_MAX_CHARS 기준 문자 수 자르기 (truncated 및 total_chars 반환)
    반환: (content, truncated, total_chars, error_message)
    """
    try:
        size = target_file.stat().st_size
    except OSError as e:
        return None, False, 0, f"Failed to access file '{target_file.name}': {e}"

    if size > FILE_MAX_BYTES:
        return None, False, 0, f"File size ({size} bytes) exceeds limit ({FILE_MAX_BYTES} bytes)."

    try:
        raw_bytes = target_file.read_bytes()
    except OSError as e:
        return None, False, 0, f"Failed to read file '{target_file.name}': {e}"

    if b"\x00" in raw_bytes:
        return None, False, 0, "Binary file detected (null byte)."

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return None, False, 0, f"Failed to decode file as UTF-8: {e}"

    total_chars = len(text)
    if total_chars > SKILL_VIEW_MAX_CHARS:
        content = text[:SKILL_VIEW_MAX_CHARS]
        truncated = True
    else:
        content = text
        truncated = False

    return content, truncated, total_chars, None


def _collect_linked_files(skill_dir: Path) -> dict[str, list[str]]:
    """스킬 디렉터리 내 허용된 4대 폴더(references, templates, assets, scripts)의 유효한 파일 목록 수집."""
    resolved_skill_dir = skill_dir.resolve()
    result: dict[str, list[str]] = {}

    for d in SKILL_LINKED_DIRS:
        sub_dir = resolved_skill_dir / d
        if not sub_dir.exists() or not sub_dir.is_dir():
            continue

        rel_list: list[str] = []
        for f in sub_dir.rglob("*"):
            if not f.is_file():
                continue
            try:
                rel_f = f.relative_to(resolved_skill_dir)
            except ValueError:
                continue

            rel_posix = rel_f.as_posix()
            # 보호 이름 검사 (.env*, .git 등)
            if is_protected_path(rel_posix, resolved_skill_dir):
                continue
            # 탈출 및 심볼릭 링크 검사
            if not is_inside(rel_posix, resolved_skill_dir):
                continue

            rel_list.append(rel_posix)

        if rel_list:
            rel_list.sort()
            if len(rel_list) > SKILL_LINKED_FILES_MAX_PER_KIND:
                rel_list = rel_list[:SKILL_LINKED_FILES_MAX_PER_KIND]
            result[d] = rel_list

    return result


def skill_view_tool(
    name: str,
    file_path: Optional[str] = None,
    skills_root: Optional[Path] = None,
) -> str:
    """스킬의 SKILL.md 본문 또는 부속 파일을 조회하여 JSON 문자열로 반환한다.

    - skills_root가 None이면 get_skills_dir()를 사용하며 항상 resolve() 기준으로 검사한다.
    - name과 file_path는 path_guard.check_path를 통해 제어문자, 경로 이탈, 보호 이름을 엄격히 검사한다.
    - name 해석: path 정확 일치 우선 -> 폴더명 또는 머리말 name 일치. 중복 시 candidates, 없을 시 available_skills 반환.
    - file_path 미지정 시: SKILL.md 본문(20,000자 상한, truncated, total_chars) 및 linked_files 목록 반환.
    - file_path 지정 시: 스킬 폴더 내 부속 파일 조회 (존재하지 않으면 available_files 반환).
    - 어떤 경우에도 파일을 생성, 수정, 삭제하지 않는다 (읽기 전용).
    """
    if skills_root is None:
        target_root = get_skills_dir().resolve()
    else:
        target_root = Path(skills_root).resolve()

    # 1. name 검사
    if not isinstance(name, str) or not name.strip():
        return json.dumps(
            {"success": False, "error": "Skill name is required and cannot be empty."},
            ensure_ascii=False,
        )
    trimmed_name = name.strip()

    # 2. check_path로 제어문자, 이탈, 보호 이름 차단
    _, err = check_path(trimmed_name, target_root)
    if err:
        return json.dumps(
            {"success": False, "error": f"Invalid skill name or path: {err}"},
            ensure_ascii=False,
        )

    # 3. 루트 디렉터리 존재 여부 확인 및 discover_skills
    if not target_root.exists() or not target_root.is_dir():
        return json.dumps(
            {"success": False, "error": f"Skills directory '{target_root}' does not exist."},
            ensure_ascii=False,
        )

    all_skills = discover_skills(target_root)

    # 이름 해석: 1) path == trimmed_name 우선
    exact_matches = [s for s in all_skills if s.get("path") == trimmed_name]
    if len(exact_matches) == 1:
        matched_skill = exact_matches[0]
    elif len(exact_matches) > 1:
        matched_skill = exact_matches[0]
    else:
        # 2) 폴더 이름 또는 머리말 name 일치
        name_matches = [
            s
            for s in all_skills
            if s.get("name") == trimmed_name
            or (s.get("path") and Path(s["path"]).name == trimmed_name)
        ]
        if len(name_matches) == 1:
            matched_skill = name_matches[0]
        elif len(name_matches) > 1:
            candidates = [s["path"] for s in name_matches if s.get("path")]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Multiple skills matched '{trimmed_name}'. Please specify by path.",
                    "candidates": candidates,
                },
                ensure_ascii=False,
            )
        else:
            available = [s["path"] for s in all_skills[:20] if s.get("path")]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{trimmed_name}' not found.",
                    "available_skills": available,
                },
                ensure_ascii=False,
            )

    skill_path_str = matched_skill.get("path") or ""
    skill_dir = (target_root / skill_path_str).resolve()

    # 4. file_path 분기
    trimmed_file_path = file_path.strip() if isinstance(file_path, str) and file_path.strip() else None

    if trimmed_file_path is None:
        # 본문 모드: SKILL.md
        skill_file = skill_dir / SKILL_FILE_NAME
        if not skill_file.exists() or not skill_file.is_file():
            return json.dumps(
                {"success": False, "error": f"SKILL.md not found in skill '{skill_path_str}'."},
                ensure_ascii=False,
            )

        content, truncated, total_chars, read_err = _read_skill_text_file(skill_file)
        if read_err:
            return json.dumps({"success": False, "error": read_err}, ensure_ascii=False)

        linked = _collect_linked_files(skill_dir)
        res_main: dict[str, Any] = {
            "success": True,
            "name": matched_skill.get("name"),
            "description": matched_skill.get("description"),
            "category": matched_skill.get("category"),
            "path": skill_path_str,
            "content": content,
            "truncated": truncated,
            "total_chars": total_chars,
        }
        if linked:
            res_main["linked_files"] = linked

        return json.dumps(res_main, ensure_ascii=False)
    else:
        # 부속 파일 모드
        resolved_sub, path_err = check_path(trimmed_file_path, skill_dir)
        if path_err:
            return json.dumps(
                {"success": False, "error": f"Invalid file_path: {path_err}"},
                ensure_ascii=False,
            )

        if not resolved_sub.exists():
            available_files = _collect_linked_files(skill_dir)
            return json.dumps(
                {
                    "success": False,
                    "error": f"File '{trimmed_file_path}' not found in skill '{skill_path_str}'.",
                    "available_files": available_files,
                },
                ensure_ascii=False,
            )

        if not resolved_sub.is_file():
            return json.dumps(
                {"success": False, "error": f"Path '{trimmed_file_path}' is a directory, not a file."},
                ensure_ascii=False,
            )

        content, truncated, total_chars, read_err = _read_skill_text_file(resolved_sub)
        if read_err:
            return json.dumps({"success": False, "error": read_err}, ensure_ascii=False)

        res_sub: dict[str, Any] = {
            "success": True,
            "name": matched_skill.get("name"),
            "path": skill_path_str,
            "file": trimmed_file_path,
            "content": content,
            "truncated": truncated,
            "total_chars": total_chars,
        }
        return json.dumps(res_sub, ensure_ascii=False)


SKILL_VIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skill_view",
        "description": (
            "View the full content of a skill (SKILL.md) or one of its linked/attached files. "
            "To view the main skill, specify only 'name'. To view an attached file, also specify 'file_path'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name or relative path of the skill to view.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional relative path of an attached file inside the skill directory to view.",
                },
            },
            "required": ["name"],
        },
    },
}
