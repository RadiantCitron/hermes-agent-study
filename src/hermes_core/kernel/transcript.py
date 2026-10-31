"""대화 규칙 및 메시지 조립·인자 해석 도우미."""

from __future__ import annotations

import json
from typing import Any, Optional


def parse_tool_arguments(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """LLM이 반환한 arguments를 dict로 파싱.

    Returns:
        (parsed_dict, parse_error_string)
        - 파싱 성공: (dict, None)
        - 파싱 실패: (None, error_message)
    """
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, "arguments must be a JSON object"
    stripped = raw.strip()
    if not stripped:
        return {}, None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, f"JSON decode error: {exc}"
    if not isinstance(parsed, dict):
        return None, "arguments must be a JSON object"
    return parsed, None


def first_message_unchanged(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> bool:
    """두 메시지 목록의 첫 원소(system 메시지 등)가 불변인지 검사한다."""
    if not before and not after:
        return True
    if not before or not after:
        return False
    first_b = before[0]
    first_a = after[0]
    return first_b.get("role") == first_a.get("role") and first_b.get(
        "content"
    ) == first_a.get("content")


def assistant_message(
    content: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """assistant 메시지 딕셔너리를 조립한다."""
    msg: dict[str, Any] = {"role": "assistant"}
    if content:
        msg["content"] = content
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def tool_result_message(call_id: str, content: str) -> dict[str, Any]:
    """도구 실행 결과(role: tool) 메시지 딕셔너리를 조립한다."""
    return {"role": "tool", "tool_call_id": call_id, "content": content}
