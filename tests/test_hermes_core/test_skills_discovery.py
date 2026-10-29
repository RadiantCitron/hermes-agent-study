"""06 로드맵 010 단계: 머리말 파서 및 스킬 발견 단위/인수 테스트.

[인수 테스트 시나리오]
- 1: parse_frontmatter 기본 추출
- 2: 머리말 없는 본문 처리
- 3: 닫는 '---'가 없는 경우
- 4: 따옴표 제거 및 인라인 리스트 [a, b] 파싱
- 5: 중첩 필드 무시 (최상위 키만 파싱)
- 6: 콜론이 포함된 값 파싱
- 7: 주석 및 콜론 없는 줄 무시
- 8: skills_list 기본 발견 및 정렬 (분류 없음 우선)
- 9: name/description 폴더명 및 본문 첫 줄 대체
- 10: name/description 비문자열(리스트 등) 대체
- 11: name(64자) 및 description(1024자) 길이 상한
- 12: 보호 이름(.git, .github, .hub, .env*) 폴더 제외
- 13: 루트 바로 아래의 SKILL.md 제외
- 14: 심볼릭 링크 격리 (외부 파일/폴더 제외, 내부 링크 허용)
- 15: 디코드 불가능한 파일 건너뛰기
- 16: 스킬 루트 미존재 또는 파일일 때 빈 목록 반환 및 디렉터리 미생성
- 17: 동명 스킬(path 다름) 모두 반환
- 18: get_skills_dir() 경로 반환 및 디렉터리 미생성
- 18b: CRLF 및 BOM 정규화
- 18c: 깊은 상대 경로 (구성요소 4개 이상) 분류 추출
- 18d: 생성 순서와 무관한 결정적 정렬
"""

from pathlib import Path

from hermes_core.constants import (
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_NAME_MAX_CHARS,
    get_skills_dir,
)
from hermes_core.tools.skills_tool import discover_skills, parse_frontmatter


def test_scenario_01_parse_frontmatter_basic():
    content = "---\nname: deploy\ndescription: Deploy the app\n---\n# Body\ntext"
    fm, body = parse_frontmatter(content)
    assert fm == {"name": "deploy", "description": "Deploy the app"}
    assert body == "# Body\ntext"


def test_scenario_02_parse_frontmatter_no_header():
    content = "# Title\ntext"
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == "# Title\ntext"


def test_scenario_03_parse_frontmatter_unclosed():
    content = "---\nname: x\nbody"
    fm, body = parse_frontmatter(content)
    assert fm == {}
    assert body == "---\nname: x\nbody"


def test_scenario_04_parse_frontmatter_quotes_and_list():
    content = '---\nname: "My Skill"\ntags: [a, b]\n---\nbody'
    fm, body = parse_frontmatter(content)
    assert fm.get("name") == "My Skill"
    assert fm.get("tags") == ["a", "b"]


def test_scenario_05_parse_frontmatter_nested_ignored():
    content = "---\nname: x\nmetadata:\n  hermes:\n    name: wrong\n---\n"
    fm, _ = parse_frontmatter(content)
    assert fm.get("name") == "x"
    assert "wrong" not in fm.values()


def test_scenario_06_parse_frontmatter_colon_in_value():
    content = "---\ndescription: Use: a thing\n---\nbody"
    fm, _ = parse_frontmatter(content)
    assert fm.get("description") == "Use: a thing"


def test_scenario_07_parse_frontmatter_comments_and_junk():
    content = "---\n# comment\njunk without colon\nname: valid\n---\nbody"
    fm, _ = parse_frontmatter(content)
    assert fm == {"name": "valid"}


def test_scenario_08_discover_skills_category_and_ordering(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    hello = root / "hello"
    hello.mkdir()
    (hello / "SKILL.md").write_text("---\nname: hello\n---\nHello text")

    deploy = root / "devops" / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "SKILL.md").write_text("---\nname: deploy\n---\nDeploy text")

    skills = discover_skills(root)
    assert len(skills) == 2

    # 정렬: category가 None(hello)인 것이 devops보다 앞선다
    assert skills[0]["name"] == "hello"
    assert skills[0]["category"] is None
    assert skills[0]["path"] == "hello"

    assert skills[1]["name"] == "deploy"
    assert skills[1]["category"] == "devops"
    assert skills[1]["path"] == "devops/deploy"


def test_scenario_09_discover_skills_fallback_name_and_desc(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    my_tool = root / "my_tool"
    my_tool.mkdir()
    (my_tool / "SKILL.md").write_text("# Title\n\nFirst line.\nSecond line.")

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["name"] == "my_tool"
    assert skills[0]["description"] == "First line."


def test_scenario_10_discover_skills_invalid_types_fallback(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    sdir = root / "fallback_skill"
    sdir.mkdir()
    (sdir / "SKILL.md").write_text(
        "---\nname: [a, b]\ndescription: [x, y]\n---\n# Title\n\nBody line."
    )

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["name"] == "fallback_skill"
    assert skills[0]["description"] == "Body line."


def test_scenario_11_discover_skills_length_limits(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    sdir = root / "limit_skill"
    sdir.mkdir()
    long_name = "N" * 65
    long_desc = "D" * 1025
    (sdir / "SKILL.md").write_text(f"---\nname: {long_name}\ndescription: {long_desc}\n---\nBody")

    skills = discover_skills(root)
    assert len(skills) == 1
    skill = skills[0]
    assert len(skill["name"]) == SKILL_NAME_MAX_CHARS
    assert skill["name"].endswith("...")
    assert len(skill["description"]) == SKILL_DESCRIPTION_MAX_CHARS
    assert skill["description"].endswith("...")


def test_scenario_12_discover_skills_protected_paths_excluded(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    for p in [".git/x", ".hub/x", ".github/x", ".envx"]:
        d = root / p
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: bad\n---\nBad")

    good = root / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\n---\nGood")

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["name"] == "good"


def test_scenario_13_discover_skills_root_skill_md_excluded(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    # 루트 바로 아래의 SKILL.md
    (root / "SKILL.md").write_text("---\nname: root\n---\nRoot")

    # 정상 하위 스킬
    valid = root / "valid"
    valid.mkdir()
    (valid / "SKILL.md").write_text("---\nname: valid\n---\nValid")

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["name"] == "valid"


def test_scenario_14_discover_skills_symlinks(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.md").write_text("---\nname: ext\n---\nExt")

    # ① 외부 파일을 가리키는 파일 심볼릭 링크
    fl_dir = root / "fl"
    fl_dir.mkdir()
    (fl_dir / "SKILL.md").symlink_to(outside / "external.md")

    # ② 외부 폴더를 가리키는 디렉터리 심볼릭 링크
    evil_dir = outside / "evil_dir"
    evil_dir.mkdir()
    (evil_dir / "SKILL.md").write_text("---\nname: evil\n---\nEvil")
    try:
        (root / "evil").symlink_to(evil_dir, target_is_directory=True)
    except OSError:
        pass

    # ③ 루트 내부를 가리키는 심볼릭 링크
    real_dir = root / "real"
    real_dir.mkdir()
    (real_dir / "SKILL.md").write_text("---\nname: real\n---\nReal")

    alias_dir = root / "alias"
    alias_dir.mkdir()
    (alias_dir / "SKILL.md").symlink_to(real_dir / "SKILL.md")

    skills = discover_skills(root)
    names = {s["name"] for s in skills}

    assert "ext" not in names
    assert "evil" not in names
    assert "real" in names


def test_scenario_15_discover_skills_corrupted_utf8_skipped(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    bad = root / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_bytes(b"\xff\xfe\x00\x00")

    good = root / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\n---\nGood")

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["name"] == "good"


def test_scenario_16_discover_skills_nonexistent_or_file_root(tmp_path: Path):
    nonexistent = tmp_path / "skills_nonexistent"
    assert not nonexistent.exists()
    skills = discover_skills(nonexistent)
    assert skills == []
    # 호출 후에도 폴더가 생성되지 않아야 함
    assert not nonexistent.exists()

    file_root = tmp_path / "file_root"
    file_root.write_text("not a dir")
    assert discover_skills(file_root) == []


def test_scenario_17_discover_skills_duplicate_names(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    a_dup = root / "a" / "dup"
    a_dup.mkdir(parents=True)
    (a_dup / "SKILL.md").write_text("---\nname: same\n---\nBody A")

    b_dup = root / "b" / "dup"
    b_dup.mkdir(parents=True)
    (b_dup / "SKILL.md").write_text("---\nname: same\n---\nBody B")

    skills = discover_skills(root)
    assert len(skills) == 2
    paths = {s["path"] for s in skills}
    assert paths == {"a/dup", "b/dup"}


def test_scenario_18_get_skills_dir(tmp_path: Path, monkeypatch):
    custom_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(custom_home))

    skills_dir = get_skills_dir()
    expected = (custom_home / "skills").resolve()
    assert skills_dir.resolve() == expected
    # get_skills_dir는 skills 폴더를 생성하지 않음
    assert not skills_dir.exists()


def test_scenario_18b_crlf_and_bom(tmp_path: Path):
    # 파서 단독 CRLF 검증
    crlf_content = "---\r\nname: crlf_skill\r\n---\r\nbody\r\nline2"
    fm, body = parse_frontmatter(crlf_content)
    assert fm.get("name") == "crlf_skill"
    assert "\r" not in body

    # discover_skills BOM 파일 검증
    root = tmp_path / "skills"
    root.mkdir()
    bom_dir = root / "bom_skill"
    bom_dir.mkdir()
    (bom_dir / "SKILL.md").write_bytes(
        "\ufeff---\nname: bom_skill\n---\nBOM body".encode("utf-8")
    )

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["name"] == "bom_skill"


def test_scenario_18c_deep_components_category(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()

    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text("---\nname: deep\n---\nDeep body")

    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0]["category"] == "a"
    assert skills[0]["path"] == "a/b/c"


def test_scenario_18d_deterministic_sorting(tmp_path: Path):
    # 두 개의 디렉터리에 서로 다른 순서로 생성
    root1 = tmp_path / "skills1"
    root2 = tmp_path / "skills2"
    root1.mkdir()
    root2.mkdir()

    # 순서 1: z -> a -> m
    for name in ["z", "a", "m"]:
        d = root1 / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody")

    # 순서 2: a -> m -> z
    for name in ["a", "m", "z"]:
        d = root2 / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nBody")

    res1 = discover_skills(root1)
    res2 = discover_skills(root2)

    assert [s["name"] for s in res1] == ["a", "m", "z"]
    assert res1 == res2
