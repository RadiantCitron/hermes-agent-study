"""CLI 진입점 테스트: 실제 모델이 없을 때 가짜 모델을 조용히 쓰지 않는다 (로드맵 02 / 구현지시서 030 인수 시나리오)."""

import io
import sys

import pytest

from hermes_core import cli


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    # 표준 입력은 터미널이 아닌 가짜 입력으로 고정한다 (실행 환경에 기대지 않는다)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cli.main()


def test_without_mock_exits_with_code_2(hermes_home, monkeypatch):  # 시나리오 1
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)
    assert exc.value.code == 2


def test_without_mock_message_mentions_mock_flag(hermes_home, monkeypatch, capsys):  # 시나리오 2
    with pytest.raises(SystemExit):
        _run(monkeypatch)
    assert "--mock" in capsys.readouterr().err


def test_without_mock_creates_no_session_db(hermes_home, monkeypatch):  # 시나리오 3
    with pytest.raises(SystemExit):
        _run(monkeypatch)
    assert not (hermes_home / "state.db").exists()


def test_mock_mode_finishes_cleanly(hermes_home, monkeypatch, capsys):  # 시나리오 4
    _run(monkeypatch, "--mock")
    assert "Mock session finished cleanly." in capsys.readouterr().out


def test_mock_mode_keeps_mock_notice(hermes_home, monkeypatch, capsys):  # 시나리오 5
    _run(monkeypatch, "--mock")
    assert "MOCK mode" in capsys.readouterr().out
