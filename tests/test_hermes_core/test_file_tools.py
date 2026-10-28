"""파일 읽기 도구(read_file_tool) 단위 및 디스패처 통합 테스트.

[인수 테스트 시나리오 대응: 020_read_file_도구.md §3]
"""

import json
from pathlib import Path
import pytest

from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.file_tools import (
    PATCH_SCHEMA,
    READ_FILE_SCHEMA,
    WRITE_FILE_SCHEMA,
    patch_tool,
    read_file_tool,
    write_file_tool,
)


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


def test_030_scenario_13_read_allowed_for_instruction_and_state_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """030 시나리오 13: read_file_tool은 AGENTS.md 및 .hermes/memories/MEMORY.md 읽기를 허용함."""
    w = (tmp_path / "w_read_allowed").resolve()
    w.mkdir()
    hermes_home = w / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    (w / "AGENTS.md").write_text("standing instructions\n", encoding="utf-8")
    mem_dir = hermes_home / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("memory content\n", encoding="utf-8")

    raw1 = read_file_tool("AGENTS.md", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is True
    assert "1|standing instructions" in res1["content"]

    raw2 = read_file_tool(".hermes/memories/MEMORY.md", workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is True
    assert "1|memory content" in res2["content"]


# =============================================================================
# 로드맵 040 write_file 인수 테스트 시나리오 (§3)
# =============================================================================


@pytest.fixture
def write_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """040 write_file 테스트용 작업 폴더 W 구성."""
    w = (tmp_path / "workspace").resolve()
    w.mkdir(exist_ok=True)

    outside = (tmp_path / "outside.txt").resolve()
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")

    (w / "a.txt").write_text("line1\n", encoding="utf-8")
    (w / "sub").mkdir(exist_ok=True)
    (w / "link").symlink_to(outside)
    (w / "dang").symlink_to(w / "ghost.txt")

    hermes_home = (tmp_path / "hermes_home").resolve()
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    out_dir = (tmp_path / "OUT").resolve()
    out_dir.mkdir(exist_ok=True)

    return w, outside, out_dir


def test_040_scenario_01_create_new_file(write_test_env):
    """시나리오 1: new.txt, hello\\n -> success true, 바이트 일치, path, chars_written."""
    w, _, _ = write_test_env
    raw = write_file_tool("new.txt", "hello\n", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert (w / "new.txt").read_bytes() == b"hello\n"
    assert res["path"] == "new.txt"
    assert res["chars_written"] == 6
    assert res["bytes_written"] == 6


def test_040_scenario_02_create_with_parent_dirs(write_test_env):
    """시나리오 2: d1/d2/new.txt, x (부모 폴더 없음) -> 부모 생성 및 파일 생성."""
    w, _, _ = write_test_env
    raw = write_file_tool("d1/d2/new.txt", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert (w / "d1/d2/new.txt").read_text(encoding="utf-8") == "x"
    assert res["path"] == "d1/d2/new.txt"


def test_040_scenario_03_existing_file_rejection(write_test_env):
    """시나리오 3: a.txt, NEW -> already exists와 patch 포함, a.txt 내용 불변."""
    w, _, _ = write_test_env
    raw = write_file_tool("a.txt", "NEW", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "already exists" in res["error"]
    assert "patch" in res["error"]
    assert (w / "a.txt").read_text(encoding="utf-8") == "line1\n"


def test_040_scenario_04_existing_dir_rejection(write_test_env):
    """시나리오 4: sub (기존 폴더), x -> already exists 포함."""
    w, _, _ = write_test_env
    raw = write_file_tool("sub", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "already exists" in res["error"]


def test_040_scenario_05_crlf_and_korean(write_test_env):
    """시나리오 5: kr.txt, 한글\\r\\n -> 바이트 정확 일치 (줄바꿈 변환 없음)."""
    w, _, _ = write_test_env
    content = "한글\r\n"
    raw = write_file_tool("kr.txt", content, workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert (w / "kr.txt").read_bytes() == content.encode("utf-8")


def test_040_scenario_06_max_chars_limit(write_test_env):
    """시나리오 6: 길이 100,000 성공, 100,001 실패(too large, 파일 미생성)."""
    w, _, _ = write_test_env
    res1 = json.loads(write_file_tool("exact100k.txt", "x" * 100_000, workspace=w))
    assert res1["success"] is True
    assert (w / "exact100k.txt").exists()

    res2 = json.loads(write_file_tool("over100k.txt", "x" * 100_001, workspace=w))
    assert res2["success"] is False
    assert "too large" in res2["error"]
    assert not (w / "over100k.txt").exists()


def test_040_scenario_07_empty_content(write_test_env):
    """시나리오 7: empty.txt, '' -> success true, 크기 0."""
    w, _, _ = write_test_env
    raw = write_file_tool("empty_created.txt", "", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert (w / "empty_created.txt").stat().st_size == 0


def test_040_scenario_08_outside_paths_no_creation(write_test_env):
    """시나리오 8: ../x.txt, OUT/x.txt, ../newdir/x.txt -> outside 포함, 어떤 파일/폴더도 미생성."""
    w, _, out_dir = write_test_env
    parent_before = set(w.parent.iterdir())
    out_before = set(out_dir.iterdir())

    for target in ["../x.txt", str(out_dir / "x.txt"), "../newdir/x.txt"]:
        raw = write_file_tool(target, "content", workspace=w)
        res = json.loads(raw)
        assert res["success"] is False
        assert "outside" in res["error"]

    assert set(w.parent.iterdir()) == parent_before
    assert set(out_dir.iterdir()) == out_before


def test_040_scenario_09_instruction_and_state_folder_rejection(write_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 9: AGENTS.md, .hermes/x.txt, newdir/SOUL.md -> false, newdir 폴더도 미생성."""
    w, _, _ = write_test_env
    hermes_home = w / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    res1 = json.loads(write_file_tool("AGENTS.md", "c", workspace=w))
    assert res1["success"] is False
    assert not (w / "AGENTS.md").exists()

    res2 = json.loads(write_file_tool(".hermes/x.txt", "c", workspace=w))
    assert res2["success"] is False
    assert not (hermes_home / "x.txt").exists()

    res3 = json.loads(write_file_tool("newdir/SOUL.md", "c", workspace=w))
    assert res3["success"] is False
    assert not (w / "newdir").exists()


def test_040_scenario_10_dangling_symlink_rejection(write_test_env):
    """시나리오 10: dang (끊어진 링크), x -> already exists 포함, ghost.txt 미생성."""
    w, _, _ = write_test_env
    raw = write_file_tool("dang", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "already exists" in res["error"]
    assert not (w / "ghost.txt").exists()


def test_040_scenario_11_outside_symlink_rejection(write_test_env):
    """시나리오 11: link, OVERWRITE -> outside 포함, outside.txt 내용 유지."""
    w, outside, _ = write_test_env
    raw = write_file_tool("link", "OVERWRITE", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "outside" in res["error"]
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"


def test_040_scenario_12_control_chars_in_path(write_test_env):
    """시나리오 12: 경로 a\\nb -> control 포함."""
    w, _, _ = write_test_env
    raw = write_file_tool("a\nb", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "control" in res["error"]


def test_040_scenario_13_atomic_replace_failure_cleanup(write_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 13: os.replace가 OSError를 던지면 Failed to write 포함, fail.txt 미생성, 임시 파일 미잔존."""
    w, _, _ = write_test_env

    def _broken_replace(src, dst):
        raise OSError("disk failure simulation")

    monkeypatch.setattr("os.replace", _broken_replace)

    raw = write_file_tool("fail.txt", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "Failed to write" in res["error"]
    assert not (w / "fail.txt").exists()

    # 임시 파일이 남아 있지 않은지 확인
    tmp_files = [f for f in w.iterdir() if f.name.startswith(".tmp_write_")]
    assert len(tmp_files) == 0


def test_040_scenario_14_no_leftover_temp_files(write_test_env):
    """시나리오 14: 성공한 쓰기 직후 W 폴더 목록에 임시 파일이 남아 있지 않음."""
    w, _, _ = write_test_env
    write_file_tool("success.txt", "content", workspace=w)
    tmp_files = [f for f in w.iterdir() if f.name.startswith(".tmp_write_")]
    assert len(tmp_files) == 0


def test_040_scenario_15_file_permissions(write_test_env):
    """시나리오 15: 새로 만든 파일의 권한 0o644."""
    w, _, _ = write_test_env
    write_file_tool("perm.txt", "hello", workspace=w)
    mode = (w / "perm.txt").stat().st_mode & 0o777
    assert mode == 0o644


def test_040_scenario_16_dispatcher_schema_write_file():
    """시나리오 16: get_tools_schema()에 write_file이 있고 properties에 workspace가 없음."""
    dispatcher = create_default_dispatcher()
    schemas = dispatcher.get_tools_schema()
    wf_def = next((s["function"] for s in schemas if s["function"]["name"] == "write_file"), None)
    assert wf_def is not None
    assert "workspace" not in wf_def["parameters"]["properties"]
    assert "path" in wf_def["parameters"]["properties"]
    assert "content" in wf_def["parameters"]["properties"]


def test_040_scenario_17_dispatcher_blocks_workspace(write_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 17: 기본 디스패처로 workspace 넘기면 거부, n.txt 미생성."""
    w, _, _ = write_test_env
    monkeypatch.chdir(w)
    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call("write_file", {"path": "n.txt", "content": "x", "workspace": "/"})
    res = json.loads(raw)
    assert res["success"] is False
    assert "workspace" in res["error"]
    assert not (w / "n.txt").exists()


def test_040_scenario_18_dispatcher_write_success(write_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 18: 기본 디스패처로 {'path': 'n.txt', 'content': 'x'} -> W/n.txt 생성."""
    w, _, _ = write_test_env
    monkeypatch.chdir(w)
    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call("write_file", {"path": "n.txt", "content": "x"})
    res = json.loads(raw)
    assert res["success"] is True
    assert (w / "n.txt").read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------
# [050 단계 인수 테스트 시나리오: 050_patch_도구.md §3]
# ---------------------------------------------------------


@pytest.fixture
def patch_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """050 patch_tool 인수 테스트용 환경 구성.

    작업 폴더 W 구성:
    - p.txt: alpha\nbeta\nalpha\n
    - q.txt: one two three\n
    - crlf.txt: a\r\nb\r\nc\r\n
    - sub/ (폴더)
    - binary.bin: \x00\x01
    - latin.txt: \xff\xfe
    - AGENTS.md: rules\n
    - W 밖에 outside.txt (OUTSIDE-SECRET)
    - W/link -> outside.txt
    - W/agents_link -> AGENTS.md
    - HERMES_HOME: W 밖 임시 폴더
    """
    hermes_home = (tmp_path / "hermes_home").resolve()
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    outside = (tmp_path / "outside.txt").resolve()
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")

    (workspace / "p.txt").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    (workspace / "q.txt").write_text("one two three\n", encoding="utf-8")
    (workspace / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\n")

    sub = workspace / "sub"
    sub.mkdir()
    (sub / "inside.txt").write_text("sub-inside", encoding="utf-8")

    (workspace / "binary.bin").write_bytes(b"\x00\x01")
    (workspace / "latin.txt").write_bytes(b"\xff\xfe")
    (workspace / "AGENTS.md").write_text("rules\n", encoding="utf-8")

    (workspace / "link").symlink_to(outside)
    (workspace / "agents_link").symlink_to(workspace / "AGENTS.md")

    return workspace, outside, hermes_home


def test_050_scenario_01_single_replacement(patch_test_env):
    """시나리오 1: q.txt, two -> 2 => success true, replacements == 1, 파일 내용 one 2 three\\n."""
    w, _, _ = patch_test_env
    raw = patch_tool("q.txt", "two", "2", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["replacements"] == 1
    assert res["chars_before"] == len("one two three\n")
    assert res["chars_after"] == len("one 2 three\n")
    assert (w / "q.txt").read_text(encoding="utf-8") == "one 2 three\n"


def test_050_scenario_02_multiple_occurrences_without_replace_all(patch_test_env):
    """시나리오 2: p.txt, alpha -> X (replace_all 없음) => success false, 오류에 2 times 포함, 파일 그대로."""
    w, _, _ = patch_test_env
    raw = patch_tool("p.txt", "alpha", "X", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "2 times" in res["error"]
    assert (w / "p.txt").read_text(encoding="utf-8") == "alpha\nbeta\nalpha\n"


def test_050_scenario_03_multiple_occurrences_with_replace_all(patch_test_env):
    """시나리오 3: p.txt, alpha -> X, replace_all=True => replacements == 2, 파일 X\\nbeta\\nX\\n."""
    w, _, _ = patch_test_env
    raw = patch_tool("p.txt", "alpha", "X", replace_all=True, workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["replacements"] == 2
    assert (w / "p.txt").read_text(encoding="utf-8") == "X\nbeta\nX\n"


def test_050_scenario_04_missing_string(patch_test_env):
    """시나리오 4: q.txt, missing -> x => success false, 오류에 not found 포함, 파일 그대로."""
    w, _, _ = patch_test_env
    raw = patch_tool("q.txt", "missing", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "not found" in res["error"]
    assert (w / "q.txt").read_text(encoding="utf-8") == "one two three\n"


def test_050_scenario_05_invalid_strings(patch_test_env):
    """시나리오 5: q.txt, two -> two / q.txt, '' -> x => 각각 success false, differ / empty 포함."""
    w, _, _ = patch_test_env
    raw1 = patch_tool("q.txt", "two", "two", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is False
    assert "differ" in res1["error"]

    raw2 = patch_tool("q.txt", "", "x", workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is False
    assert "empty" in res2["error"]

    assert (w / "q.txt").read_text(encoding="utf-8") == "one two three\n"


def test_050_scenario_06_deletion(patch_test_env):
    """시나리오 6: q.txt, ' two' -> '' (삭제) => 파일 one three\\n."""
    w, _, _ = patch_test_env
    raw = patch_tool("q.txt", " two", "", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["replacements"] == 1
    assert (w / "q.txt").read_text(encoding="utf-8") == "one three\n"


def test_050_scenario_07_multiline_replacement(patch_test_env):
    """시나리오 7: p.txt, alpha\\nbeta -> Z (여러 줄, 유일) => 파일 Z\\nalpha\\n."""
    w, _, _ = patch_test_env
    raw = patch_tool("p.txt", "alpha\nbeta", "Z", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["replacements"] == 1
    assert (w / "p.txt").read_text(encoding="utf-8") == "Z\nalpha\n"


def test_050_scenario_08_crlf_preservation(patch_test_env):
    """시나리오 8: crlf.txt, b -> B => 파일 바이트 b'a\\r\\nB\\r\\nc\\r\\n' (\\r\\n 유지)."""
    w, _, _ = patch_test_env
    raw = patch_tool("crlf.txt", "b", "B", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert (w / "crlf.txt").read_bytes() == b"a\r\nB\r\nc\r\n"


def test_050_scenario_09_crlf_exact_matching(patch_test_env):
    """시나리오 9: crlf.txt, a\\nb -> x (LF로 적음) => success false, 오류에 not found 포함, 파일 그대로."""
    w, _, _ = patch_test_env
    raw = patch_tool("crlf.txt", "a\nb", "x", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "not found" in res["error"]
    assert (w / "crlf.txt").read_bytes() == b"a\r\nb\r\nc\r\n"


def test_050_scenario_10_permission_preservation(patch_test_env):
    """시나리오 10: 권한 0o755인 exec.sh(old\\n), old -> new => success true, 권한 여전히 0o755."""
    import stat
    w, _, _ = patch_test_env
    exec_sh = w / "exec.sh"
    exec_sh.write_text("old\n", encoding="utf-8")
    exec_sh.chmod(0o755)

    raw = patch_tool("exec.sh", "old", "new", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert (exec_sh.stat().st_mode & 0o777) == 0o755
    assert exec_sh.read_text(encoding="utf-8") == "new\n"


def test_050_scenario_11_not_found_and_not_regular_file(patch_test_env):
    """시나리오 11: nofile.txt / sub => 각각 success false, not found / regular file 포함."""
    w, _, _ = patch_test_env
    raw1 = patch_tool("nofile.txt", "x", "y", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is False
    assert "not found" in res1["error"].lower()

    raw2 = patch_tool("sub", "x", "y", workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is False
    assert "regular file" in res2["error"]


def test_050_scenario_12_binary_and_invalid_utf8(patch_test_env):
    """시나리오 12: binary.bin / latin.txt => 각각 success false, binary / UTF-8 포함."""
    w, _, _ = patch_test_env
    raw1 = patch_tool("binary.bin", "x", "y", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is False
    assert "binary" in res1["error"]

    raw2 = patch_tool("latin.txt", "x", "y", workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is False
    assert "UTF-8" in res2["error"]


def test_050_scenario_13_file_and_result_too_large(patch_test_env):
    """시나리오 13: (a) 1,000,001바이트 big.txt, (b) 999,990바이트 near.txt에서 교체 결과 1,000,009바이트 => too large, 파일 불변."""
    w, _, _ = patch_test_env
    big_path = w / "big.txt"
    big_path.write_bytes(b"x" * 1_000_001)

    near_path = w / "near.txt"
    near_content = "y" * 999_989 + "Z"
    near_path.write_text(near_content, encoding="utf-8")

    raw1 = patch_tool("big.txt", "x", "y", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is False
    assert "too large" in res1["error"]
    assert big_path.stat().st_size == 1_000_001

    raw2 = patch_tool("near.txt", "Z", "Z" * 20, workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is False
    assert "too large" in res2["error"]
    assert near_path.read_text(encoding="utf-8") == near_content


def test_050_scenario_14_protected_files(patch_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 14: AGENTS.md / agents_link / HERMES_HOME=W/.hermes 일 때 .hermes/memories/MEMORY.md => success false, 내용 불변."""
    w, _, _ = patch_test_env
    raw1 = patch_tool("AGENTS.md", "rules", "new_rules", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is False
    assert "instruction" in res1["error"]
    assert (w / "AGENTS.md").read_text(encoding="utf-8") == "rules\n"

    raw2 = patch_tool("agents_link", "rules", "new_rules", workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is False
    assert "instruction" in res2["error"]
    assert (w / "AGENTS.md").read_text(encoding="utf-8") == "rules\n"

    # HERMES_HOME=W/.hermes 설정
    hermes_dir = w / ".hermes"
    mem_dir = hermes_dir / "memories"
    mem_dir.mkdir(parents=True)
    mem_file = mem_dir / "MEMORY.md"
    mem_file.write_text("memory content\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_dir))

    raw3 = patch_tool(".hermes/memories/MEMORY.md", "memory", "new_mem", workspace=w)
    res3 = json.loads(raw3)
    assert res3["success"] is False
    assert "state folder" in res3["error"]
    assert mem_file.read_text(encoding="utf-8") == "memory content\n"


def test_050_scenario_15_outside_and_symlink_escape(patch_test_env):
    """시나리오 15: ../outside.txt / link => success false, outside 포함, outside.txt 내용 불변."""
    w, outside, _ = patch_test_env
    raw1 = patch_tool("../outside.txt", "OUTSIDE", "INSIDE", workspace=w)
    res1 = json.loads(raw1)
    assert res1["success"] is False
    assert "outside" in res1["error"]
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"

    raw2 = patch_tool("link", "OUTSIDE", "INSIDE", workspace=w)
    res2 = json.loads(raw2)
    assert res2["success"] is False
    assert "outside" in res2["error"]
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"


def test_050_scenario_16_control_characters_in_path(patch_test_env):
    """시나리오 16: 경로 a\\nb => success false, 오류에 control 포함."""
    w, _, _ = patch_test_env
    raw = patch_tool("a\nb", "x", "y", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "control" in res["error"]


def test_050_scenario_17_atomic_failure_cleanup(patch_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 17: os.replace가 OSError 던질 때 => Failed to write 포함, q.txt 원본 그대로, 임시 파일 없음."""
    w, _, _ = patch_test_env

    def _broken_replace(src, dst):
        raise OSError("Disk full error simulation")

    monkeypatch.setattr("os.replace", _broken_replace)

    raw = patch_tool("q.txt", "two", "2", workspace=w)
    res = json.loads(raw)
    assert res["success"] is False
    assert "Failed to write" in res["error"]
    assert (w / "q.txt").read_text(encoding="utf-8") == "one two three\n"

    # 임시 파일이 남아 있지 않은지 확인
    tmp_files = [f for f in w.iterdir() if f.name.startswith(".tmp_patch_")]
    assert len(tmp_files) == 0


def test_050_scenario_18_overlapping_match(patch_test_env):
    """시나리오 18: 겹치는 일치: aaa, aa -> b, replace_all=True => replacements == 1, 파일 ba."""
    w, _, _ = patch_test_env
    (w / "overlap.txt").write_text("aaa", encoding="utf-8")

    raw = patch_tool("overlap.txt", "aa", "b", replace_all=True, workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["replacements"] == 1
    assert (w / "overlap.txt").read_text(encoding="utf-8") == "ba"


def test_050_scenario_19_korean_utf8(patch_test_env):
    """시나리오 19: 한글 파일 kr.txt (안녕 세계\\n), 세계 -> 우주 => 파일 안녕 우주\\n."""
    w, _, _ = patch_test_env
    (w / "kr.txt").write_text("안녕 세계\n", encoding="utf-8")

    raw = patch_tool("kr.txt", "세계", "우주", workspace=w)
    res = json.loads(raw)
    assert res["success"] is True
    assert res["replacements"] == 1
    assert (w / "kr.txt").read_text(encoding="utf-8") == "안녕 우주\n"


def test_050_scenario_20_dispatcher_schema_patch():
    """시나리오 20: 기본 디스패처의 get_tools_schema()에 patch가 있고 properties에 workspace가 없음."""
    dispatcher = create_default_dispatcher()
    schemas = dispatcher.get_tools_schema()
    patch_def = next((s["function"] for s in schemas if s["function"]["name"] == "patch"), None)
    assert patch_def is not None
    assert "workspace" not in patch_def["parameters"]["properties"]
    assert "path" in patch_def["parameters"]["properties"]
    assert "old_string" in patch_def["parameters"]["properties"]
    assert "new_string" in patch_def["parameters"]["properties"]
    assert "replace_all" in patch_def["parameters"]["properties"]


def test_050_scenario_21_dispatcher_blocks_workspace(patch_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 21: 디스패처로 workspace 넘기면 거부, q.txt 원본 유지."""
    w, _, _ = patch_test_env
    monkeypatch.chdir(w)
    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call(
        "patch",
        {"path": "q.txt", "old_string": "two", "new_string": "2", "workspace": "/"},
    )
    res = json.loads(raw)
    assert res["success"] is False
    assert "workspace" in res["error"]
    assert (w / "q.txt").read_text(encoding="utf-8") == "one two three\n"


def test_050_scenario_22_dispatcher_type_check_replace_all(patch_test_env, monkeypatch: pytest.MonkeyPatch):
    """시나리오 22: 디스패처로 replace_all에 문자열 'yes' => 거부 (타입 검사), q.txt 원본 유지."""
    w, _, _ = patch_test_env
    monkeypatch.chdir(w)
    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call(
        "patch",
        {"path": "q.txt", "old_string": "two", "new_string": "2", "replace_all": "yes"},
    )
    res = json.loads(raw)
    assert res["success"] is False
    assert "replace_all" in res["error"]
    assert (w / "q.txt").read_text(encoding="utf-8") == "one two three\n"
