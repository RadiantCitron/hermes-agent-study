"""tests.test_hermes_core.test_file_domain - 파일 도구 순수 도메인 모듈 단위 시험.

디스크 I/O 없이 순수 인메모리 텍스트 연산 및 불변식, AST 순수성을 독립 검증한다.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import pytest

from hermes_core.tools.file_domain import (
    apply_text_patch,
    match_text_lines,
    slice_lines_with_numbers,
    split_text_to_lines,
)


def test_scenario_r21_file_domain_slice_empty_and_normal():
    """시나리오 1: 빈 텍스트 및 일반 텍스트 줄 슬라이싱 검증."""
    # 1. 빈 문자열 offset=1
    res_empty = slice_lines_with_numbers("", offset=1)
    assert res_empty.content == ""
    assert res_empty.total_lines == 0
    assert res_empty.lines_returned == 0
    assert res_empty.truncated is False
    assert res_empty.next_offset is None
    assert res_empty.to_dict() == {
        "content": "",
        "total_lines": 0,
        "offset": 1,
        "lines_returned": 0,
        "truncated": False,
        "next_offset": None,
    }

    # 2. 빈 문자열 offset=2 -> 예외
    with pytest.raises(ValueError, match="offset 2 is beyond end of file \\(0 lines\\)\\."):
        slice_lines_with_numbers("", offset=2)

    # 3. 일반 텍스트 offset=1, limit=2 (절단 발생)
    text = "line1\nline2\nline3"
    res1 = slice_lines_with_numbers(text, offset=1, limit=2)
    assert res1.content == "1|line1\n2|line2"
    assert res1.total_lines == 3
    assert res1.lines_returned == 2
    assert res1.truncated is True
    assert res1.next_offset == 3

    # 4. 일반 텍스트 offset=2, limit=5 (끝까지 읽음)
    res2 = slice_lines_with_numbers(text, offset=2, limit=5)
    assert res2.content == "2|line2\n3|line3"
    assert res2.total_lines == 3
    assert res2.lines_returned == 2
    assert res2.truncated is False
    assert res2.next_offset is None


def test_scenario_r21_file_domain_slice_crlf_and_trailing_newline():
    """시나리오 2: CRLF 개행 및 마지막 빈 줄 정규화 검증."""
    raw = "hello\r\nworld\r\n"
    lines = split_text_to_lines(raw)
    assert lines == ["hello", "world"]

    res = slice_lines_with_numbers(raw, offset=1, limit=2)
    assert res.content == "1|hello\n2|world"
    assert res.total_lines == 2
    assert res.lines_returned == 2
    assert res.truncated is False
    assert res.next_offset is None


def test_scenario_r21_file_domain_slice_truncation_max_chars():
    """시나리오 3: max_chars 누적 상한선 초과 절단 및 단일 긴 라인 절단 검증."""
    # 1. 두 번째 줄에서 max_chars 누적 초과
    text = ("a" * 50) + "\n" + ("b" * 50)
    # 1|{50자} -> 52자, 다음 줄 2|{50자} 더하면 52 + 1 + 52 = 105자 > 60자
    res = slice_lines_with_numbers(text, offset=1, limit=10, max_chars=60)
    assert res.lines_returned == 1
    assert res.truncated is True
    assert res.next_offset == 2
    assert res.content == "1|" + ("a" * 50)

    # 2. 첫 줄 자체가 max_chars를 초과하여 절단
    # 2-1. 단일 줄만 있는 경우 (다음 줄이 없으므로 next_offset=None)
    long_single = "x" * 100
    res_single = slice_lines_with_numbers(long_single, offset=1, limit=10, max_chars=30)
    assert res_single.lines_returned == 1
    assert res_single.truncated is True
    assert res_single.next_offset is None
    assert len(res_single.content) == 30
    assert res_single.content == "1|" + ("x" * 28)

    # 2-2. 다음 줄이 있는 경우 (다음 줄 번호인 next_offset=2)
    long_multi = ("x" * 100) + "\ny"
    res_multi = slice_lines_with_numbers(long_multi, offset=1, limit=10, max_chars=30)
    assert res_multi.lines_returned == 1
    assert res_multi.truncated is True
    assert res_multi.next_offset == 2
    assert len(res_multi.content) == 30
    assert res_multi.content == "1|" + ("x" * 28)


def test_scenario_r21_file_domain_apply_patch_success():
    """시나리오 4: 단일 치환 및 replace_all 치환 정상 성공 검증."""
    # 1. 단일 치환
    text1 = "hello world\nhello universe"
    patch1 = apply_text_patch(text1, old_string="world", new_string="earth", replace_all=False)
    assert patch1.new_text == "hello earth\nhello universe"
    assert patch1.replacements == 1
    assert patch1.chars_before == len(text1)
    assert patch1.chars_after == len(patch1.new_text)
    assert patch1.encoded_bytes == patch1.new_text.encode("utf-8")

    # 2. replace_all 치환
    text2 = "foo bar foo baz"
    patch2 = apply_text_patch(text2, old_string="foo", new_string="qux", replace_all=True)
    assert patch2.new_text == "qux bar qux baz"
    assert patch2.replacements == 2
    assert patch2.chars_before == len(text2)
    assert patch2.chars_after == len(patch2.new_text)


def test_scenario_r21_file_domain_apply_patch_validation_errors():
    """시나리오 5: 패치 인자 유효성 및 크기 제한 오류 검증."""
    text = "alpha beta gamma"

    # 1. old_string 공백
    with pytest.raises(ValueError, match="old_string must not be empty\\."):
        apply_text_patch(text, old_string="", new_string="test")

    # 2. old_string == new_string
    with pytest.raises(ValueError, match="old_string and new_string must differ\\."):
        apply_text_patch(text, old_string="same", new_string="same")

    # 3. 미발견 문자열
    with pytest.raises(ValueError, match="old_string not found in file: 'test\\.py'"):
        apply_text_patch(text, old_string="omega", new_string="test", file_path="test.py")

    # 4. 다중 일치인데 replace_all=False
    multi_text = "a b a c"
    with pytest.raises(
        ValueError,
        match="old_string found 2 times; make it unique or set replace_all=True: 'test\\.py'",
    ):
        apply_text_patch(multi_text, old_string="a", new_string="z", replace_all=False, file_path="test.py")

    # 5. 크기 초과
    with pytest.raises(ValueError, match="Patch result too large .* bytes limit\\): 'test\\.py'"):
        apply_text_patch(
            "hello",
            old_string="hello",
            new_string="x" * 100,
            max_bytes=50,
            file_path="test.py",
        )


def test_scenario_r21_file_domain_match_text_lines_literal_and_regex():
    """시나리오 6: 리터럴 및 정규식 라인 매칭과 미리보기 길이 제한 검증."""
    text = "alpha\nbeta gamma\nalpha delta"

    # 1. 리터럴 매칭
    matches_lit = match_text_lines(text, pattern="alpha")
    assert matches_lit == [(1, "alpha"), (3, "alpha delta")]

    # 2. 정규식 매칭
    compiled = re.compile(r"b\w+")
    matches_re = match_text_lines(text, pattern=r"b\w+", compiled_re=compiled)
    assert matches_re == [(2, "beta gamma")]

    # 3. 긴 라인 프리뷰 절단
    long_line = "x" * 500
    text_with_long = f"short\n{long_line}"
    matches_long = match_text_lines(
        text_with_long,
        pattern="x",
        max_line_chars=100,
    )
    assert len(matches_long) == 1
    line_num, preview = matches_long[0]
    assert line_num == 2
    assert preview == ("x" * 100) + "..."
    assert len(preview) == 103


def test_scenario_r21_file_tools_regression_and_line_count():
    """시나리오 7: file_tools.py 800줄 이하 검증."""
    repo_root = Path(__file__).resolve().parents[2]
    file_tools_path = repo_root / "src/hermes_core/tools/file_tools.py"
    assert file_tools_path.exists()
    lines = file_tools_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 800, f"file_tools.py is {len(lines)} lines (must be < 800)"


def test_scenario_r21_pure_file_domain_no_io_or_infra():
    """시나리오 8: file_domain.py AST 순수성 검증 (디스크 I/O 및 인프라 임포트 0건)."""
    repo_root = Path(__file__).resolve().parents[2]
    domain_path = repo_root / "src/hermes_core/tools/file_domain.py"
    assert domain_path.exists()

    tree = ast.parse(domain_path.read_text(encoding="utf-8"), filename=str(domain_path))

    forbidden_modules = {"sqlite3", "httpx", "tempfile", "os.path", "shutil"}
    forbidden_calls = {"open", "os.replace", "read_bytes", "write_bytes"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden_modules, f"Forbidden from-import: {node.module}"

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, f"Forbidden function call: {node.func.id}"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_calls, f"Forbidden method call: {node.func.attr}"
