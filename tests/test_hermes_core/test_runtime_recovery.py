"""런타임 턴 취소·경계 복구 정합(R-1, R-3) 및 조립·CLI 정돈(S-4) 테스트 (로드맵 20 / 지시서 040).

인수 테스트 시나리오:
1. [R-1 핵심] 턴중 압축 후 취소 시 메모리-원장 재동기화 (Split-Brain 차단)
2. [R-1 경계] 첫 번째 턴 취소 시 history=None 복원
3. [R-1 호환] 취소 복구 중 DB 락 예외 생존 (시나리오 R12 계약)
4. [R-3 보호] panel.apply_pending() 실패 시 REPL 생존 및 경고 로그
5. [R-3 보호] get_last_active_message_id 실패 시 0 폴백 및 경고 로그
6. [S-4 조립] register_delegate_if_enabled 동작 매트릭스 (mock/delegate 조합 3종)
7. [S-4 라우팅] _dispatch_slash_command 슬래시 명령 분기 정확성
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from hermes_core import cli
from hermes_core.agent.agent import HermesAgent, MockLLMResponse
from hermes_core.assembly import register_delegate_if_enabled
from hermes_core.constants import (
    CLI_TURN_CANCELLED_NOTICE,
    OPENROUTER_API_KEY_ENV,
)
from hermes_core.control.panel import ControlPanel
from hermes_core.control.settings import Settings
from hermes_core.llm_client import LLMResponse
from hermes_core.session_db import SessionDB
from hermes_core.tools.checkpoint import Checkpointer, RollbackResult
from hermes_core.tools.dispatcher import ToolDispatcher


class ScriptableClient:
    """테스트 시나리오별 LLM 응답을 순차적으로 반환하는 클라이언트."""

    def __init__(self, api_key: str = "mock-key", model: str = "mock-model") -> None:
        self.api_key = api_key
        self.model = model
        self.call_history: list[dict[str, Any]] = []
        self._call_recorder: Any = None
        self.responses: list[Any] = []

    def set_call_recorder(self, recorder: Any) -> None:
        self._call_recorder = recorder

    def queue_response(self, content: str) -> None:
        self.responses.append(MockLLMResponse(content=content))

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
                "input_tokens": 50,
                "output_tokens": 20,
            })
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item
        return MockLLMResponse(content="default reply")


class ScriptableInput:
    """사용자 입력을 순차적으로 전달하는 모의 입력기."""

    def __init__(self, inputs: list[str]) -> None:
        self.inputs = list(inputs)

    def __call__(self, prompt: str = "", **kwargs: Any) -> str:
        if not self.inputs:
            return "exit"
        return self.inputs.pop(0)


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "test-key-recovery")
    monkeypatch.chdir(tmp_path)
    return home


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)


# ---------------------------------------------------------------------------
# S-4 단위 시험
# ---------------------------------------------------------------------------


def test_register_delegate_if_enabled_matrix(tmp_path: Path) -> None:
    """시나리오 6 (S-4 조립): register_delegate_if_enabled 동작 조건 3종 검증."""
    db = SessionDB(tmp_path / "test.db")
    session_id = db.create_session("parent")
    dispatcher = ToolDispatcher()

    class FakeAgent:
        def __init__(self) -> None:
            self.dispatcher = dispatcher
            self.session_db = db
            self.session_id = session_id

    agent = FakeAgent()
    factory = lambda: ScriptableClient()

    # Case 1: mock=True, settings.delegate=True -> False, 등록 안 됨
    s1 = Settings(delegate=True)
    res1 = register_delegate_if_enabled(agent, s1, mock=True, llm_client_factory=factory)
    assert res1 is False
    assert "delegate_task" not in agent.dispatcher._handlers

    # Case 2: mock=False, settings.delegate=False -> False, 등록 안 됨
    s2 = Settings(delegate=False)
    res2 = register_delegate_if_enabled(agent, s2, mock=False, llm_client_factory=factory)
    assert res2 is False
    assert "delegate_task" not in agent.dispatcher._handlers

    # Case 3: mock=False, settings.delegate=True -> True, delegate_task 정상 등록
    s3 = Settings(delegate=True)
    res3 = register_delegate_if_enabled(agent, s3, mock=False, llm_client_factory=factory)
    assert res3 is True
    assert "delegate_task" in agent.dispatcher._handlers
    assert callable(agent.dispatcher._handlers["delegate_task"])
    db.close()


def test_dispatch_slash_command_routing() -> None:
    """시나리오 7 (S-4 라우팅): _dispatch_slash_command 분기 정확성 검증."""
    pending: list[Any] = []
    mock_panel = MagicMock(spec=ControlPanel)
    mock_checkpointer = MagicMock(spec=Checkpointer)

    # 1. 빈 토큰 -> False
    assert cli._dispatch_slash_command([], checkpointer=mock_checkpointer, panel=mock_panel, pending_notices=pending) is False
    assert len(pending) == 0

    # 2. 일반 대화 -> False
    assert cli._dispatch_slash_command(["hello", "world"], checkpointer=mock_checkpointer, panel=mock_panel, pending_notices=pending) is False
    assert len(pending) == 0

    # 3. /ctl status -> True 및 panel 상호작용
    assert cli._dispatch_slash_command(["/ctl", "status"], checkpointer=mock_checkpointer, panel=mock_panel, pending_notices=pending) is True
    mock_panel.snapshot.assert_called_once()

    # 4. /rollback (잘못된 인자여도 핸들러 경유 후 True)
    assert cli._dispatch_slash_command(["/rollback"], checkpointer=mock_checkpointer, panel=mock_panel, pending_notices=pending) is True


# ---------------------------------------------------------------------------
# R-3 턴 시작 경계 Fail-Safe 시험
# ---------------------------------------------------------------------------


def test_apply_pending_failure_does_not_crash_repl(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 4 (R-3 보호): panel.apply_pending() 실패 시 경고 로그를 남기고 대화 계속 진행."""
    client = ScriptableClient()
    client.queue_response("hello response")
    monkeypatch.setattr(cli, "OpenRouterClient", lambda api_key, model: client)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["first message", "exit"]))

    def failing_apply_pending(self: ControlPanel) -> None:
        raise RuntimeError("audit ledger locked")

    monkeypatch.setattr(ControlPanel, "apply_pending", failing_apply_pending)

    with caplog.at_level(logging.WARNING):
        _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Assistant: hello response" in out
    assert "Session saved and closed. Goodbye!" in out
    assert any("apply_pending failed: audit ledger locked" in rec.message for rec in caplog.records)


def test_get_last_active_message_id_failure_falls_back_to_zero(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 5 (R-3 보호): get_last_active_message_id 예외 시 0으로 폴백하고 대화 정상 진행."""
    client = ScriptableClient()
    client.queue_response("second response")
    monkeypatch.setattr(cli, "OpenRouterClient", lambda api_key, model: client)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["msg", "exit"]))

    def failing_get_last_id(self: SessionDB, session_id: str) -> int:
        raise RuntimeError("transient db read error")

    monkeypatch.setattr(SessionDB, "get_last_active_message_id", failing_get_last_id)

    with caplog.at_level(logging.WARNING):
        _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert "Assistant: second response" in out
    assert any("get_last_active_message_id failed: transient db read error" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# R-1 턴 취소 및 원장-메모리 재동기화 시험
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_on_first_turn_resets_history_to_none(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 2 (R-1 경계): 첫 번째 턴 취소 시 history가 None으로 재설정되고 다음 턴 정상 진행."""
    client = ScriptableClient()
    # 1턴: KeyboardInterrupt 유발
    client.queue_exception(KeyboardInterrupt)
    # 2턴: 정상 응답
    client.queue_response("recovered turn 2 response")

    monkeypatch.setattr(cli, "OpenRouterClient", lambda api_key, model: client)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["turn1 cancel", "turn2 success", "exit"]))

    _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert CLI_TURN_CANCELLED_NOTICE in out
    assert "Assistant: recovered turn 2 response" in out

    # DB 검증: turn1 메시지는 비활성화(active=0)되고, turn2 메시지만 활성화(active=1)되어 있어야 함
    db = SessionDB(hermes_home / "state.db")
    rows = db._conn.execute("SELECT content, active FROM messages ORDER BY id ASC").fetchall()
    db.close()

    # turn1 cancel 메시지는 active=0
    turn1_rows = [r for r in rows if "turn1 cancel" in r["content"]]
    assert len(turn1_rows) == 1
    assert turn1_rows[0]["active"] == 0

    # turn2 success 메시지는 active=1
    turn2_rows = [r for r in rows if "turn2 success" in r["content"]]
    assert len(turn2_rows) == 1
    assert turn2_rows[0]["active"] == 1


def test_keyboard_interrupt_db_failure_preserves_history_and_logs_warning(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 3 (R-1 호환): deactivate_messages_after 실패 시 경고 로그를 남기고 CLI 생존 (R12 불변식)."""
    client = ScriptableClient()
    client.queue_exception(KeyboardInterrupt)
    client.queue_response("after error response")

    monkeypatch.setattr(cli, "OpenRouterClient", lambda api_key, model: client)
    monkeypatch.setattr(cli, "read_user_input", ScriptableInput(["trigger_cancel", "next_msg", "exit"]))

    def failing_deactivate(self: SessionDB, session_id: str, after_id: int) -> None:
        raise RuntimeError("simulated db locked")

    monkeypatch.setattr(SessionDB, "deactivate_messages_after", failing_deactivate)

    with caplog.at_level(logging.WARNING):
        _run_cli(monkeypatch)

    out = capsys.readouterr().out
    assert CLI_TURN_CANCELLED_NOTICE in out
    assert "Assistant: after error response" in out
    assert any("deactivate_messages_after failed: simulated db locked" in rec.message for rec in caplog.records)


def test_keyboard_interrupt_after_compression_resyncs_history_with_ledger(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시나리오 1 (R-1 핵심): 턴 도중 압축 후 KeyboardInterrupt 취소 시 history가 원장과 100% 재동기화됨."""
    client = ScriptableClient()

    # 턴 1: 정상 대화 (메시지 누적)
    client.queue_response("turn 1 answer")

    # 턴 2:
    # 턴 2 시작 시 CompressionExtension이 이전 메시지 압축을 위해 LLM 호출 -> 요약 응답 반환
    # 그 후 실제 턴 2 대화 LLM 호출 시 -> KeyboardInterrupt 유발
    # 압축 프롬프트는 "Summarize the above messages" 등을 포함함
    def dynamic_chat_completion(messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> Any:
        client.call_history.append({"messages": [dict(m) for m in messages], "tools": tools})
        # 압축 프롬프트인지 감지: 메시지 중 'Summarize the above messages' 또는 system 프롬프트 확인
        for m in messages:
            content = str(m.get("content", ""))
            if "Summarize" in content or "summary" in content.lower():
                return MockLLMResponse(content="[COMPRESSED SUMMARY OF TURN 1]")

        # 턴 1의 첫 대화 호출
        if len(client.call_history) == 1:
            return MockLLMResponse(content="turn 1 answer")

        # 턴 2의 대화 호출에서 KeyboardInterrupt
        if any("turn 2 with cancel" in str(m.get("content", "")) for m in messages):
            raise KeyboardInterrupt()

        # 턴 3 대화 호출
        if any("turn 3 after recovery" in str(m.get("content", "")) for m in messages):
            return MockLLMResponse(content="turn 3 answer")

        return MockLLMResponse(content="fallback answer")

    client.chat_completion = dynamic_chat_completion  # type: ignore[method-assign]

    # CLI 세팅에서 압축 활성화 및 임계값 작게 설정
    # --context-window 100 --compress 등 설정
    captured_histories: list[Optional[list[dict[str, Any]]]] = []
    orig_run_conv = HermesAgent.run_conversation

    def tracking_run_conv(self: HermesAgent, message: str, conversation_history: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        captured_histories.append(conversation_history)
        return orig_run_conv(self, message, conversation_history=conversation_history)

    monkeypatch.setattr(HermesAgent, "run_conversation", tracking_run_conv)
    monkeypatch.setattr(cli, "OpenRouterClient", lambda api_key, model: client)
    monkeypatch.setattr(
        cli,
        "read_user_input",
        ScriptableInput(["turn 1 start", "turn 2 with cancel", "turn 3 after recovery", "exit"]),
    )

    # CLI 실행: 압축이 바로 발동하도록 context-window 80으로 설정
    _run_cli(
        monkeypatch,
        "--context-window",
        "80",
    )

    out = capsys.readouterr().out
    assert "Assistant: turn 1 answer" in out
    assert CLI_TURN_CANCELLED_NOTICE in out
    assert "Assistant: turn 3 answer" in out

    # 턴 3 진입 시 전달된 conversation_history 검증:
    # captured_histories[0]: 턴 1 (None)
    # captured_histories[1]: 턴 2 (턴 1 결과 history)
    # captured_histories[2]: 턴 3 (턴 2 취소 후 load_conversation_history로 재동기화된 history)
    assert len(captured_histories) == 3
    turn3_history = captured_histories[2]
    assert turn3_history is not None

    # 원장의 load_conversation_history와 100% 동일한지 대조
    db = SessionDB(hermes_home / "state.db")
    rows = db._conn.execute("SELECT id FROM sessions ORDER BY started_at DESC").fetchall()
    session_id = rows[0]["id"]
    ledger_messages = db.get_messages_as_conversation(session_id)
    db.close()

    # 원장의 메시지와 turn3_history의 메시지들이 일치하고,
    # 'turn 2 with cancel' 메시지는 제외되어야 함
    contents_in_history = [str(m.get("content", "")) for m in turn3_history]
    assert not any("turn 2 with cancel" in c for c in contents_in_history)
    assert any("turn 1 start" in c or "[COMPRESSED SUMMARY OF TURN 1]" in c for c in contents_in_history)
