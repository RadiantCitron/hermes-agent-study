"""search_files 도구 인수 테스트 (17 로드맵 010 단계).

[검증 범위]
- 시나리오 1: 파일 검색 모드 (pattern="")
- 시나리오 2: 내용 검색 모드 (pattern="def login", literal regex=False)
- 시나리오 3: 정규식 내용 검색 모드 (pattern=r"user_\\d+", regex=True)
- 시나리오 4: 잘못된 정규식 패턴 거부
- 시나리오 5: 빈 pattern 및 빈 file_glob 거부
- 시나리오 6: 보호 파일·폴더(.env*, .git) 자동 제외 (가정 B)
- 시나리오 7: Hermes 상태 폴더 자동 제외 및 격리 검증 (가정 B)
- 시나리오 8: 심볼릭 링크 및 탈출 방어 (가정 B)
- 시나리오 9: 작업 폴더 밖 시작 경로 및 보호 시작 경로 거부
- 시나리오 10: 바이너리 및 비UTF-8, 크기 초과 파일 건너뛰기
- 시나리오 11: 매치 건수 상한 (SEARCH_MAX_MATCHES = 50, 가정 C)
- 시나리오 12: 줄 길이 상한 (SEARCH_MAX_LINE_CHARS = 200, 가정 C)
- 시나리오 13: 스캔 파일 수 상한 (SEARCH_MAX_FILES_SCANNED = 5000, 가정 C)
- 시나리오 14: 기본 및 자식 디스패처 등록 및 스키마 검증
- 시나리오 15: Checkpointer 무변경(읽기 전용) 및 프롬프트 가이던스 배선
"""

import json
from pathlib import Path
import pytest

from hermes_core.agent.prompt_builder import SEARCH_FILES_GUIDANCE, select_tools_guidance
from hermes_core.constants import FILE_MAX_BYTES
from hermes_core.tools import file_tools
from hermes_core.tools.checkpoint import Checkpointer
from hermes_core.tools.dispatcher import create_child_dispatcher, create_default_dispatcher
from hermes_core.tools.file_tools import search_files_tool


def test_scenario_01_file_search_mode(tmp_path: Path):
    """시나리오 1: a.py, sub/b.py, sub/note.txt가 있을 때 *.py 파일 검색."""
    (tmp_path / "a.py").write_text("print('a')")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("print('b')")
    (sub / "note.txt").write_text("note")

    res_raw = search_files_tool(file_glob="*.py", workspace=tmp_path)
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["mode"] == "files"
    assert res["truncated"] is False
    assert res["total_matches"] == 2
    assert res["matches"] == [{"path": "a.py"}, {"path": "sub/b.py"}]


def test_scenario_02_content_search_literal(tmp_path: Path):
    """시나리오 2: def login(user): 처럼 괄호가 포함된 문자열 리터럴 검색."""
    (tmp_path / "a.py").write_text("import os\n\ndef login(user):\n    return True\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("# other code\ndef logout(): pass\n")

    res_raw = search_files_tool(pattern="def login", file_glob="*.py", regex=False, workspace=tmp_path)
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["mode"] == "content"
    assert res["total_matches"] == 1
    assert len(res["matches"]) == 1
    match = res["matches"][0]
    assert match["path"] == "a.py"
    assert match["line"] == 3
    assert match["content"] == "def login(user):"


def test_scenario_03_content_search_regex(tmp_path: Path):
    """시나리오 3: regex=True로 정규식 매칭 검색."""
    (tmp_path / "service.py").write_text("user_1 = 'alice'\nadmin = 'bob'\nuser_99 = 'charlie'\n")

    res_raw = search_files_tool(pattern=r"user_\d+", regex=True, workspace=tmp_path)
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["mode"] == "content"
    assert res["total_matches"] == 2
    assert res["matches"][0] == {"path": "service.py", "line": 1, "content": "user_1 = 'alice'"}
    assert res["matches"][1] == {"path": "service.py", "line": 3, "content": "user_99 = 'charlie'"}


def test_scenario_04_invalid_regex(tmp_path: Path):
    """시나리오 4: 잘못된 정규식 패턴 제공 시 에러 JSON 반환."""
    res_raw = search_files_tool(pattern="[unclosed", regex=True, workspace=tmp_path)
    res = json.loads(res_raw)

    assert res["success"] is False
    assert "Invalid regex pattern" in res["error"]


def test_scenario_05_empty_pattern_and_glob(tmp_path: Path):
    """시나리오 5: pattern과 file_glob 둘 다 비어있을 때 거부."""
    res_raw = search_files_tool(pattern="", file_glob="", workspace=tmp_path)
    res = json.loads(res_raw)

    assert res["success"] is False
    assert res["error"] == "At least one of 'pattern' or 'file_glob' must be non-empty."


def test_scenario_06_protected_paths_excluded(tmp_path: Path):
    """시나리오 6: .env, .env.local, .git/config, sub/.env.prod 등 보호 파일 자동 제외."""
    (tmp_path / ".env").write_text("SECRET_TOKEN=123")
    (tmp_path / ".env.local").write_text("SECRET_TOKEN=456")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("SECRET_TOKEN=git")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".env.prod").write_text("SECRET_TOKEN=prod")
    (tmp_path / "app.py").write_text("TOKEN = 'SECRET_TOKEN'\n")

    # 1. 내용 검색
    res_raw = search_files_tool(pattern="SECRET_TOKEN", workspace=tmp_path)
    res = json.loads(res_raw)
    assert res["success"] is True
    assert res["total_matches"] == 1
    assert res["matches"][0]["path"] == "app.py"

    # 2. 파일 검색
    res_raw_files = search_files_tool(file_glob="*", workspace=tmp_path)
    res_files = json.loads(res_raw_files)
    assert res_files["success"] is True
    paths = [m["path"] for m in res_files["matches"]]
    assert "app.py" in paths
    assert not any(p.startswith(".env") or "/.env" in p or p.startswith(".git") for p in paths)


def test_scenario_07_hermes_home_path_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 7: Hermes 상태 폴더 자동 제외 및 path 직접 지정 시 거부."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "state.db").write_text("SECRET_MEMORY")
    memories_dir = hermes_home / "memories"
    memories_dir.mkdir()
    (memories_dir / "MEMORY.md").write_text("SECRET_MEMORY")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # 루트 탐색 시 상태 폴더 내부 자동 제외
    res_raw = search_files_tool(pattern="SECRET_MEMORY", workspace=tmp_path)
    res = json.loads(res_raw)
    assert res["success"] is True
    assert res["matches"] == []

    # path=".hermes" 직접 지정 시 거부
    res_direct_raw = search_files_tool(path=".hermes", pattern="SECRET_MEMORY", workspace=tmp_path)
    res_direct = json.loads(res_direct_raw)
    assert res_direct["success"] is False
    assert "Searching inside the Hermes state folder is not allowed." in res_direct["error"]

    # 작업 폴더 자체가 HERMES_HOME / "workspace" 일 때 정상 파일 검색 가능
    nested_workspace = hermes_home / "workspace"
    nested_workspace.mkdir()
    (nested_workspace / "code.py").write_text("SECRET_MEMORY = 1")

    res_nested = json.loads(search_files_tool(pattern="SECRET_MEMORY", workspace=nested_workspace))
    assert res_nested["success"] is True
    assert len(res_nested["matches"]) == 1
    assert res_nested["matches"][0]["path"] == "code.py"


def test_scenario_08_symlinks_and_traversal_defense(tmp_path: Path):
    """시나리오 8: 심볼릭 링크 및 순환 링크 방어."""
    outside_dir = tmp_path.parent / "outside_dir_08"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("OUTSIDE_SECRET")

    # 심볼릭 링크 파일 및 디렉터리 생성
    symlink_file = tmp_path / "ext.py"
    symlink_file.symlink_to(outside_file)

    symlink_dir = tmp_path / "ext_dir"
    symlink_dir.symlink_to(outside_dir)

    # 순환 심볼릭 링크
    loop_dir = tmp_path / "loop_dir"
    loop_dir.symlink_to(tmp_path)

    (tmp_path / "normal.py").write_text("print('normal')")

    # 전체 탐색 시 심볼릭 링크 건너뜀
    res = json.loads(search_files_tool(file_glob="*", workspace=tmp_path))
    assert res["success"] is True
    paths = [m["path"] for m in res["matches"]]
    assert "normal.py" in paths
    assert "ext.py" not in paths
    assert "ext_dir" not in paths

    # path="ext.py" 직접 지정 시 거부
    res_direct = json.loads(search_files_tool(path="ext.py", pattern="OUTSIDE", workspace=tmp_path))
    assert res_direct["success"] is False
    assert "is a symbolic link" in res_direct["error"]


def test_scenario_09_outside_and_protected_start_path(tmp_path: Path):
    """시나리오 9: 작업 폴더 밖 경로 및 보호 경로를 path로 지정 시 거부."""
    (tmp_path / ".env").write_text("FOO=BAR")

    res_outside = json.loads(search_files_tool(path="../outside", pattern="x", workspace=tmp_path))
    assert res_outside["success"] is False
    assert "outside the workspace" in res_outside["error"]

    res_env = json.loads(search_files_tool(path=".env", pattern="x", workspace=tmp_path))
    assert res_env["success"] is False
    assert "protected file or folder" in res_env["error"]


def test_scenario_10_binary_and_large_files_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 10: 바이너리, 비UTF-8, 크기 초과 파일 조용히 건너뜀."""
    (tmp_path / "bin.dat").write_bytes(b"hello\x00world target")
    (tmp_path / "non_utf8.txt").write_bytes(b"\xff\xfe target")
    (tmp_path / "large.txt").write_text("target " * 1000)
    (tmp_path / "ok.txt").write_text("valid target here")

    monkeypatch.setattr(file_tools, "FILE_MAX_BYTES", 50)

    res = json.loads(search_files_tool(pattern="target", workspace=tmp_path))
    assert res["success"] is True
    assert res["total_matches"] == 1
    assert res["matches"][0]["path"] == "ok.txt"


def test_scenario_11_max_matches_limit(tmp_path: Path):
    """시나리오 11: 매치 건수 상한 SEARCH_MAX_MATCHES = 50 검증."""
    # 1. 80줄 매치 파일
    lines = [f"line {i} hit target" for i in range(80)]
    (tmp_path / "multi.txt").write_text("\n".join(lines))

    res_content = json.loads(search_files_tool(pattern="hit target", workspace=tmp_path))
    assert res_content["total_matches"] == 50
    assert len(res_content["matches"]) == 50
    assert res_content["truncated"] is True

    # 2. 정확히 50줄 매치 파일
    lines_50 = [f"line {i} match50" for i in range(50)]
    (tmp_path / "exact50.txt").write_text("\n".join(lines_50))
    res_exact = json.loads(search_files_tool(pattern="match50", workspace=tmp_path))
    assert res_exact["total_matches"] == 50
    assert res_exact["truncated"] is False

    # 3. 80개 파일에 대한 파일 검색
    files_dir = tmp_path / "many_files"
    files_dir.mkdir()
    for i in range(80):
        (files_dir / f"f_{i:03d}.py").write_text("x=1")

    res_files = json.loads(search_files_tool(path="many_files", file_glob="*.py", workspace=tmp_path))
    assert res_files["total_matches"] == 50
    assert res_files["truncated"] is True


def test_scenario_12_line_length_limit(tmp_path: Path):
    """시나리오 12: 줄 길이 상한 SEARCH_MAX_LINE_CHARS = 200 검증."""
    long_line = "A" * 350
    (tmp_path / "long.txt").write_text(f"prefix {long_line}\n")

    res = json.loads(search_files_tool(pattern="prefix", workspace=tmp_path))
    assert res["success"] is True
    assert res["total_matches"] == 1
    content = res["matches"][0]["content"]
    assert len(content) == 203
    assert content.endswith("...")


def test_scenario_13_max_files_scanned_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 13: SEARCH_MAX_FILES_SCANNED 초과 시 조기 중단."""
    monkeypatch.setattr(file_tools, "SEARCH_MAX_FILES_SCANNED", 5)

    for i in range(10):
        (tmp_path / f"test_{i:02d}.txt").write_text(f"content {i}")

    res = json.loads(search_files_tool(pattern="content", workspace=tmp_path))
    assert res["success"] is True
    assert res["files_scanned"] == 5
    assert res["truncated"] is True


def test_scenario_14_dispatcher_registration_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 14: 기본 및 자식 디스패처에 search_files 등록 및 스키마 검증 확인."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.py").write_text("print('hello world')")

    # 1. 기본 디스패처
    disp = create_default_dispatcher()
    raw = disp.handle_function_call(
        "search_files",
        {"pattern": "world", "path": "."},
    )
    res = json.loads(raw)
    assert res["success"] is True
    assert res["total_matches"] == 1

    # 2. 자식 디스패처
    child_disp = create_child_dispatcher()
    child_raw = child_disp.handle_function_call(
        "search_files",
        {"pattern": "world", "path": "."},
    )
    child_res = json.loads(child_raw)
    assert child_res["success"] is True
    assert child_res["total_matches"] == 1

    # 3. 정의되지 않은 인자 전달 시 스키마 검증기 거부
    invalid_raw = disp.handle_function_call(
        "search_files",
        {"pattern": "world", "unexpected_arg": "invalid"},
    )
    assert "unexpected argument 'unexpected_arg'" in invalid_raw


def test_scenario_15_read_only_checkpointer_and_prompt_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 15: Checkpointer 무변경(읽기 전용) 및 프롬프트 가이던스 배선 확인."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sample.py").write_text("print('test')")

    from hermes_core.session_db import SessionDB

    db = SessionDB(tmp_path / "session.db")
    db.create_session("s_test", "Test Session")
    cp = Checkpointer(db=db, session_id="s_test", workspace=tmp_path)
    disp = create_default_dispatcher(checkpointer=cp)

    disp.handle_function_call("search_files", {"pattern": "test"})

    # file_checkpoints 테이블에 행이 추가되지 않아야 함 (읽기 전용 확인)
    rows = db.get_file_checkpoints("s_test")
    assert len(rows) == 0

    # prompt_builder의 select_tools_guidance에 SEARCH_FILES_GUIDANCE 포함 확인
    guidances = select_tools_guidance(["search_files", "memory"])
    assert SEARCH_FILES_GUIDANCE in guidances
