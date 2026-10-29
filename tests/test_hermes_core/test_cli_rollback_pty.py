"""로드맵 11: 구현지시서 050 가상 터미널(pty) /rollback 인수 테스트.

IUTF8 플래그를 끈 가상 터미널(pty) 환경에서 실제 CLI의 /rollback 대화식 상호작용
(질문, y/n, Enter, Ctrl+C, Ctrl+D, 폴백 모드, --no-checkpoints, 비터미널 파이프, 연쇄 되돌리기, 충돌)을 프로세스 수준에서 검증한다.
시나리오 12 ~ 21.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from hermes_core.constants import (
    CLI_CHECKPOINT_DISABLED_NOTICE,
    CLI_CHECKPOINT_ENABLED_NOTICE,
    CLI_ROLLBACK_CANCELLED_NOTICE,
    CLI_ROLLBACK_DISABLED_NOTICE,
    CLI_ROLLBACK_MODEL_UNAWARE_NOTICE,
    CLI_ROLLBACK_NOTHING_NOTICE,
    CLI_ROLLBACK_PROMPT,
)

pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")

if not hasattr(termios, "IUTF8"):
    pytest.skip("termios.IUTF8 is not available on this platform", allow_module_level=True)


DEFAULT_SCRIPT = {
    "go1": [
        {"name": "write_file", "arguments": {"path": "new.txt", "content": "hello"}},
        {"name": "patch", "arguments": {"path": "old.txt", "old_string": "before", "new_string": "after"}},
    ],
    "go2": [
        {"name": "patch", "arguments": {"path": "old.txt", "old_string": "after", "new_string": "later"}},
    ],
}


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
        self._counter = 0

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
            return LLMResponse(content="[Scripted] done", usage={{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

        script_data = json.loads(os.environ.get("R11_PTY_SCRIPT_JSON", "{{}}"))
        user_content = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_content = m.get("content", "").strip()
                break

        calls_spec = script_data.get(user_content)
        if not calls_spec:
            return LLMResponse(content="[Scripted] idle", usage={{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

        self._counter += 1
        tc = []
        for idx, c in enumerate(calls_spec):
            tc.append({{
                "id": f"call_pty_{{self._counter}}_{{idx}}",
                "type": "function",
                "function": {{
                    "name": c["name"],
                    "arguments": json.dumps(c.get("arguments", {{}})),
                }},
            }})
        return LLMResponse(content="", tool_calls=tc, usage={{"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})

hermes_core.llm_client.OpenRouterClient = ScriptedClient
sys.argv = ["hermes-core"] + {list(extra_args)!r}
runpy.run_module("hermes_core.cli", run_name="__main__")
"""


class RollbackPtySession:
    """IUTF8가 꺼진 가상 터미널(pty)에서 /rollback 상호작용을 실행하고 검증하는 도구."""

    def __init__(
        self,
        script: Optional[dict[str, Any]] = None,
        extra_args: tuple = (),
        fallback: bool = False,
    ) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.home = Path(self.temp_dir)
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # 초기 old.txt 준비
        (self.workspace / "old.txt").write_text("before", encoding="utf-8")

        self.master, self.slave = pty.openpty()
        attrs = termios.tcgetattr(self.slave)
        attrs[0] &= ~termios.IUTF8
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

        launcher = _make_launcher(extra_args=extra_args, fallback=fallback)
        repo_root = Path(__file__).resolve().parent.parent.parent

        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HERMES_HOME"] = str(self.home)
        env["OPENROUTER_API_KEY"] = "FAKE-NOT-A-REAL-KEY-0004"
        env["TERM"] = "xterm-256color"
        # 커서 위치 질의(CPR)에 응답할 터미널이 없어 입력마다 1초씩 응답을 기다리므로 질의를 끈다
        env["PROMPT_TOOLKIT_NO_CPR"] = "1"
        env["R11_PTY_SCRIPT_JSON"] = json.dumps(script or DEFAULT_SCRIPT)
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
        self.cursor = 0

    def read_available(self, timeout: float = 0.05) -> str:
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

    def wait_for(self, marker: str, timeout: float = 15.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pos = self.accumulated.find(marker, self.cursor)
            if pos != -1:
                self.cursor = pos + len(marker)
                return self.accumulated
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(self.master, 1024)
                    if not chunk:
                        break
                    self.accumulated += chunk.decode("utf-8", "replace")
                    pos = self.accumulated.find(marker, self.cursor)
                    if pos != -1:
                        self.cursor = pos + len(marker)
                        return self.accumulated
                except OSError:
                    break
        raise TimeoutError(f"Marker {marker!r} not found within {timeout}s. Output tail: {self.accumulated[-400:]}")

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def close(self) -> None:
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
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def get_checkpoint_rows(self) -> list[dict[str, Any]]:
        db_path = self.home / "state.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            cur.execute("SELECT * FROM file_checkpoints ORDER BY id ASC")
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def test_scenario_12_pty_rollback_approved():
    """시나리오 12: 정상 되돌림 (/rollback -> 질문 대기 중 DB applied -> y -> 복원 및 모델 미인지 안내)."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for(CLI_CHECKPOINT_ENABLED_NOTICE)
        pty_session.wait_for("User >")

        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())

        # 질문 직전 계획 출력 확인
        out = pty_session.read_available()
        assert "2 to restore, 0 to skip" in out
        assert "[restore] old.txt" in out
        assert "[delete]  new.txt" in out

        # 질문이 떠 있는 동안 DB 읽기: 행 2개 applied
        rows_during = pty_session.get_checkpoint_rows()
        assert len(rows_during) == 2
        assert all(r["status"] == "applied" for r in rows_during)

        # y 입력
        pty_session.write(b"y\r")
        pty_session.wait_for("Rolled back 2 file(s), skipped 0.")
        pty_session.wait_for(CLI_ROLLBACK_MODEL_UNAWARE_NOTICE)
        pty_session.wait_for("User >")

        # 파일 상태 검증
        assert not (pty_session.workspace / "new.txt").exists()
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "before"

        # DB 행 rolled_back 검증
        rows_after = pty_session.get_checkpoint_rows()
        assert all(r["status"] == "rolled_back" for r in rows_after)

        pty_session.write(b"exit\r")
        pty_session.wait_for("Session saved and closed. Goodbye!")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
        assert "Traceback" not in pty_session.accumulated
        assert pty_session.accumulated.count(CLI_CHECKPOINT_ENABLED_NOTICE) == 1
    finally:
        pty_session.close()


def test_scenario_13_pty_rollback_cancelled_with_n():
    """시나리오 13: 거절 (/rollback -> 질문에 n -> 취소 안내 및 파일/행 불변)."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())

        pty_session.write(b"n\r")
        pty_session.wait_for(CLI_ROLLBACK_CANCELLED_NOTICE)
        pty_session.wait_for("User >")

        assert (pty_session.workspace / "new.txt").read_text(encoding="utf-8") == "hello"
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "after"

        rows = pty_session.get_checkpoint_rows()
        assert len(rows) == 2
        assert all(r["status"] == "applied" for r in rows)

        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
    finally:
        pty_session.close()


def test_scenario_14_pty_rollback_cancelled_with_empty_enter():
    """시나리오 14: 빈 입력 (질문에 Enter만 입력 -> fail-closed 취소)."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())

        pty_session.write(b"\r")
        pty_session.wait_for(CLI_ROLLBACK_CANCELLED_NOTICE)
        pty_session.wait_for("User >")

        assert (pty_session.workspace / "new.txt").exists()
        rows = pty_session.get_checkpoint_rows()
        assert all(r["status"] == "applied" for r in rows)

        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
    finally:
        pty_session.close()


def test_scenario_15_pty_rollback_ctrl_c():
    """시나리오 15: 질문 대기 중 Ctrl+C -> 취소 처리 후 REPL 계속 유지 (종료되지 않음)."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())

        # Ctrl+C 전송 (\x03)
        pty_session.write(b"\x03")
        pty_session.wait_for(CLI_ROLLBACK_CANCELLED_NOTICE)
        pty_session.wait_for("User >")

        assert "Exiting session..." not in pty_session.accumulated
        assert (pty_session.workspace / "new.txt").exists()

        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
        assert "Traceback" not in pty_session.accumulated
    finally:
        pty_session.close()


def test_scenario_16_pty_rollback_ctrl_d():
    """시나리오 16: 질문 대기 중 Ctrl+D -> 취소 처리 후 REPL 계속 유지."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())

        # Ctrl+D 전송 (\x04)
        pty_session.write(b"\x04")
        pty_session.wait_for(CLI_ROLLBACK_CANCELLED_NOTICE)
        pty_session.wait_for("User >")

        assert (pty_session.workspace / "new.txt").exists()

        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
        assert "Traceback" not in pty_session.accumulated
    finally:
        pty_session.close()


def test_scenario_17_pty_rollback_fallback_input():
    """시나리오 17: fallback=True (prompt_toolkit 미설치 모사, input() 폴백 경로)."""
    pty_session = RollbackPtySession(fallback=True)
    try:
        pty_session.wait_for(CLI_CHECKPOINT_ENABLED_NOTICE)
        pty_session.wait_for("User >")

        pty_session.write(b"go1\n")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"/rollback\n")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())

        pty_session.write(b"y\n")
        pty_session.wait_for("Rolled back 2 file(s), skipped 0.")
        pty_session.wait_for("User >")

        assert not (pty_session.workspace / "new.txt").exists()
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "before"

        rows = pty_session.get_checkpoint_rows()
        assert all(r["status"] == "rolled_back" for r in rows)

        pty_session.write(b"exit\n")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
    finally:
        pty_session.close()


def test_scenario_18_pty_no_checkpoints_flag():
    """시나리오 18: --no-checkpoints 실행 시 안내 및 원장 0행, /rollback 비활성화 안내."""
    pty_session = RollbackPtySession(extra_args=("--no-checkpoints",))
    try:
        pty_session.wait_for(CLI_CHECKPOINT_DISABLED_NOTICE)
        assert CLI_CHECKPOINT_ENABLED_NOTICE not in pty_session.accumulated

        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        # 파일 도구는 정상 실행됨
        assert (pty_session.workspace / "new.txt").read_text(encoding="utf-8") == "hello"
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "after"

        # 원장 행 0
        rows = pty_session.get_checkpoint_rows()
        assert len(rows) == 0

        # /rollback 시도 -> 비활성화 안내, 질문 없음
        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_DISABLED_NOTICE)
        assert CLI_ROLLBACK_PROMPT not in pty_session.accumulated

        pty_session.wait_for("User >")
        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
    finally:
        pty_session.close()


def test_scenario_19_non_terminal_pipe_input(tmp_path):
    """시나리오 19: 비터미널 stdin=PIPE 파이프 입력에서도 /rollback 정상 동작 검증."""
    home = tmp_path / "home"
    home.mkdir()
    ws = home / "workspace"
    ws.mkdir()
    (ws / "old.txt").write_text("before", encoding="utf-8")

    launcher = _make_launcher()
    repo_root = Path(__file__).resolve().parent.parent.parent

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    env["HERMES_HOME"] = str(home)
    env["OPENROUTER_API_KEY"] = "FAKE-NOT-A-REAL-KEY-0004"
    env["TERM"] = "dumb"
    env["R11_PTY_SCRIPT_JSON"] = json.dumps(DEFAULT_SCRIPT)

    proc = subprocess.Popen(
        [sys.executable, "-c", launcher],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(home),
    )

    stdout, stderr = proc.communicate(input="go1\n/rollback\ny\nexit\n", timeout=15)
    assert proc.returncode == 0
    assert not (ws / "new.txt").exists()
    assert (ws / "old.txt").read_text(encoding="utf-8") == "before"

    # DB 행 검증
    db_path = home / "state.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT status FROM file_checkpoints")
    statuses = [r[0] for r in cur.fetchall()]
    conn.close()
    assert len(statuses) == 2
    assert all(s == "rolled_back" for s in statuses)


def test_scenario_20_pty_consecutive_rollbacks():
    """시나리오 20: 두 턴 연속 실행 후 연쇄 /rollback -> turn 2 복원 -> turn 1 복원 -> nothing."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        pty_session.write(b"go2\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "later"

        # 1회 롤백 (turn 2)
        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())
        out1 = pty_session.read_available()
        assert "turn 2" in out1
        pty_session.write(b"y\r")
        pty_session.wait_for("Rolled back 1 file(s), skipped 0.")
        pty_session.wait_for("User >")
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "after"

        # 2회 롤백 (turn 1)
        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())
        out2 = pty_session.read_available()
        assert "turn 1" in out2
        pty_session.write(b"y\r")
        pty_session.wait_for("Rolled back 2 file(s), skipped 0.")
        pty_session.wait_for("User >")
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "before"
        assert not (pty_session.workspace / "new.txt").exists()

        # 3회 롤백 (남은 것 없음)
        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_NOTHING_NOTICE)
        nothing_pos = pty_session.cursor
        pty_session.wait_for("User >")
        assert CLI_ROLLBACK_PROMPT.strip() not in pty_session.accumulated[nothing_pos:]

        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
    finally:
        pty_session.close()


def test_scenario_21_pty_rollback_conflict():
    """시나리오 21: 충돌 발생 시 [skip] 표시 및 건너뜀 검증."""
    pty_session = RollbackPtySession()
    try:
        pty_session.wait_for("User >")
        pty_session.write(b"go1\r")
        pty_session.wait_for("[Scripted] done")
        pty_session.wait_for("User >")

        # new.txt를 human edit으로 덮어씀
        (pty_session.workspace / "new.txt").write_text("human edit", encoding="utf-8")

        pty_session.write(b"/rollback\r")
        pty_session.wait_for(CLI_ROLLBACK_PROMPT.strip())
        out = pty_session.read_available()
        assert "[skip]    new.txt" in out

        pty_session.write(b"y\r")
        pty_session.wait_for("Rolled back 1 file(s), skipped 1.")
        pty_session.wait_for("User >")

        # new.txt는 human edit 유지, old.txt는 before로 복원
        assert (pty_session.workspace / "new.txt").read_text(encoding="utf-8") == "human edit"
        assert (pty_session.workspace / "old.txt").read_text(encoding="utf-8") == "before"

        pty_session.write(b"exit\r")
        pty_session.proc.wait(timeout=5)
        assert pty_session.proc.returncode == 0
    finally:
        pty_session.close()
