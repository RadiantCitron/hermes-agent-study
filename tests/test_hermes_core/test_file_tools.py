"""파일 읽기 도구(read_file_tool) 단위 및 디스패처 통합 테스트.

[인수 테스트 시나리오 대응: 020_read_file_도구.md §3]
"""

import json
from pathlib import Path
import pytest

from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.file_tools import READ_FILE_SCHEMA, read_file_tool


@pytest.fixture
def workspace_env(tmp_path: Path):
    """테스트용 작업 폴더 및 파일 구조 구성.

    W 구성:
    - a.txt: line1\nline2\nline3\n
    - empty.txt: 빈 파일
    - crlf.txt: a\r\nb\r\n
    - sub/b.txt
    - binary.bin: \x00\x01
    - latin.txt: \xff\xfe
    - W 밖에 outside.txt (내용 OUTSIDE-SECRET)
    - W/link -> outside.txt
    """
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    outside = (tmp_path / "outside.txt").resolve()
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")

    (workspace / "a.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (workspace / "empty.txt").write_text("", encoding="utf-8")
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\n")

    sub = workspace / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("sub-b", encoding="utf-8")

    (workspace / "binary.bin").write_bytes(b"\x00\x01")
    (workspace / "latin.txt").write_bytes(b"\xff\xfe")

    (workspace / "link").symlink_to(outside)

    return workspace


def test_scenario_01_read_default(workspace_env: Path):
    """시나리오 1: a.txt 기본 읽기."""
    raw = read_file_tool("a.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"] == "1|line1\n2|line2\n3|line3"
    assert res["total_lines"] == 3
    assert res["lines_returned"] == 3
    assert res["truncated"] is False
    assert res["next_offset"] is None


def test_scenario_02_read_offset(workspace_env: Path):
    """시나리오 2: a.txt, offset=2."""
    raw = read_file_tool("a.txt", offset=2, workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"] == "2|line2\n3|line3"
    assert res["offset"] == 2
    assert res["lines_returned"] == 2


def test_scenario_03_read_limit(workspace_env: Path):
    """시나리오 3: a.txt, limit=1."""
    raw = read_file_tool("a.txt", limit=1, workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"] == "1|line1"
    assert res["truncated"] is True
    assert res["next_offset"] == 2


def test_scenario_04_read_offset_and_limit(workspace_env: Path):
    """시나리오 4: a.txt, offset=2, limit=1."""
    raw = read_file_tool("a.txt", offset=2, limit=1, workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"] == "2|line2"
    assert res["next_offset"] == 3


def test_scenario_05_read_offset_beyond_end(workspace_env: Path):
    """시나리오 5: a.txt, offset=4."""
    raw = read_file_tool("a.txt", offset=4, workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "beyond" in res["error"]


def test_scenario_06_invalid_offset_and_limit(workspace_env: Path):
    """시나리오 6: offset=0 / limit=0 / limit=2001."""
    res1 = json.loads(read_file_tool("a.txt", offset=0, workspace=workspace_env))
    assert res1["success"] is False
    assert "offset" in res1["error"]

    res2 = json.loads(read_file_tool("a.txt", limit=0, workspace=workspace_env))
    assert res2["success"] is False
    assert "limit" in res2["error"]

    res3 = json.loads(read_file_tool("a.txt", limit=2001, workspace=workspace_env))
    assert res3["success"] is False
    assert "limit" in res3["error"]


def test_scenario_07_empty_file(workspace_env: Path):
    """시나리오 7: empty.txt."""
    raw = read_file_tool("empty.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"] == ""
    assert res["total_lines"] == 0
    assert res["lines_returned"] == 0
    assert res["truncated"] is False


def test_scenario_08_crlf_handling(workspace_env: Path):
    """시나리오 8: crlf.txt 줄 끝 \\r 제외 확인."""
    raw = read_file_tool("crlf.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"] == "1|a\n2|b"


def test_scenario_09_max_lines_limit(workspace_env: Path):
    """시나리오 9: 2,500줄 파일 (각 줄 x)."""
    p = workspace_env / "lines2500.txt"
    p.write_text("x\n" * 2500, encoding="utf-8")

    raw = read_file_tool("lines2500.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["lines_returned"] == 2000
    assert res["truncated"] is True
    assert res["next_offset"] == 2001


def test_scenario_10_max_chars_limit_multiline(workspace_env: Path):
    """시나리오 10: 줄 3개(각 줄 x를 60,000개)."""
    p = workspace_env / "long3lines.txt"
    p.write_text(("x" * 60000 + "\n") * 3, encoding="utf-8")

    raw = read_file_tool("long3lines.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["lines_returned"] == 1
    assert res["truncated"] is True
    assert res["next_offset"] == 2
    assert len(res["content"]) <= 100_000


def test_scenario_11_single_line_exceeding_max_chars(workspace_env: Path):
    """시나리오 11: 한 줄에 x를 150,000개 (파일 크기는 상한 이내)."""
    p = workspace_env / "long1line.txt"
    p.write_text("x" * 150000, encoding="utf-8")

    raw = read_file_tool("long1line.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"].startswith("1|")
    assert len(res["content"]) == 100_000
    assert res["truncated"] is True
    assert res["next_offset"] is None


def test_scenario_12_binary_file(workspace_env: Path):
    """시나리오 12: binary.bin."""
    raw = read_file_tool("binary.bin", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "binary" in res["error"]


def test_scenario_13_non_utf8_file(workspace_env: Path):
    """시나리오 13: latin.txt."""
    raw = read_file_tool("latin.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "UTF-8" in res["error"]


def test_scenario_14_missing_file(workspace_env: Path):
    """시나리오 14: missing.txt."""
    raw = read_file_tool("missing.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "not found" in res["error"]


def test_scenario_15_directory_rejection(workspace_env: Path):
    """시나리오 15: sub (폴더)."""
    raw = read_file_tool("sub", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "is a directory" in res["error"]


def test_scenario_16_outside_paths_and_symlinks(workspace_env: Path):
    """시나리오 16: ../outside.txt / /etc/hosts / link -> outside 포함, OUTSIDE-SECRET 유출 없음."""
    for path in ["../outside.txt", "/etc/hosts", "link"]:
        raw = read_file_tool(path, workspace=workspace_env)
        res = json.loads(raw)
        assert res["success"] is False
        assert "outside" in res["error"]
        assert "OUTSIDE-SECRET" not in raw


def test_scenario_17_control_chars_in_path(workspace_env: Path):
    """시나리오 17: 경로 a\\nb (줄바꿈 문자가 든 경로)."""
    raw = read_file_tool("a\nb", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "control" in res["error"]


def test_scenario_18_file_too_large(workspace_env: Path):
    """시나리오 18: 크기 1,000,001바이트 파일."""
    p = workspace_env / "toolarge.txt"
    p.write_bytes(b"x" * 1_000_001)

    raw = read_file_tool("toolarge.txt", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "too large" in res["error"]


def test_scenario_19_tilde_not_expanded(workspace_env: Path):
    """시나리오 19: 경로 ~/x."""
    raw = read_file_tool("~/x", workspace=workspace_env)
    res = json.loads(raw)
    assert res["success"] is False
    assert "not found" in res["error"]


def test_scenario_20_dispatcher_blocks_workspace_arg(workspace_env: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 20: 기본 디스패처로 read_file에 workspace 인자를 전달하면 도구 통로가 거부."""
    monkeypatch.chdir(workspace_env)
    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call("read_file", {"path": "a.txt", "workspace": "/"})
    res = json.loads(raw)
    assert res["success"] is False
    assert "workspace" in res["error"]


def test_scenario_21_dispatcher_read_success(workspace_env: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 21: 기본 디스패처로 {"path": "a.txt"} (현재 폴더는 W)."""
    monkeypatch.chdir(workspace_env)
    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call("read_file", {"path": "a.txt"})
    res = json.loads(raw)
    assert res["success"] is True
    assert res["content"].startswith("1|line1")


def test_scenario_22_dispatcher_schema_properties():
    """시나리오 22: get_tools_schema()에 read_file이 있고 properties에 workspace가 없음."""
    dispatcher = create_default_dispatcher()
    schemas = dispatcher.get_tools_schema()
    read_file_def = next((s["function"] for s in schemas if s["function"]["name"] == "read_file"), None)
    assert read_file_def is not None
    assert "workspace" not in read_file_def["parameters"]["properties"]
