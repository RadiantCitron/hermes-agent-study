"""OpenRouter LLM 클라이언트 단위 테스트.

[인수 테스트 시나리오 대응: 020_OpenRouter_클라이언트.md §3]
"""

import json
import os
from typing import Any
import httpx
import pytest

from hermes_core.constants import (
    LLM_ERROR_BODY_MAX_CHARS,
    LLM_MAX_OUTPUT_TOKENS,
    OPENROUTER_CHAT_URL,
)
from hermes_core.llm_client import LLMError, LLMResponse, OpenRouterClient

FAKE_KEY = "test-key-1234567890"
MODEL_NAME = "x/y"


def test_scenario_01_successful_completion():
    """시나리오 1: 서버가 choices 및 usage 반환 -> 정상 파싱 및 LLMResponse 반환."""
    response_payload = {
        "choices": [
            {
                "message": {"content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)
    resp = client.chat_completion([{"role": "user", "content": "hello"}])

    assert resp.content == "hi"
    assert resp.tool_calls is None
    assert resp.finish_reason == "stop"
    assert resp.usage == {"input_tokens": 10, "output_tokens": 3}


def test_scenario_02_tool_calls_response():
    """시나리오 2: 서버가 content: null, tool_calls 반환 -> content is None, tool_calls 보존."""
    tool_calls_data = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
        }
    ]
    response_payload = {
        "choices": [
            {
                "message": {"content": None, "tool_calls": tool_calls_data},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 12},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)
    resp = client.chat_completion([{"role": "user", "content": "list files"}])

    assert resp.content is None
    assert resp.tool_calls == tool_calls_data
    assert resp.finish_reason == "tool_calls"


def test_scenario_03_request_payload_and_tools():
    """시나리오 3: chat_completion 요청 payload 규격 (POST, URL, model, max_tokens, tools)."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    # 1. tools가 있을 때
    dummy_tool = {"type": "function", "function": {"name": "terminal"}}
    client.chat_completion([{"role": "user", "content": "a"}], tools=[dummy_tool])

    req1 = captured_requests[0]
    assert req1.method == "POST"
    assert str(req1.url) == OPENROUTER_CHAT_URL
    body1 = json.loads(req1.content.decode("utf-8"))
    assert body1["model"] == MODEL_NAME
    assert body1["messages"] == [{"role": "user", "content": "a"}]
    assert body1["tools"] == [dummy_tool]
    assert body1["max_tokens"] == LLM_MAX_OUTPUT_TOKENS

    # 2. tools가 없을 때 (tools=None) -> 본문에 tools 키가 없어야 함
    client.chat_completion([{"role": "user", "content": "b"}], tools=None)
    req2 = captured_requests[1]
    body2 = json.loads(req2.content.decode("utf-8"))
    assert "tools" not in body2


def test_scenario_04_headers_and_body_key_protection():
    """시나리오 4: Authorization 헤더에 키 포함, 본문 JSON 문자열에는 키 없음."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)
    client.chat_completion([{"role": "user", "content": "hello"}])

    req = captured[0]
    assert req.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    body_str = req.content.decode("utf-8")
    assert FAKE_KEY not in body_str


def test_scenario_05_rate_limited_429():
    """시나리오 5: 서버 429 -> LLMError.kind == 'rate_limited'."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too many requests")

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.kind == "rate_limited"


def test_scenario_06_http_error_500_leak_free():
    """시나리오 6: 서버 500과 boom 본문 -> kind == 'http_error', 메시지에 500 포함, 예외에 키 없음."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    err = exc_info.value
    assert err.kind == "http_error"
    assert "500" in str(err)
    assert FAKE_KEY not in str(err)
    assert FAKE_KEY not in repr(err)


def test_scenario_07_http_error_masks_reflected_key():
    """시나리오 7: 서버 본문이 키를 되풀이('bad key test-key-1234567890') -> 메시지와 on_call에 키 없고 *** 치환."""
    recorded_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"bad key {FAKE_KEY}")

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(
        FAKE_KEY,
        MODEL_NAME,
        transport=transport,
        on_call=lambda r: recorded_calls.append(r),
    )

    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    err_msg = str(exc_info.value)
    assert FAKE_KEY not in err_msg
    assert "***" in err_msg

    rec = recorded_calls[0]
    assert FAKE_KEY not in rec["error_message"]
    assert "***" in rec["error_message"]


def test_scenario_08_error_body_max_chars():
    """시나리오 8: 오류 본문이 500자보다 길면 메시지에 포함된 본문이 최대 500자."""
    long_body = "x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=long_body)

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    err_str = str(exc_info.value)
    # "HTTP error 502: " 뒤에 붙은 본문 길이가 최대 LLM_ERROR_BODY_MAX_CHARS(500자)
    prefix = "HTTP error 502: "
    assert err_str.startswith(prefix)
    body_part = err_str[len(prefix) :]
    assert len(body_part) <= LLM_ERROR_BODY_MAX_CHARS


def test_scenario_09_timeout_exception():
    """시나리오 9: httpx.TimeoutException -> kind == 'timeout'."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.kind == "timeout"


def test_scenario_10_network_error():
    """시나리오 10: httpx.ConnectError -> kind == 'network'."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    with pytest.raises(LLMError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert exc_info.value.kind == "network"


def test_scenario_11_bad_response_variations():
    """시나리오 11: 200이지만 JSON 아님 / choices 빈 배열 / 빈 객체 -> 각각 kind == 'bad_response'."""
    bad_bodies = [
        ("not json", 200),
        (json.dumps({"choices": []}), 200),
        (json.dumps({}), 200),
    ]

    for body_text, status in bad_bodies:
        def handler(request: httpx.Request, b=body_text, s=status) -> httpx.Response:
            return httpx.Response(s, text=b)

        transport = httpx.MockTransport(handler)
        client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

        with pytest.raises(LLMError) as exc_info:
            client.chat_completion([{"role": "user", "content": "hi"}])

        assert exc_info.value.kind == "bad_response"


def test_scenario_12_empty_api_key_value_error():
    """시나리오 12: 빈 문자열이나 공백 키 -> ValueError."""
    for empty_k in ["", "   ", "\t\n"]:
        with pytest.raises(ValueError):
            OpenRouterClient(empty_k, MODEL_NAME)


def test_scenario_13_repr_and_str_zero_leakage():
    """시나리오 13: repr(client), str(client)에 키가 포함되지 않음."""
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME)
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in str(client)


def test_scenario_14_on_call_record_success_and_failure():
    """시나리오 14: on_call 콜백이 성공 1회 + 실패 1회 정확히 기록하고 키가 유출되지 않음."""
    records: list[dict[str, Any]] = []

    toggle = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        if toggle[0]:
            toggle[0] = False
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )
        else:
            return httpx.Response(429, text="Rate limited")

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(
        FAKE_KEY,
        MODEL_NAME,
        transport=transport,
        on_call=lambda r: records.append(r),
    )

    # 1. 성공 호출
    client.chat_completion([{"role": "user", "content": "hi"}])

    # 2. 실패 호출
    with pytest.raises(LLMError):
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert len(records) == 2

    # 성공 기록 검증
    r_success = records[0]
    assert r_success["status_code"] == 200
    assert r_success["input_tokens"] == 10
    assert r_success["output_tokens"] == 3
    assert r_success["latency_ms"] >= 0
    assert r_success["error_kind"] is None
    assert "Authorization" not in r_success["request"]
    assert FAKE_KEY not in json.dumps(r_success["request"])

    # 실패 기록 검증
    r_fail = records[1]
    assert r_fail["status_code"] == 429
    assert r_fail["error_kind"] == "rate_limited"
    assert "Authorization" not in r_fail["request"]
    assert FAKE_KEY not in json.dumps(r_fail["request"])
    assert FAKE_KEY not in str(r_fail)


def test_scenario_15_on_call_exception_swallowed():
    """시나리오 15: on_call이 예외를 던져도 chat_completion 결과는 정상 반환."""
    def buggy_recorder(record: dict[str, Any]):
        raise RuntimeError("broken recorder")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "hello"}, "finish_reason": "stop"}
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(
        FAKE_KEY,
        MODEL_NAME,
        transport=transport,
        on_call=buggy_recorder,
    )

    resp = client.chat_completion([{"role": "user", "content": "test"}])
    assert resp.content == "hello"


def test_scenario_16_assistant_message_content_null_normalized():
    """시나리오 16: assistant 메시지가 tool_calls만 있고 content 없을 때 요청에 'content': null 추가, 원본 리스트 불변."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME, transport=transport)

    assistant_msg = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f"}}],
    }
    messages = [assistant_msg]

    client.chat_completion(messages)

    # 호출자가 넘긴 원본 목록 및 메시지 불변 확인
    assert "content" not in assistant_msg
    assert len(messages) == 1

    # 전송된 요청 본문에는 content: null 포함 확인
    req_body = json.loads(captured[0].content.decode("utf-8"))
    sent_msg = req_body["messages"][0]
    assert "content" in sent_msg
    assert sent_msg["content"] is None


def test_scenario_17_real_network_blocked_in_tests():
    """시나리오 17: transport 없이 chat_completion 호출 시 conftest에 의해 RuntimeError(실제 통신 차단)."""
    client = OpenRouterClient(FAKE_KEY, MODEL_NAME)

    with pytest.raises(RuntimeError) as exc_info:
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert "real network is blocked in tests" in str(exc_info.value)


def test_scenario_18_clean_api_key_env_in_tests():
    """시나리오 18: 시험 실행 중 OPENROUTER_API_KEY 환경변수가 conftest에 의해 자동 제거되어 있음."""
    assert os.getenv("OPENROUTER_API_KEY") is None
