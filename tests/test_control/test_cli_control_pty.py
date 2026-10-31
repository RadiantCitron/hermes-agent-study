"""가상 터미널(pty) CLI /ctl 어댑터 통합 검증 테스트 (로드맵 19 / 구현지시서 050 시나리오 7).

pty.openpty() 환경에서 실제 CLI 프로세스를 subprocess로 실행하고,
/ctl status, /ctl set max_iterations 3, exit 명령을 터미널 I/O를 통해 종단간으로 검증한다.
파일명 규약(_pty.py)에 따라 pytest가 자동으로 pty 마커를 부여한다.
"""

import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
from typing import Optional

import pytest

pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")

if not hasattr(termios, "IUTF8"):
    pytest.skip("termios.IUTF8 is not available on this platform", allow_module_level=True)


class PtyCliSession:
    """IUTF8가 꺼진 가상 터미널(pty)에서 CLI --mock 프로세스를 실행하고 입출력을 중계하는 세션 도구."""

    def __init__(self, hermes_home: Optional[Path] = None) -> None:
        if hermes_home is None:
            self.temp_dir = tempfile.TemporaryDirectory()
            self.home = Path(self.temp_dir.name)
        else:
            self.temp_dir = None
            self.home = hermes_home

        self.master, self.slave = pty.openpty()
        attrs = termios.tcgetattr(self.slave)
        attrs[0] &= ~termios.IUTF8
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

        repo_root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HERMES_HOME"] = str(self.home)
        env["TERM"] = "xterm-256color"
        env["PROMPT_TOOLKIT_NO_CPR"] = "1"
        env.pop("PYTHONUTF8", None)

        self.proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_core.cli", "--mock"],
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

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                self.proc.kill()
        for fd in (self.slave, self.master):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.temp_dir is not None:
            self.temp_dir.cleanup()

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

    def wait_for(self, marker: str, timeout: float = 15.0, from_last_match: bool = False) -> str:
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
        raise TimeoutError(f"Marker {marker!r} not found in output: {self.accumulated[-400:]}")

    def wait_for_prompt(self, timeout: float = 15.0) -> str:
        self.wait_for("\x1b[?2004h", timeout=timeout)
        return self.wait_for("User >", timeout=timeout, from_last_match=True)

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def wait_exit(self, timeout: float = 5.0) -> int:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.read_available(0.05)
            returncode = self.proc.poll()
            if returncode is not None:
                return returncode
        raise TimeoutError(f"Process did not exit in {timeout}s: {self.accumulated[-400:]}")


def test_scenario_7_pty_real_cli_control_flow():
    """시나리오 7: 가상 터미널(PTY) 환경에서 실제 CLI 프로세스 종단간 /ctl 동작 (ROADMAP §6-5 시나리오 7)."""
    session = PtyCliSession()
    try:
        # CLI 프롬프트 대기
        session.wait_for_prompt()

        # 1. /ctl status 전송
        session.write(b"/ctl status\r")
        session.wait_for("[Settings]")
        session.wait_for("[Extensions]", from_last_match=True)

        # 다음 프롬프트 대기
        session.wait_for_prompt()

        # 2. /ctl set max_iterations 3 전송
        session.write(b"/ctl set max_iterations 3\r")
        session.wait_for("Setting 'max_iterations' queued to change to 3")

        # 다음 프롬프트 대기
        session.wait_for_prompt()

        # 3. exit 전송
        session.write(b"exit\r")
        session.wait_for("Session saved and closed. Goodbye!")

        # 4. 프로세스 정상 종료(exit code 0) 확인
        exit_code = session.wait_exit(timeout=5.0)
        assert exit_code == 0
    finally:
        session.close()
