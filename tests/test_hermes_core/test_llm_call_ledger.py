"""LLM 호출 원장, 토큰 집계, 모델/모드 기록 및 호출 실패 처리 통합 테스트.

[인수 테스트 시나리오 대응: 030_호출원장_토큰_실패처리.md §3]
"""

import json
from typing import Any, Callable, Optional, Union
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient
from hermes_core.llm_client import LLMError, LLMResponse
from hermes_core.session_db import SessionDB


class FakeRealLLMClient:
    """테스트용 가짜 실제 LLM 클라이언트.

    - 속성 model 노출
    - chat_completion: responses 큐에서 순차 반환 또는 LLMError 발생
    - set_call_recorder: 호출마다 콜백 호출
    """

    def __init__(self, responses: list[Union[LLMResponse, LLMError]], model: str = "x/y"):
        self.model = model
        self.responses = list(responses)
        self.call_count = 0
        self._recorder: Optional[Callable[[dict[str, Any]], None]] = None

    def set_call_recorder(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._recorder = fn

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        self.call_count += 1
        if not self.responses:
            raise RuntimeError("FakeRealLLMClient: No more responses in queue.")

        item = self.responses.pop(0)

        if isinstance(item, LLMError):
            status = 429 if item.kind == "rate_limited" else 500
            record = {
                "model": self.model,
                "request": {"model": self.model, "messages": messages},
                "response": None,
                "status_code": status,
                "input_tokens": None,
                "output_tokens": None,
                "latency_ms": 15,
                "error_kind": item.kind,
                "error_message": item.message,
            }
            if self._recorder:
                self._recorder(record)
            raise item

        usage = getattr(item, "usage", None) or {}
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        record = {
            "model": self.model,
            "request": {"model": self.model, "messages": messages},
            "response": {"content": item.content, "tool_calls": item.tool_calls},
            "status_code": 200,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "latency_ms": 15,
            "error_kind": None,
            "error_message": None,
        }
        if self._recorder:
            self._recorder(record)
        return item


def test_scenario_01_single_call_token_accumulation():
    """시나리오 1: FakeReal이 usage 입력 100·출력 20인 정상 응답 1회 -> 세션의 input_tokens==100, output_tokens==20."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(
        responses=[
            LLMResponse(
                content="hello",
                usage={"input_tokens": 100, "output_tokens": 20},
                finish_reason="stop",
            )
        ]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("hi")

    assert res["stopped_reason"] == "completed"
    session = db.get_session(agent.session_id)
    assert session["input_tokens"] == 100
    assert session["output_tokens"] == 20
    db.close()


def test_scenario_02_multi_turn_token_accumulation():
    """시나리오 2: 도구 호출 응답(usage 50/5) 후 최종 응답(usage 70/9) -> 누적 120/14."""
    db = SessionDB(":memory:")
    tool_calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "terminal", "arguments": '{"command":"echo test"}'},
        }
    ]
    client = FakeRealLLMClient(
        responses=[
            LLMResponse(
                content=None,
                tool_calls=tool_calls,
                usage={"input_tokens": 50, "output_tokens": 5},
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="done echo",
                usage={"input_tokens": 70, "output_tokens": 9},
                finish_reason="stop",
            ),
        ]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("run echo")

    assert res["stopped_reason"] == "completed"
    session = db.get_session(agent.session_id)
    assert session["input_tokens"] == 120
    assert session["output_tokens"] == 14
    db.close()


def test_scenario_03_mock_llm_client_token_and_result_unchanged():
    """시나리오 3: MockLLMClient 응답(usage 없음) -> 토큰 0, 오류 없음, stopped_reason == 'completed', 기존 반환값 동일."""
    db = SessionDB(":memory:")
    agent = HermesAgent(session_db=db, llm_client=MockLLMClient(), quiet_mode=True)
    res = agent.run_conversation("hello mock")

    assert res["stopped_reason"] == "completed"
    assert "error" not in res
    assert res["final_response"] != ""
    assert res["iterations"] >= 1
    assert res["session_id"] == agent.session_id

    session = db.get_session(agent.session_id)
    assert session["input_tokens"] == 0
    assert session["output_tokens"] == 0
    db.close()


def test_scenario_04_first_call_llm_error():
    """시나리오 4: FakeReal이 첫 호출에서 LLMError('rate_limited', ...) -> run_conversation 예외 없이 반환, stopped_reason == 'llm_error', final_response == '', iterations == 1."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(
        responses=[LLMError("rate_limited", "Rate limit exceeded (429)")]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("start")

    assert res["stopped_reason"] == "llm_error"
    assert res["final_response"] == ""
    assert res["iterations"] == 1
    assert "error" in res
    assert res["error"]["kind"] == "rate_limited"
    assert "Rate limit exceeded" in res["error"]["message"]
    db.close()


def test_scenario_05_first_call_error_ledger_user_message_only():
    """시나리오 5: 4번의 원장 -> user 메시지 1건만 있고 assistant 메시지가 없음."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(
        responses=[LLMError("rate_limited", "Too many requests")]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    agent.run_conversation("user only msg")

    conv = db.get_messages_as_conversation(agent.session_id)
    assert len(conv) == 1
    assert conv[0]["role"] == "user"
    assert conv[0]["content"] == "user_only msg".replace("_", " ")

    session = db.get_session(agent.session_id)
    assert session["message_count"] == 1
    db.close()


def test_scenario_06_tool_call_then_second_call_error():
    """시나리오 6: 도구 호출 후 두 번째 호출이 LLMError('timeout') -> stopped_reason == 'llm_error', 원장에 user, assistant(tool_calls), tool 메시지 보존."""
    db = SessionDB(":memory:")
    tool_calls = [
        {
            "id": "tc_test_99",
            "type": "function",
            "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
        }
    ]
    client = FakeRealLLMClient(
        responses=[
            LLMResponse(content=None, tool_calls=tool_calls, finish_reason="tool_calls"),
            LLMError("timeout", "Gateway timed out"),
        ]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("check pwd")

    assert res["stopped_reason"] == "llm_error"
    assert res["error"]["kind"] == "timeout"

    conv = db.get_messages_as_conversation(agent.session_id)
    assert len(conv) == 3
    assert conv[0]["role"] == "user"
    assert conv[1]["role"] == "assistant"
    assert conv[1]["tool_calls"] == tool_calls
    assert conv[2]["role"] == "tool"
    assert conv[2]["tool_call_id"] == "tc_test_99"
    db.close()


def test_scenario_07_recorded_llm_calls_success_and_failure():
    """시나리오 7: FakeReal이 호출 기록을 콜백으로 전달(성공 1·실패 1) -> get_llm_calls(session_id) 2행, 오름차순, 성공/실패 필드 검증."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(
        responses=[
            LLMResponse(
                content=None,
                tool_calls=[{"id": "c1", "function": {"name": "terminal", "arguments": '{"command":"ls"}'}}],
                usage={"input_tokens": 30, "output_tokens": 10},
            ),
            LLMError("rate_limited", "Rate limit 429"),
        ]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    agent.run_conversation("go")

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 2
    assert calls[0]["id"] < calls[1]["id"]  # id 오름차순

    c_success = calls[0]
    assert c_success["status_code"] == 200
    assert c_success["error_kind"] is None
    assert c_success["input_tokens"] == 30
    assert c_success["output_tokens"] == 10

    c_fail = calls[1]
    assert c_fail["status_code"] == 429
    assert c_fail["error_kind"] == "rate_limited"
    assert "Rate limit 429" in c_fail["error_message"]
    db.close()


def test_scenario_08_json_parsing_and_no_authorization_in_ledger():
    """시나리오 8: 기록된 request_json·response_json이 JSON 파싱되고, Authorization 키 없음."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(
        responses=[
            LLMResponse(
                content="hello world",
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        ]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    agent.run_conversation("ping")

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 1
    call = calls[0]

    req_obj = json.loads(call["request_json"])
    resp_obj = json.loads(call["response_json"])

    assert isinstance(req_obj, dict)
    assert isinstance(resp_obj, dict)

    assert "Authorization" not in req_obj
    assert "Authorization" not in resp_obj
    assert "Authorization" not in (call["error_message"] or "")
    db.close()


def test_scenario_09_llm_calls_isolated_per_session():
    """시나리오 9: 다른 세션의 호출 -> get_llm_calls가 세션별로 엄격히 분리됨."""
    db = SessionDB(":memory:")
    client1 = FakeRealLLMClient(responses=[LLMResponse(content="s1")])
    client2 = FakeRealLLMClient(responses=[LLMResponse(content="s2")])

    agent1 = HermesAgent(session_db=db, llm_client=client1, quiet_mode=True)
    agent2 = HermesAgent(session_db=db, llm_client=client2, quiet_mode=True)

    agent1.run_conversation("msg1")
    agent2.run_conversation("msg2")

    calls1 = db.get_llm_calls(agent1.session_id)
    calls2 = db.get_llm_calls(agent2.session_id)

    assert len(calls1) == 1
    assert len(calls2) == 1
    assert calls1[0]["session_id"] == agent1.session_id
    assert calls2[0]["session_id"] == agent2.session_id
    db.close()


def test_scenario_10_mock_llm_client_agent_model_and_mode():
    """시나리오 10: MockLLMClient로 만든 에이전트 -> 세션 model == 'mock', mode == 'mock', llm_calls 0행."""
    db = SessionDB(":memory:")
    agent = HermesAgent(session_db=db, llm_client=MockLLMClient(), quiet_mode=True)
    agent.run_conversation("hi")

    s = db.get_session(agent.session_id)
    assert s["model"] == "mock"
    assert s["mode"] == "mock"

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 0
    db.close()


def test_scenario_11_fake_real_agent_model_and_mode():
    """시나리오 11: FakeReal(model 'x/y')로 만든 에이전트 -> 세션 model == 'x/y', mode == 'real'."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(responses=[LLMResponse(content="ok")], model="x/y")
    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    agent.run_conversation("hi")

    s = db.get_session(agent.session_id)
    assert s["model"] == "x/y"
    assert s["mode"] == "real"
    db.close()


def test_scenario_12_explicit_model_precedence():
    """시나리오 12: HermesAgent(model='foo', llm_client=MockLLMClient()) -> 세션 model == 'foo', mode == 'mock'."""
    db = SessionDB(":memory:")
    agent = HermesAgent(model="foo", session_db=db, llm_client=MockLLMClient(), quiet_mode=True)
    agent.run_conversation("hi")

    s = db.get_session(agent.session_id)
    assert s["model"] == "foo"
    assert s["mode"] == "mock"
    db.close()


def test_scenario_15_empty_content_and_tool_calls_with_usage():
    """시나리오 15: 응답 객체가 usage는 있고 tool_calls·content가 모두 비어 있음 -> 정상 종료(빈 최종 응답), 예외 없음."""
    db = SessionDB(":memory:")
    client = FakeRealLLMClient(
        responses=[
            LLMResponse(
                content=None,
                tool_calls=None,
                usage={"input_tokens": 15, "output_tokens": 2},
                finish_reason="stop",
            )
        ]
    )

    agent = HermesAgent(session_db=db, llm_client=client, quiet_mode=True)
    res = agent.run_conversation("empty response test")

    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == ""
    assert res["iterations"] == 1

    session = db.get_session(agent.session_id)
    assert session["input_tokens"] == 15
    assert session["output_tokens"] == 2
    db.close()
