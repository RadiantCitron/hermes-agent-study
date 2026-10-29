"""Hermes Core 패키지 스모크 테스트 및 격리 환경 검증."""

from pathlib import Path

import hermes_core
from hermes_core.constants import (
    CONTEXT_FILE_MAX_CHARS,
    ENTRY_DELIMITER,
    MEMORY_MAX_CHARS,
    PROJECT_ROOT,
    USER_MAX_CHARS,
    get_hermes_home,
)


def test_get_hermes_home_local_isolation(monkeypatch, tmp_path):
    """기본 상태에서 호스트의 ~/.hermes가 아닌 프로젝트 로컬 .hermes로 격리되는지 검증."""
    fake_project_root = tmp_path / "fake_project"
    fake_local_home = fake_project_root / ".hermes"
    monkeypatch.setattr("hermes_core.constants.DEFAULT_LOCAL_HERMES_HOME", fake_local_home)
    monkeypatch.setattr("hermes_core.constants.PROJECT_ROOT", fake_project_root)

    # 환경변수가 비어있을 때 기본 로컬 격리 경로 확인
    monkeypatch.delenv("HERMES_HOME", raising=False)
    home = get_hermes_home()

    assert home == fake_local_home
    assert home != Path.home() / ".hermes"
    assert home.exists()


def test_get_hermes_home_env_override(monkeypatch, tmp_path):
    """HERMES_HOME 환경변수가 주어지면 해당 경로를 우선 사용하는지 검증."""
    custom_home = tmp_path / "custom_hermes"
    monkeypatch.setenv("HERMES_HOME", str(custom_home))

    home = get_hermes_home()
    assert home == custom_home
    assert home.exists()
