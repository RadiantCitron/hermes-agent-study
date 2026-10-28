"""터미널 도구 허용목록 가드 테스트 (로드맵 02 / 구현지시서 040 인수 시나리오).

시나리오 17(기존 e2e 터미널 테스트)은 test_agent_e2e.py에 이미 있어 여기서 다시 쓰지 않는다.
작업 폴더 지정은 050에서 추가되므로, 이 단계의 시험은 현재 작업 폴더를 임시 폴더로 바꿔서 한다.
"""

import json

import pytest

from hermes_core.tools import terminal_tool as terminal_module
from hermes_core.tools.terminal_tool import TERMINAL_MAX_OUTPUT_CHARS, terminal_tool


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(command, **kwargs):
    return json.loads(terminal_tool(command, **kwargs))


def test_echo_runs(in_tmp_dir):  # 시나리오 1
    result = _run("echo hello")
    assert result["success"] is True
    assert result["stdout"] == "hello\n"


def test_quotes_only_group_arguments():  # 시나리오 2
    assert _run("echo 'a b'")["stdout"] == "a b\n"


def test_semicolon_is_not_interpreted(in_tmp_dir):  # 시나리오 3
    result = _run("echo a; touch marker")
    assert result["stdout"] == "a; touch marker\n"
    assert not (in_tmp_dir / "marker").exists()


def test_pipe_character_is_passed_literally():  # 시나리오 4
    pipe = "|"
    result = _run(f"echo hello {pipe} cat")
    assert result["stdout"] == f"hello {pipe} cat\n"


def test_variable_is_not_expanded():  # 시나리오 5
    assert _run("echo $HOME")["stdout"] == "$HOME\n"


def test_command_substitution_is_not_expanded():  # 시나리오 6
    assert _run("echo $(whoami)")["stdout"] == "$(whoami)\n"


def test_rm_is_rejected_and_file_survives(in_tmp_dir):  # 시나리오 7
    target = in_tmp_dir / "x"
    target.write_text("keep", encoding="utf-8")
    result = _run("rm -rf x")
    assert result["success"] is False
    assert "rm" in result["error"]
    assert target.exists()


def test_python_is_rejected():  # 시나리오 8
    result = _run("python -c 'print(1)'")
    assert result["success"] is False
    assert "python" in result["error"]


def test_sudo_is_rejected():  # 시나리오 9
    result = _run("sudo ls")
    assert result["success"] is False
    assert "sudo" in result["error"]


def test_program_with_path_is_rejected():  # 시나리오 10
    assert _run("/bin/ls")["success"] is False


def test_unterminated_quote_is_reported_without_exception():  # 시나리오 11
    result = _run("echo 'unterminated")
    assert result["success"] is False
    assert "parse" in result["error"]


@pytest.mark.parametrize("command", ["", "   "])  # 시나리오 12
def test_empty_command_keeps_existing_message(command):
    result = _run(command)
    assert result["success"] is False
    assert result["error"] == "Command cannot be empty."


def test_cat_without_arguments_does_not_wait_for_input():  # 시나리오 13
    result = _run("cat")
    assert result["success"] is True
    assert result["stdout"] == ""


def test_long_output_is_truncated(in_tmp_dir):  # 시나리오 14
    (in_tmp_dir / "big.txt").write_text("a" * 20_000, encoding="utf-8")
    result = _run("cat big.txt")
    notice = "\n...[stdout truncated]..."
    assert result["stdout"].endswith(notice)
    assert result["stdout"] == "a" * TERMINAL_MAX_OUTPUT_CHARS + notice


def test_timeout_is_reported(monkeypatch):  # 시나리오 15
    import subprocess

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="echo", timeout=1)

    monkeypatch.setattr(terminal_module.subprocess, "run", fake_run)
    result = _run("echo hi")
    assert result["success"] is False
    assert "timed out" in result["error"]
    assert result["exit_code"] == -1


def test_nonzero_exit_is_reported():  # 시나리오 16
    result = _run("ls missing_file")
    assert result["success"] is False
    assert result["exit_code"] != 0
    assert result["stderr"]


@pytest.mark.parametrize("bad_timeout", [0, 31])  # 시나리오 18, 19
def test_timeout_outside_range_is_rejected_without_running(monkeypatch, bad_timeout):
    calls = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))
    result = _run("echo hi", timeout=bad_timeout)
    assert result["success"] is False
    assert "timeout" in result["error"]
    assert calls == []


def test_timeout_inside_range_runs():  # 시나리오 20
    result = _run("echo hi", timeout=5)
    assert result["success"] is True
    assert result["stdout"] == "hi\n"
