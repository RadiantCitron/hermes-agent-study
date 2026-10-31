"""tests/test_hermes_core/test_llm_wiring.py — 조립 지점 LLM 배선 및 기록 연결 테스트.

시나리오 12~16.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient
from hermes_core.assembly import (
    connect_call_recorder,
    default_llm_middlewares,
    usage_ledger_middleware,
)
from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.llm_pipeline import LLMRequest
from hermes_core.kernel.ports import CallRecordable, LLMError, LLMReply
from hermes_core.llm_client import LLMResponse, OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher


class FakeReply:
    def __init__(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[list[dict[str, Any]]] = None,
        usage: Optional[Any] = None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage


class FakeStore:
    def __init__(self) -> None:
        self.records: list[tuple[str, int, int]] = []

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.records.append((session_id, input_tokens, output_tokens))


def test_scenario_12_usage_ledger_middleware_rules() -> None:
    """시나리오 12: 사용량 미들웨어 판정은 옛 루프와 같다."""
    fake_store = FakeStore()
    mw = usage_ledger_middleware(fake_store)
    req = LLMRequest(
        messages=[],
        tools=None,
        scope=TurnScope(session_id="s1"),
    )

    # (a) input 100, output 7 -> 1회
    rep_a = FakeReply(content="a", usage={"input_tokens": 100, "output_tokens": 7})
    res_a = mw(req, lambda r: rep_a)
    assert res_a is rep_a
    assert fake_store.records == [("s1", 100, 7)]

    # (b) input 0, output 0 -> 0회
    fake_store.records.clear()
    rep_b = FakeReply(content="b", usage={"input_tokens": 0, "output_tokens": 0})
    res_b = mw(req, lambda r: rep_b)
    assert res_b is rep_b
    assert len(fake_store.records) == 0

    # (c) output 4 -> 1회
    fake_store.records.clear()
    rep_c = FakeReply(content="c", usage={"output_tokens": 4})
    res_c = mw(req, lambda r: rep_c)
    assert res_c is rep_c
    assert fake_store.records == [("s1", 0, 4)]

    # (d) usage 속성 없음 -> 0회
    fake_store.records.clear()
    rep_d = FakeReply(content="d", usage=None)
    del rep_d.usage
    res_d = mw(req, lambda r: rep_d)
    assert res_d is rep_d
    assert len(fake_store.records) == 0

    # (e) "not a dict" -> 0회
    fake_store.records.clear()
    rep_e = FakeReply(content="e", usage="not a dict")
    res_e = mw(req, lambda r: rep_e)
    assert res_e is rep_e
    assert len(fake_store.records) == 0

    # (f) {"prompt_tokens": 9, "completion_tokens": 1} -> 0회 (본 호출 경로는 이 키를 보지 않음)
    fake_store.records.clear()
    rep_f = FakeReply(content="f", usage={"prompt_tokens": 9, "completion_tokens": 1})
    res_f = mw(req, lambda r: rep_f)
    assert res_f is rep_f
    assert len(fake_store.records) == 0


def test_scenario_13_failed_call_does_not_accumulate() -> None:
    """시나리오 13: 실패한 호출은 누적하지 않는다."""
    fake_store = FakeStore()
    mw = usage_ledger_middleware(fake_store)
    req = LLMRequest(
        messages=[],
        tools=None,
        scope=TurnScope(session_id="s1"),
    )
    err = LLMError("network", "n")

    def fail_next(r: LLMRequest) -> LLMReply:
        raise err

    with pytest.raises(LLMError) as exc_info:
        mw(req, fail_next)

    assert exc_info.value is err
    assert len(fake_store.records) == 0


def test_scenario_14_agent_ledger_totals_and_middleware_factory(tmp_path: Path) -> None:
    """시나리오 14: 에이전트 원장 합계 (가정 E)."""
    db = SessionDB(tmp_path / "session.db")

    class FakeClientWithoutRecorder:
        def __init__(self, responses: list[LLMResponse]) -> None:
            self.responses = list(responses)

        def chat_completion(
            self,
            messages: list[dict[str, Any]],
            tools: Optional[list[dict[str, Any]]] = None,
        ) -> LLMResponse:
            return self.responses.pop(0)

    client = FakeClientWithoutRecorder([
        LLMResponse(
            tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": "{}"}}],
            usage={"input_tokens": 50, "output_tokens": 5},
        ),
        LLMResponse(
            content="done",
            usage={"input_tokens": 70, "output_tokens": 9},
        ),
    ])

    dispatcher = ToolDispatcher()
    dispatcher.register_tool(
        name="echo",
        handler=lambda: "ok",
        schema={"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}},
    )

    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        dispatcher=dispatcher,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    agent.run_conversation("go")

    session = db.get_session(agent.session_id)
    assert session is not None
    assert session["input_tokens"] == 120
    assert session["output_tokens"] == 14

    list1 = default_llm_middlewares(db)
    list2 = default_llm_middlewares(db)
    assert len(list1) == 1
    assert len(list2) == 1
    assert list1 is not list2


def test_scenario_15_connect_call_recorder(tmp_path: Path) -> None:
    """시나리오 15: 기록처 연결."""
    db = SessionDB(tmp_path / "session.db")

    # (a) set_call_recorder를 가진 가짜 client
    class FakeRecordableClient:
        def __init__(self) -> None:
            self.recorder = None

        def set_call_recorder(self, fn: Any) -> None:
            self.recorder = fn

        def chat_completion(self, messages: Any, tools: Any = None) -> Any:
            return LLMResponse(content="ok")

    recordable_client = FakeRecordableClient()
    agent = HermesAgent(
        session_db=db,
        llm_client=recordable_client,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    assert recordable_client.recorder is not None
    recordable_client.recorder({
        "model": "x/y",
        "request": {"a": 1},
        "response": None,
        "status_code": 200,
        "input_tokens": 1,
        "output_tokens": 2,
        "latency_ms": 3,
        "error_kind": None,
        "error_message": None,
    })

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 1
    assert calls[0]["model"] == "x/y"
    assert calls[0]["status_code"] == 200
    assert calls[0]["latency_ms"] == 3

    # (b) MockLLMClient (set_call_recorder 없음)
    obj_b = SimpleNamespace(
        llm_client=MockLLMClient(),
        session_db=db,
        session_id="s_b",
    )
    assert connect_call_recorder(obj_b) is False

    # (c) set_call_recorder가 callable이 아닌 경우
    obj_c = SimpleNamespace(
        llm_client=SimpleNamespace(set_call_recorder="x"),
        session_db=db,
        session_id="s_c",
    )
    assert connect_call_recorder(obj_c) is False


def test_scenario_16_contract_conformance() -> None:
    """시나리오 16: 계약 적합성."""
    assert issubclass(OpenRouterClient, CallRecordable)
    assert not issubclass(MockLLMClient, CallRecordable)
