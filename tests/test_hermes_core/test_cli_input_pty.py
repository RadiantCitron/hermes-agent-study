"""가상 터미널(pty) 통합 검증 테스트 (로드맵 09 / 구현지시서 040).

IUTF8 플래그를 끈 가상 터미널 환경에서 백스페이스 시 UnicodeDecodeError로 죽는 결함의 회귀를 방지하고,
prompt_toolkit 터미널 경로와 input() 폴백 경로의 다중 행·단축키 동작을 검증한다.
"""

import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Optional

import pytest

from hermes_core.constants import (
    CLI_INPUT_DECODE_NOTICE,
    CLI_MULTILINE_HINT,
    CLI_PROMPT_TOOLKIT_MISSING_NOTICE,
)

# 유닉스 계열이 아니거나 pty/termios가 없으면 모듈 전체 건너뛰기
pty = pytest.importorskip("pty")
termios = pytest.importorskip("termios")

if not hasattr(termios, "IUTF8"):
    pytest.skip("termios.IUTF8 is not available on this platform", allow_module_level=True)


THINKING_MARKER = "[[pty-thinking]]"


class PtySession:
    """IUTF8가 꺼진 가상 터미널(pty)에서 CLI 모의 세션을 실행하고 입출력을 중계하는 테스트 도구."""

    def __init__(
        self, fallback: bool = False, hermes_home: Optional[Path] = None, think_delay: float = 0.0
    ) -> None:
        """think_delay > 0이면 모의 모델 호출마다 THINKING_MARKER를 찍고 그만큼 잠들어 「처리 중」 구간을 만든다.
        이때 pty를 프로세스의 제어 터미널로 잡아, 실제 터미널처럼 처리 중 Ctrl+C가 SIGINT가 되게 한다.
        """
        self.temp_dir: Optional[str] = None
        if hermes_home is None:
            self.temp_dir = tempfile.mkdtemp()
            self.home = Path(self.temp_dir)
        else:
            self.home = hermes_home

        self.master, self.slave = pty.openpty()
        attrs = termios.tcgetattr(self.slave)
        attrs[0] &= ~termios.IUTF8
        termios.tcsetattr(self.slave, termios.TCSANOW, attrs)

        # 시험을 백그라운드(&)로 띄우면 SIGINT가 무시 상태로 상속되므로 대화형 터미널처럼 기본 처리기로 되돌린다
        slow_model = (
            "import fcntl, signal, termios, time; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
            + "signal.signal(signal.SIGINT, signal.default_int_handler); "
            + "import hermes_core.agent as _agent; _orig = _agent.MockLLMClient.chat_completion; "
            + "_agent.MockLLMClient.chat_completion = lambda self, *a, **kw: ("
            + f"print({THINKING_MARKER!r}, flush=True), time.sleep({think_delay!r}), _orig(self, *a, **kw))[-1]; "
        )
        launcher = (
            "import sys; "
            + ("sys.modules['prompt_toolkit'] = None; " if fallback else "")
            + (slow_model if think_delay > 0 else "")
            + "import runpy; "
            + "sys.argv = ['hermes-core', '--mock']; "
            + "runpy.run_module('hermes_core.cli', run_name='__main__')"
        )

        repo_root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HERMES_HOME"] = str(self.home)
        env["TERM"] = "xterm-256color"
        # 커서 위치 질의(CPR)에 응답할 터미널이 없어 입력마다 1초씩 응답을 기다리므로 질의를 끈다
        env["PROMPT_TOOLKIT_NO_CPR"] = "1"
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
        raise TimeoutError(f"Marker {marker!r} not found. Output tail: {self.accumulated[-300:]}")

    def wait_for_prompt(self, timeout: float = 15.0) -> str:
        """입력을 보낸 뒤 다음 prompt_toolkit 프롬프트가 입력받을 준비가 될 때까지 기다린다.

        `User >`만 기다리면 같은 프롬프트를 다시 그린 출력(`User > ok`)에 걸려, CLI가 아직 처리 중이라
        raw 모드가 풀린 때 다음 입력을 보내게 된다. 그러면 tty가 `\\r`을 `\\n`(Ctrl+J 줄바꿈)으로 바꿔 입력이
        다음 메시지에 붙고 세션이 끝나지 않는다. prompt_toolkit은 raw 모드에 들어간 뒤 첫 그리기에서만
        `\\x1b[?2004h`를 출력하므로, 그 뒤의 `User >`를 새 프롬프트로 본다.
        """
        self.wait_for("\x1b[?2004h", timeout=timeout)
        return self.wait_for("User >", timeout=timeout, from_last_match=True)

    def write(self, data: bytes) -> None:
        """마스터 터미널에 원시 바이트를 쓴다."""
        os.write(self.master, data)

    def wait_exit(self, timeout: float = 5.0) -> int:
        """출력을 계속 읽으면서 프로세스 종료를 기다리고 종료 코드를 돌려준다.

        아무도 마스터를 읽지 않으면 자식은 출력 쓰기에서 막힌다. pty를 제어 터미널로 잡은 프로세스는
        종료할 때도 남은 출력이 비워지기를 기다리므로, `proc.wait()`만으로는 끝나지 않을 수 있다.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.read_available(0.05)
            returncode = self.proc.poll()
            if returncode is not None:
                return returncode
        raise subprocess.TimeoutExpired(self.proc.args, timeout)

    def close(self) -> None:
        """자식 프로세스를 정리하고 파일 기술자와 임시 폴더를 닫는다."""
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.wait_exit(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.wait_exit(timeout=1.0)
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

    def get_user_messages(self) -> list[str]:
        """세션 DB에 영구 기록된 user 역할 메시지를 순서대로 읽는다."""
        db_path = self.home / "state.db"
        if not db_path.exists():
            return []
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        rows = cur.execute("SELECT content FROM messages WHERE role='user' ORDER BY id").fetchall()
        conn.close()
        return [r[0] for r in rows]


def test_scenario_01_baseline_input_crashes_without_iutf8() -> None:
    """시나리오 1 (대조군): 같은 pty(IUTF8 끔)에서 input() 한 줄 실행 시 한글+백스페이스로 비정상 종료."""
    temp_dir = tempfile.mkdtemp()
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[0] &= ~termios.IUTF8
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        env = os.environ.copy()
        env.pop("PYTHONUTF8", None)

        proc = subprocess.Popen(
            [sys.executable, "-c", "input('User > ')"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            start_new_session=True,
            env=env,
            cwd=temp_dir,
        )

        # User > 프롬프트 대기
        buf = b""
        deadline = time.time() + 10.0
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                chunk = os.read(master, 1024)
                if not chunk:
                    break
                buf += chunk
                if b"User >" in buf:
                    break

        assert b"User >" in buf, f"Prompt not reached. Buffer: {buf!r}"

        # 한글 + 백스페이스 + Enter
        os.write(master, "한".encode("utf-8") + b"\x7f\r")
        proc.wait(timeout=5)

        # 결함이 재현되어야 하므로 종료 코드가 0이 아니어야 함 (비정상 종료)
        assert proc.returncode != 0, f"Expected non-zero exit code (reproduction), got {proc.returncode}"
    finally:
        try:
            os.close(slave)
        except OSError:
            pass
        try:
            os.close(master)
        except OSError:
            pass
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)


def test_scenario_02_korean_backspace_clean_exit() -> None:
    """시나리오 2: 한글 입력 후 백스페이스 교정 후 정상 완료 및 DB 기록 정합성."""
    sess = PtySession(fallback=False)
    try:
        sess.wait_for("User >")
        sess.write("한".encode("utf-8") + b"\x7fok\r")
        sess.wait_for_prompt()
        sess.write("한글ab".encode("utf-8") + b"\x7f\r")
        sess.wait_for_prompt()
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert "Session saved and closed. Goodbye!" in sess.accumulated
        assert "Traceback" not in sess.accumulated
        assert "could not be decoded" not in sess.accumulated
        assert sess.get_user_messages() == ["ok", "한글a"]
    finally:
        sess.close()


def test_scenario_03_ctrl_j_inserts_newline() -> None:
    """시나리오 3: Ctrl+J는 전송이 아닌 줄바꿈으로 동작하여 다중 행 단일 메시지 기록."""
    sess = PtySession(fallback=False)
    try:
        sess.wait_for("User >")
        sess.write(b"a\nb\r")
        sess.wait_for_prompt()
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert sess.get_user_messages() == ["a\nb"]
    finally:
        sess.close()


def test_scenario_04_bracketed_paste_multiline() -> None:
    """시나리오 4: 브래킷 붙여넣기(ESC[200~ ... ESC[201~) 여러 줄이 한 입력으로 전송."""
    sess = PtySession(fallback=False)
    try:
        sess.wait_for("User >")
        sess.write(b"\x1b[200~l1\nl2\r\nl3\x1b[201~\r")
        sess.wait_for_prompt()
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert sess.get_user_messages() == ["l1\nl2\nl3"]
    finally:
        sess.close()


def test_scenario_05_ctrl_c_exits_cleanly() -> None:
    """시나리오 5: 입력 도중 Ctrl+C 입력 시 정상 종료 안내 후 프로세스 종료."""
    sess = PtySession(fallback=False)
    try:
        sess.wait_for("User >")
        sess.write(b"x\x03")
        sess.proc.wait(timeout=5)

        assert sess.proc.returncode == 0
        sess.read_available(0.05)
        assert "Exiting session..." in sess.accumulated
    finally:
        sess.close()


def test_scenario_06_ctrl_d_on_empty_prompt() -> None:
    """시나리오 6: 빈 프롬프트에서 Ctrl+D 입력 시 정상 종료 안내 후 프로세스 종료."""
    sess = PtySession(fallback=False)
    try:
        sess.wait_for("User >")
        sess.write(b"\x04")
        sess.proc.wait(timeout=5)

        assert sess.proc.returncode == 0
        sess.read_available(0.05)
        assert "Exiting session..." in sess.accumulated
    finally:
        sess.close()


def test_scenario_07_startup_banner_hint() -> None:
    """시나리오 7: 시작 직후 배너에 다중 행 안내가 종료 안내 바로 다음 줄에 1회 출력."""
    sess = PtySession(fallback=False)
    try:
        sess.wait_for("User >")
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert sess.accumulated.count(CLI_MULTILINE_HINT) == 1

        lines = [line.strip() for line in sess.accumulated.splitlines() if line.strip()]
        hint_idx = -1
        for i, l in enumerate(lines):
            if CLI_MULTILINE_HINT in l:
                hint_idx = i
                break
        assert hint_idx > 0
        assert "Type 'exit' or 'quit' to terminate." in lines[hint_idx - 1]
    finally:
        sess.close()


def test_scenario_08_fallback_banner_and_notice() -> None:
    """시나리오 8 (폴백): prompt_toolkit 미설치 모사 시 힌트가 없고 미설치 알림이 1회 출력."""
    sess = PtySession(fallback=True)
    try:
        sess.wait_for("User >")
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert CLI_MULTILINE_HINT not in sess.accumulated
        assert sess.accumulated.count(CLI_PROMPT_TOOLKIT_MISSING_NOTICE) == 1
    finally:
        sess.close()


def test_scenario_09_fallback_korean_backspace_recovery() -> None:
    """시나리오 9 (폴백, 결함 재현 조건): 깨진 입력 안내 후 프로세스 생존 및 재입력 성공."""
    sess = PtySession(fallback=True)
    try:
        sess.wait_for("User >")
        sess.write("한".encode("utf-8") + b"\x7f\r")
        sess.wait_for(CLI_INPUT_DECODE_NOTICE)
        sess.write(b"ok\r")
        sess.wait_for("User >")
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert sess.accumulated.count(CLI_INPUT_DECODE_NOTICE) == 1
        assert sess.get_user_messages() == ["ok"]
    finally:
        sess.close()


def test_scenario_10_fallback_multiline_sent_line_by_line() -> None:
    """시나리오 10 (폴백, 관찰 고정): basic input 환경에서는 여러 줄이 줄마다 분리되어 전송."""
    sess = PtySession(fallback=True)
    try:
        sess.wait_for("User >")
        sess.write(b"l1\nl2\nl3\r")
        sess.wait_for("User >")
        sess.write(b"exit\r")
        sess.proc.wait(timeout=5)
        sess.read_available(0.1)

        assert sess.proc.returncode == 0
        assert sess.get_user_messages() == ["l1", "l2", "l3"]
    finally:
        sess.close()


def test_scenario_11_non_terminal_pipe_clean_exit() -> None:
    """시나리오 11 (비터미널): 파이프 환경에서는 힌트/미설치알림/제어열 없이 깔끔히 종료."""
    temp_dir = tempfile.mkdtemp()
    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        env["HERMES_HOME"] = temp_dir
        env["TERM"] = "xterm-256color"
        env.pop("PYTHONUTF8", None)

        cmd = [
            sys.executable,
            "-c",
            "import sys, runpy; sys.argv = ['hermes-core', '--mock']; runpy.run_module('hermes_core.cli', run_name='__main__')",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=temp_dir,
            text=True,
        )
        stdout, stderr = proc.communicate(input="", timeout=5)

        assert proc.returncode == 0
        assert "Mock session finished cleanly." in stdout
        assert CLI_MULTILINE_HINT not in stdout
        assert CLI_PROMPT_TOOLKIT_MISSING_NOTICE not in stdout
        assert "\x1b[?2004h" not in stdout
    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)


# =============================================================================
# 계획 014 S2: 처리 중 미리 입력 보존 (hold_typeahead)
# =============================================================================

THINK_DELAY = 0.8


def test_scenario_12_typeahead_exit_during_processing() -> None:
    """계획 014 #8: 처리 중에 미리 친 'exit⏎'가 다음 프롬프트에서 전송되어 세션이 정상 종료된다."""
    sess = PtySession(think_delay=THINK_DELAY)
    try:
        sess.wait_for("User >")
        sess.write(b"hello\r")
        sess.wait_for(THINKING_MARKER)
        sess.write(b"exit\r")

        sess.wait_for("Session saved and closed. Goodbye!")

        assert sess.wait_exit() == 0
        assert sess.get_user_messages() == ["hello"]
    finally:
        sess.close()


def test_scenario_13_typeahead_korean_backspace_during_processing() -> None:
    """계획 014 #9: 처리 중에 친 '한글ab⌫⏎'가 다음 메시지 '한글a'로 기록되고, 처리 중 화면에 에코되지 않는다."""
    sess = PtySession(think_delay=THINK_DELAY)
    try:
        sess.wait_for("User >")
        sess.write(b"hello\r")
        sess.wait_for(THINKING_MARKER)
        typed_at = len(sess.accumulated)
        sess.write("한글ab".encode("utf-8") + b"\x7f\r")

        sess.wait_for_prompt()  # 미리 친 입력을 받아 바로 전송하는 프롬프트
        next_prompt_at = sess.accumulated.find("\x1b[?2004h", typed_at)
        sess.wait_for_prompt()  # 그 메시지 처리 뒤의 프롬프트
        sess.write(b"exit\r")

        assert sess.wait_exit() == 0
        assert "한글" not in sess.accumulated[typed_at:next_prompt_at]
        assert sess.get_user_messages() == ["hello", "한글a"]
    finally:
        sess.close()


def test_scenario_14_ctrl_c_during_processing_restores_terminal() -> None:
    """계획 014 #10: 처리 중 Ctrl+C로 프로세스가 중단되고, 종료 뒤 터미널의 줄 입력 모드 플래그가 복원되어 있다."""
    sess = PtySession(think_delay=THINK_DELAY)
    try:
        sess.wait_for("User >")
        sess.write(b"hello\r")
        sess.wait_for(THINKING_MARKER)
        sess.write(b"\x03")
        sess.wait_exit()

        # 제어 터미널을 잡은 세션 리더가 끝나면 macOS가 slave를 회수해 slave fd로는 읽을 수 없다.
        # master 쪽 속성은 회수 뒤에도 마지막 설정을 보여 준다(복원하지 않고 죽으면 꺼진 채로 보인다)
        attrs = termios.tcgetattr(sess.master)
        assert attrs[3] & termios.ICANON
        assert attrs[3] & termios.ECHO
        assert attrs[0] & termios.ICRNL
    finally:
        sess.close()
