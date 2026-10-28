"""경로 판정 공용 모듈(path_guard) 단위 테스트.

[인수 테스트 시나리오 대응: 010_경로판정_공용모듈_추출.md §3]
"""

from pathlib import Path
import pytest

from hermes_core.agent.prompt_builder import build_context_files_prompt
from hermes_core.constants import INSTRUCTION_FILE_NAMES
from hermes_core.tools.path_guard import (
    check_path,
    check_write_path,
    is_inside,
    path_values,
    resolve_workspace,
)


@pytest.fixture
def workspace_env(tmp_path: Path):
    """테스트용 작업 폴더 및 파일 구조 구성.

    임시 폴더 구성: 작업 폴더 W 안에 a.txt, sub/b.txt, 그리고 W 밖의 outside.txt를 가리키는 링크 link.
    """
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    outside = (tmp_path / "outside.txt").resolve()
    outside.write_text("outside", encoding="utf-8")

    (workspace / "a.txt").write_text("a", encoding="utf-8")
    sub = workspace / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("b", encoding="utf-8")

    (workspace / "link").symlink_to(outside)

    return workspace


def test_scenario_01_resolve_workspace_none(workspace_env: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 1: resolve_workspace(None) (현재 폴더를 W로 바꾼 뒤) -> W.resolve()"""
    monkeypatch.chdir(workspace_env)
    assert resolve_workspace(None) == workspace_env.resolve()


def test_scenario_02_resolve_workspace_explicit(workspace_env: Path):
    """시나리오 2: resolve_workspace(W) -> W.resolve()"""
    assert resolve_workspace(workspace_env) == workspace_env.resolve()


def test_scenario_03_path_values_long_opt_with_value():
    """시나리오 3: path_values("--file=/etc/x") -> ["/etc/x"]"""
    assert path_values("--file=/etc/x") == ["/etc/x"]


def test_scenario_04_path_values_long_opt_flag():
    """시나리오 4: path_values("--long") -> []"""
    assert path_values("--long") == []


def test_scenario_05_path_values_short_opt_with_value():
    """시나리오 5: path_values("-fVAL") -> ["VAL"]"""
    assert path_values("-fVAL") == ["VAL"]


def test_scenario_06_path_values_short_opt_flag_and_dash():
    """시나리오 6: path_values("-r"), path_values("-") -> 둘 다 []"""
    assert path_values("-r") == []
    assert path_values("-") == []


def test_scenario_07_path_values_plain_argument():
    """시나리오 7: path_values("plain.txt") -> ["plain.txt"]"""
    assert path_values("plain.txt") == ["plain.txt"]


def test_scenario_08_is_inside_file_in_workspace(workspace_env: Path):
    """시나리오 8: is_inside("a.txt", W) -> 참"""
    assert is_inside("a.txt", workspace_env) is True


def test_scenario_09_is_inside_parent_escape(workspace_env: Path):
    """시나리오 9: is_inside("../outside.txt", W) -> 거짓"""
    assert is_inside("../outside.txt", workspace_env) is False


def test_scenario_10_is_inside_absolute_path_outside(workspace_env: Path):
    """시나리오 10: is_inside("/etc/hosts", W) -> 거짓"""
    assert is_inside("/etc/hosts", workspace_env) is False


def test_scenario_11_is_inside_symlink_pointing_outside(workspace_env: Path):
    """시나리오 11: is_inside("link", W) (폴더 안 링크가 폴더 밖 파일을 가리킴) -> 거짓"""
    assert is_inside("link", workspace_env) is False


def test_scenario_12_is_inside_relative_parent_staying_inside(workspace_env: Path):
    """시나리오 12: is_inside("sub/../a.txt", W) -> 참"""
    assert is_inside("sub/../a.txt", workspace_env) is True


def test_scenario_13_is_inside_not_yet_created(workspace_env: Path):
    """시나리오 13: is_inside("not/yet/created.txt", W) (없는 경로) -> 참"""
    assert is_inside("not/yet/created.txt", workspace_env) is True


def test_scenario_14_is_inside_tilde_not_expanded(workspace_env: Path):
    """시나리오 14: is_inside("~", W) -> 참 (홈 폴더로 확장하지 않음)"""
    assert is_inside("~", workspace_env) is True


def test_scenario_15_is_inside_nul_byte_returns_false_no_exception(workspace_env: Path):
    """시나리오 15: is_inside("a\\x00b", W) (NUL 문자가 든 경로) -> 거짓, 예외 없음"""
    assert is_inside("a\x00b", workspace_env) is False


# =============================================================================
# 로드맵 030 쓰기 금지 정책 인수 테스트 시나리오 (§3)
# =============================================================================


@pytest.fixture
def write_guard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """쓰기 금지 테스트용 환경. HERMES_HOME을 W 밖의 임시 폴더로 기본 설정."""
    w = (tmp_path / "workspace").resolve()
    w.mkdir(exist_ok=True)
    outside = (tmp_path / "outside.txt").resolve()
    outside.write_text("outside", encoding="utf-8")

    hermes_home = (tmp_path / "hermes_home").resolve()
    hermes_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    return w


def test_030_scenario_01_normal_write_path(write_guard_env: Path):
    """시나리오 1: notes.txt -> error None, target == (W/'notes.txt').resolve()"""
    target, err = check_write_path("notes.txt", write_guard_env)
    assert err is None
    assert target == (write_guard_env / "notes.txt").resolve()


def test_030_scenario_02_agents_rejection(write_guard_env: Path):
    """시나리오 2: AGENTS.md -> error에 instruction 포함, target None"""
    target, err = check_write_path("AGENTS.md", write_guard_env)
    assert target is None
    assert err is not None
    assert "instruction" in err


def test_030_scenario_03_sub_agents_rejection(write_guard_env: Path):
    """시나리오 3: sub/agents.md -> error에 instruction 포함"""
    target, err = check_write_path("sub/agents.md", write_guard_env)
    assert target is None
    assert err is not None
    assert "instruction" in err


def test_030_scenario_04_soul_and_cursorrules_rejection(write_guard_env: Path):
    """시나리오 4: SOUL.md, .cursorrules -> 각각 error에 instruction 포함"""
    for name in ["SOUL.md", ".cursorrules"]:
        target, err = check_write_path(name, write_guard_env)
        assert target is None
        assert err is not None
        assert "instruction" in err


def test_030_scenario_05_case_insensitive_rejection(write_guard_env: Path):
    """시나리오 5: Agents.MD (대소문자 다름) -> error에 instruction 포함"""
    target, err = check_write_path("Agents.MD", write_guard_env)
    assert target is None
    assert err is not None
    assert "instruction" in err


def test_030_scenario_06_nonexistent_parent_folder_rejection(write_guard_env: Path):
    """시나리오 6: sub/new/SOUL.md (부모 폴더가 아직 없음) -> error에 instruction 포함"""
    target, err = check_write_path("sub/new/SOUL.md", write_guard_env)
    assert target is None
    assert err is not None
    assert "instruction" in err


def test_030_scenario_07_link_to_instruction_file_rejection(write_guard_env: Path):
    """시나리오 7: notes_link (W 안 링크가 기존 W/AGENTS.md를 가리킴) -> error에 instruction 포함"""
    agents_file = write_guard_env / "AGENTS.md"
    agents_file.write_text("agents", encoding="utf-8")
    link = write_guard_env / "notes_link"
    link.symlink_to(agents_file)

    target, err = check_write_path("notes_link", write_guard_env)
    assert target is None
    assert err is not None
    assert "instruction" in err


def test_030_scenario_08_instruction_link_pointing_to_target_rejection(write_guard_env: Path):
    """시나리오 8: notes.txt (W/AGENTS.md가 W/notes.txt를 가리키는 링크로 존재) -> error에 instruction 포함"""
    notes_file = write_guard_env / "notes.txt"
    notes_file.write_text("notes", encoding="utf-8")
    agents_link = write_guard_env / "AGENTS.md"
    agents_link.symlink_to(notes_file)

    target, err = check_write_path("notes.txt", write_guard_env)
    assert target is None
    assert err is not None
    assert "instruction" in err


def test_030_scenario_09_hermes_state_folder_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 9: HERMES_HOME을 W/.hermes로 지정하고 .hermes/memories/MEMORY.md, .hermes/state.db, .hermes/new.txt"""
    w = (tmp_path / "w_state").resolve()
    w.mkdir()
    hermes_home = w / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    for subpath in [".hermes/memories/MEMORY.md", ".hermes/state.db", ".hermes/new.txt"]:
        target, err = check_write_path(subpath, w)
        assert target is None
        assert err is not None
        assert "state folder" in err


def test_030_scenario_10_hermes_folder_name_not_state_folder(write_guard_env: Path):
    """시나리오 10: HERMES_HOME이 W 밖일 때 .hermes/x.txt -> error None"""
    target, err = check_write_path(".hermes/x.txt", write_guard_env)
    assert err is None
    assert target == (write_guard_env / ".hermes/x.txt").resolve()


def test_030_scenario_11_outside_workspace_write_rejection(write_guard_env: Path):
    """시나리오 11: ../x.txt -> error에 outside 포함"""
    target, err = check_write_path("../x.txt", write_guard_env)
    assert target is None
    assert err is not None
    assert "outside" in err


def test_030_scenario_12_control_chars_write_rejection(write_guard_env: Path):
    """시나리오 12: a\\nb -> error에 control 포함"""
    target, err = check_write_path("a\nb", write_guard_env)
    assert target is None
    assert err is not None
    assert "control" in err


def test_030_scenario_13_read_allowed_for_instruction_and_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 13 (경로 검증부): AGENTS.md와 .hermes/memories/MEMORY.md의 check_path는 오류 없음."""
    w = (tmp_path / "w_read").resolve()
    w.mkdir()
    hermes_home = w / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    (w / "AGENTS.md").write_text("instructions", encoding="utf-8")
    mem_dir = hermes_home / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("memory content", encoding="utf-8")

    target1, err1 = check_path("AGENTS.md", w)
    assert err1 is None
    assert target1 == (w / "AGENTS.md").resolve()

    target2, err2 = check_path(".hermes/memories/MEMORY.md", w)
    assert err2 is None
    assert target2 == (mem_dir / "MEMORY.md").resolve()


def test_030_scenario_14_all_instruction_files_injected(tmp_path: Path):
    """시나리오 14: INSTRUCTION_FILE_NAMES의 각 이름으로 파일 생성 시 build_context_files_prompt에 포함됨."""
    for idx, name in enumerate(INSTRUCTION_FILE_NAMES):
        w = tmp_path / f"w_inj_{idx}"
        w.mkdir()
        marker = f"UNIQUE-MARKER-{idx}-{name}"
        (w / name).write_text(marker, encoding="utf-8")

        prompt = build_context_files_prompt(str(w))
        assert marker in prompt


def test_030_scenario_15_non_instruction_file_not_injected(tmp_path: Path):
    """시나리오 15: 작업 폴더에 NOTES.md만 있을 때 build_context_files_prompt(cwd) -> 빈 문자열"""
    w = tmp_path / "w_notes"
    w.mkdir()
    (w / "NOTES.md").write_text("notes content", encoding="utf-8")

    prompt = build_context_files_prompt(str(w))
    assert prompt == ""


def test_030_scenario_16_instruction_file_names_tuple():
    """시나리오 16: INSTRUCTION_FILE_NAMES == ('AGENTS.md', 'agents.md', 'SOUL.md', '.cursorrules')"""
    assert INSTRUCTION_FILE_NAMES == ("AGENTS.md", "agents.md", "SOUL.md", ".cursorrules")
