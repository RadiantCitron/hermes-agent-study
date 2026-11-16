"""OpenRouter 호환 외부 LLM 클라이언트.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/llm.py 및 hermes-agent/agent.py 내 API 통신 로직
- 핵심 역할:
  - 셸이나 외부 SDK 없이 `httpx`를 통한 단일 HTTP POST 요청으로 LLM 추론 호출.
  - Zero Leakage: API 키는 문자열 표현(__repr__, __str__), 오류 메시지, 기록(on_call)에서 철저히 마스킹(***).
  - 바운디드 방어: 단일 타임아웃(60초), 최대 출력 토큰(4,096), 오류 본문 절단(500자).
  - 호출 결과 및 오류를 표준 규격(LLMResponse, LLMError)으로 변환.
"""

from dataclasses import dataclass
import logging
import time
from typing import Any, Callable, Optional

import httpx

from hermes_core.constants import (
    LLM_ERROR_BODY_MAX_CHARS,
    LLM_MAX_OUTPUT_TOKENS,
    LLM_TIMEOUT_SECONDS,
    OPENROUTER_CHAT_URL,
)
from hermes_core.kernel.ports import LLMError

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """LLM 추론 응답 규격 데이터클래스."""

    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    usage: Optional[dict[str, int]] = None
    finish_reason: Optional[str] = None


def mask_str(text: str, key: str) -> str:
    """문자열에 포함된 API 키를 ***로 마스킹한다."""
    if key and key in text:
        return text.replace(key, "***")
    return text


def mask_obj(obj: Any, key: str) -> Any:
    """사전, 리스트 또는 문자열 객체 내부의 API 키를 재귀적으로 마스킹한 사본을 반환한다."""
    if isinstance(obj, str):
        return mask_str(obj, key)
    if isinstance(obj, dict):
        return {k: mask_obj(v, key) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(item, key) for item in obj]
    return obj


def invoke_on_call_safe(
    on_call: Optional[Callable[[dict[str, Any]], None]],
    record: dict[str, Any],
    call_logger: logging.Logger = logger,
) -> None:
    """on_call 콜백을 안전하게 호출한다 (예외 발생 시 삼키고 경고 기록)."""
    if on_call is not None:
        try:
            on_call(record)
        except Exception as e:
            call_logger.warning("on_call recorder raised an exception: %s", e)


_mask_str = mask_str
_mask_obj = mask_obj


class OpenRouterClient:
    """OpenRouter 호환 API 클라이언트."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = OPENROUTER_CHAT_URL,
        timeout: int = LLM_TIMEOUT_SECONDS,
        transport: Optional[httpx.BaseTransport] = None,
        on_call: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("API key cannot be empty.")

        self.api_key = api_key.strip()
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.transport = transport
        self._on_call = on_call

    def __repr__(self) -> str:
        return f"OpenRouterClient(model={self.model!r}, base_url={self.base_url!r})"

    def __str__(self) -> str:
        return f"OpenRouterClient(model={self.model})"

    def set_call_recorder(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """호출 기록 콜백 함수를 설정한다."""
        self._on_call = fn

    def _invoke_on_call(self, record: dict[str, Any]) -> None:
        """on_call 콜백을 안전하게 호출한다 (예외 발생 시 삼키고 경고 기록)."""
        invoke_on_call_safe(self._on_call, record, logger)

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        """OpenRouter API로 단일 chat completion 요청을 보내고 LLMResponse를 반환한다."""
        # 1. assistant 메시지 보정: tool_calls가 있고 content가 없으면 content: None 명시
        prepared_messages: list[dict[str, Any]] = []
        for m in messages:
            m_copy = dict(m)
            if m_copy.get("role") == "assistant" and "tool_calls" in m_copy and "content" not in m_copy:
                m_copy["content"] = None
            prepared_messages.append(m_copy)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": prepared_messages,
            "max_tokens": LLM_MAX_OUTPUT_TOKENS,
        }
        if tools is not None:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        masked_request = _mask_obj(payload, self.api_key)

        start_time = time.perf_counter()
        status_code: Optional[int] = None
        masked_response: Optional[dict[str, Any]] = None
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        error_kind: Optional[str] = None
        error_message: Optional[str] = None

        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                resp = client.post(self.base_url, json=payload, headers=headers)
                status_code = resp.status_code

                if resp.status_code == 429:
                    error_kind = "rate_limited"
                    error_message = _mask_str(
                        f"Rate limited (429): {resp.text[:LLM_ERROR_BODY_MAX_CHARS]}",
                        self.api_key,
                    )
                    raise LLMError(error_kind, error_message)

                if resp.is_error:
                    error_kind = "http_error"
                    body_snippet = resp.text[:LLM_ERROR_BODY_MAX_CHARS]
                    error_message = _mask_str(
                        f"HTTP error {resp.status_code}: {body_snippet}",
                        self.api_key,
                    )
                    raise LLMError(error_kind, error_message)

                try:
                    data = resp.json()
                except Exception as e:
                    error_kind = "bad_response"
                    error_message = f"Invalid JSON response: {e}"
                    raise LLMError(error_kind, error_message)

                if not isinstance(data, dict):
                    error_kind = "bad_response"
                    error_message = "Response payload is not a JSON object."
                    raise LLMError(error_kind, error_message)

                masked_response = _mask_obj(data, self.api_key)

                choices = data.get("choices")
                if not isinstance(choices, list) or len(choices) == 0:
                    error_kind = "bad_response"
                    error_message = "Response choices field is missing or empty."
                    raise LLMError(error_kind, error_message)

                first_choice = choices[0]
                if not isinstance(first_choice, dict):
                    error_kind = "bad_response"
                    error_message = "Choice item is not an object."
                    raise LLMError(error_kind, error_message)

                msg = first_choice.get("message")
                if not isinstance(msg, dict):
                    error_kind = "bad_response"
                    error_message = "Message field is missing or invalid in choice."
                    raise LLMError(error_kind, error_message)

                finish_reason = first_choice.get("finish_reason")
                content = msg.get("content")
                tool_calls = msg.get("tool_calls")
                if tool_calls is not None and len(tool_calls) == 0:
                    tool_calls = None

                usage_data = data.get("usage")
                usage = None
                if isinstance(usage_data, dict):
                    input_tokens = usage_data.get("prompt_tokens")
                    output_tokens = usage_data.get("completion_tokens")
                    usage = {
                        "input_tokens": input_tokens if input_tokens is not None else 0,
                        "output_tokens": output_tokens if output_tokens is not None else 0,
                    }

                return LLMResponse(
                    content=content,
                    tool_calls=tool_calls,
                    usage=usage,
                    finish_reason=finish_reason,
                )

        except httpx.TimeoutException as e:
            error_kind = "timeout"
            error_message = _mask_str(f"Request timed out: {e}", self.api_key)
            raise LLMError(error_kind, error_message)
        except httpx.TransportError as e:
            error_kind = "network"
            error_message = _mask_str(f"Network error: {e}", self.api_key)
            raise LLMError(error_kind, error_message)
        finally:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record = {
                "model": self.model,
                "request": masked_request,
                "response": masked_response,
                "status_code": status_code,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "error_kind": error_kind,
                "error_message": error_message,
            }
            self._invoke_on_call(record)
