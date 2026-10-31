"""hermes_core.tools.file_domain - 순수 파일 텍스트 도메인 모듈.

디스크 I/O나 외부 라이브러리 없이 순수 인메모리 텍스트 분할, 줄 번호 포맷팅,
글자 수 절단, 패치 문자열 치환, 정규식/리터럴 라인 매칭을 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from hermes_core.constants import (
    FILE_MAX_BYTES,
    FILE_READ_MAX_CHARS,
    FILE_READ_MAX_LINES,
    SEARCH_MAX_LINE_CHARS,
)


@dataclass(frozen=True)
class SliceLinesResult:
    """줄 슬라이싱 결과 불변 데이터 클래스."""

    content: str
    total_lines: int
    offset: int
    lines_returned: int
    truncated: bool
    next_offset: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        """도구 반환 JSON 딕셔너리로 변환."""
        return {
            "content": self.content,
            "total_lines": self.total_lines,
            "offset": self.offset,
            "lines_returned": self.lines_returned,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
        }


@dataclass(frozen=True)
class PatchResult:
    """텍스트 패치 결과 불변 데이터 클래스."""

    new_text: str
    replacements: int
    chars_before: int
    chars_after: int
    encoded_bytes: bytes


def split_text_to_lines(text: str) -> list[str]:
    """텍스트를 줄 목록으로 분할하고 CRLF 및 마지막 빈 줄을 정규화한다."""
    if not text:
        return []
    split_lines = text.split("\n")
    if split_lines and split_lines[-1] == "":
        split_lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in split_lines]


def slice_lines_with_numbers(
    text: str,
    offset: int = 1,
    limit: int = FILE_READ_MAX_LINES,
    max_chars: int = FILE_READ_MAX_CHARS,
) -> SliceLinesResult:
    """텍스트에서 지정된 범위의 줄을 가져와 줄 번호를 붙이고 상한을 검사한다."""
    lines = split_text_to_lines(text)
    total_lines = len(lines)

    if total_lines == 0:
        if offset > 1:
            raise ValueError(f"offset {offset} is beyond end of file (0 lines).")
        return SliceLinesResult(
            content="",
            total_lines=0,
            offset=offset,
            lines_returned=0,
            truncated=False,
            next_offset=None,
        )

    if offset > total_lines:
        raise ValueError(f"offset {offset} is beyond end of file ({total_lines} lines).")

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

        if needed <= max_chars:
            selected.append(formatted)
            current_chars = needed
        else:
            truncated = True
            if not selected:
                truncated_line = formatted[:max_chars]
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

    return SliceLinesResult(
        content=content,
        total_lines=total_lines,
        offset=offset,
        lines_returned=len(selected),
        truncated=truncated,
        next_offset=next_offset,
    )


def apply_text_patch(
    text: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    max_bytes: int = FILE_MAX_BYTES,
    file_path: Optional[str] = None,
) -> PatchResult:
    """텍스트 내 일치하는 문자열을 교체하고 유효성 및 크기 제한을 검증한다."""
    path_suffix = f": '{file_path}'" if file_path else ""

    if not old_string:
        raise ValueError("old_string must not be empty.")

    if old_string == new_string:
        raise ValueError("old_string and new_string must differ.")

    count = text.count(old_string)
    if count == 0:
        raise ValueError(f"old_string not found in file{path_suffix}")

    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string found {count} times; make it unique or set replace_all=True{path_suffix}"
        )

    if replace_all:
        new_text = text.replace(old_string, new_string)
        replacements = count
    else:
        new_text = text.replace(old_string, new_string, 1)
        replacements = 1

    encoded_new = new_text.encode("utf-8")
    if len(encoded_new) > max_bytes:
        raise ValueError(
            f"Patch result too large ({len(encoded_new)} bytes > {max_bytes} bytes limit){path_suffix}"
        )

    return PatchResult(
        new_text=new_text,
        replacements=replacements,
        chars_before=len(text),
        chars_after=len(new_text),
        encoded_bytes=encoded_new,
    )


def match_text_lines(
    text: str,
    pattern: str,
    compiled_re: Optional[re.Pattern] = None,
    max_line_chars: int = SEARCH_MAX_LINE_CHARS,
) -> list[tuple[int, str]]:
    """텍스트를 줄 단위로 검사하여 패턴과 일치하는 줄 번호와 미리보기를 반환한다."""
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    matches: list[tuple[int, str]] = []

    for line_num, line in enumerate(lines, start=1):
        is_match = (compiled_re.search(line) is not None) if compiled_re else (pattern in line)
        if is_match:
            preview = (
                line[:max_line_chars] + "..."
                if len(line) > max_line_chars
                else line
            )
            matches.append((line_num, preview))

    return matches
