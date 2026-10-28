"""경로 판정 공용 모듈(path_guard) 단위 테스트.

[인수 테스트 시나리오 대응: 010_경로판정_공용모듈_추출.md §3]
"""

from pathlib import Path
import pytest

from hermes_core.tools.path_guard import is_inside, path_values, resolve_workspace


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
