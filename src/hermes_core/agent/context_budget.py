"""컨텍스트 압축을 위한 토큰 예산 산정 및 트리거 판정 순수 함수 모듈.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py (토큰 추정 및 컨텍스트 관리 로직)
- 설계 원칙:
  1. 순수 함수(Pure Functions): 파일·DB·네트워크 등 부수 효과(Side-effect)가 전혀 없음.
  2. CJK 가중치: 한글·한자·가나 등 멀티바이트 글자는 1자=1토큰, 그 외 문자는 UTF-8 4바이트당 1토큰으로 거칠게 추정.
  3. 안전 예산: 창이 작을 때 최근 보호 예산이 중간 요약 구간을 잠식하지 않도록 상한/비율 하이브리드 적용.
"""

import json
import math
from typing import Any, Optional

from hermes_core.constants import (
    COMPRESSION_RETRY_GROWTH,
    COMPRESSION_TAIL_RATIO,
    COMPRESSION_TAIL_TOKENS,
    MESSAGE_TOKEN_OVERHEAD,
)


def _is_cjk(ch: str) -> bool:
    """주어진 문자가 한글, 한자, 일본어 가나 등 CJK 범위에 속하는지 판정."""
    cp = ord(ch)
    return (
        (0xAC00 <= cp <= 0xD7AF)  # Hangul Syllables
        or (0x1100 <= cp <= 0x11FF)  # Hangul Jamo
        or (0x3130 <= cp <= 0x318F)  # Hangul Compatibility Jamo
        or (0xA960 <= cp <= 0xA97F)  # Hangul Jamo Extended-A
        or (0xD7B0 <= cp <= 0xD7FF)  # Hangul Jamo Extended-B
        or (0x4E00 <= cp <= 0x9FFF)  # CJK Unified Ideographs
        or (0x3400 <= cp <= 0x4DBF)  # CJK Unified Ideographs Extension A
        or (0x20000 <= cp <= 0x2A6DF)  # Extension B
        or (0x2A700 <= cp <= 0x2B73F)  # Extension C
        or (0x2B740 <= cp <= 0x2B81F)  # Extension D
        or (0x2B820 <= cp <= 0x2CEAF)  # Extension E
        or (0xF900 <= cp <= 0xFAFF)  # CJK Compatibility Ideographs
        or (0x3040 <= cp <= 0x309F)  # Hiragana
        or (0x30A0 <= cp <= 0x30FF)  # Katakana
        or (0x31F0 <= cp <= 0x31FF)  # Katakana Phonetic Extensions
        or (0xFF65 <= cp <= 0xFF9F)  # Halfwidth Katakana
    )


def estimate_tokens_rough(text: Optional[str]) -> int:
    """문자열의 대략적인 토큰 수를 추정.

    - None이거나 빈 문자열은 0.
    - CJK 글자는 1자=1토큰.
    - 나머지는 ceil(UTF-8 바이트 수 / 4).
    """
    if not text:
        return 0

    cjk_count = 0
    non_cjk_bytes = 0

    for ch in text:
        if _is_cjk(ch):
            cjk_count += 1
        else:
            non_cjk_bytes += len(ch.encode("utf-8"))

    non_cjk_tokens = math.ceil(non_cjk_bytes / 4) if non_cjk_bytes else 0
    return cjk_count + non_cjk_tokens


def estimate_messages_tokens(
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
) -> int:
    """메시지 목록과 도구 스키마의 총 토큰 수를 추정.

    - 각 메시지의 content 추정치
    - tool_calls(있는 경우)의 JSON 직렬화 문자열 추정치
    - 메시지당 고정 메타데이터 오버헤드 (MESSAGE_TOKEN_OVERHEAD)
    - tools 스키마(있는 경우)의 JSON 직렬화 문자열 추정치
    """
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens_rough(content)

        tool_calls = msg.get("tool_calls")
        if tool_calls:
            tc_str = json.dumps(tool_calls, ensure_ascii=False)
            total += estimate_tokens_rough(tc_str)

        total += MESSAGE_TOKEN_OVERHEAD

    if tools:
        tools_str = json.dumps(tools, ensure_ascii=False)
        total += estimate_tokens_rough(tools_str)

    return total


def estimate_current_tokens(
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]],
    last_input_tokens: Optional[int],
    last_message_count: Optional[int],
) -> int:
    """현재 컨텍스트의 예상 토큰 수를 계산.

    - 모델 호출 결과로 보고된 실제 입력 토큰 수(`last_input_tokens`)가 있으면,
      그 시점(`last_message_count`) 이후 새로 추가된 메시지만 거칠게 추정하여 합산.
    - 없으면 전체 messages와 tools를 대상으로 거칠게 추정.
    """
    if last_input_tokens is not None:
        start = last_message_count if last_message_count is not None else len(messages)
        tail_msgs = messages[start:]
        return last_input_tokens + estimate_messages_tokens(tail_msgs)

    return estimate_messages_tokens(messages, tools=tools)


def compression_threshold(window: int, ratio: float) -> int:
    """컨텍스트 창 크기와 비율로 압축 트리거 임계 토큰 수를 계산."""
    return int(window * ratio)


def should_compress(tokens: int, threshold: int) -> bool:
    """현재 토큰 수가 임계값에 도달했는지 판정 (tokens >= threshold)."""
    return tokens >= threshold


def compression_tail_budget(threshold: int) -> int:
    """최근 대화 보존을 위한 테일 예산(토큰) 계산.

    작은 창 크기에서도 중간 요약 구간이 사라지지 않도록
    절대 상한(COMPRESSION_TAIL_TOKENS)과 비율(COMPRESSION_TAIL_RATIO)의 최솟값을 취함.
    """
    return min(COMPRESSION_TAIL_TOKENS, int(threshold * COMPRESSION_TAIL_RATIO))


def may_retry_after_reject(current_tokens: int, rejected_at_tokens: Optional[int]) -> bool:
    """압축 거부 이력이 있을 때 재시도 가능 여부 판정.

    - 거부 기록이 없으면(None) 즉시 True.
    - 거부 기록이 있으면 현재 토큰이 거부 시점 토큰 대비 COMPRESSION_RETRY_GROWTH 배 이상 증가해야 True.
    """
    if rejected_at_tokens is None:
        return True
    return current_tokens >= rejected_at_tokens * COMPRESSION_RETRY_GROWTH
