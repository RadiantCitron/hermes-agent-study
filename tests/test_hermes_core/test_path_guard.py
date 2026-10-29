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


def test_030_scenario_14_all_instruction_files_injected(tmp_path: Path):
    """시나리오 14: INSTRUCTION_FILE_NAMES의 각 이름으로 파일 생성 시 build_context_files_prompt에 포함됨."""
    for idx, name in enumerate(INSTRUCTION_FILE_NAMES):
        w = tmp_path / f"w_inj_{idx}"
        w.mkdir()
        marker = f"UNIQUE-MARKER-{idx}-{name}"
        (w / name).write_text(marker, encoding="utf-8")

        prompt = build_context_files_prompt(str(w))
        assert marker in prompt


# =============================================================================
# 로드맵 04: 구현지시서 010 인수 테스트 시나리오 (시나리오 1 ~ 10)
# =============================================================================


@pytest.fixture
def r04_guard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """상태 폴더 H(tmp_path/'home'), 작업 폴더 W(H/'workspace')."""
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("HERMES_HOME", str(home))
    workspace = (home / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # memories/MEMORY.md 및 state.db 파일 생성
    mem_dir = home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("memory content", encoding="utf-8")
    (home / "state.db").write_text("sqlite db placeholder", encoding="utf-8")

    return home, workspace


def test_010_scenario_01_get_default_workspace(r04_guard_env):
    """시나리오 1: get_default_workspace() -> H/'workspace'와 같고 폴더가 만들어져 있음"""
    from hermes_core.constants import get_default_workspace

    home, workspace = r04_guard_env
    default_ws = get_default_workspace()
    assert default_ws == workspace
    assert default_ws.is_dir()


def test_010_scenario_02_write_inside_nested_workspace_allowed(r04_guard_env):
    """시나리오 2: check_write_path('notes.txt', W), check_write_path('sub/new.txt', W) -> error None, W 아래"""
    _, w = r04_guard_env
    t1, err1 = check_write_path("notes.txt", w)
    assert err1 is None
    assert t1 == (w / "notes.txt").resolve()

    t2, err2 = check_write_path("sub/new.txt", w)
    assert err2 is None
    assert t2 == (w / "sub" / "new.txt").resolve()


def test_010_scenario_03_memory_and_db_outside_workspace_rejected(r04_guard_env):
    """시나리오 3: check_path('../memories/MEMORY.md', W), check_path('../state.db', W) -> error에 outside 포함"""
    _, w = r04_guard_env
    t1, err1 = check_path("../memories/MEMORY.md", w)
    assert t1 is None
    assert err1 is not None and "outside" in err1

    t2, err2 = check_path("../state.db", w)
    assert t2 is None
    assert err2 is not None and "outside" in err2


def test_010_scenario_04_write_state_db_outside_rejected(r04_guard_env):
    """시나리오 4: check_write_path('../state.db', W) -> error에 outside 포함"""
    _, w = r04_guard_env
    t, err = check_write_path("../state.db", w)
    assert t is None
    assert err is not None and "outside" in err


def test_010_scenario_05_workspace_equals_hermes_home_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 5: HERMES_HOME을 W로 지정(작업 폴더 == 상태 폴더), check_write_path('MEMORY.md', W) -> error에 state folder 포함"""
    w = (tmp_path / "same_home_ws").resolve()
    w.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(w))

    t, err = check_write_path("MEMORY.md", w)
    assert t is None
    assert err is not None and "state folder" in err


def test_010_scenario_06_env_files_rejected(r04_guard_env):
    """시나리오 6: check_path('.env', W), check_path('.env.local', W), check_path('sub/.env', W), check_path('.ENV', W) -> error에 protected 포함"""
    _, w = r04_guard_env
    for p in [".env", ".env.local", "sub/.env", ".ENV"]:
        t, err = check_path(p, w)
        assert t is None
        assert err is not None and "protected" in err


def test_010_scenario_07_git_files_rejected(r04_guard_env):
    """시나리오 7: check_path('.git/hooks/pre-push', W), check_write_path('.git/hooks/pre-push', W), check_path('sub/.git/config', W) -> error에 protected 포함"""
    _, w = r04_guard_env
    t1, err1 = check_path(".git/hooks/pre-push", w)
    assert t1 is None
    assert err1 is not None and "protected" in err1

    t2, err2 = check_write_path(".git/hooks/pre-push", w)
    assert t2 is None
    assert err2 is not None and "protected" in err2

    t3, err3 = check_path("sub/.git/config", w)
    assert t3 is None
    assert err3 is not None and "protected" in err3


def test_010_scenario_08_symlink_to_env_rejected(r04_guard_env):
    """시나리오 8: W/link가 W/.env를 가리키는 링크일 때 check_path('link', W) -> error에 protected 포함 (풀린 경로 구성요소 검사)"""
    _, w = r04_guard_env
    env_target = w / ".env"
    env_target.write_text("SECRET=1", encoding="utf-8")
    link = w / "link"
    link.symlink_to(env_target)

    t, err = check_path("link", w)
    assert t is None
    assert err is not None and "protected" in err


def test_010_scenario_09_non_protected_names_allowed(r04_guard_env):
    """시나리오 9: check_path('environment.txt', W), check_path('docs/.gitignore', W), check_path('.github/x.yml', W), check_path('notes.txt', W) -> error None"""
    _, w = r04_guard_env
    (w / "docs").mkdir(parents=True, exist_ok=True)
    (w / ".github").mkdir(parents=True, exist_ok=True)

    for p in ["environment.txt", "docs/.gitignore", ".github/x.yml", "notes.txt"]:
        t, err = check_path(p, w)
        assert err is None
        assert t == (w / p).resolve()

