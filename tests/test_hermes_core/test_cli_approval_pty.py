"""가상 터미널(pty) 승인 프롬프트 통합 검증 테스트 (로드맵 10 / 구현지시서 050).

IUTF8 플래그를 끈 가상 터미널 환경에서 실제 대화 루프 중 승인 질문(Run it? [y/N])이 표시되고
사용자의 y/n/Enter/Ctrl+C/Ctrl+D 키 입력이 Approver -> terminal_tool 로 올바르게 전달되는지
프로세스 수준에서 검증한다.
"""

import json
import os
from pathlib import Path
import select
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

import pytest

from hermes_core.constants import (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_CANCELLED,
    APPROVAL_DECISION_DENIED,
    APPROVAL_DECISION_PENDING,
    APPROVAL_HEADER,
    APPROVAL_PROMPT,
    CLI_APPROVAL_DISABLED_NOTICE,
    CLI_APPROVAL_ENABLED_NOTICE,
)
from hermes_core.tools.approval import format_command_for_display

# 유닉스 계열이 아니거나 pty/termios가 없으면 모듈 전체 건너뛰기
pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")

if not hasattr(termios, "IUTF8"):
    pytest.skip("termios.IUTF8 is not available on this platform", allow_module_level=True)


def _make_launcher(extra_args: tuple = (), fallback: bool = False) -> str:
    fallback_line = "sys.modules['prompt_toolkit'] = None\n" if fallback else ""
    return f"""
import sys
import os
import json
import runpy
from hermes_core.llm_client import LLMResponse
import hermes_core.llm_client

{fallback_line}

class ScriptedClient:
    def __init__(self, **kw):
        self.model = "scripted"
        self._on_call = None

    def set_call_recorder(self, r):
        self._on_call = r

    def chat_completion(self, messages, tools=None):
        if self._on_call:
            try:
                self._on_call({{"model": self.model, "prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "raw_response": "{{}}"}})
            except Exception:
                pass
        last = messages[-1] if messages else {{}}
        if last.get("role") == "tool":
            try:
                res = json.loads(last.get("content", "{{}}"))
                succ = res.get("success", False)
            except Exception:
                succ = False
            return LLMResponse(content=f"[Scripted] tool_success={{succ}}", usage={{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
        argv = json.loads(os.environ.get("R10_PTY_ARGV_JSON", "[]"))
        import shlex
        cmd = shlex.join(argv)
        tc = [{{
            "id": "call_pty_1",
            "type": "function",
            "function": {{"name": "terminal", "arguments": json.dumps({{"command": cmd}})}},
        }}]
        return LLMResponse(content="", tool_calls=tc, usage={{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

hermes_core.llm_client.OpenRouterClient = ScriptedClient
sys.argv = ["hermes-core"] + {list(extra_args)!r}
runpy.run_module("hermes_core.cli", run_name="__main__")
"""


class ApprovalPtySession:
    """IUTF8가 꺼진 가상 터미널(pty)에서 대본형 모델 기반 실제 CLI 세션을 실행하고 입출력을 중계하는 테스트 도구."""

    def __init__(
        self,
        argv: list[str],
        extra_args: tuple = (),
        fallback: bool = False,
        hermes_home: Optional[Path] = None,
    ) -> None:
        self.temp_dir: Optional[str] = None
        if hermes_home is None:
            self.temp_dir = tempfile.mkdtemp()
            self.home = Path(self.temp_dir)
        else:
            self.home = hermes_home

        # workspace 디렉터리 사전 준비
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        self.master, self.slave = pty.openpty()
        attrs = termios.tcgetattr(self.slave)
        attrs[0] &= ~termios.IUTF8
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

        launcher = _make_launcher(extra_args=extra_args, fallback=fallback)

        repo_root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HERMES_HOME"] = str(self.home)
        env["OPENROUTER_API_KEY"] = "FAKE-NOT-A-REAL-KEY-0003"
        env["TERM"] = "xterm-256color"
        # 커서 위치 질의(CPR)에 응답할 터미널이 없어 입력마다 1초씩 응답을 기다리므로 질의를 끈다
        env["PROMPT_TOOLKIT_NO_CPR"] = "1"
        env["R10_PTY_ARGV_JSON"] = json.dumps(argv)
        env.pop("PYTHONUTF8", None)

        self.proc = subprocess.Popen(
            [sys.executable, "-c", launcher],
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            close_fds=True,
            start_new_session=True,
            env=env,
            cwd=str(self.home),
        )
        self.accumulated = ""
        self.cursor = 0  # 마지막으로 찾은 표지 바로 뒤. 한 번에 같이 읽힌 다음 표지를 놓치지 않는다

    def read_available(self, timeout: float = 0.05) -> str:
        """현재 마스터 버퍼에 도착해 있는 출력을 가능한 만큼 모두 읽는다."""
        while True:
            r, _, _ = select.select([self.master], [], [], timeout)
            if not r:
                break
            try:
                chunk = os.read(self.master, 1024)
                if not chunk:
                    break
                self.accumulated += chunk.decode("utf-8", "replace")
            except OSError:
                break
        return self.accumulated

    def wait_for(self, marker: str, timeout: float = 15.0, from_last_match: bool = False) -> str:
        """표지 문자열이 출력에 나타날 때까지 기다린다.

        기본은 이 호출 이후에 새로 도착한 출력에서만 찾는다(prompt_toolkit이 같은 프롬프트를 다시 그리는
        경우에 옛 출력을 새 프롬프트로 오인하지 않기 위해). `from_last_match=True`는 직전에 찾은 표지 바로
        뒤부터 찾는다 — 앞선 대기가 한 번에 같이 읽어 버린 다음 표지를 놓치지 않으려는 용도다.
        """
        deadline = time.time() + timeout
        start_pos = self.cursor if from_last_match else len(self.accumulated)
        while time.time() < deadline:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.master, 1024)
                    if not chunk:
                        break
                    self.accumulated += chunk.decode("utf-8", "replace")
                except OSError:
                    break
            pos = self.accumulated.find(marker, start_pos)
            if pos != -1:
                self.cursor = pos + len(marker)
                return self.accumulated
        raise TimeoutError(f"Marker {marker!r} not found within {timeout}s. Output tail: {self.accumulated[-400:]}")

    def write(self, data: bytes) -> None:
        """마스터 터미널에 원시 바이트를 쓴다."""
        os.write(self.master, data)

    def close(self) -> None:
        """자식 프로세스를 정리하고 파일 기술자와 임시 폴더를 닫는다."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1.0)
        self.read_available(0.05)
        try:
            os.close(self.slave)
        except OSError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        if self.temp_dir:
            import shutil

            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def get_approval_rows(self) -> list[dict[str, Any]]:
        """세션 DB의 approval_requests 행을 id 오름차순으로 읽는다."""
        db_path = self.home / "state.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM approval_requests ORDER BY id").fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result


def _default_py_argv() -> list[str]:
    return [sys.executable, "-c", "open('marker.txt', 'w').write('x'); print('ran')"]


def test_scenario_13_approved_pty_flow() -> None:
    """시나리오 13 (승인): 가상 터미널 승인 프롬프트에서 'y' 입력 시 실행 성공 및 원장 갱신."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv)
    try:
        session.wait_for("User >")
        assert session.accumulated.count(CLI_APPROVAL_ENABLED_NOTICE) == 1

        session.write(b"go\r")

        # 질문 대기
        session.wait_for(APPROVAL_PROMPT.strip())
        assert APPROVAL_HEADER in session.accumulated
        assert format_command_for_display(argv) in session.accumulated

        # 질문이 떠 있는 동안 DB 읽기: 1행이 pending 상태여야 함
        rows_during = session.get_approval_rows()
        assert len(rows_during) == 1
        assert rows_during[0]["decision"] == APPROVAL_DECISION_PENDING

        # 'y' 입력
        session.write(b"y\r")

        # 모델 응답 대기
        session.wait_for("[Scripted] tool_success=True")
        session.wait_for("User >")

        # 세션 정상 종료
        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert "Traceback" not in session.accumulated

        # marker.txt 존재 및 원장 approved 갱신 확인
        assert (session.workspace / "marker.txt").exists()
        rows_after = session.get_approval_rows()
        assert len(rows_after) == 1
        assert rows_after[0]["decision"] == APPROVAL_DECISION_APPROVED
    finally:
        session.close()


def test_scenario_14_denied_pty_flow() -> None:
    """시나리오 14 (거부): 가상 터미널 승인 프롬프트에서 'n' 입력 시 실행되지 않고 denied 기록."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv)
    try:
        session.wait_for("User >")
        session.write(b"go\r")

        # 실제 승인 헤더 후 승인 프롬프트 대기
        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip())
        session.write(b"n\r")

        session.wait_for("[Scripted] tool_success=False")
        session.wait_for("User >")

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert not (session.workspace / "marker.txt").exists()

        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_DENIED
    finally:
        session.close()


def test_scenario_15_empty_input_denied() -> None:
    """시나리오 15 (빈 입력): 질문에 Enter만 전송 시 fail-closed로 denied 기록."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv)
    try:
        session.wait_for("User >")
        session.write(b"go\r")

        # 실제 승인 헤더 후 승인 프롬프트 대기
        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip())
        session.write(b"\r")

        session.wait_for("[Scripted] tool_success=False")
        session.wait_for("User >")

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert not (session.workspace / "marker.txt").exists()

        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_DENIED
    finally:
        session.close()


def test_scenario_16_ctrl_c_cancels_and_keeps_repl() -> None:
    """시나리오 16 (Ctrl+C): 질문 중 Ctrl+C 입력 시 cancelled 기록 후 프로세스가 죽지 않고 다음 User > 복구."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv)
    try:
        session.wait_for("User >")
        session.write(b"go\r")

        # 실제 승인 헤더 후 승인 프롬프트 대기
        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip())
        session.write(b"\x03")  # Ctrl+C

        session.wait_for("[Scripted] tool_success=False")
        session.wait_for("User >")

        # Exiting session 메시지는 나오지 않아야 함 (REPL 계속)
        assert "Exiting session..." not in session.accumulated

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert "Traceback" not in session.accumulated
        assert not (session.workspace / "marker.txt").exists()

        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_CANCELLED
    finally:
        session.close()


def test_scenario_17_ctrl_d_cancels_and_keeps_repl() -> None:
    """시나리오 17 (Ctrl+D): 질문 중 Ctrl+D(EOF) 입력 시 cancelled 기록 후 REPL 정상 유지."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv)
    try:
        session.wait_for("User >")
        session.write(b"go\r")

        # 실제 승인 헤더 후 승인 프롬프트 대기
        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip())
        session.write(b"\x04")  # Ctrl+D

        session.wait_for("[Scripted] tool_success=False")
        session.wait_for("User >")

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert not (session.workspace / "marker.txt").exists()

        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_CANCELLED
    finally:
        session.close()


def test_scenario_18_fallback_input_path() -> None:
    """시나리오 18 (폴백 경로): prompt_toolkit 미설치 환경(input() 경로)에서도 질문이 정상 표시되고 승인 동작."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv, fallback=True)
    try:
        session.wait_for("User >")
        session.write(b"go\r")

        # 실제 승인 헤더 후 승인 프롬프트 대기
        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip(), from_last_match=True)
        session.write(b"y\r")

        session.wait_for("[Scripted] tool_success=True")
        session.wait_for("User >", from_last_match=True)

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert (session.workspace / "marker.txt").exists()

        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_APPROVED
    finally:
        session.close()


def test_scenario_19_no_approval_flag() -> None:
    """시나리오 19 (--no-approval): 플래그 지정 시 승인 질문 없이 즉시 거부 및 배너 비활성화 표시."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv, extra_args=("--no-approval",))
    try:
        session.wait_for("User >")
        assert session.accumulated.count(CLI_APPROVAL_DISABLED_NOTICE) == 1
        assert CLI_APPROVAL_ENABLED_NOTICE not in session.accumulated

        session.write(b"go\r")

        session.wait_for("[Scripted] tool_success=False")
        assert APPROVAL_PROMPT not in session.accumulated

        session.wait_for("User >")
        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert not (session.workspace / "marker.txt").exists()
        assert len(session.get_approval_rows()) == 0
    finally:
        session.close()


def test_scenario_20_non_terminal_pipeline() -> None:
    """시나리오 20 (비터미널): stdin/stdout 파이프 환경에서는 승인 안내/질문 없이 즉시 거부 및 15초 내 클린 종료."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        ws = home / "workspace"
        ws.mkdir(parents=True, exist_ok=True)

        argv = _default_py_argv()
        launcher = _make_launcher()

        repo_root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HERMES_HOME"] = str(home)
        env["OPENROUTER_API_KEY"] = "FAKE-NOT-A-REAL-KEY-0003"
        env["R10_PTY_ARGV_JSON"] = json.dumps(argv)

        proc = subprocess.Popen(
            [sys.executable, "-c", launcher],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(home),
        )

        stdout, stderr = proc.communicate(input="go\nexit\n", timeout=15.0)
        assert proc.returncode == 0

        # 두 승인 안내 문구 모두 없어야 함
        assert CLI_APPROVAL_ENABLED_NOTICE not in stdout
        assert CLI_APPROVAL_DISABLED_NOTICE not in stdout
        assert APPROVAL_PROMPT not in stdout

        # marker 미생성 및 행 0개
        assert not (ws / "marker.txt").exists()

        db_path = home / "state.db"
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT * FROM approval_requests").fetchall()
            conn.close()
            assert len(rows) == 0


def test_scenario_21_display_spoofing_defense_in_pty() -> None:
    """시나리오 21 (위장 방어): 제어문자 및 개행이 포함된 명령 표시 시 실제 ESC 바이트가 없고 이스케이프되어 출력."""
    raw_code = "print(1)\n\x1b[5;35mFAKE-PROMPT\x1b[0m\nRun it? [y/N] y"
    argv = [sys.executable, "-c", raw_code]
    session = ApprovalPtySession(argv)
    try:
        session.wait_for("User >")
        session.write(b"go\r")

        # 실제 승인 헤더 후 승인 프롬프트 대기
        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip())

        # 표시 검증: 이스케이프된 \x1b[5;35m 가 보이고 실제 ESC 바이트는 없어야 함
        assert "\\x1b[5;35m" in session.accumulated
        assert "\x1b[5;35mFAKE-PROMPT" not in session.accumulated

        session.write(b"n\r")
        session.wait_for("[Scripted] tool_success=False")
        session.wait_for("User >")

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_DENIED
    finally:
        session.close()


def _run_typeahead_answer_is_discarded(fallback: bool) -> None:
    """질문이 뜨기 전에 미리 보낸 'y'가 승인으로 쓰이지 않고, 질문은 사람의 새 입력을 기다리는지 검증한다."""
    argv = _default_py_argv()
    session = ApprovalPtySession(argv, fallback=fallback)
    try:
        session.wait_for("User >")

        # 대화 입력과 승인 답을 한 번에 미리 보낸다 (질문을 보기 전에 친 입력)
        session.write(b"go\ry\r")

        session.wait_for(APPROVAL_HEADER)
        session.wait_for(APPROVAL_PROMPT.strip(), from_last_match=True)

        # 미리 보낸 'y'가 쓰였다면 이 사이에 실행이 끝난다. 잠시 기다려도 질문이 그대로 떠 있어야 한다
        session.read_available(0.5)
        rows_during = session.get_approval_rows()
        assert len(rows_during) == 1
        assert rows_during[0]["decision"] == APPROVAL_DECISION_PENDING
        assert not (session.workspace / "marker.txt").exists()

        # 질문을 본 뒤의 빈 입력은 fail-closed 거절
        session.write(b"\r")
        session.wait_for("[Scripted] tool_success=False")
        session.wait_for("User >", from_last_match=True)

        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")
        exit_code = session.proc.wait(timeout=5.0)

        assert exit_code == 0
        assert not (session.workspace / "marker.txt").exists()
        rows = session.get_approval_rows()
        assert len(rows) == 1
        assert rows[0]["decision"] == APPROVAL_DECISION_DENIED
    finally:
        session.close()


def test_scenario_22_typeahead_answer_discarded() -> None:
    """시나리오 22 (계획 014 #4): prompt_toolkit 경로에서 질문 전에 미리 친 'y'는 버려지고 자동 승인되지 않음."""
    _run_typeahead_answer_is_discarded(fallback=False)


def test_scenario_23_typeahead_answer_discarded_fallback() -> None:
    """시나리오 23 (계획 014 #5): 폴백(input()) 경로에서도 질문 전에 미리 친 'y'는 버려지고 자동 승인되지 않음."""
    _run_typeahead_answer_is_discarded(fallback=True)
