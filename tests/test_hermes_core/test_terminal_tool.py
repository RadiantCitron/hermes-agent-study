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


# =============================================================================
# 050: 작업 폴더 경로 제한
# =============================================================================

OUTSIDE_SECRET = "OUTSIDE-SECRET-CONTENT"


@pytest.fixture
def workspace(in_tmp_dir):
    """작업 폴더 W: a.txt, sub/b.txt, 폴더 밖 outside.txt를 가리키는 링크 link."""
    w = in_tmp_dir / "w"
    (w / "sub").mkdir(parents=True)
    (w / "a.txt").write_text("hello", encoding="utf-8")
    (w / "sub" / "b.txt").write_text("bee", encoding="utf-8")
    outside = in_tmp_dir / "outside.txt"
    outside.write_text(OUTSIDE_SECRET, encoding="utf-8")
    (w / "link").symlink_to(outside)
    return w


def _run_in(workspace, command):
    raw = terminal_tool(command, workspace=workspace)
    assert OUTSIDE_SECRET not in raw  # 폴더 밖 파일 내용은 어떤 반환값에도 나타나지 않는다
    return json.loads(raw)


def _assert_outside(result):
    assert result["success"] is False
    assert "outside" in result["error"]


def test_file_in_workspace_is_readable(workspace):  # 시나리오 1
    result = _run_in(workspace, "cat a.txt")
    assert result["success"] is True
    assert "hello" in result["stdout"]


def test_file_in_subfolder_is_readable(workspace):  # 시나리오 2
    assert _run_in(workspace, "cat sub/b.txt")["success"] is True


def test_dotdot_that_stays_inside_is_allowed(workspace):  # 시나리오 3
    assert _run_in(workspace, "cat sub/../a.txt")["success"] is True


def test_absolute_path_outside_is_rejected(workspace):  # 시나리오 4
    _assert_outside(_run_in(workspace, "cat /etc/hosts"))


def test_relative_path_escaping_workspace_is_rejected(workspace):  # 시나리오 5
    _assert_outside(_run_in(workspace, "cat ../outside.txt"))


def test_symlink_to_outside_is_rejected(workspace):  # 시나리오 6
    _assert_outside(_run_in(workspace, "cat link"))


def test_recursive_grep_over_outside_path_is_rejected(workspace):  # 시나리오 7
    assert _run_in(workspace, "grep -r root /etc")["success"] is False


def test_grep_in_workspace_works(workspace):  # 시나리오 8
    assert _run_in(workspace, "grep hello a.txt")["success"] is True


def test_long_option_value_path_is_checked(workspace):  # 시나리오 9
    assert _run_in(workspace, "grep --file=/etc/passwd x a.txt")["success"] is False


def test_options_only_are_allowed(workspace):  # 시나리오 10
    assert _run_in(workspace, "ls -la")["success"] is True


def test_runs_inside_workspace(workspace):  # 시나리오 11
    assert "a.txt" in _run_in(workspace, "ls")["stdout"]


def test_pwd_is_the_resolved_workspace(workspace):  # 시나리오 12
    assert _run_in(workspace, "pwd")["stdout"] == f"{workspace.resolve()}\n"


def test_tilde_is_not_expanded_to_home(workspace):  # 시나리오 13
    result = _run_in(workspace, "ls ~")
    assert result["success"] is False
    assert str(workspace.parent) not in result["stdout"]


def test_pattern_that_looks_like_outside_path_is_rejected_known_limit(workspace):  # 시나리오 14
    _assert_outside(_run_in(workspace, "grep / a.txt"))


def test_default_workspace_is_current_directory(workspace, monkeypatch):  # 시나리오 15
    monkeypatch.chdir(workspace)
    result = json.loads(terminal_tool("cat a.txt"))
    assert result["success"] is True


def test_short_option_with_attached_outside_path_is_rejected(workspace):  # 시나리오 16
    _assert_outside(_run_in(workspace, "grep -f/etc/passwd x a.txt"))


def test_short_option_with_attached_inside_value_is_allowed(workspace):  # 시나리오 17
    assert _run_in(workspace, "head -n5 a.txt")["success"] is True


def test_workspace_argument_from_model_is_blocked_by_dispatcher(workspace, monkeypatch):  # 시나리오 18
    from hermes_core.tools.dispatcher import create_default_dispatcher

    calls = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))
    monkeypatch.chdir(workspace)
    dispatcher = create_default_dispatcher()
    result = json.loads(
        dispatcher.handle_function_call("terminal", {"command": "cat a.txt", "workspace": "/"})
    )
    assert result["success"] is False
    assert "workspace" in result["error"]
    assert calls == []


# =============================================================================
# 로드맵 04: 구현지시서 010 인수 테스트 시나리오 (시나리오 12, 13)
# =============================================================================


def test_010_scenario_12_terminal_blocks_protected_without_executing(workspace, monkeypatch):
    """시나리오 12: terminal_tool('cat .env'), ('ls .git'), ('grep x --file=.env a.txt')
    -> 각각 success False, 오류에 protected 포함, subprocess.run 호출 0회.
    """
    calls = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    for cmd in ["cat .env", "ls .git", "grep x --file=.env a.txt"]:
        res = json.loads(terminal_tool(cmd, workspace=workspace))
        assert res["success"] is False
        assert "protected" in res["error"]

    assert calls == []


def test_010_scenario_13_terminal_normal_file_allowed(workspace):
    """시나리오 13: terminal_tool('cat notes.txt', workspace=W) (W/notes.txt 존재) -> success True"""
    notes = workspace / "notes.txt"
    notes.write_text("hello notes\n", encoding="utf-8")

    res = json.loads(terminal_tool("cat notes.txt", workspace=workspace))
    assert res["success"] is True
    assert "hello notes" in res["stdout"]
