"""Anthropic Messages API 어댑터 클라이언트 단위 및 인수 테스트.

[인수 테스트 시나리오 대응: 020_AnthropicClient_어댑터.md §3]
"""

import json
from typing import Any

import httpx
import pytest

from hermes_core.anthropic_client import AnthropicClient
from hermes_core.constants import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_MESSAGES_URL,
    LLM_ERROR_BODY_MAX_CHARS,
    LLM_MAX_OUTPUT_TOKENS,
)
from hermes_core.kernel.ports import CallRecordable, LLMError, LLMPort

FAKE_KEY = "test-anthropic-key-1234"
MODEL_NAME = "claude-sonnet-4-5"


def test_scenario_01_basic_text_response_and_headers():
    """시나리오 1: 기본 텍스트 응답, system 분리 및 Anthropic 헤더 규격."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 4},
            },
        )

    client = AnthropicClient(FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(handler))
    resp = client.chat_completion(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
    )

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == ANTHROPIC_MESSAGES_URL
    assert req.headers["x-api-key"] == FAKE_KEY
    assert req.headers["anthropic-version"] == ANTHROPIC_API_VERSION
    assert "authorization" not in {k.lower() for k in req.headers.keys()}

    body = json.loads(req.content.decode("utf-8"))
    assert body["model"] == MODEL_NAME
    assert body["max_tokens"] == LLM_MAX_OUTPUT_TOKENS
    assert body["system"] == "sys"
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    ]

    assert resp.content == "hello"
    assert resp.tool_calls is None
    assert resp.finish_reason == "stop"
    assert resp.usage == {"input_tokens": 12, "output_tokens": 4}


def test_scenario_02_tool_schema_conversion_and_tools_none():
    """시나리오 2: 도구 스키마 input_schema 변환 및 tools=None 생략."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            },
        )

    client = AnthropicClient(FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(handler))
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "read",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]

    client.chat_completion([{"role": "user", "content": "a"}], tools=openai_tools)
    body1 = json.loads(captured[0].content.decode("utf-8"))
    assert "tools" in body1
    assert body1["tools"] == [
        {
            "name": "read_file",
            "description": "read",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]

    client.chat_completion([{"role": "user", "content": "b"}], tools=None)
    body2 = json.loads(captured[1].content.decode("utf-8"))
    assert "tools" not in body2


def test_scenario_03_tool_use_roundtrip_and_consecutive_tool_merge():
    """시나리오 3: tool_use 응답 역변환 및 연속 role=tool 결과 단일 user 병합."""
    captured: list[httpx.Request] = []
    turn = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        captured.append(request)
        if turn == 1:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "text", "text": "Checking..."},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "read_file",
                            "input": {"path": "a.txt"},
                        },
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "read_file",
                            "input": {"path": "b.txt"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Done"}],
                "stop_reason": "end_turn",
            },
        )

    client = AnthropicClient(FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(handler))

    # 1턴: tool_use 2건 수신
    resp1 = client.chat_completion([{"role": "user", "content": "read both"}])
    assert resp1.content == "Checking..."
    assert resp1.finish_reason == "tool_calls"
    assert resp1.tool_calls is not None and len(resp1.tool_calls) == 2
    assert resp1.tool_calls[0]["id"] == "t1"
    assert resp1.tool_calls[0]["type"] == "function"
    assert resp1.tool_calls[0]["function"]["name"] == "read_file"
    assert json.loads(resp1.tool_calls[0]["function"]["arguments"]) == {"path": "a.txt"}

    # 2턴: 연속 role=tool 2건 전달 -> 단일 user 메시지로 병합되어야 함
    resp2 = client.chat_completion(
        [
            {"role": "user", "content": "read both"},
            {
                "role": "assistant",
                "content": resp1.content,
                "tool_calls": resp1.tool_calls,
            },
            {"role": "tool", "tool_call_id": "t1", "content": "AAA"},
            {"role": "tool", "tool_call_id": "t2", "content": "BBB"},
        ]
    )
    assert resp2.content == "Done"

    body2 = json.loads(captured[1].content.decode("utf-8"))
    msgs = body2["messages"]
    assert len(msgs) == 3  # user -> assistant -> merged user(tool_result x2)
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "AAA"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "BBB"},
    ]


def test_scenario_04_malformed_tool_arguments_fallback():
    """시나리오 4: 깨진 tool_calls arguments JSON 안전 폴백."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"},
        )

    client = AnthropicClient(FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(handler))
    client.chat_completion(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t_bad",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{bad json"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "t_bad", "content": "err"},
        ]
    )

    body = json.loads(captured[0].content.decode("utf-8"))
    assistant_msg = body["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == [
        {
            "type": "tool_use",
            "id": "t_bad",
            "name": "terminal",
            "input": {"raw": "{bad json"},
        }
    ]


@pytest.mark.parametrize(
    ("status_code", "resp_kwargs", "expected_kind"),
    [
        (429, {"text": f"rate limit {FAKE_KEY}"}, "rate_limited"),
        (500, {"text": f"server error {FAKE_KEY} " + ("x" * 600)}, "http_error"),
        (200, {"text": "not json"}, "bad_response"),
        (200, {"json": {"id": "no_content"}}, "bad_response"),
    ],
)
def test_scenario_05_http_and_bad_response_errors(
    status_code: int,
    resp_kwargs: dict[str, Any],
    expected_kind: str,
):
    """시나리오 5a: 429, 5xx(키 포함), 비-JSON/누락 content -> LLMError 및 키 마스킹."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, **resp_kwargs)

    client = AnthropicClient(FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.kind == expected_kind
    assert FAKE_KEY not in exc_info.value.message
    if status_code == 500:
        assert len(exc_info.value.message) <= LLM_ERROR_BODY_MAX_CHARS + 50


def test_scenario_05_timeout_and_network_errors():
    """시나리오 5b: 타임아웃 및 네트워크 오류 -> LLMError(timeout / network)."""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout {FAKE_KEY}", request=request)

    c1 = AnthropicClient(
        FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(timeout_handler)
    )
    with pytest.raises(LLMError) as exc1:
        c1.chat_completion([{"role": "user", "content": "hi"}])
    assert exc1.value.kind == "timeout"
    assert FAKE_KEY not in exc1.value.message

    def network_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed {FAKE_KEY}", request=request)

    c2 = AnthropicClient(
        FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(network_handler)
    )
    with pytest.raises(LLMError) as exc2:
        c2.chat_completion([{"role": "user", "content": "hi"}])
    assert exc2.value.kind == "network"
    assert FAKE_KEY not in exc2.value.message


def test_scenario_06_zero_leakage_and_on_call_recorder():
    """시나리오 6: repr/str 키 미노출 및 on_call 레코드 마스킹."""
    records: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": f"echo {FAKE_KEY}"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
        )

    client = AnthropicClient(FAKE_KEY, MODEL_NAME, transport=httpx.MockTransport(handler))
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in str(client)

    client.set_call_recorder(records.append)
    client.chat_completion([{"role": "user", "content": f"my key is {FAKE_KEY}"}])

    assert len(records) == 1
    rec = records[0]
    assert rec["model"] == MODEL_NAME
    assert rec["status_code"] == 200
    assert rec["input_tokens"] == 5
    assert rec["output_tokens"] == 7
    assert isinstance(rec["latency_ms"], int)
    assert FAKE_KEY not in json.dumps(rec["request"])
    assert FAKE_KEY not in json.dumps(rec["response"])
    assert "***" in json.dumps(rec["request"])
    assert "***" in json.dumps(rec["response"])


def test_scenario_07_empty_key_and_protocol_conformance():
    """시나리오 7: 빈 API 키 거부 및 LLMPort / CallRecordable 적합성."""
    with pytest.raises(ValueError):
        AnthropicClient("   ", MODEL_NAME)

    client = AnthropicClient(FAKE_KEY, MODEL_NAME)
    assert isinstance(client, LLMPort)
    assert isinstance(client, CallRecordable)
