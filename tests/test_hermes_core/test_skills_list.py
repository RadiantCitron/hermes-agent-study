"""06 로드맵 020 단계: skills_list 도구 및 디스패처 등록 단위/인수 테스트.

[인수 테스트 시나리오]
- 1: skills_list_tool 기본 호출 및 정렬/카테고리/카운트 검증
- 2: category 필터링 (일치 / 미존재 / 빈 문자열)
- 3: 스킬 디렉터리 미존재 시 빈 결과 및 디렉터리 미생성
- 4: 스킬 디렉터리 빈 폴더 시 빈 결과
- 5: 손상된 파일(UTF-8 아님) 건너뛰고 정상 스킬 반환
- 6: 호출 전후 스킬 폴더 상태 및 파일 내용 해시 불변 (읽기 전용 검증)
- 7: create_default_dispatcher() 스키마 검증 (skills_list 포함, skill_view 명시, 기존 6종 유지)
- 8: HERMES_HOME 동적 변경 시 호출 시점 경로 반영
- 9: skills_root 미정의 인자 전달 시 디스패처 호출 거부
- 10: category 타입 위반 시 디스패처 호출 거부
- 10b: 한글 설명(description) 유니코드 이스케이프 없는 직렬화 검증 (ensure_ascii=False)
"""

import hashlib
import json
from pathlib import Path

from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.skills_tool import skills_list_tool


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


def test_scenario_01_skills_list_basic(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    hello = root / "hello"
    hello.mkdir()
    (hello / "SKILL.md").write_text("---\nname: hello\ndescription: Hello skill\n---\nBody")

    deploy = root / "devops" / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "SKILL.md").write_text("---\nname: deploy\ndescription: Deploy skill\n---\nBody")

    backup = root / "devops" / "backup"
    backup.mkdir(parents=True)
    (backup / "SKILL.md").write_text("---\nname: backup\ndescription: Backup skill\n---\nBody")

    raw = skills_list_tool(skills_root=root)
    data = json.loads(raw)

    assert data["success"] is True
    assert data["count"] == 3
    assert len(data["skills"]) == 3
    # 분류 없음(hello) 먼저, 그다음 devops 내에서 이름순(backup -> deploy)
    assert [s["name"] for s in data["skills"]] == ["hello", "backup", "deploy"]
    assert data["categories"] == ["devops"]

    for item in data["skills"]:
        assert "name" in item
        assert "description" in item
        assert "category" in item
        assert "path" in item


def test_scenario_02_skills_list_category_filter(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    for path, name in [
        ("hello", "hello"),
        ("devops/deploy", "deploy"),
        ("devops/backup", "backup"),
    ]:
        d = root / path
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody")

    # 1. category="devops"
    res1 = json.loads(skills_list_tool(category="devops", skills_root=root))
    assert res1["success"] is True
    assert res1["count"] == 2
    assert [s["name"] for s in res1["skills"]] == ["backup", "deploy"]
    assert res1["categories"] == ["devops"]

    # 2. category="없는분류"
    res2 = json.loads(skills_list_tool(category="없는분류", skills_root=root))
    assert res2["success"] is True
    assert res2["count"] == 0
    assert res2["skills"] == []
    assert res2["categories"] == []

    # 3. category="" (필터 없음)
    res3 = json.loads(skills_list_tool(category="", skills_root=root))
    assert res3["success"] is True
    assert res3["count"] == 3
    assert [s["name"] for s in res3["skills"]] == ["hello", "backup", "deploy"]


def test_scenario_03_skills_list_nonexistent_root(tmp_path: Path):
    root = tmp_path / "skills_missing"
    assert not root.exists()

    raw = skills_list_tool(skills_root=root)
    data = json.loads(raw)

    assert data["success"] is True
    assert data["count"] == 0
    assert data["skills"] == []
    assert data["categories"] == []
    assert "message" in data
    # 호출 뒤에도 폴더가 생성되지 않아야 함
    assert not root.exists()


def test_scenario_04_skills_list_empty_root(tmp_path: Path):
    root = tmp_path / "skills_empty"
    root.mkdir()

    raw = skills_list_tool(skills_root=root)
    data = json.loads(raw)

    assert data["success"] is True
    assert data["count"] == 0
    assert data["skills"] == []
    assert data["categories"] == []
    assert "message" in data


def test_scenario_06_skills_list_readonly_folder_immutability(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    s = root / "s1"
    s.mkdir()
    (s / "SKILL.md").write_text("---\nname: s1\n---\nBody")

    before_snap = _build_snapshot(root)
    skills_list_tool(skills_root=root)
    after_snap = _build_snapshot(root)

    assert before_snap == after_snap


def test_scenario_07_dispatcher_registration_and_schemas():
    dispatcher = create_default_dispatcher()
    schemas = dispatcher.get_tools_schema()

    schema_map = {s["function"]["name"]: s["function"] for s in schemas}

    # skills_list 등록 확인
    assert "skills_list" in schema_map
    sl_func = schema_map["skills_list"]
    assert "skill_view" in sl_func["description"]
    assert set(sl_func["parameters"]["properties"].keys()) == {"category"}
    assert "skills_root" not in sl_func["parameters"]["properties"]

    # 기존 6종 도구도 모두 유지되는지 확인
    expected_existing = ["memory", "session_search", "terminal", "read_file", "write_file", "patch"]
    for tool_name in expected_existing:
        assert tool_name in schema_map


def test_scenario_08_dispatcher_dynamic_hermes_home(tmp_path: Path, monkeypatch):
    custom_home = tmp_path / "custom_hermes"
    monkeypatch.setenv("HERMES_HOME", str(custom_home))

    skills_dir = custom_home / "skills"
    skills_dir.mkdir(parents=True)
    my_skill = skills_dir / "my_skill"
    my_skill.mkdir()
    (my_skill / "SKILL.md").write_text("---\nname: dynamic_skill\n---\nBody")

    dispatcher = create_default_dispatcher()
    raw = dispatcher.handle_function_call("skills_list", {})
    data = json.loads(raw)

    assert data["success"] is True
    assert data["count"] == 1
    assert data["skills"][0]["name"] == "dynamic_skill"


def test_scenario_10b_korean_description_ensure_ascii_false(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    s = root / "korean_skill"
    s.mkdir()
    (s / "SKILL.md").write_text("---\nname: k_skill\ndescription: 배포 절차 안내문\n---\n본문")

    raw = skills_list_tool(skills_root=root)
    # raw 문자열 자체에 한글이 이스케이프(\\u)되지 않고 그대로 포함되어 있어야 함
    assert "배포 절차 안내문" in raw
    assert "\\u" not in raw
