"""CLI 체크포인트·롤백 MVP 연동 테스트 (로드맵 11 / 구현지시서 040 인수 시나리오 1~22)."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from typing import Any, Optional

import pytest

from hermes_core import cli
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.constants import (
    APPROVAL_PROMPT,
    CHECKPOINT_REFUSED_HINT,
    CLI_APPROVAL_ENABLED_NOTICE,
    CLI_CHECKPOINT_DISABLED_NOTICE,
    CLI_CHECKPOINT_ENABLED_NOTICE,
    CLI_CHECKPOINT_REFUSED_NOTICE,
    CLI_ROLLBACK_CANCELLED_NOTICE,
    CLI_ROLLBACK_DISABLED_NOTICE,
    CLI_ROLLBACK_ERROR_NOTICE,
    CLI_ROLLBACK_EXTERNAL_WARNING,
    CLI_ROLLBACK_HEADER,
    CLI_ROLLBACK_LINE_DELETE,
    CLI_ROLLBACK_LINE_RESTORE,
    CLI_ROLLBACK_LINE_SKIP,
    CLI_ROLLBACK_MODEL_UNAWARE_NOTICE,
    CLI_ROLLBACK_NOTHING_NOTICE,
    CLI_ROLLBACK_PENDING_WARNING,
    CLI_ROLLBACK_PROMPT,
    CLI_ROLLBACK_RESULT_DELETED,
    CLI_ROLLBACK_RESULT_RESTORED,
    CLI_ROLLBACK_RESULT_SKIPPED,
    CLI_ROLLBACK_RESULT_SUMMARY,
    CLI_ROLLBACK_USAGE_NOTICE,
    CLI_USER_PROMPT,
    OPENROUTER_API_KEY_ENV,
    get_default_workspace,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.checkpoint import (
    Checkpointer,
    RollbackItem,
    RollbackItemResult,
    RollbackPlan,
    RollbackResult,
)

FAKE_API_KEY = "FAKE-NOT-A-REAL-KEY-0003"


class ScriptableClient:
    """테스트용 대본형 가짜 OpenRouter 클라이언트."""

    created_instances: list["ScriptableClient"] = []
    responses_by_instance: dict[int, list[Any]] = {}

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self.model = model
        self.instance_idx = len(ScriptableClient.created_instances)
        ScriptableClient.created_instances.append(self)
        self.call_history: list[dict[str, Any]] = []
        self.responses = list(ScriptableClient.responses_by_instance.get(self.instance_idx, []))
        self._call_recorder = None

    def set_call_recorder(self, recorder: Any) -> None:
        self._call_recorder = recorder

    def chat_completion(
        self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None
    ) -> Any:
        self.call_history.append({"messages": messages, "tools": tools})
        if self._call_recorder:
            self._call_recorder({
                "model": self.model,
                "status_code": 200,
                "input_tokens": 10,
                "output_tokens": 5,
            })
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return MockLLMResponse(content="[Scriptable] default response")


@pytest.fixture(autouse=True)
def reset_scriptable_client():
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()
    yield
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()


@pytest.fixture
def cli_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI 테스트 환경 픽스처."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, FAKE_API_KEY)
    monkeypatch.setattr(cli, "OpenRouterClient", ScriptableClient)
    monkeypatch.chdir(tmp_path)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


class ScriptableInput:
    """사용자 입력, 되돌리기 및 승인 질문을 가로채는 대본형 가짜 입력 핸들러."""

    def __init__(
        self,
        user_inputs: list[Any],
        rollback_answers: Optional[list[Any]] = None,
        approval_answers: Optional[list[Any]] = None,
        capsys: Optional[pytest.CaptureFixture] = None,
        db_path: Optional[Path] = None,
    ) -> None:
        self.user_inputs = list(user_inputs)
        self.rollback_answers = list(rollback_answers or [])
        self.approval_answers = list(approval_answers or [])
        self.recorded_prompts: list[str] = []
        self.recorded_calls: list[dict[str, Any]] = []
        self.capsys = capsys
        self.db_path = db_path
        self.output_before_rollback_prompt: list[str] = []
        self.checkpoint_rows_at_rollback_prompt: list[list[dict[str, Any]]] = []

    def __call__(self, prompt: str = CLI_USER_PROMPT, **kwargs: Any) -> str:
        self.recorded_prompts.append(prompt)
        self.recorded_calls.append({"prompt": prompt, "kwargs": dict(kwargs)})
        if prompt == CLI_ROLLBACK_PROMPT:
            if self.capsys:
                captured = self.capsys.readouterr()
                self.output_before_rollback_prompt.append(captured.out)
                # 복원하여 capsys에 계속 누적되도록 함
                sys.stdout.write(captured.out)
                sys.stderr.write(captured.err)
            if self.db_path and self.db_path.exists():
                conn = sqlite3.connect(self.db_path)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute("SELECT * FROM file_checkpoints ORDER BY id ASC").fetchall()
                    self.checkpoint_rows_at_rollback_prompt.append([dict(r) for r in rows])
                finally:
                    conn.close()
            if not self.rollback_answers:
                raise AssertionError(f"Unexpected rollback prompt received: {prompt}")
            val = self.rollback_answers.pop(0)
            if isinstance(val, type) and issubclass(val, BaseException):
                raise val()
            if isinstance(val, BaseException):
                raise val
            if callable(val):
                return str(val())
            return str(val)
        elif prompt == APPROVAL_PROMPT:
            if not self.approval_answers:
                raise AssertionError(f"Unexpected approval prompt received: {prompt}")
            val = self.approval_answers.pop(0)
            if isinstance(val, type) and issubclass(val, BaseException):
                raise val()
            if isinstance(val, BaseException):
                raise val
            if callable(val):
                return str(val())
            return str(val)
        else:
            if not self.user_inputs:
                raise AssertionError(f"User inputs exhausted when prompted with: {prompt}")
            val = self.user_inputs.pop(0)
            if isinstance(val, type) and issubclass(val, BaseException):
                raise val()
            if isinstance(val, BaseException):
                raise val
            if callable(val):
                return str(val())
            return str(val)


def make_write_file_response(call_id: str, path: str, content: str) -> MockLLMResponse:
    """write_file 도구를 호출하는 LLM 응답 생성."""
    return MockLLMResponse(
        content=None,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": path, "content": content}),
            },
        }],
    )


def make_patch_response(
    call_id: str, path: str, old_string: str, new_string: str
) -> MockLLMResponse:
    """patch 도구를 호출하는 LLM 응답 생성."""
    return MockLLMResponse(
        content=None,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": "patch",
                "arguments": json.dumps({
                    "path": path,
                    "old_string": old_string,
                    "new_string": new_string,
                }),
            },
        }],
    )


# =============================================================================
# 3-1. 켜짐·꺼짐 판정과 배너 (가정 A)
# =============================================================================


def test_scenario_01_default_banner_order(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 1: 인자 없음, 실제 모드, 터미널 참 -> CLI_CHECKPOINT_ENABLED_NOTICE 정확히 1회, 배너 순서 정합."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_CHECKPOINT_ENABLED_NOTICE) == 1
    assert CLI_CHECKPOINT_DISABLED_NOTICE not in captured.out

    lines = [line.strip() for line in captured.out.splitlines() if line.strip()]
    workdir_idx = -1
    appr_idx = -1
    ckpt_idx = -1
    exit_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("📁 Working directory:"):
            workdir_idx = i
        if CLI_APPROVAL_ENABLED_NOTICE in line:
            appr_idx = i
        if CLI_CHECKPOINT_ENABLED_NOTICE in line:
            ckpt_idx = i
        if "Type 'exit' or 'quit' to terminate." in line:
            exit_idx = i

    assert workdir_idx != -1
    assert appr_idx != -1
    assert ckpt_idx != -1
    assert exit_idx != -1
    assert workdir_idx < ckpt_idx
    assert appr_idx < ckpt_idx
    assert ckpt_idx < exit_idx


def test_scenario_02_no_checkpoints_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 2: --no-checkpoints -> CLI_CHECKPOINT_DISABLED_NOTICE 1회, 켜짐 안내 없음."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-checkpoints"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_CHECKPOINT_DISABLED_NOTICE) == 1
    assert CLI_CHECKPOINT_ENABLED_NOTICE not in captured.out


def test_scenario_03_non_terminal_default_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 3: 터미널 거짓, 인자 없음 -> CLI_CHECKPOINT_ENABLED_NOTICE 1회, 승인 안내 없음."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: False)
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_CHECKPOINT_ENABLED_NOTICE) == 1
    assert CLI_APPROVAL_ENABLED_NOTICE not in captured.out


def test_scenario_04_mock_repl_banner(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 4: --mock, REPL -> 켜짐 안내 1번."""
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_CHECKPOINT_ENABLED_NOTICE) == 1


def test_scenario_05_help_and_flag_combinations(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 5: --help 확인 및 다양한 플래그 조합과 함께 실행 시 정상 작동."""
    # 5a: --help
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 0
    help_out = capsys.readouterr().out
    assert "--no-checkpoints" in help_out

    # 5b: --mock --no-checkpoints
    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: True
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock", "--no-checkpoints"])
    cli.main()
    out_b = capsys.readouterr().out
    assert out_b.count(CLI_CHECKPOINT_DISABLED_NOTICE) == 1

    # 5c: --delegate --no-checkpoints
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate", "--no-checkpoints"])
    cli.main()
    out_c = capsys.readouterr().out
    assert out_c.count(CLI_CHECKPOINT_DISABLED_NOTICE) == 1

    # 5d: --no-approval --no-checkpoints
    fake_in = ScriptableInput(["exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-approval", "--no-checkpoints"])
    cli.main()
    out_d = capsys.readouterr().out
    assert out_d.count(CLI_CHECKPOINT_DISABLED_NOTICE) == 1


# =============================================================================
# 3-2. 조립: 작업 폴더·턴 경계·기록 (030 지시서 1-5, 가정 A·B·I)
# =============================================================================


def test_scenario_06_real_mode_write_and_ledger(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 6: 실제 모드 쓰기 시 작업 폴더 일치, 파일 생성, 원장 행 1개 기록."""
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "new.txt", "hello"),
        MockLLMResponse(content="Done writing."),
    ]
    fake_in = ScriptableInput(["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 작업 폴더 및 파일 확인
    ws = get_default_workspace()
    new_file = ws / "new.txt"
    assert new_file.exists()
    assert new_file.read_text(encoding="utf-8") == "hello"

    # 모델이 받은 도구 결과 확인
    client = ScriptableClient.created_instances[0]
    call_msg = client.call_history[1]["messages"]
    tool_msg = [m for m in call_msg if m.get("role") == "tool"][0]
    tool_content = json.loads(tool_msg["content"])
    assert tool_content.get("success") is True or tool_content.get("status") == "success"

    # 원장 행 확인
    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["status"] == "applied"
    assert row["tool_name"] == "write_file"
    assert row["rel_path"] == "new.txt"
    assert row["before_exists"] == 0
    assert row["turn_index"] == 1
    assert row["after_sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert row["workspace"] == str(ws)

    # 거부 알림 없음
    assert CLI_CHECKPOINT_REFUSED_NOTICE not in captured.out
    db.close()


def test_scenario_07_turn_index_counting_rules(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 7: 빈 입력과 /rollback은 턴을 소비하지 않음 -> 쓰기 행 turn_index == 1."""
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "turn_file.txt", "content"),
        MockLLMResponse(content="Done."),
    ]
    # 빈 입력, 되돌릴 것이 없는 /rollback, 쓰기 턴, exit
    fake_in = ScriptableInput(["", "/rollback", "go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()

    client = ScriptableClient.created_instances[0]
    # 모델 호출은 쓰기 턴의 2번뿐
    assert len(client.call_history) == 2

    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 1
    assert rows[0]["turn_index"] == 1
    db.close()


def test_scenario_08_mock_single_shot_turn_boundary(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 8: 모의 단발 실행 시 begin_turn 1회 호출, 정상 종료, 원장 0행."""
    begin_turn_count = 0
    orig_begin_turn = Checkpointer.begin_turn

    def tracked_begin_turn(self):
        nonlocal begin_turn_count
        begin_turn_count += 1
        return orig_begin_turn(self)

    monkeypatch.setattr(Checkpointer, "begin_turn", tracked_begin_turn)

    fake_stdin = io.StringIO()
    fake_stdin.isatty = lambda: False
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])

    cli.main()
    captured = capsys.readouterr()

    assert "Mock session finished cleanly." in captured.out
    assert begin_turn_count == 1

    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 0
    db.close()


def test_scenario_09_disabled_checkpoints_same_as_before(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 9: --no-checkpoints 실행 시 파일 생성 및 도구 응답이 켜진 구성과 완전히 동일."""
    # 시나리오 6과 같은 대본
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "new.txt", "hello"),
        MockLLMResponse(content="Done writing."),
    ]
    fake_in = ScriptableInput(["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-checkpoints"])

    cli.main()
    captured = capsys.readouterr()

    ws = get_default_workspace()
    new_file = ws / "new.txt"
    assert new_file.exists()
    assert new_file.read_text(encoding="utf-8") == "hello"

    client = ScriptableClient.created_instances[0]
    call_msg = client.call_history[1]["messages"]
    tool_msg = [m for m in call_msg if m.get("role") == "tool"][0]
    # 도구 응답 내용 확인
    assert json.loads(tool_msg["content"]).get("success") is True

    # 원장 행 0
    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 0
    assert CLI_CHECKPOINT_ENABLED_NOTICE not in captured.out
    db.close()


def test_scenario_10_delegated_child_not_injected(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 10: --delegate 시 자식 세션의 쓰기는 원장에 없고 부모 쓰기만 기록됨."""
    # 인스턴스 0: 부모 모델
    # 인스턴스 1: 자식 모델
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            content=None,
            tool_calls=[{
                "id": "c_del",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "child task"}),
                },
            }],
        ),
        make_write_file_response("c_p", "parent.txt", "y"),
        MockLLMResponse(content="Parent done."),
    ]
    ScriptableClient.responses_by_instance[1] = [
        make_write_file_response("c_c", "child.txt", "x"),
        MockLLMResponse(content="Child done."),
    ]

    fake_in = ScriptableInput(["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])

    cli.main()

    ws = get_default_workspace()
    assert (ws / "child.txt").exists()
    assert (ws / "parent.txt").exists()

    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 1
    assert rows[0]["rel_path"] == "parent.txt"
    db.close()


# =============================================================================
# 3-3. /rollback 흐름 (가정 C·D·G, 위험 12·13)
# =============================================================================


def test_scenario_11_rollback_basic_flow(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 11: /rollback 기본 흐름 (순서 역순, 복원, 파일 상태, 원장 전이, 모델 미인지 안내)."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    old_file = ws / "old.txt"
    old_file.write_text("before", encoding="utf-8")

    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "new.txt", "hello"),
        make_patch_response("c2", "old.txt", "before", "after"),
        MockLLMResponse(content="Turn done."),
    ]

    fake_in = ScriptableInput(
        user_inputs=["go", "/rollback", "exit"],
        rollback_answers=["y"],
        capsys=capsys,
        db_path=cli_test_env["hermes_home"] / "state.db",
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 프롬프트 열 확인
    assert fake_in.recorded_prompts == [
        CLI_USER_PROMPT,
        CLI_USER_PROMPT,
        CLI_ROLLBACK_PROMPT,
        CLI_USER_PROMPT,
    ]

    # 질문 전 출력 검증
    prompt_out = fake_in.output_before_rollback_prompt[0]
    assert CLI_ROLLBACK_HEADER.format(turn_index=1, restore_count=2, skip_count=0) in prompt_out
    # old.txt(patch, restore) 줄이 new.txt(write_file, delete) 줄보다 먼저
    old_idx = prompt_out.find(
        CLI_ROLLBACK_LINE_RESTORE.format(rel_path="old.txt", tool_name="patch")
    )
    new_idx = prompt_out.find(
        CLI_ROLLBACK_LINE_DELETE.format(rel_path="new.txt", tool_name="write_file")
    )
    assert old_idx != -1 and new_idx != -1
    assert old_idx < new_idx

    # 파일 복원 상태 검증
    assert not (ws / "new.txt").exists()
    assert (ws / "old.txt").read_text(encoding="utf-8") == "before"

    # 출력 메시지 확인
    assert CLI_ROLLBACK_RESULT_RESTORED.format(rel_path="old.txt") in captured.out
    assert CLI_ROLLBACK_RESULT_DELETED.format(rel_path="new.txt") in captured.out
    assert CLI_ROLLBACK_RESULT_SUMMARY.format(rolled_back=2, skipped=0) in captured.out
    assert CLI_ROLLBACK_MODEL_UNAWARE_NOTICE in captured.out

    # 모델 호출 횟수 (쓰기 턴의 3회뿐, /rollback은 모델 미호출)
    client = ScriptableClient.created_instances[0]
    assert len(client.call_history) == 3

    # 질문 진행 중 원장 상태 검증: 아직 applied 상태 유지
    assert len(fake_in.checkpoint_rows_at_rollback_prompt) == 1
    during_rows = fake_in.checkpoint_rows_at_rollback_prompt[0]
    assert len(during_rows) == 2
    assert all(r["status"] == "applied" for r in during_rows)

    # 원장 상태 확인 (되돌림 완료 후 rolled_back 전이)
    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["status"] == "rolled_back"

    # messages 테이블에 /rollback이 없음
    msg_rows = db._conn.execute("SELECT content FROM messages").fetchall()
    for m in msg_rows:
        if m["content"]:
            assert "/rollback" not in m["content"]
    db.close()


@pytest.mark.parametrize(
    "cancel_answer",
    ["n", "", "maybe", KeyboardInterrupt, EOFError],
)
def test_scenario_12_rollback_cancellation(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, cancel_answer
):
    """시나리오 12: 확인 질문 취소(5변형) 시 아무것도 변경되지 않고 REPL 정상 유지."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    old_file = ws / "old.txt"
    old_file.write_text("before", encoding="utf-8")

    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "new.txt", "hello"),
        make_patch_response("c2", "old.txt", "before", "after"),
        MockLLMResponse(content="Turn done."),
    ]

    fake_in = ScriptableInput(
        user_inputs=["go", "/rollback", "exit"],
        rollback_answers=[cancel_answer],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert CLI_ROLLBACK_CANCELLED_NOTICE in captured.out
    assert (ws / "new.txt").exists()
    assert (ws / "old.txt").read_text(encoding="utf-8") == "after"

    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["status"] == "applied"
    db.close()

    assert "Session saved and closed. Goodbye!" in captured.out


@pytest.mark.parametrize(
    "answer, should_rollback",
    [("YES ", True), ("y please", False)],
)
def test_scenario_13_exact_yes_validation(
    cli_test_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    answer,
    should_rollback,
):
    """시나리오 13: 정확히 예('YES ')만 승인되고 변형('y please')은 취소됨."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    old_file = ws / "old.txt"
    old_file.write_text("before", encoding="utf-8")

    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "new.txt", "hello"),
        make_patch_response("c2", "old.txt", "before", "after"),
        MockLLMResponse(content="Turn done."),
    ]

    fake_in = ScriptableInput(
        user_inputs=["go", "/rollback", "exit"],
        rollback_answers=[answer],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    if should_rollback:
        assert not (ws / "new.txt").exists()
        assert (ws / "old.txt").read_text(encoding="utf-8") == "before"
        assert CLI_ROLLBACK_MODEL_UNAWARE_NOTICE in captured.out
    else:
        assert (ws / "new.txt").exists()
        assert (ws / "old.txt").read_text(encoding="utf-8") == "after"
        assert CLI_ROLLBACK_CANCELLED_NOTICE in captured.out


def test_scenario_14_rollback_token_and_arguments_filtering(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 14: 인자 있는 /rollback, 대소문자, 접두어 유사어(/rollbackup) 구분 처리."""
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(content="Response to rollbackup."),
    ]

    fake_in = ScriptableInput(
        user_inputs=["/rollback", "/rollback 3", "/Rollback", "/rollbackup", "exit"],
        rollback_answers=[],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert captured.out.count(CLI_ROLLBACK_NOTHING_NOTICE) == 2
    assert captured.out.count(CLI_ROLLBACK_USAGE_NOTICE) == 1
    assert CLI_ROLLBACK_PROMPT not in fake_in.recorded_prompts

    client = ScriptableClient.created_instances[0]
    assert len(client.call_history) == 1
    user_msgs = [m for m in client.call_history[0]["messages"] if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == "/rollbackup"


def test_scenario_15_sequential_rollbacks(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 15: 차례로 되돌리기 (턴 2 -> 턴 1 -> 되돌릴 것 없음)."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "a.txt", "one"),
        MockLLMResponse(content="Turn 1 done."),
        make_patch_response("c2", "a.txt", "one", "two"),
        MockLLMResponse(content="Turn 2 done."),
    ]

    fake_in = ScriptableInput(
        user_inputs=["t1", "t2", "/rollback", "/rollback", "/rollback", "exit"],
        rollback_answers=["y", "y"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    assert not (ws / "a.txt").exists()
    assert fake_in.recorded_prompts.count(CLI_ROLLBACK_PROMPT) == 2
    assert CLI_ROLLBACK_NOTHING_NOTICE in captured.out

    # 롤백 헤더의 turn 번호 검증 (연속 되돌리기: 턴 2 -> 턴 1)
    assert len(fake_in.output_before_rollback_prompt) == 2
    assert CLI_ROLLBACK_HEADER.format(turn_index=2, restore_count=1, skip_count=0) in fake_in.output_before_rollback_prompt[0]
    assert CLI_ROLLBACK_HEADER.format(turn_index=1, restore_count=1, skip_count=0) in fake_in.output_before_rollback_prompt[1]

    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT * FROM file_checkpoints").fetchall()
    assert len(rows) == 2
    assert rows[0]["status"] == "rolled_back"
    assert rows[1]["status"] == "rolled_back"
    db.close()


def test_scenario_16_conflict_skip_and_partial_rollback(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 16: 외부 수정 충돌 파일은 건너뛰고 나머지 파일은 정상 삭제/복원."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "a.txt", "one"),
        make_write_file_response("c2", "b.txt", "two"),
        MockLLMResponse(content="Turn done."),
    ]

    def human_edit_and_rollback():
        (ws / "a.txt").write_text("human edit", encoding="utf-8")
        return "/rollback"

    fake_in = ScriptableInput(
        user_inputs=["go", human_edit_and_rollback, "exit"],
        rollback_answers=["y"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    prompt_out = fake_in.output_before_rollback_prompt[0]
    assert (
        CLI_ROLLBACK_LINE_SKIP.format(
            rel_path="a.txt", tool_name="write_file", reason="충돌(외부 변경)"
        )
        in prompt_out
    )
    assert (
        CLI_ROLLBACK_LINE_DELETE.format(rel_path="b.txt", tool_name="write_file")
        in prompt_out
    )

    assert (ws / "a.txt").read_text(encoding="utf-8") == "human edit"
    assert not (ws / "b.txt").exists()

    db = SessionDB(cli_test_env["hermes_home"] / "state.db")
    rows = db._conn.execute("SELECT rel_path, status FROM file_checkpoints").fetchall()
    row_map = {r["rel_path"]: r["status"] for r in rows}
    assert row_map["a.txt"] == "skipped"
    assert row_map["b.txt"] == "rolled_back"
    db.close()

    assert CLI_ROLLBACK_RESULT_SUMMARY.format(rolled_back=1, skipped=1) in captured.out
    assert CLI_ROLLBACK_MODEL_UNAWARE_NOTICE in captured.out


def test_scenario_17_external_warning_on_terminal_or_subagent(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 17: 해당 턴에 terminal 도구가 실행된 경우 CLI_ROLLBACK_EXTERNAL_WARNING 출력."""
    monkeypatch.setattr(cli, "is_interactive_terminal", lambda: True)

    # 17a: terminal 실행 포함 (허용목록 밖 명령어 -> 승인 기록 발생)
    py_cmd = shlex.join([sys.executable, "-c", "print(1)"])
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            content=None,
            tool_calls=[
                {
                    "id": "c_t",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": py_cmd}),
                    },
                },
                {
                    "id": "c_w",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "a.txt", "content": "1"}),
                    },
                },
            ],
        ),
        MockLLMResponse(content="Done."),
    ]

    fake_in = ScriptableInput(
        user_inputs=["go", "/rollback", "exit"],
        rollback_answers=["n"],
        approval_answers=["y"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    prompt_out = fake_in.output_before_rollback_prompt[0]
    assert CLI_ROLLBACK_EXTERNAL_WARNING in prompt_out

    # 17b: terminal 없는 쓰기만
    capsys.readouterr()  # 이전 실행 출력 버퍼 비우기
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c_w2", "b.txt", "2"),
        MockLLMResponse(content="Done."),
    ]
    fake_in2 = ScriptableInput(
        user_inputs=["go", "/rollback", "exit"],
        rollback_answers=["n"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in2)
    cli.main()
    prompt_out2 = fake_in2.output_before_rollback_prompt[0]
    assert CLI_ROLLBACK_EXTERNAL_WARNING not in prompt_out2


def test_scenario_18_mock_plan_display_and_execute(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 18: 모의 계획 표시 포맷 및 execution 인자 정합성 확인."""
    fake_plan = RollbackPlan(
        session_id="dummy-session",
        turn_index=7,
        items=[
            RollbackItem(
                checkpoint_id="cp1",
                tool_name="patch",
                workspace="/ws",
                rel_path="x.txt",
                before_exists=True,
                before_content=b"old",
                after_sha256="abc",
                status="applied",
                will_skip=False,
                skip_reason=None,
            ),
            RollbackItem(
                checkpoint_id="cp2",
                tool_name="write_file",
                workspace="/ws",
                rel_path="y.txt",
                before_exists=False,
                before_content=None,
                after_sha256="def",
                status="applied",
                will_skip=False,
                skip_reason=None,
            ),
            RollbackItem(
                checkpoint_id="cp3",
                tool_name="write_file",
                workspace="/ws",
                rel_path="z.txt",
                before_exists=True,
                before_content=b"prev",
                after_sha256="123",
                status="applied",
                will_skip=True,
                skip_reason="충돌(외부 변경)",
            ),
            RollbackItem(
                checkpoint_id="cp4",
                tool_name="patch",
                workspace="/ws",
                rel_path="p.txt",
                before_exists=True,
                before_content=b"pend",
                after_sha256="456",
                status="pending",
                will_skip=True,
                skip_reason="결과 모름(pending)",
            ),
        ],
        terminal_approvals_count=0,
        subagent_count=1,
        has_warnings=True,
    )

    fake_result = RollbackResult(
        session_id="dummy-session",
        turn_index=7,
        rolled_back_count=2,
        skipped_count=2,
        items=[
            RollbackItemResult("cp1", "x.txt", "restored", "rolled_back", None),
            RollbackItemResult("cp2", "y.txt", "deleted", "rolled_back", None),
            RollbackItemResult("cp3", "z.txt", "skipped", "skipped", "충돌(외부 변경)"),
            RollbackItemResult("cp4", "p.txt", "skipped", "skipped", "결과 모름(pending)"),
        ],
    )

    executed_plans = []
    monkeypatch.setattr(Checkpointer, "plan_rollback", lambda self, turn_index=None: fake_plan)
    monkeypatch.setattr(
        Checkpointer,
        "execute_rollback",
        lambda self, plan: executed_plans.append(plan) or fake_result,
    )

    fake_in = ScriptableInput(
        user_inputs=["/rollback", "exit"],
        rollback_answers=["y"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 질문 앞 출력 검증
    prompt_out = fake_in.output_before_rollback_prompt[0]
    assert (
        CLI_ROLLBACK_HEADER.format(turn_index=7, restore_count=2, skip_count=2) in prompt_out
    )
    assert (
        CLI_ROLLBACK_LINE_RESTORE.format(rel_path="x.txt", tool_name="patch") in prompt_out
    )
    assert (
        CLI_ROLLBACK_LINE_DELETE.format(rel_path="y.txt", tool_name="write_file")
        in prompt_out
    )
    assert (
        CLI_ROLLBACK_LINE_SKIP.format(
            rel_path="z.txt", tool_name="write_file", reason="충돌(외부 변경)"
        )
        in prompt_out
    )
    assert (
        CLI_ROLLBACK_LINE_SKIP.format(
            rel_path="p.txt", tool_name="patch", reason="결과 모름(pending)"
        )
        in prompt_out
    )
    assert CLI_ROLLBACK_EXTERNAL_WARNING in prompt_out
    assert CLI_ROLLBACK_PENDING_WARNING in prompt_out

    # 실행 인자 확인
    assert len(executed_plans) == 1
    assert executed_plans[0] is fake_plan

    # 결과 출력 검증
    assert CLI_ROLLBACK_RESULT_RESTORED.format(rel_path="x.txt") in captured.out
    assert CLI_ROLLBACK_RESULT_DELETED.format(rel_path="y.txt") in captured.out
    assert (
        CLI_ROLLBACK_RESULT_SKIPPED.format(rel_path="z.txt", reason="충돌(외부 변경)")
        in captured.out
    )
    assert (
        CLI_ROLLBACK_RESULT_SKIPPED.format(rel_path="p.txt", reason="결과 모름(pending)")
        in captured.out
    )
    assert CLI_ROLLBACK_RESULT_SUMMARY.format(rolled_back=2, skipped=2) in captured.out
    assert CLI_ROLLBACK_MODEL_UNAWARE_NOTICE in captured.out


def test_scenario_19_no_checkpoints_rollback_notice(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 19: --no-checkpoints 상태에서 /rollback 입력 시 안내 출력 후 REPL 유지."""
    fake_in = ScriptableInput(
        user_inputs=["/rollback", "exit"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-checkpoints"])

    cli.main()
    captured = capsys.readouterr()

    assert CLI_ROLLBACK_DISABLED_NOTICE in captured.out
    assert len(ScriptableClient.created_instances) == 0 or len(
        ScriptableClient.created_instances[0].call_history
    ) == 0


def test_scenario_20_exceptions_during_rollback(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 20: plan 또는 execute 중 예외 발생 시 CLI_ROLLBACK_ERROR_NOTICE 출력 후 계속."""
    # 20a: plan_rollback 예외
    monkeypatch.setattr(
        Checkpointer, "plan_rollback", lambda self, turn_index=None: (_ for _ in ()).throw(RuntimeError("boom_plan"))
    )
    fake_in = ScriptableInput(["/rollback", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()
    assert CLI_ROLLBACK_ERROR_NOTICE.format(reason="boom_plan") in captured.out

    # 20b: execute_rollback 예외
    fake_plan = RollbackPlan("s", 1, [
        RollbackItem("c", "patch", "/w", "f", True, b"", "a", "applied", False, None)
    ], 0, 0, False)
    monkeypatch.setattr(Checkpointer, "plan_rollback", lambda self, turn_index=None: fake_plan)
    monkeypatch.setattr(
        Checkpointer,
        "execute_rollback",
        lambda self, plan: (_ for _ in ()).throw(RuntimeError("boom_exec")),
    )
    fake_in2 = ScriptableInput(["/rollback", "exit"], rollback_answers=["y"])
    monkeypatch.setattr(cli, "read_user_input", fake_in2)
    cli.main()
    captured2 = capsys.readouterr()
    assert CLI_ROLLBACK_ERROR_NOTICE.format(reason="boom_exec") in captured2.out


# =============================================================================
# 3-4. 거부 알림 (핵심 위험 R5 ③)
# =============================================================================


def test_scenario_21_checkpoint_refused_notice_once(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 21: 원장 기록 실패로 쓰기 거부 시 알림 문구는 세션당 정확히 1번만 출력."""
    def fail_record(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionDB, "record_file_checkpoint", fail_record)

    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "n1.txt", "content 1"),
        MockLLMResponse(content="First response."),
        make_write_file_response("c2", "n2.txt", "content 2"),
        MockLLMResponse(content="Second response."),
    ]

    fake_in = ScriptableInput(["w1", "w2", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()

    # 파일 생성되지 않음
    ws = get_default_workspace()
    assert not (ws / "n1.txt").exists()
    assert not (ws / "n2.txt").exists()

    # 모델 도구 메시지에 안내 확인
    client = ScriptableClient.created_instances[0]
    tool_msg_1 = [m for m in client.call_history[1]["messages"] if m.get("role") == "tool"][0]
    assert "The file was not modified." in tool_msg_1["content"]
    assert "do not repeat the same write" in tool_msg_1["content"]

    # CLI 알림은 정확히 1회
    assert captured.out.count(CLI_CHECKPOINT_REFUSED_NOTICE) == 1


def test_scenario_22_no_false_positive_refusal_notice(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 22: 정상 쓰기 및 도구 자체의 거부(.env)에서는 거부 알림 오탐이 발생하지 않음."""
    # 22a: 정상 쓰기
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "ok.txt", "fine"),
        MockLLMResponse(content="OK."),
    ]
    fake_in = ScriptableInput(["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    captured = capsys.readouterr()
    assert CLI_CHECKPOINT_REFUSED_NOTICE not in captured.out

    # 22b: .env 쓰기 (도구 자체의 path_guard 거부)
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c2", ".env", "SECRET=1"),
        MockLLMResponse(content="Attempted .env."),
    ]
    fake_in2 = ScriptableInput(["go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", fake_in2)
    cli.main()
    captured2 = capsys.readouterr()

    assert CLI_CHECKPOINT_REFUSED_NOTICE not in captured2.out
    ws = get_default_workspace()
    assert not (ws / ".env").exists()


def test_scenario_rollback_prompt_passes_discard_typeahead(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """계획 015 S1 ①: /rollback 확인 질문이 discard_typeahead=True를 넘기는지 검증."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)
    ScriptableClient.responses_by_instance[0] = [
        make_write_file_response("c1", "test.txt", "hello"),
        MockLLMResponse(content="Done."),
    ]
    fake_in = ScriptableInput(
        user_inputs=["create", "/rollback", "exit"],
        rollback_answers=["y"],
        capsys=capsys,
    )
    monkeypatch.setattr(cli, "read_user_input", fake_in)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()

    rollback_calls = [
        c for c in fake_in.recorded_calls if c["prompt"] == CLI_ROLLBACK_PROMPT
    ]
    assert len(rollback_calls) == 1
    assert rollback_calls[0]["kwargs"].get("discard_typeahead") is True

