"""파일 읽기·쓰기·수정 도구 (File Tools).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/file_tools.py
- 핵심 역할:
  - read_file: 작업 폴더 내 텍스트 파일을 줄 번호가 붙은 형식으로 분할 조회.
  - 상한선 가드: 최대 2000줄, 100,000자(출력 합계), 1,000,000바이트(파일 크기).
  - 안전성 가드: NUL 바이트(바이너리) 차단, 비UTF-8 차단, 작업 폴더 외부 탈출 차단.
"""

import fnmatch
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Optional

from hermes_core.constants import (
    FILE_MAX_BYTES,
    FILE_READ_MAX_CHARS,
    FILE_READ_MAX_LINES,
    FILE_WRITE_MAX_CHARS,
    SEARCH_MAX_FILES_SCANNED,
    SEARCH_MAX_LINE_CHARS,
    SEARCH_MAX_MATCHES,
)
from hermes_core.tools.path_guard import (
    check_path,
    check_write_path,
    is_hermes_home_path,
    is_inside,
    is_protected_path,
    resolve_workspace,
)

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a text file with line numbers up to 2000 lines. "
            "Returns lines formatted as 'line_number|content'. "
            "Paths outside the working directory are rejected. "
            "To write or modify files, use write_file or patch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path of the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "description": "The line number to start reading from (1-based, default 1).",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of lines to read (1-{FILE_READ_MAX_LINES}, default {FILE_READ_MAX_LINES}).",
                    "default": FILE_READ_MAX_LINES,
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Create a new file with the specified content. "
            "Only new files can be created; to modify existing files, use patch. "
            "Writing to instruction files or the state folder is not allowed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path of the new file to create.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch",
        "description": (
            "Replace an exact string in a text file. "
            "Replaces exactly one occurrence by default; if old_string occurs multiple times, "
            "provide more surrounding context to make it unique or set replace_all=True. "
            "Modifying instruction files or the state folder is not allowed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path of the file to modify.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find and replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement string.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "If true, replace all occurrences. If false (default), replace exactly one occurrence.",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
}


def _failure(error: str) -> str:
    """실행 실패 시 에러 JSON 반환."""
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def read_file_tool(
    path: str,
    offset: int = 1,
    limit: int = FILE_READ_MAX_LINES,
    workspace: Optional[Path] = None,
) -> str:
    """작업 폴더 안의 텍스트 파일을 줄 번호가 붙은 형태로 읽어 JSON 문자열로 반환한다.

    순서: resolve_workspace -> check_path -> offset/limit 범위 검사 -> 대상 검사 -> 읽기.
    """
    base = resolve_workspace(workspace)
    target_path, err = check_path(path, base)
    if err:
        return _failure(err)

    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
        return _failure("offset must be >= 1.")

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= FILE_READ_MAX_LINES:
        return _failure(f"limit must be between 1 and {FILE_READ_MAX_LINES}.")

    if not target_path.exists():
        return _failure(f"File not found: '{path}'")

    if target_path.is_dir():
        return _failure(f"Path '{path}' is a directory.")

    file_size = target_path.stat().st_size
    if file_size > FILE_MAX_BYTES:
        return _failure(f"File '{path}' is too large ({file_size} bytes > {FILE_MAX_BYTES} bytes limit).")

    try:
        raw_bytes = target_path.read_bytes()
    except OSError as e:
        return _failure(f"Failed to read file '{path}': {e}")

    if b"\x00" in raw_bytes:
        return _failure(f"File '{path}' is binary (contains NUL byte).")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        return _failure(f"File '{path}' is not valid UTF-8: {e}")

    if not text:
        lines: list[str] = []
    else:
        split_lines = text.split("\n")
        if split_lines and split_lines[-1] == "":
            split_lines.pop()
        lines = [line[:-1] if line.endswith("\r") else line for line in split_lines]

    total_lines = len(lines)
    if total_lines == 0:
        if offset > 1:
            return _failure(f"offset {offset} is beyond end of file (0 lines).")
        return json.dumps(
            {
                "success": True,
                "content": "",
                "total_lines": 0,
                "offset": offset,
                "lines_returned": 0,
                "truncated": False,
                "next_offset": None,
            },
            ensure_ascii=False,
        )

    if offset > total_lines:
        return _failure(f"offset {offset} is beyond end of file ({total_lines} lines).")

    start_idx = offset - 1
    target_lines = lines[start_idx : start_idx + limit]

    selected: list[str] = []
    current_chars = 0
    truncated = False
    next_offset: Optional[int] = None

    for i, raw_line in enumerate(target_lines):
        line_num = offset + i
        formatted = f"{line_num}|{raw_line}"
        needed = len(formatted) if not selected else (current_chars + 1 + len(formatted))

        if needed <= FILE_READ_MAX_CHARS:
            selected.append(formatted)
            current_chars = needed
        else:
            truncated = True
            if not selected:
                truncated_line = formatted[:FILE_READ_MAX_CHARS]
                selected.append(truncated_line)
                if line_num < total_lines:
                    next_offset = line_num + 1
                else:
                    next_offset = None
            else:
                next_offset = line_num
            break

    if not truncated:
        last_read_line_num = offset + len(selected) - 1
        if last_read_line_num < total_lines:
            truncated = True
            next_offset = last_read_line_num + 1
        else:
            next_offset = None

    content = "\n".join(selected)

    result = {
        "success": True,
        "content": content,
        "total_lines": total_lines,
        "offset": offset,
        "lines_returned": len(selected),
        "truncated": truncated,
        "next_offset": next_offset,
    }
    return json.dumps(result, ensure_ascii=False)


def write_file_tool(
    path: str,
    content: str,
    workspace: Optional[Path] = None,
) -> str:
    """새 파일을 생성하고 내용을 원자적으로 기록한다.

    순서: resolve_workspace -> check_write_path -> 내용 크기 검사 -> 이미 있는지 검사 -> 부모 폴더 생성 -> 원자적 쓰기.
    """
    base = resolve_workspace(workspace)
    target_path, err = check_write_path(path, base)
    if err or target_path is None:
        return _failure(err or "Invalid path.")

    if len(content) > FILE_WRITE_MAX_CHARS:
        return _failure(f"content is too large ({len(content)} chars > {FILE_WRITE_MAX_CHARS} limit).")

    raw_target = base / path
    if target_path.exists() or raw_target.is_symlink() or raw_target.exists():
        return _failure(f"File '{path}' already exists. To modify it, use patch.")

    # 부모 폴더 생성 (작업 폴더 안임이 판정된 상태)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 같은 폴더에 임시 파일 생성 후 원자적 교체
    tmp_fd = None
    tmp_path_str = None
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            prefix=".tmp_write_",
            dir=str(target_path.parent),
        )
        content_bytes = content.encode("utf-8")
        os.write(tmp_fd, content_bytes)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None

        os.chmod(tmp_path_str, 0o644)

        if target_path.exists():
            os.unlink(tmp_path_str)
            return _failure(f"File '{path}' already exists. To modify it, use patch.")

        os.replace(tmp_path_str, target_path)
    except OSError as e:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path_str and os.path.exists(tmp_path_str):
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
        return _failure(f"Failed to write file '{path}': {e}")

    rel_path = target_path.relative_to(base).as_posix()
    result = {
        "success": True,
        "path": rel_path,
        "chars_written": len(content),
        "bytes_written": len(content.encode("utf-8")),
    }
    return json.dumps(result, ensure_ascii=False)


def patch_tool(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    workspace: Optional[Path] = None,
) -> str:
    """작업 폴더 안의 텍스트 파일의 일치하는 문자열을 교체한다.

    순서: resolve_workspace -> check_write_path -> 대상 검사 -> 문자열 검사 -> 교체 -> 결과 크기 검사 -> 원자적 쓰기.
    """
    base = resolve_workspace(workspace)
    target_path, err = check_write_path(path, base)
    if err or target_path is None:
        return _failure(err or "Invalid path.")

    if not target_path.exists():
        return _failure(f"File not found: '{path}'")

    if not target_path.is_file():
        return _failure(f"Path '{path}' is not a regular file.")

    file_size = target_path.stat().st_size
    if file_size > FILE_MAX_BYTES:
        return _failure(f"File '{path}' is too large ({file_size} bytes > {FILE_MAX_BYTES} bytes limit).")

    try:
        raw_bytes = target_path.read_bytes()
    except OSError as e:
        return _failure(f"Failed to read file '{path}': {e}")

    if b"\x00" in raw_bytes:
        return _failure(f"Cannot patch binary file (contains NUL byte): '{path}'")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        return _failure(f"File '{path}' is not valid UTF-8: {e}")

    if not old_string:
        return _failure("old_string must not be empty.")

    if old_string == new_string:
        return _failure("old_string and new_string must differ.")

    count = text.count(old_string)
    if count == 0:
        return _failure(f"old_string not found in file: '{path}'")

    if count > 1 and not replace_all:
        return _failure(
            f"old_string found {count} times; make it unique or set replace_all=True: '{path}'"
        )

    if replace_all:
        new_text = text.replace(old_string, new_string)
        replacements = count
    else:
        new_text = text.replace(old_string, new_string, 1)
        replacements = 1

    encoded_new = new_text.encode("utf-8")
    if len(encoded_new) > FILE_MAX_BYTES:
        return _failure(
            f"Patch result too large ({len(encoded_new)} bytes > {FILE_MAX_BYTES} bytes limit): '{path}'"
        )

    # 원본 파일 권한 보존
    original_mode = stat.S_IMODE(target_path.stat().st_mode)

    tmp_fd = None
    tmp_path_str = None
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            prefix=".tmp_patch_",
            dir=str(target_path.parent),
        )
        os.chmod(tmp_path_str, original_mode)
        os.write(tmp_fd, encoded_new)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None

        os.replace(tmp_path_str, target_path)
    except OSError as e:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path_str and os.path.exists(tmp_path_str):
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
        return _failure(f"Failed to write file '{path}': {e}")

    rel_path = target_path.relative_to(base).as_posix()
    result = {
        "success": True,
        "path": rel_path,
        "replacements": replacements,
        "chars_before": len(text),
        "chars_after": len(new_text),
    }
    return json.dumps(result, ensure_ascii=False)


SEARCH_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "Search for files by glob pattern or search text content inside files within the working directory. "
            "Use this instead of find or grep in terminal. "
            "If 'pattern' is empty, returns matching file paths (file search mode). "
            "If 'pattern' is provided, returns matching lines with 1-based line numbers (content search mode). "
            "Protected paths (.env*, .git, state folder), symlinks, and binary files are automatically skipped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory or file path to search in (default '.').",
                    "default": ".",
                },
                "pattern": {
                    "type": "string",
                    "description": "Text or regex pattern to search inside file contents. Leave empty ('') to list files matching file_glob.",
                    "default": "",
                },
                "file_glob": {
                    "type": "string",
                    "description": "Filename or relative-path glob filter (e.g. '*.py', 'src/*.py', default '*').",
                    "default": "*",
                },
                "regex": {
                    "type": "boolean",
                    "description": "If true, treat 'pattern' as a regular expression. If false (default), match literal substring.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}


def search_files_tool(
    path: str = ".",
    pattern: str = "",
    file_glob: str = "*",
    regex: bool = False,
    workspace: Optional[Path] = None,
) -> str:
    """작업 폴더 내에서 파일 이름 검색 또는 파일 내용 텍스트 검색을 수행한다."""
    # 인자 정규화 및 검증
    if path is None or (isinstance(path, str) and not path.strip()):
        path = "."
    elif not isinstance(path, str):
        return _failure("path must be a string.")

    if pattern is None:
        pattern = ""
    elif not isinstance(pattern, str):
        return _failure("pattern must be a string.")

    if file_glob is None:
        file_glob = "*"
    elif not isinstance(file_glob, str):
        return _failure("file_glob must be a string.")

    if not isinstance(regex, bool):
        return _failure("regex must be a boolean.")

    if pattern == "" and file_glob.strip() == "":
        return _failure("At least one of 'pattern' or 'file_glob' must be non-empty.")

    eff_glob = file_glob.strip() or "*"

    # 시작 경로 보안 검증
    base = resolve_workspace(workspace)
    raw_start = base / path
    if raw_start.is_symlink():
        return _failure(f"Path '{path}' is a symbolic link; symlinks are not searched.")

    target_path, err = check_path(path, base)
    if err or target_path is None:
        return _failure(err or "Invalid path.")

    if is_hermes_home_path(target_path, base):
        return _failure("Searching inside the Hermes state folder is not allowed.")

    if not target_path.exists():
        return _failure(f"Path not found: '{path}'")

    # 정규식 사전 컴파일
    compiled_re = None
    if pattern and regex:
        try:
            compiled_re = re.compile(pattern)
        except re.error as e:
            return _failure(f"Invalid regex pattern: {e}")

    # 탐색 대상 파일 목록 수집 (결정론적 순회)
    candidate_files: list[Path] = []
    if target_path.is_file():
        # 단일 파일 직접 지정 시에도 eff_glob 일치 확인
        try:
            rel_str = target_path.relative_to(base).as_posix()
        except ValueError:
            rel_str = target_path.name
        if fnmatch.fnmatchcase(target_path.name, eff_glob) or fnmatch.fnmatchcase(rel_str, eff_glob):
            candidate_files.append(target_path)
    else:
        def _collect(cur_dir: Path) -> None:
            try:
                entries = sorted(cur_dir.iterdir(), key=lambda p: p.name)
            except OSError:
                return

            for entry in entries:
                if entry.is_symlink():
                    continue
                try:
                    rel_str = entry.relative_to(base).as_posix()
                except ValueError:
                    continue

                if not is_inside(rel_str, base) or is_protected_path(rel_str, base) or is_hermes_home_path(entry, base):
                    continue

                if entry.is_dir():
                    _collect(entry)
                elif entry.is_file():
                    if fnmatch.fnmatchcase(entry.name, eff_glob) or fnmatch.fnmatchcase(rel_str, eff_glob):
                        candidate_files.append(entry)

        _collect(target_path)

    # 상한 및 매칭
    files_scanned = 0
    truncated = False
    matches: list[dict] = []

    for file_path in candidate_files:
        if files_scanned >= SEARCH_MAX_FILES_SCANNED:
            truncated = True
            break
        files_scanned += 1

        try:
            rel_str = file_path.relative_to(base).as_posix()
        except ValueError:
            rel_str = file_path.name

        if not pattern:
            # 파일 찾기 모드
            if len(matches) >= SEARCH_MAX_MATCHES:
                truncated = True
                break
            matches.append({"path": rel_str})
        else:
            # 내용 찾기 모드
            try:
                st = file_path.stat()
                if st.st_size > FILE_MAX_BYTES:
                    continue
                raw_bytes = file_path.read_bytes()
            except OSError:
                continue

            if b"\x00" in raw_bytes:
                continue

            try:
                text_content = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue

            lines = [ln.rstrip("\r") for ln in text_content.split("\n")]
            file_truncated = False
            for line_num, line in enumerate(lines, start=1):
                is_match = (compiled_re.search(line) is not None) if compiled_re else (pattern in line)
                if is_match:
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        truncated = True
                        file_truncated = True
                        break
                    preview = (
                        line[:SEARCH_MAX_LINE_CHARS] + "..."
                        if len(line) > SEARCH_MAX_LINE_CHARS
                        else line
                    )
                    matches.append({"path": rel_str, "line": line_num, "content": preview})

            if file_truncated:
                break

    return json.dumps(
        {
            "success": True,
            "mode": "content" if pattern else "files",
            "matches": matches,
            "total_matches": len(matches),
            "files_scanned": files_scanned,
            "truncated": truncated,
        },
        ensure_ascii=False,
    )
