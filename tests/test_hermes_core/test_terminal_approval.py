"""터미널 도구 승인 연동 단위/통합 테스트 (로드맵 10 - 030단계).

[근거 문서]
- 지시서: docs/로드맵/10-승인-흐름-MVP/instructions/030_터미널_도구_연동.md
- ADR-0010: §2-4, §2-5, §2-6, §2-10
- ROADMAP: §2-2, §2-4, §3, §4 (위험 1·5·8·14·15), §6-3
- WHY: 가정 A, B, C, D, I, J
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any
import uuid

import pytest

from hermes_core.constants import (
    APPROVAL_AUTO_DENIED_MESSAGE,
    APPROVAL_DENIED_MESSAGE,
    APPROVAL_LIMIT_MESSAGE,
    OPENROUTER_API_KEY_ENV,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import Approver
from hermes_core.tools.dispatcher import create_child_dispatcher, create_default_dispatcher
import hermes_core.tools.terminal_tool as terminal_module
from hermes_core.tools.terminal_tool import terminal_tool


def py(code: str) -> str:
    """현재 파이썬 인터프리터로 실행하는 명령 문자열을 생성한다."""
    return shlex.join([sys.executable, "-c", code])


@pytest.fixture
def session_db(tmp_path: Path) -> SessionDB:
    """테스트용 격리된 SessionDB 인스턴스를 생성한다."""
    db_path = tmp_path / "state.db"
    return SessionDB(str(db_path))


@pytest.fixture
def session_id(session_db: SessionDB) -> str:
    """기본 테스트 세션을 생성하고 session_id를 반환한다."""
    sid = "test-session-term-001"
    session_db.create_session(sid, source="cli")
    return sid


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """테스트용 격리된 작업 디렉터리를 생성한다."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# =============================================================================
# 3-1. 승인기가 없는 구성은 지금과 같다 (가정 A)
# =============================================================================
def test_scenario_01_no_approver_default_denies_unallowed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 1: approver 없이 허용목록 밖 명령 실행 시 기존 오류 문구로 거부, 실행 0회, 원장 행 0."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    # 직접 함수 호출
    res1 = json.loads(terminal_tool(py("print(1)"), workspace=workspace))
    res2 = json.loads(terminal_tool("rm x", workspace=workspace))

    # 기본 디스패처를 통한 호출
    d = create_default_dispatcher()
    res3 = json.loads(d.handle_function_call("terminal", {"command": py("print(1)")}))

    expected_prefix = "Command '"
    expected_allowed = "Allowed: echo, pwd, ls, cat, head, tail, wc, grep"

    for res in [res1, res2, res3]:
        assert res["success"] is False
        assert expected_prefix in res["error"]
        assert expected_allowed in res["error"]

    assert len(calls) == 0


def test_scenario_02_child_dispatcher_cannot_have_approver(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 2: create_child_dispatcher의 terminal은 별도 승인기가 없어 python 명령 거부."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    child_d = create_child_dispatcher()
    res = json.loads(child_d.handle_function_call("terminal", {"command": py("print(1)")}))

    assert res["success"] is False
    assert "not allowed. Allowed: echo, pwd, ls, cat, head, tail, wc, grep" in res["error"]
    assert len(calls) == 0


# =============================================================================
# 3-2. 승인기가 있을 때: 허용목록 안은 묻지 않는다
# =============================================================================
def test_scenario_03_allowed_commands_bypass_approval(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 3: 승인기가 있어도 허용목록 안 명령은 질문/원장 기록 없이 즉시 실행."""
    monkeypatch.chdir(workspace)
    inner_file = workspace / "inner.txt"
    inner_file.write_text("inner content\n", encoding="utf-8")

    ask_calls: list[str] = []
    say_calls: list[str] = []
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=say_calls.append,
    )

    res1 = json.loads(terminal_tool("echo hi", approver=approver))
    res2 = json.loads(terminal_tool("ls", approver=approver))
    res3 = json.loads(terminal_tool("cat inner.txt", approver=approver))

    assert res1["success"] is True
    assert res1["stdout"] == "hi\n"
    assert res2["success"] is True
    assert "inner.txt" in res2["stdout"]
    assert res3["success"] is True
    assert res3["stdout"] == "inner content\n"

    assert len(ask_calls) == 0
    assert len(say_calls) == 0
    assert len(session_db.get_approval_requests(session_id)) == 0


def test_scenario_04_allowed_commands_outside_workspace_rejected_without_asking(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 4: 승인기가 있어도 허용목록 안 명령의 작업 폴더 밖 경로는 묻지 않고 거부."""
    monkeypatch.chdir(workspace)
    ask_calls: list[str] = []
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    res = json.loads(terminal_tool("cat /etc/hosts", approver=approver))
    assert res["success"] is False
    assert "outside the workspace" in res["error"]
    assert len(ask_calls) == 0
    assert len(session_db.get_approval_requests(session_id)) == 0


# =============================================================================
# 3-3. 승인 흐름 (가정 B·C)
# =============================================================================
def test_scenario_05_approval_flow_approved(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 5: 답 'y' 승인 시 정상 실행, 원장 선기록(pending) 및 승인(approved) 갱신."""
    monkeypatch.chdir(workspace)
    ask_snapshots: list[tuple[int, str | None]] = []

    def ask_fn(prompt: str) -> str:
        rows = session_db.get_approval_requests(session_id)
        decision = rows[0]["decision"] if rows else None
        ask_snapshots.append((len(rows), decision))
        return "y"

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=ask_fn,
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    res = json.loads(terminal_tool(py("print('ok')"), approver=approver))
    assert res["success"] is True
    assert res["exit_code"] == 0
    assert res["stdout"] == "ok\n"

    # 질문 순간 이미 pending 상태로 선기록되어 있어야 함
    assert ask_snapshots == [(1, "pending")]

    # 호출 후 원장 확인
    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "approved"
    assert json.loads(rows[0]["argv_json"]) == [sys.executable, "-c", "print('ok')"]


def test_scenario_06_approval_flow_denied(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 6: 답 'n', '', 'maybe' 시 각각 denied 거부, 실행 0회."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    answers = iter(["n", "", "maybe"])
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: next(answers),
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    for i in range(3):
        res = json.loads(terminal_tool(py(f"print({i})"), approver=approver))
        assert res["success"] is False
        assert res["error"] == APPROVAL_DENIED_MESSAGE

    assert len(calls) == 0
    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 3
    for r in rows:
        assert r["decision"] == "denied"


def test_scenario_07_approval_flow_cancelled_on_exception(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 7: ask가 EOFError 또는 KeyboardInterrupt 시 cancelled 거부, 실행 0회."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    exceptions = [EOFError("EOF"), KeyboardInterrupt()]
    for exc in exceptions:
        approver = Approver(
            session_db=session_db,
            session_id=session_id,
            ask=lambda p, e=exc: (_ for _ in ()).throw(e),
            is_interactive=lambda: True,
            say=lambda s: None,
        )
        res = json.loads(terminal_tool(py("print('ex')"), approver=approver))
        assert res["success"] is False
        assert res["error"] == APPROVAL_DENIED_MESSAGE

    assert len(calls) == 0
    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 2
    for r in rows:
        assert r["decision"] == "cancelled"


def test_scenario_08_approval_flow_non_interactive(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 8: is_interactive가 False면 ask 호출 없이 auto_denied 거부, 실행 0회."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: pytest.fail("ask should not be called"),
        is_interactive=lambda: False,
        say=lambda s: None,
    )

    res = json.loads(terminal_tool(py("print('non_interactive')"), approver=approver))
    assert res["success"] is False
    assert res["error"] == APPROVAL_AUTO_DENIED_MESSAGE
    assert len(calls) == 0

    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "auto_denied"


def test_scenario_09_approval_request_limit(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 9: limit=2 설정 시 3번째 요청은 ask 없이 limit_exceeded 거부."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    answers = iter(["n", "n", "y"])
    ask_count = 0

    def ask_fn(prompt: str) -> str:
        nonlocal ask_count
        ask_count += 1
        return next(answers)

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=ask_fn,
        is_interactive=lambda: True,
        say=lambda s: None,
        limit=2,
    )

    # 1, 2번째 요청 (denied)
    res1 = json.loads(terminal_tool(py("print(1)"), approver=approver))
    res2 = json.loads(terminal_tool(py("print(2)"), approver=approver))
    assert res1["success"] is False
    assert res2["success"] is False
    assert res1["error"] == APPROVAL_DENIED_MESSAGE
    assert res2["error"] == APPROVAL_DENIED_MESSAGE

    # 3번째 요청 (limit exceeded)
    res3 = json.loads(terminal_tool(py("print(3)"), approver=approver))
    assert res3["success"] is False
    assert res3["error"] == APPROVAL_LIMIT_MESSAGE.format(limit=2)

    assert ask_count == 2
    assert len(calls) == 0

    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 3
    assert rows[0]["decision"] == "denied"
    assert rows[1]["decision"] == "denied"
    assert rows[2]["decision"] == "limit_exceeded"


def test_scenario_10_pre_approval_validation(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 10: 빈 명령, 타임아웃 오류, shlex 구문 오류는 승인 질문/원장 기록 전에 거부."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    ask_calls: list[str] = []
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    res_empty = json.loads(terminal_tool("", approver=approver))
    res_timeout = json.loads(terminal_tool(py("print(1)"), timeout=0, approver=approver))
    res_parse = json.loads(terminal_tool("python 'unterminated", approver=approver))

    assert res_empty["success"] is False
    assert "Command cannot be empty." in res_empty["error"]

    assert res_timeout["success"] is False
    assert "Invalid timeout" in res_timeout["error"]

    assert res_parse["success"] is False
    assert "Cannot parse command:" in res_parse["error"]

    assert len(ask_calls) == 0
    assert len(calls) == 0
    assert len(session_db.get_approval_requests(session_id)) == 0


# =============================================================================
# 3-4. 보호 파일 바닥 (가정 D)
# =============================================================================
def test_scenario_11_protected_files_floor(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 11: .env*, .git 등 보호 파일 인자는 승인 질문 전에 거부, ask/원장 0회."""
    monkeypatch.chdir(workspace)
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("notes\n", encoding="utf-8")
    (workspace / "sub").mkdir(exist_ok=True)

    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    ask_calls: list[str] = []
    say_calls: list[str] = []
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=say_calls.append,
    )

    commands = [
        py("pass") + " .env",
        py("pass") + " .env.local",
        py("pass") + " .git",
        py("pass") + " --opt=.env",
        py("pass") + " sub/../.env",
        py("pass") + " -f.env",
    ]

    for cmd in commands:
        res = json.loads(terminal_tool(cmd, approver=approver))
        assert res["success"] is False, f"Failed to reject: {cmd}"
        assert "refers to a protected file or folder (.env*, .git)." in res["error"]

    assert len(ask_calls) == 0
    assert len(say_calls) == 0
    assert len(calls) == 0
    assert len(session_db.get_approval_requests(session_id)) == 0


def test_scenario_12_symlink_to_protected_file(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 12: 심볼릭 링크가 보호 파일을 가리킬 때도 풀린 경로 기준으로 승인 전 거부."""
    monkeypatch.chdir(workspace)
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to(workspace / ".env")

    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    ask_calls: list[str] = []
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    res = json.loads(terminal_tool(py("pass") + " link", approver=approver))
    assert res["success"] is False
    assert "refers to a protected file or folder (.env*, .git)." in res["error"]
    assert len(ask_calls) == 0
    assert len(calls) == 0
    assert len(session_db.get_approval_requests(session_id)) == 0


def test_scenario_13_protected_file_in_argv0(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 13: args[0] 프로그램 경로 자체도 보호 파일이면 승인 전 거부."""
    monkeypatch.chdir(workspace)
    (workspace / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    (workspace / ".git" / "hooks" / "run").write_text("#!/bin/sh\n", encoding="utf-8")
    (workspace / ".env").mkdir(exist_ok=True)
    (workspace / ".env" / "run").write_text("#!/bin/sh\n", encoding="utf-8")

    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    ask_calls: list[str] = []
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    for prog in [".git/hooks/run", "./.env/run"]:
        res = json.loads(terminal_tool(prog, approver=approver))
        assert res["success"] is False
        assert "refers to a protected file or folder (.env*, .git)." in res["error"]

    assert len(ask_calls) == 0
    assert len(calls) == 0
    assert len(session_db.get_approval_requests(session_id)) == 0


def test_scenario_14_non_protected_argument(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 14: 일반 파일 인자는 바닥 가드에 걸리지 않고 정상 승인 흐름으로 진행."""
    monkeypatch.chdir(workspace)
    (workspace / "notes.txt").write_text("hello\n", encoding="utf-8")

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    res = json.loads(terminal_tool(py("pass") + " notes.txt", approver=approver))
    assert res["success"] is True
    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "approved"


# =============================================================================
# 3-5. 승인된 실행의 방식 (가정 I, ROADMAP §6-3)
# =============================================================================
def test_scenario_15_approved_execution_outside_workspace_allowed(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 15: 승인된 명령은 작업 폴더 이탈 검사를 건너뛰고 실행됨 (허용목록 안 명령은 차단)."""
    monkeypatch.chdir(workspace)
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    # 승인된 명령: / 인자 허용
    cmd = py("import sys; print(sys.argv[1])") + " /"
    res = json.loads(terminal_tool(cmd, approver=approver))
    assert res["success"] is True
    assert res["stdout"] == "/\n"

    # 허용목록 안 명령: 같은 / 인자 거부
    res_ls = json.loads(terminal_tool("ls /", approver=approver))
    assert res_ls["success"] is False
    assert "outside the workspace" in res_ls["error"]


def test_scenario_16_approved_execution_mechanics(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 16: 승인된 실행 방식 (셸 없음, cwd=작업폴더, stdin=DEVNULL, 출력절단, 타임아웃)."""
    monkeypatch.chdir(workspace)
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    # (a) shell=False 검증: 세미콜론이 셸 메타문자로 해석되지 않아야 함
    res_a = json.loads(terminal_tool(py("import sys; print(sys.argv[1:])") + " 'a; touch marker'", approver=approver))
    assert res_a["success"] is True
    assert "['a; touch marker']" in res_a["stdout"]
    assert not (workspace / "marker").exists()

    # (b) cwd 검증: 작업 폴더의 resolve 경로와 일치
    res_b = json.loads(terminal_tool(py("import os; print(os.getcwd())"), approver=approver))
    assert res_b["success"] is True
    assert res_b["stdout"].strip() == str(workspace.resolve())

    # (c) stdin=DEVNULL 검증: EOF 즉시 반환
    res_c = json.loads(terminal_tool(py("import sys; print(repr(sys.stdin.read()))"), approver=approver))
    assert res_c["success"] is True
    assert res_c["stdout"] == "''\n"

    # (d) 출력 10,000자 상한 절단
    res_d = json.loads(terminal_tool(py("print('a'*20000)"), approver=approver))
    assert res_d["success"] is True
    assert res_d["stdout"] == "a" * 10000 + "\n...[stdout truncated]..."

    # (e) timeout=1 초과
    res_e = json.loads(terminal_tool(py("import time; time.sleep(5)"), timeout=1, approver=approver))
    assert res_e["success"] is False
    assert "timed out" in res_e["error"]
    assert res_e["exit_code"] == -1


# =============================================================================
# 3-6. 키 환경변수 차단 (가정 J)
# =============================================================================
def test_scenario_17_child_env_stripped(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 17: 하위 프로세스 환경에서 키 제거 및 Zero Leakage 확인 (부모 환경 보존)."""
    fake_key = "FAKE-NOT-A-REAL-KEY-0001"
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, fake_key)
    monkeypatch.setenv("HERMES_TEST_KEEP", "keep")
    monkeypatch.chdir(workspace)

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    cmd1 = py(
        "import os; print(os.environ.get('OPENROUTER_API_KEY','<unset>'), os.environ.get('HERMES_TEST_KEEP'))"
    )
    raw_json1 = terminal_tool(cmd1, approver=approver)
    res1 = json.loads(raw_json1)
    assert res1["success"] is True
    assert res1["stdout"] == "<unset> keep\n"

    cmd2 = py("import os; print(dict(os.environ))")
    raw_json2 = terminal_tool(cmd2, approver=approver)
    res2 = json.loads(raw_json2)
    assert res2["success"] is True

    # JSON 전체 및 원장에 키 문자열 미노출 검증
    assert fake_key not in raw_json1
    assert fake_key not in raw_json2

    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 2
    for r in rows:
        assert fake_key not in (r["argv_json"] or "")
        assert fake_key not in (r["reason"] or "")

    # 부모 환경에는 키가 그대로 남아 있어야 함
    assert os.environ[OPENROUTER_API_KEY_ENV] == fake_key


def test_scenario_18_subprocess_env_argument_verified(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 18: 허용목록 안/밖 명령 모두 subprocess.run에 키가 제거된 env가 전달됨."""
    fake_key = "FAKE-NOT-A-REAL-KEY-0001"
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, fake_key)
    monkeypatch.setenv("HERMES_TEST_KEEP", "keep")
    monkeypatch.chdir(workspace)

    captured_envs: list[dict[str, str] | None] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="mocked\n", stderr="")

    monkeypatch.setattr(terminal_module.subprocess, "run", fake_run)

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    # 허용목록 안 명령
    terminal_tool("echo hi", approver=approver)
    # 승인된 명령
    terminal_tool(py("pass"), approver=approver)

    assert len(captured_envs) == 2
    for env in captured_envs:
        assert env is not None
        assert OPENROUTER_API_KEY_ENV not in env
        assert env.get("HERMES_TEST_KEEP") == "keep"
        assert "PATH" in env


# =============================================================================
# 3-7. 디스패처 주입과 기록 정합 (가정 A·C·I, 위험 15)
# =============================================================================
def test_scenario_19_dispatcher_injection_and_isolation(
    workspace: Path,
    session_db: SessionDB,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 19: 디스패처에 승인기 주입 시 연동 동작 및 미주입 디스패처 격리 확인."""
    monkeypatch.chdir(workspace)
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    d_app = create_default_dispatcher(session_db=session_db, approver=approver)
    d_no = create_default_dispatcher(session_db=session_db)

    # 승인기 있는 디스패처: 실행 성공
    res_app = json.loads(d_app.handle_function_call("terminal", {"command": py("print(7)")}))
    assert res_app["success"] is True
    assert res_app["stdout"] == "7\n"

    rows = session_db.get_approval_requests(session_id)
    assert len(rows) == 1
    assert rows[0]["decision"] == "approved"

    # 승인기 없는 디스패처: 기존 거부
    res_no = json.loads(d_no.handle_function_call("terminal", {"command": py("print(7)")}))
    assert res_no["success"] is False
    assert "not allowed" in res_no["error"]
    assert len(session_db.get_approval_requests(session_id)) == 1


def test_scenario_20_model_schema_identical_and_forbids_approver(
    session_db: SessionDB,
    session_id: str,
) -> None:
    """시나리오 20: 모델에게 노출되는 terminal 스키마는 approver와 무관하게 동일하고 approver 인자 주입 시 거부."""
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )

    d1 = create_default_dispatcher(approver=approver)
    d2 = create_default_dispatcher()

    schema1 = next(t for t in d1.get_tools_schema() if t["function"]["name"] == "terminal")
    schema2 = next(t for t in d2.get_tools_schema() if t["function"]["name"] == "terminal")
    assert schema1 == schema2
    assert set(schema1["function"]["parameters"]["properties"].keys()) == {"command", "timeout"}

    # 모델이 approver 인자를 넘기려 할 경우 인자 유효성 검사에서 차단
    res = json.loads(d1.handle_function_call("terminal", {"command": py("print(1)"), "approver": "x"}))
    assert res["success"] is False
    assert "unexpected argument 'approver'" in res["error"]

    # include에 terminal이 없는 경우 오류 없이 생성되고 terminal 미등록
    d3 = create_default_dispatcher(include=["read_file"], approver=approver)
    names = [t["function"]["name"] for t in d3.get_tools_schema()]
    assert names == ["read_file"]
    assert "terminal" not in names


def test_scenario_21_database_failure_does_not_execute(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 21: 선기록 또는 결정 갱신 DB 실패 시 Tool failed 예외 전파 및 명령 미실행."""
    monkeypatch.chdir(workspace)
    calls: list[Any] = []
    monkeypatch.setattr(terminal_module.subprocess, "run", lambda *a, **k: calls.append(1))

    # (a) 없는 session_id (선기록 외래키 실패)
    db = SessionDB(str(workspace / "state_orphan.db"))
    app_orphan = Approver(
        session_db=db,
        session_id="orphan-session-id",
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )
    d1 = create_default_dispatcher(session_db=db, approver=app_orphan)
    res1 = json.loads(d1.handle_function_call("terminal", {"command": py("print(1)")}))
    assert res1["success"] is False
    assert res1["error"].startswith("Tool 'terminal' failed:")
    assert len(calls) == 0

    # (b) resolve_approval_request 실패 가짜 DB
    db2 = SessionDB(str(workspace / "state_broken.db"))
    db2.create_session("s1", source="cli")

    class BrokenResolveDB:
        def __init__(self, real: SessionDB):
            self.real = real

        def __getattr__(self, name: str) -> Any:
            return getattr(self.real, name)

        def resolve_approval_request(self, *a: Any, **k: Any) -> bool:
            return False

    app_broken = Approver(
        session_db=BrokenResolveDB(db2),  # type: ignore[arg-type]
        session_id="s1",
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda s: None,
    )
    d2 = create_default_dispatcher(session_db=db2, approver=app_broken)
    res2 = json.loads(d2.handle_function_call("terminal", {"command": py("print(1)")}))
    assert res2["success"] is False
    assert res2["error"].startswith("Tool 'terminal' failed:")
    assert len(calls) == 0


# =============================================================================
# 3-8. 에이전트까지 이어서 (가정 I, 연결 방식 확정의 검증)
# =============================================================================
def test_scenario_23_delegate_tool_post_registration_retains_approver(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 23: register_delegate_tool 사후 등록 후에도 부모 terminal은 승인기를 유지하고 자식은 승인기 없음."""
    monkeypatch.chdir(workspace)
    from hermes_core.agent.agent import HermesAgent, MockLLMClient
    from hermes_core.tools.delegate_tool import register_delegate_tool

    sid = str(uuid.uuid4())
    db = SessionDB(str(workspace / "state_del.db"))
    ask_calls: list[str] = []
    approver = Approver(
        session_db=db,
        session_id=sid,
        ask=lambda p: (ask_calls.append(p), "y")[1],
        is_interactive=lambda: True,
        say=lambda s: None,
    )
    dispatcher = create_default_dispatcher(session_db=db, approver=approver)
    agent = HermesAgent(
        session_id=sid,
        session_db=db,
        dispatcher=dispatcher,
        llm_client=MockLLMClient(),
        skip_memory=True,
        skip_context_files=True,
    )

    # 위임 도구 사후 등록
    register_delegate_tool(
        agent.dispatcher,
        llm_client_factory=lambda: MockLLMClient(),
        parent_session_id=sid,
        session_db=db,
    )

    # 부모 디스패처의 terminal은 여전히 승인기를 거침
    res = json.loads(agent.dispatcher.handle_function_call("terminal", {"command": py("print('parent after del')")}))
    assert res["success"] is True
    assert len(ask_calls) == 1

    # 자식 디스패처는 승인기 없이 거부
    child_d = create_child_dispatcher()
    res_child = json.loads(child_d.handle_function_call("terminal", {"command": py("print('child')")}))
    assert res_child["success"] is False
    assert "not allowed" in res_child["error"]
