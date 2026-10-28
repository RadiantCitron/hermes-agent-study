"""06 로드맵 030 단계: skill_view 도구 및 디스패처 등록 단위/인수 테스트.

[인수 테스트 시나리오]
- 1: name="hello" 기본 조회
- 1b: BOM이 붙은 SKILL.md 디코딩 (BOM 미포함)
- 2: name="devops/deploy" path 조회 및 linked_files 확인
- 3: 폴더명(deploy) 및 머리말 name(Deploy App) 조회
- 4: skills_list 반환 path 목록 전수 조회 (왕복 검증)
- 5: 동명 스킬 충돌 시 candidates 반환, path 지정 시 성공
- 5b: path 일치가 폴더명 일치보다 우선 (name="foo")
- 6: 존재하지 않는 스킬 조회 시 available_skills 반환 (20개 상한)
- 7: 빈 name 또는 공백 name 거부
- 8: 경로 이탈 name 차단 (SECRET 미노출)
- 9: 경로 이탈 file_path 차단 (SECRET/OUTSIDE 미노출)
- 10: 보호 파일(.env, references/.env, .git/config) 차단 (KEY=1 미노출 및 linked_files 제외)
- 11: 제어문자 차단
- 12: 심볼릭 링크 이탈 차단 (OUTSIDE 미노출)
- 13: file_path="references/api.md" 부속 파일 조회
- 14: file_path="SKILL.md" 및 4대 폴더 외 파일(notes.md) 조회
- 15: 존재하지 않는 file_path 시 available_files 반환
- 16: 디렉터리 경로 조회 거부
- 17: 이진 파일(NUL) 및 깨진 UTF-8 디코드 실패 거부
- 18: 본문 및 부속 파일 20,000자 상한 경계 검증 (한글 포함)
- 19: 1,000,000바이트 초과 파일 거부
- 20: references/ 파일 101개 시 linked_files 100개 상한 절삭
- 21: 호출 전후 파일 및 디렉터리 불변 검증 (읽기 전용)
- 22: get_tools_schema() 스키마 검증 및 name 누락 거부
- 23: 디스패처 경유 경로 이탈 호출 시 success false 반환
- 24: 스킬 루트 미존재 시 실패 및 디렉터리 미생성
"""

import hashlib
import json
from pathlib import Path
import pytest

from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.skills_tool import skills_list_tool, skill_view_tool


def _build_snapshot(root: Path) -> dict[str, str]:
    """디렉터리 내 모든 파일의 상대경로 및 sha256 해시 스냅샷 생성."""
    snapshot = {}
    if not root.exists():
        return snapshot
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            snapshot[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snapshot


@pytest.fixture
def skills_env(tmp_path: Path):
    """공통 테스트 환경 fixture."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()

    # 스킬 루트 밖 비밀 파일들
    (hermes_home / "state.db").write_text("SECRET")
    (hermes_home / "outside.txt").write_text("OUTSIDE")

    skills_root = hermes_home / "skills"
    skills_root.mkdir()

    # 1. hello 스킬
    hello = skills_root / "hello"
    hello.mkdir()
    (hello / "SKILL.md").write_text("# Hello\nSimple hello world skill.")

    # 2. devops/deploy 스킬 및 부속 파일들
    deploy = skills_root / "devops" / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "SKILL.md").write_text(
        "---\nname: Deploy App\ndescription: Deploy instructions.\n---\n# Deploy App\nBody."
    )

    refs = deploy / "references"
    refs.mkdir()
    (refs / "api.md").write_text("# API Docs\nAPI specification.")
    (refs / ".env").write_text("KEY=1")

    tmpl = deploy / "templates"
    tmpl.mkdir()
    (tmpl / "t.yaml").write_text("template: v1")

    assets = deploy / "assets"
    assets.mkdir()
    (assets / "logo.bin").write_bytes(b"BIN\x00DATA")
    (assets / "bad.txt").write_bytes(b"\xff\xfe\xaa\xbb")

    scripts = deploy / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("echo 'run'")

    # 스킬 폴더 안 .env 및 4대 폴더 외 파일
    (deploy / ".env").write_text("KEY=1")
    (deploy / "notes.md").write_text("Notes outside 4 dirs.")

    return {
        "home": hermes_home,
        "skills_root": skills_root,
        "hello": hello,
        "deploy": deploy,
    }


def test_scenario_01_skill_view_basic(skills_env):
    raw = skill_view_tool("hello", skills_root=skills_env["skills_root"])
    data = json.loads(raw)
    assert data["success"] is True
    assert data["path"] == "hello"
    assert data["content"] == "# Hello\nSimple hello world skill."
    assert data["truncated"] is False
    assert data["total_chars"] == len("# Hello\nSimple hello world skill.")


def test_scenario_01b_skill_view_bom(skills_env):
    bom_skill = skills_env["skills_root"] / "bom_skill"
    bom_skill.mkdir()
    (bom_skill / "SKILL.md").write_bytes("\ufeff# BOM Title\nContent".encode("utf-8"))

    raw = skill_view_tool("bom_skill", skills_root=skills_env["skills_root"])
    data = json.loads(raw)
    assert data["success"] is True
    assert not data["content"].startswith("\ufeff")
    assert data["content"] == "# BOM Title\nContent"


def test_scenario_02_skill_view_path_and_linked_files(skills_env):
    raw = skill_view_tool("devops/deploy", skills_root=skills_env["skills_root"])
    data = json.loads(raw)
    assert data["success"] is True
    assert data["path"] == "devops/deploy"
    assert "linked_files" in data
    linked = data["linked_files"]
    assert "references" in linked
    assert "references/api.md" in linked["references"]
    assert "templates" in linked
    assert "templates/t.yaml" in linked["templates"]
    assert "scripts" in linked
    assert "scripts/run.sh" in linked["scripts"]
    assert "assets" in linked


def test_scenario_03_skill_view_by_folder_or_frontmatter_name(skills_env):
    # 폴더명 "deploy"
    raw1 = skill_view_tool("deploy", skills_root=skills_env["skills_root"])
    data1 = json.loads(raw1)
    assert data1["success"] is True
    assert data1["path"] == "devops/deploy"

    # 머리말 name "Deploy App"
    raw2 = skill_view_tool("Deploy App", skills_root=skills_env["skills_root"])
    data2 = json.loads(raw2)
    assert data2["success"] is True
    assert data2["path"] == "devops/deploy"


def test_scenario_04_skills_list_roundtrip(skills_env):
    list_raw = skills_list_tool(skills_root=skills_env["skills_root"])
    list_data = json.loads(list_raw)
    assert list_data["success"] is True

    for item in list_data["skills"]:
        p = item["path"]
        view_raw = skill_view_tool(p, skills_root=skills_env["skills_root"])
        view_data = json.loads(view_raw)
        assert view_data["success"] is True
        assert view_data["path"] == p


def test_scenario_05_duplicate_name_collision(skills_env):
    root = skills_env["skills_root"]
    a = root / "a" / "dup"
    a.mkdir(parents=True)
    (a / "SKILL.md").write_text("---\nname: dup\n---\nBody A")

    b = root / "b" / "dup"
    b.mkdir(parents=True)
    (b / "SKILL.md").write_text("---\nname: dup\n---\nBody B")

    # 이름만으로 조회 시 충돌 -> candidates 반환
    raw_col = skill_view_tool("dup", skills_root=root)
    data_col = json.loads(raw_col)
    assert data_col["success"] is False
    assert "candidates" in data_col
    assert sorted(data_col["candidates"]) == ["a/dup", "b/dup"]

    # path 지정 시 성공
    raw_ok = skill_view_tool("a/dup", skills_root=root)
    assert json.loads(raw_ok)["success"] is True


def test_scenario_05b_exact_path_precedence(skills_env):
    root = skills_env["skills_root"]
    foo1 = root / "foo"
    foo1.mkdir()
    (foo1 / "SKILL.md").write_text("# Top Foo")

    foo2 = root / "x" / "foo"
    foo2.mkdir(parents=True)
    (foo2 / "SKILL.md").write_text("# Sub Foo")

    raw = skill_view_tool("foo", skills_root=root)
    data = json.loads(raw)
    assert data["success"] is True
    assert data["path"] == "foo"
    assert data["content"] == "# Top Foo"


def test_scenario_06_skill_not_found_available_skills(skills_env):
    root = skills_env["skills_root"]
    # 25개 스킬 생성
    for i in range(25):
        s = root / f"skill_{i:02d}"
        s.mkdir()
        (s / "SKILL.md").write_text(f"# Skill {i}")

    raw = skill_view_tool("nope", skills_root=root)
    data = json.loads(raw)
    assert data["success"] is False
    assert "available_skills" in data
    assert "hello" in data["available_skills"]
    assert len(data["available_skills"]) == 20


def test_scenario_07_empty_name_rejected(skills_env):
    for n in ["", "   "]:
        raw = skill_view_tool(n, skills_root=skills_env["skills_root"])
        data = json.loads(raw)
        assert data["success"] is False


def test_scenario_08_path_traversal_name_blocked(skills_env):
    bad_names = ["../state.db", "../../etc/passwd", "/etc/passwd", "devops/../../state.db"]
    for n in bad_names:
        raw = skill_view_tool(n, skills_root=skills_env["skills_root"])
        data = json.loads(raw)
        assert data["success"] is False
        assert "SECRET" not in raw


def test_scenario_09_path_traversal_file_path_blocked(skills_env):
    bad_files = ["../../../state.db", "../../../outside.txt", "../../hello/SKILL.md", "/etc/passwd"]
    for f in bad_files:
        raw = skill_view_tool("deploy", file_path=f, skills_root=skills_env["skills_root"])
        data = json.loads(raw)
        assert data["success"] is False
        assert "SECRET" not in raw
        assert "OUTSIDE" not in raw


def test_scenario_10_protected_name_blocked(skills_env):
    for f in [".env", "references/.env", ".git/config"]:
        raw = skill_view_tool("deploy", file_path=f, skills_root=skills_env["skills_root"])
        data = json.loads(raw)
        assert data["success"] is False
        assert "KEY=1" not in raw

    # linked_files 에도 references/.env 가 없어야 함
    raw_main = skill_view_tool("deploy", skills_root=skills_env["skills_root"])
    data_main = json.loads(raw_main)
    linked = data_main.get("linked_files", {})
    assert "references/.env" not in linked.get("references", [])


def test_scenario_11_control_characters_blocked(skills_env):
    for n, f in [("he\nllo", None), ("deploy", "refs\x00x")]:
        raw = skill_view_tool(n, file_path=f, skills_root=skills_env["skills_root"])
        data = json.loads(raw)
        assert data["success"] is False


def test_scenario_12_symlink_escape_blocked(skills_env):
    root = skills_env["skills_root"]
    deploy = skills_env["deploy"]
    home = skills_env["home"]

    # 1. 부속 파일 심볼릭 링크 외부 탈출
    leak = deploy / "references" / "leak.md"
    leak.symlink_to(home / "outside.txt")

    raw_sub = skill_view_tool("deploy", file_path="references/leak.md", skills_root=root)
    assert json.loads(raw_sub)["success"] is False
    assert "OUTSIDE" not in raw_sub

    # linked_files에 leak.md 제외 확인
    raw_main = skill_view_tool("deploy", skills_root=root)
    linked = json.loads(raw_main).get("linked_files", {})
    assert "references/leak.md" not in linked.get("references", [])

    # 2. 루트 외부 폴더를 가리키는 스킬 링크
    ext_dir = home / "ext_skill"
    ext_dir.mkdir()
    (ext_dir / "SKILL.md").write_text("# Ext Skill")
    try:
        (root / "evil").symlink_to(ext_dir, target_is_directory=True)
    except OSError:
        pass

    raw_evil = skill_view_tool("evil", skills_root=root)
    assert json.loads(raw_evil)["success"] is False


def test_scenario_13_view_valid_sub_file(skills_env):
    raw = skill_view_tool("deploy", file_path="references/api.md", skills_root=skills_env["skills_root"])
    data = json.loads(raw)
    assert data["success"] is True
    assert data["file"] == "references/api.md"
    assert data["content"] == "# API Docs\nAPI specification."
    assert data["truncated"] is False


def test_scenario_14_view_skill_md_and_outside_dirs(skills_env):
    root = skills_env["skills_root"]
    # file_path="SKILL.md"
    raw1 = skill_view_tool("deploy", file_path="SKILL.md", skills_root=root)
    data1 = json.loads(raw1)
    assert data1["success"] is True
    assert "Deploy App" in data1["content"]

    # file_path="notes.md" (4대 디렉터리 밖)
    raw2 = skill_view_tool("deploy", file_path="notes.md", skills_root=root)
    data2 = json.loads(raw2)
    assert data2["success"] is True
    assert data2["content"] == "Notes outside 4 dirs."


def test_scenario_15_missing_sub_file_available_files(skills_env):
    raw = skill_view_tool("deploy", file_path="references/none.md", skills_root=skills_env["skills_root"])
    data = json.loads(raw)
    assert data["success"] is False
    assert "available_files" in data
    assert "references/api.md" in data["available_files"].get("references", [])


def test_scenario_16_directory_path_rejected(skills_env):
    raw = skill_view_tool("deploy", file_path="references", skills_root=skills_env["skills_root"])
    data = json.loads(raw)
    assert data["success"] is False
    assert "not a file" in data["error"]


def test_scenario_17_binary_and_bad_utf8_rejected(skills_env):
    # binary file
    raw1 = skill_view_tool("deploy", file_path="assets/logo.bin", skills_root=skills_env["skills_root"])
    data1 = json.loads(raw1)
    assert data1["success"] is False
    assert "Binary file" in data1["error"]

    # bad utf8
    raw2 = skill_view_tool("deploy", file_path="assets/bad.txt", skills_root=skills_env["skills_root"])
    data2 = json.loads(raw2)
    assert data2["success"] is False
    assert "Failed to decode" in data2["error"]


def test_scenario_18_char_length_boundaries(skills_env):
    root = skills_env["skills_root"]
    limit_dir = root / "limit_test"
    limit_dir.mkdir()

    # 1. 25,000자 본문
    (limit_dir / "SKILL.md").write_text("가" * 25000)
    data1 = json.loads(skill_view_tool("limit_test", skills_root=root))
    assert len(data1["content"]) == 20000
    assert data1["truncated"] is True
    assert data1["total_chars"] == 25000

    # 2. 정확히 20,000자
    (limit_dir / "SKILL.md").write_text("나" * 20000)
    data2 = json.loads(skill_view_tool("limit_test", skills_root=root))
    assert len(data2["content"]) == 20000
    assert data2["truncated"] is False
    assert data2["total_chars"] == 20000

    # 3. 19,999자
    (limit_dir / "SKILL.md").write_text("다" * 19999)
    data3 = json.loads(skill_view_tool("limit_test", skills_root=root))
    assert len(data3["content"]) == 19999
    assert data3["truncated"] is False
    assert data3["total_chars"] == 19999


def test_scenario_19_file_max_bytes_exceeded(skills_env):
    root = skills_env["skills_root"]
    big_dir = root / "big_skill"
    big_dir.mkdir()

    # 1,000,001 바이트
    (big_dir / "SKILL.md").write_bytes(b"A" * 1000001)
    data = json.loads(skill_view_tool("big_skill", skills_root=root))
    assert data["success"] is False
    assert "exceeds limit" in data["error"]


def test_scenario_20_linked_files_limit_100(skills_env):
    root = skills_env["skills_root"]
    many_dir = root / "many_refs"
    many_dir.mkdir()
    (many_dir / "SKILL.md").write_text("# Many Refs")

    refs = many_dir / "references"
    refs.mkdir()
    for i in range(101):
        (refs / f"doc_{i:03d}.md").write_text(f"doc {i}")

    data = json.loads(skill_view_tool("many_refs", skills_root=root))
    assert data["success"] is True
    linked_refs = data["linked_files"]["references"]
    assert len(linked_refs) == 100
    assert linked_refs[0] == "references/doc_000.md"
    assert linked_refs[-1] == "references/doc_099.md"


def test_scenario_21_readonly_immutability(skills_env):
    root = skills_env["skills_root"]
    home = skills_env["home"]

    before_snap_skills = _build_snapshot(root)
    before_snap_home = _build_snapshot(home)

    skill_view_tool("hello", skills_root=root)
    skill_view_tool("deploy", file_path="references/api.md", skills_root=root)

    after_snap_skills = _build_snapshot(root)
    after_snap_home = _build_snapshot(home)

    assert before_snap_skills == after_snap_skills
    assert before_snap_home == after_snap_home


def test_scenario_22_schema_and_required_name(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatcher = create_default_dispatcher()
    schemas = {s["function"]["name"]: s["function"] for s in dispatcher.get_tools_schema()}

    assert "skill_view" in schemas
    view_func = schemas["skill_view"]
    assert set(view_func["parameters"]["properties"].keys()) == {"name", "file_path"}
    assert view_func["parameters"]["required"] == ["name"]

    # name 누락 시 거부
    raw = dispatcher.handle_function_call("skill_view", {})
    data = json.loads(raw)
    assert data["success"] is False
    assert "missing required argument 'name'" in data["error"]


def test_scenario_23_dispatcher_traversal_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatcher = create_default_dispatcher()

    raw = dispatcher.handle_function_call("skill_view", {"name": "../state.db"})
    data = json.loads(raw)
    assert data["success"] is False
    assert "error" in data


def test_scenario_24_nonexistent_root_rejected(tmp_path):
    nonexistent = tmp_path / "skills_none"
    assert not nonexistent.exists()

    raw = skill_view_tool("hello", skills_root=nonexistent)
    data = json.loads(raw)
    assert data["success"] is False
    assert not nonexistent.exists()
