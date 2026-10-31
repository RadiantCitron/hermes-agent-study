"""CLI 세션 재개(--resume) 및 원장 정합 테스트 (로드맵 14 S14-030)."""

import json
from pathlib import Path
import sys
from typing import Any, Optional

import pytest

from hermes_core import cli
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.constants import (
    CLI_INTERRUPT_MODEL_WILL_BE_TOLD_NOTICE,
    CLI_RESUME_AMBIGUOUS_ERROR,
    CLI_RESUME_MOCK_ERROR,
    CLI_RESUME_NOT_FOUND_ERROR,
    CLI_RESUMED_BANNER_NOTICE,
    CLI_ROLLBACK_PROMPT,
    CLI_TURN_CANCELLED_NOTICE,
    OPENROUTER_API_KEY_ENV,
)
from hermes_core.session_db import SessionDB


class ScriptableClient:
    """테스트용 대본 기반 OpenRouter 클라이언트."""

    instances: list["ScriptableClient"] = []

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self.responses: list[Any] = []
        self.call_history: list[dict[str, Any]] = []
        self._call_recorder = None
        ScriptableClient.instances.append(self)

    def set_call_recorder(self, recorder: Any) -> None:
        self._call_recorder = recorder

    def queue_response(self, content: Optional[str] = None, tool_calls: Optional[list[dict[str, Any]]] = None) -> None:
        self.responses.append(MockLLMResponse(content=content, tool_calls=tool_calls))

    def queue_exception(self, exc: Any) -> None:
        self.responses.append(exc)

    def chat_completion(
        self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None
    ) -> Any:
        self.call_history.append({"messages": [dict(m) for m in messages], "tools": tools})
        if self._call_recorder:
            self._call_recorder({
                "model": self.model,
                "status_code": 200,
                "input_tokens": 10,
                "output_tokens": 5,
            })
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, BaseException):
                raise resp
            if isinstance(resp, type) and issubclass(resp, BaseException):
                raise resp()
            return resp
        return MockLLMResponse(content="[ScriptableClient] default response")


@pytest.fixture(autouse=True)
def reset_scriptable_client():
    ScriptableClient.instances.clear()
    yield
    ScriptableClient.instances.clear()


@pytest.fixture
def resume_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "test-key-safe-resume-env")
    monkeypatch.setattr(cli, "OpenRouterClient", ScriptableClient)
    monkeypatch.chdir(tmp_path)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


class ScriptableInput:
    def __init__(self, user_inputs: list[Any], rollback_answers: Optional[list[Any]] = None) -> None:
        self.user_inputs = list(user_inputs)
        self.rollback_answers = list(rollback_answers or [])
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "", **kwargs: Any) -> str:
        self.prompts.append(prompt)
        if prompt == CLI_ROLLBACK_PROMPT:
            if not self.rollback_answers:
                raise AssertionError(f"Unexpected rollback prompt: {prompt}")
            val = self.rollback_answers.pop(0)
        else:
            if not self.user_inputs:
                raise AssertionError(f"User inputs exhausted at prompt: {prompt}")
            val = self.user_inputs.pop(0)

        if isinstance(val, type) and issubclass(val, BaseException):
            raise val()
        if isinstance(val, BaseException):
            raise val
        return str(val)


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    cli.main()


def test_scenario_r1_cancelled_turn_deactivated_in_db(
    resume_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 R1: 턴 1 정상 완료 후 턴 2 처리 중 KeyboardInterrupt 취소 시 턴 2 메시지만 active=0 (가정 A)."""
    # 턴 1: "hello" -> "hi"
    # 턴 2: "go" -> write_file 호출 -> 도구 실행 후 두 번째 모델 호출에서 KeyboardInterrupt
    def client_factory(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(content="hi")
        c.queue_response(
            tool_calls=[{
                "id": "call_w1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "out.txt", "content": "hello file"}),
                },
            }]
        )
        c.queue_exception(KeyboardInterrupt)
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory)
    input_handler = ScriptableInput(["hello", "go", "exit"])
    monkeypatch.setattr(cli, "read_user_input", input_handler)

    _run_cli(monkeypatch)

    db = SessionDB(resume_env["hermes_home"] / "state.db")
    sessions = db._conn.execute("SELECT id FROM sessions").fetchall()
    assert len(sessions) == 1
    sid = sessions[0]["id"]

    # get_messages_as_conversation은 턴 1의 2건만 반환
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 2
    assert conv[0]["content"] == "hello"
    assert conv[1]["content"] == "hi"

    # get_messages는 취소된 턴 2의 행까지 모두 보존하되 active == 0
    all_msgs = db.get_messages(sid)
    assert len(all_msgs) >= 5  # user(hello), asst(hi), user(go), asst(write), tool(res)
    turn2_msgs = [m for m in all_msgs if m["id"] > 2]
    for m in turn2_msgs:
        assert m["active"] == 0
        assert m["compacted"] == 0
    db.close()


def test_scenario_r2_resume_latest_continuity(
    resume_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 R2: 1차 대화 후 2차 cli.main(['--resume']) 시 세션 ID, 배너, 대화 이력 연속성 확인 (가정 C)."""
    # 1차 세션 실행
    def client_factory1(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(content="got it")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory1)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["my secret word is apple", "exit"]))
    _run_cli(monkeypatch)
    out1 = capsys.readouterr().out
    assert "got it" in out1

    db = SessionDB(resume_env["hermes_home"] / "state.db")
    s1 = db._conn.execute("SELECT id FROM sessions").fetchone()["id"]
    db.close()

    # 2차 세션 재개 실행 (--resume)
    def client_factory2(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(content="it was apple")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory2)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["what was the word?", "exit"]))
    _run_cli(monkeypatch, "--resume")
    out2 = capsys.readouterr().out

    # 배너 확인
    assert f"Resumed session {s1[:8]}... (2 active messages, last turn: 0)" in out2
    assert "it was apple" in out2

    # 2차 실행 모델 호출 메시지 검증
    c2 = ScriptableClient.instances[-1]
    last_call_msgs = c2.call_history[0]["messages"]
    contents = [m.get("content") for m in last_call_msgs]
    assert "my secret word is apple" in contents
    assert "got it" in contents
    assert "what was the word?" in contents


def test_scenario_r3_resume_end_reason_updates(
    resume_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 R3: 1차 exit(user_exit) 종료 후 2차 재개 프롬프트에서 KeyboardInterrupt 종료 시 user_interrupt 갱신 (가정 F)."""
    # 1차 실행: exit
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["hello", "exit"]))
    _run_cli(monkeypatch)

    db = SessionDB(resume_env["hermes_home"] / "state.db")
    s_rec1 = db._conn.execute("SELECT id, ended_at, end_reason FROM sessions").fetchone()
    sid = s_rec1["id"]
    t1 = s_rec1["ended_at"]
    assert s_rec1["end_reason"] == "user_exit"
    db.close()

    # 2차 실행: 프롬프트에서 KeyboardInterrupt
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput([KeyboardInterrupt]))
    _run_cli(monkeypatch, "--resume")

    db2 = SessionDB(resume_env["hermes_home"] / "state.db")
    s_rec2 = db2.get_session(sid)
    assert s_rec2 is not None
    assert s_rec2["end_reason"] == "user_interrupt"
    assert s_rec2["ended_at"] is not None
    assert s_rec2["ended_at"] >= t1
    db2.close()


def test_scenario_r4_cross_session_rollback_and_model_notice(
    resume_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 R4: 세션 간 /rollback 및 모델 롤백 알림 주입 검증 (가정 D, 12 한계 1)."""
    # 세션 1: write_file a.txt ("hello") 후 exit
    def client_factory1(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(
            tool_calls=[{
                "id": "c1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "a.txt", "content": "hello"}),
                },
            }]
        )
        c.queue_response(content="created a.txt")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory1)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["make a", "exit"]))
    _run_cli(monkeypatch)

    ws_file = Path("a.txt")
    assert ws_file.exists()
    assert ws_file.read_text() == "hello"

    # 세션 2 (--resume): /rollback (y) -> "check file" -> exit
    def client_factory2(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(content="file deleted")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory2)
    monkeypatch.setattr(
        cli,
        "read_user_input",
        ScriptableInput(
            user_inputs=["/rollback", "check file", "exit"],
            rollback_answers=["y"],
        ),
    )
    _run_cli(monkeypatch, "--resume")

    # a.txt 삭제 확인
    assert not ws_file.exists()

    # 모델 호출에 Rollback notice가 주입되었는지 확인
    c2 = ScriptableClient.instances[-1]
    last_user_msg = c2.call_history[0]["messages"][-1]["content"]
    assert "[Rollback notice - added by the CLI, not typed by the user]" in last_user_msg
    assert "Turn 1:" in last_user_msg
    assert "deleted: a.txt" in last_user_msg
    assert "check file" in last_user_msg


def test_scenario_r5_turn_index_continuity_across_sessions(
    resume_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 R5: 세션 간 turn_index 연속성 (가정 E).

    세션 1에서 턴 1로 a.txt 생성.
    세션 2에서 턴 2로 b.txt 생성 후 /rollback(y) 시 b.txt만 삭제되고 a.txt는 보존.
    """
    def client_factory1(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(
            tool_calls=[{
                "id": "c1",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "a.txt", "content": "file a"}),
                },
            }]
        )
        c.queue_response(content="created a")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory1)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["make a", "exit"]))
    _run_cli(monkeypatch)

    def client_factory2(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(
            tool_calls=[{
                "id": "c2",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "b.txt", "content": "file b"}),
                },
            }]
        )
        c.queue_response(content="created b")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory2)
    monkeypatch.setattr(
        cli,
        "read_user_input",
        ScriptableInput(
            user_inputs=["make b", "/rollback", "exit"],
            rollback_answers=["y"],
        ),
    )
    _run_cli(monkeypatch, "--resume")

    # a.txt는 보존, b.txt만 삭제
    assert Path("a.txt").exists()
    assert not Path("b.txt").exists()

    db = SessionDB(resume_env["hermes_home"] / "state.db")
    cps = db.get_file_checkpoints(db._conn.execute("SELECT id FROM sessions").fetchone()["id"])
    b_cps = [cp for cp in cps if cp["rel_path"] == "b.txt"]
    assert len(b_cps) >= 1
    assert b_cps[0]["turn_index"] == 2
    assert b_cps[0]["status"] == "rolled_back"
    db.close()


def test_scenario_r6_resume_after_cancelled_turn_clean(
    resume_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 R6: 취소된 세션의 깨끗한 재개 (가정 A + C)."""
    # 세션 1: 턴 1 완료 -> 턴 2 도중 취소 -> exit
    def client_factory1(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(content="1")
        c.queue_response(
            tool_calls=[{
                "id": "c_two",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "t2.txt", "content": "val"}),
                },
            }]
        )
        c.queue_exception(KeyboardInterrupt)
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory1)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["one", "two", "exit"]))
    _run_cli(monkeypatch)

    # 세션 2 (--resume): "three" 입력
    def client_factory2(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_response(content="3")
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory2)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["three", "exit"]))
    _run_cli(monkeypatch, "--resume")

    c2 = ScriptableClient.instances[-1]
    history_contents = [m.get("content") for m in c2.call_history[0]["messages"]]
    assert "one" in history_contents
    assert "1" in history_contents
    assert "three" in history_contents
    assert "two" not in history_contents


def test_scenario_r7_resume_prefix_target(
    resume_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 R7: 오래된 세션이라도 접두사(prefix) 지정 시 정확한 세션 재개."""
    db = SessionDB(resume_env["hermes_home"] / "state.db")
    s1 = db.create_session("1111aaaa-0000-0000-0000-000000000000", source="cli")
    s2 = db.create_session("2222bbbb-0000-0000-0000-000000000000", source="cli")
    db.close()

    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["exit"]))
    _run_cli(monkeypatch, "--resume", "1111")

    # 재개된 세션이 s1이어야 함
    db2 = SessionDB(resume_env["hermes_home"] / "state.db")
    s1_rec = db2.get_session(s1)
    assert s1_rec is not None
    assert s1_rec["ended_at"] is not None  # exit로 닫힘
    db2.close()


def test_scenario_r8_resume_not_found(
    resume_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 R8: 세션 없을 때 또는 없는 접두사 시 SystemExit(2) 및 에러 메시지."""
    with pytest.raises(SystemExit) as exc1:
        _run_cli(monkeypatch, "--resume")
    assert exc1.value.code == 2
    err1 = capsys.readouterr().err
    assert "Error: No resumable CLI session found." in err1

    db = SessionDB(resume_env["hermes_home"] / "state.db")
    db.create_session("1111aaaa-0000", source="cli")
    db.close()

    with pytest.raises(SystemExit) as exc2:
        _run_cli(monkeypatch, "--resume", "9999")
    assert exc2.value.code == 2
    err2 = capsys.readouterr().err
    assert "Error: No resumable CLI session found matching '9999'." in err2


def test_scenario_r9_resume_ambiguous_prefix(
    resume_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 R9: 모호한 접두사 매칭 시 SystemExit(2) 및 후보 출력."""
    db = SessionDB(resume_env["hermes_home"] / "state.db")
    db.create_session("aaaa1111-0000", source="cli")
    db.create_session("aaaa2222-0000", source="cli")
    db.close()

    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, "--resume", "aaaa")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Error: Session prefix 'aaaa' is ambiguous" in err
    assert "aaaa1111" in err
    assert "aaaa2222" in err


def test_scenario_r10_resume_mock_conflict(
    resume_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 R10: --resume과 --mock 동시 사용 시 SystemExit(2)."""
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, "--mock", "--resume")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert CLI_RESUME_MOCK_ERROR in err


def test_scenario_r11_resume_with_no_checkpoints(
    resume_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 R11: --no-checkpoints와 --resume 혼용 시 정상 재개 및 last turn: 0 표시."""
    # 세션 1 생성
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["hello", "exit"]))
    _run_cli(monkeypatch)
    capsys.readouterr()

    # 세션 2 재개
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["next", "exit"]))
    _run_cli(monkeypatch, "--resume", "--no-checkpoints")
    out = capsys.readouterr().out
    assert "last turn: 0" in out
    assert "File checkpoints disabled" in out


def test_scenario_r12_deactivate_messages_after_error_handled_gracefully(
    resume_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 R12: 취소 시 deactivate_messages_after 실패해도 CLI 비정상 종료 없이 프롬프트 복귀."""
    def client_factory(api_key: str, model: str):
        c = ScriptableClient(api_key, model)
        c.queue_exception(KeyboardInterrupt)
        return c

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["trigger_cancel", "exit"]))

    def failing_deactivate(*args: Any, **kwargs: Any):
        raise RuntimeError("db locked")

    monkeypatch.setattr(SessionDB, "deactivate_messages_after", failing_deactivate)

    import logging
    with caplog.at_level(logging.WARNING):
        _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert CLI_TURN_CANCELLED_NOTICE in out
    assert any("deactivate_messages_after failed" in record.message for record in caplog.records)
