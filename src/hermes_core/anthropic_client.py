"""Anthropic Messages API 어댑터 클라이언트.

[원본 레퍼런스 매핑]
- 대응 규격: Anthropic Messages API (/v1/messages)
- 핵심 역할:
  - 외부 SDK(`anthropic`) 없이 `httpx`만으로 `LLMPort` 및 `CallRecordable` 규격을 구현.
  - OpenAI 형식 `messages`/`tools`를 Anthropic Messages API 형식으로 변환하고,
    응답의 `text`/`tool_use` 블록을 `LLMResponse`로 역변환.
  - Zero Leakage: API 키는 `__repr__`/`__str__`, 오류 메시지, `on_call` 기록에서 마스킹(`***`).
"""

import json
import logging
import time
from typing import Any, Callable, Optional

import httpx

from hermes_core.constants import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_MESSAGES_URL,
    LLM_ERROR_BODY_MAX_CHARS,
    LLM_MAX_OUTPUT_TOKENS,
    LLM_TIMEOUT_SECONDS,
)
from hermes_core.kernel.ports import LLMError
from hermes_core.llm_client import (
    LLMResponse,
    invoke_on_call_safe,
    mask_obj,
    mask_str,
)

logger = logging.getLogger(__name__)

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


def _parse_tool_arguments(raw_args: Any) -> dict[str, Any]:
    """OpenAI tool_calls arguments를 Anthropic tool_use.input(dict)으로 안전 변환한다."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except Exception:
            return {"raw": raw_args}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": raw_args}
    return {}


def _convert_tools_to_anthropic(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """OpenAI function tool 스키마 목록을 Anthropic tools 형식으로 변환한다."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        func = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema: dict[str, Any] = {
            "name": name,
            "input_schema": func.get("parameters") or {"type": "object", "properties": {}},
        }
        description = func.get("description")
        if isinstance(description, str) and description:
            schema["description"] = description
        converted.append(schema)
    return converted


def _append_user_blocks(
    converted: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> None:
    """연속된 user 역할 메시지를 단일 user 메시지의 content 블록 리스트로 병합한다."""
    if not blocks:
        return
    if converted and converted[-1].get("role") == "user":
        prev_content = converted[-1].get("content")
        if isinstance(prev_content, list):
            prev_content.extend(blocks)
            return
    converted.append({"role": "user", "content": list(blocks)})


def _convert_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """OpenAI 형식 messages를 Anthropic (system_prompt, messages)로 변환한다."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue

        if role == "user":
            if isinstance(content, list):
                _append_user_blocks(converted, list(content))
            else:
                text = "" if content is None else str(content)
                _append_user_blocks(converted, [{"type": "text", "text": text}])
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function") or {}
                    tc_id = tc.get("id") or ""
                    tc_name = func.get("name") or ""
                    tc_args = _parse_tool_arguments(func.get("arguments"))
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": tc_name,
                            "input": tc_args,
                        }
                    )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            tool_use_id = msg.get("tool_call_id") or ""
            tool_content = "" if content is None else str(content)
            _append_user_blocks(
                converted,
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": tool_content,
                    }
                ],
            )
            continue

    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, converted


def _parse_anthropic_response(
    data: dict[str, Any],
) -> tuple[LLMResponse, Optional[int], Optional[int]]:
    """Anthropic Messages API 응답 JSON을 LLMResponse와 토큰 사용량으로 변환한다."""
    raw_content = data.get("content")
    if not isinstance(raw_content, list):
        raise LLMError("bad_response", "Response content field is missing or not a list.")

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in raw_content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_val = block.get("text")
            if isinstance(text_val, str):
                text_parts.append(text_val)
        elif block_type == "tool_use":
            tool_id = block.get("id") or ""
            tool_name = block.get("name") or ""
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            tool_calls.append(
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_input, ensure_ascii=False),
                    },
                }
            )

    content: Optional[str] = "".join(text_parts) if text_parts else None
    parsed_tool_calls: Optional[list[dict[str, Any]]] = tool_calls if tool_calls else None

    raw_stop = data.get("stop_reason")
    finish_reason = (
        _STOP_REASON_MAP.get(raw_stop, raw_stop) if isinstance(raw_stop, str) else None
    )

    usage_data = data.get("usage")
    usage: Optional[dict[str, int]] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    if isinstance(usage_data, dict):
        input_tokens = usage_data.get("input_tokens")
        output_tokens = usage_data.get("output_tokens")
        usage = {
            "input_tokens": input_tokens if input_tokens is not None else 0,
            "output_tokens": output_tokens if output_tokens is not None else 0,
        }

    return (
        LLMResponse(
            content=content,
            tool_calls=parsed_tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        ),
        input_tokens,
        output_tokens,
    )


class AnthropicClient:
    """Anthropic Messages API 어댑터 클라이언트 (`LLMPort` / `CallRecordable` 구현)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = ANTHROPIC_MESSAGES_URL,
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
        return f"AnthropicClient(model={self.model!r}, base_url={self.base_url!r})"

    def __str__(self) -> str:
        return f"AnthropicClient(model={self.model})"

    def set_call_recorder(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """호출 기록 콜백 함수를 설정한다."""
        self._on_call = fn

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        """Anthropic Messages API로 단일 추론 요청을 보내고 LLMResponse를 반환한다."""
        system_text, converted_messages = _convert_messages_to_anthropic(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": LLM_MAX_OUTPUT_TOKENS,
            "messages": converted_messages,
        }
        if system_text is not None:
            payload["system"] = system_text
        if tools:
            converted_tools = _convert_tools_to_anthropic(tools)
            if converted_tools:
                payload["tools"] = converted_tools

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
        }

        masked_request = mask_obj(payload, self.api_key)

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
                    error_message = mask_str(
                        f"Rate limited (429): {resp.text[:LLM_ERROR_BODY_MAX_CHARS]}",
                        self.api_key,
                    )
                    raise LLMError(error_kind, error_message)

                if resp.is_error:
                    error_kind = "http_error"
                    body_snippet = resp.text[:LLM_ERROR_BODY_MAX_CHARS]
                    error_message = mask_str(
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

                masked_response = mask_obj(data, self.api_key)

                try:
                    llm_resp, input_tokens, output_tokens = _parse_anthropic_response(data)
                except LLMError as e:
                    error_kind = e.kind
                    error_message = e.message
                    raise

                return llm_resp

        except httpx.TimeoutException as e:
            error_kind = "timeout"
            error_message = mask_str(f"Request timed out: {e}", self.api_key)
            raise LLMError(error_kind, error_message)
        except httpx.TransportError as e:
            error_kind = "network"
            error_message = mask_str(f"Network error: {e}", self.api_key)
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
            invoke_on_call_safe(self._on_call, record, logger)
