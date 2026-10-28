"""컨텍스트 압축을 위한 도구 결과 가지치기, 경계 산정, 도구 호출/결과 쌍 정합 순수 함수 모듈.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py (대화 이력 가지치기 및 도구 호출 정합성 관리)
- 설계 원칙:
  1. 순수 함수(Pure Functions): LLM, DB, 파일 입출력 등 외부 의존성 없이 입력 목록을 불변 유지하며 새 목록 반환.
  2. 도구 쌍 원자성: assistant(tool_calls)와 tool 결과는 분할 경계에서 쪼개지지 않고 하나의 덩어리로 헤드 또는 테일에 귀속.
  3. 가지치기 멱등성: 이미 치환된 도구 표지는 재치환되지 않음.
"""

from typing import Any, Optional

from hermes_core.agent.context_budget import estimate_messages_tokens
from hermes_core.constants import TOOL_OUTPUT_CLEARED_FORMAT


def prune_old_tool_results(
    messages: list[dict[str, Any]],
    keep_from: int,
) -> list[dict[str, Any]]:
    """인덱스 `keep_from` 앞쪽의 tool 메시지 본문을 표지로 치환.

    - tool_call_id, tool_name, assistant의 호출 인자는 그대로 보존.
    - 이미 치환 표지인 메시지는 그대로 둔다 (멱등성).
    - 빈 본문(None, "")도 그대로 둔다.
    - 입력 messages와 내부 딕셔너리를 변경하지 않고 새 목록을 반환.
    """
    new_messages: list[dict[str, Any]] = []

    for i, m in enumerate(messages):
        m_copy = dict(m)
        if i < keep_from and m_copy.get("role") == "tool":
            content = m_copy.get("content")
            if content and isinstance(content, str):
                # 이미 치환 표지인지 확인
                is_already_cleared = (
                    content.startswith("[Old tool output cleared:") and content.endswith("]")
                )
                if not is_already_cleared:
                    tool_name = m_copy.get("tool_name") or "tool"
                    chars = len(content)
                    m_copy["content"] = TOOL_OUTPUT_CLEARED_FORMAT.format(
                        tool=tool_name,
                        chars=chars,
                    )
        new_messages.append(m_copy)

    return new_messages


def split_for_compression(
    messages: list[dict[str, Any]],
    head_messages: int,
    tail_tokens: int,
    min_tail_messages: int,
) -> Optional[tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]]:
    """대화 이력을 (head, middle, tail)로 3분할.

    - 헤드: 시스템 메시지(있으면) + 이어지는 head_messages개. (head_messages=0이면 system만)
    - 테일: 뒤에서부터 tail_tokens 예산까지이되, 최소 min_tail_messages개와 가장 최근 user 메시지를 포함.
    - 경계 정렬:
      - 헤드 끝이 tool_calls assistant이고 뒤에 tool 메시지가 이어지면 그 tool 메시지들까지 헤드에 포함.
      - 테일 시작이 tool 메시지이면 부모 assistant까지 테일에 포함.
    - middle이 비어 있으면 None 반환.
    - 입력 messages를 변경하지 않고 새 목록을 반환.
    """
    total_len = len(messages)
    if total_len == 0:
        return None

    # 1. 헤드 시작점 및 초기 경계
    has_system = total_len > 0 and messages[0].get("role") == "system"
    sys_count = 1 if has_system else 0

    head_end = sys_count + head_messages
    if head_end > total_len:
        head_end = total_len

    # 헤드 경계 정렬: 헤드 끝 직후에 이어지는 tool 메시지가 있다면 헤드에 포함
    # (헤드 안의 tool_calls에 대한 결과가 잘리지 않도록 보호)
    while head_end < total_len and messages[head_end].get("role") == "tool":
        head_end += 1

    # 2. 테일 경계 산정
    # 2-1. 가장 최근 user 메시지 위치 확인
    last_user_idx: Optional[int] = None
    for idx in range(total_len - 1, -1, -1):
        if messages[idx].get("role") == "user":
            last_user_idx = idx
            break

    # 2-2. 필수 테일 시작 위치 (최소 메시지 수 + 최근 user 메시지 포함)
    req_tail_start = total_len - min_tail_messages
    if last_user_idx is not None:
        req_tail_start = min(req_tail_start, last_user_idx)

    # 2-3. tail_tokens 예산에 따른 역방향 탐색
    curr_tokens = 0
    budget_tail_start = total_len
    for idx in range(total_len - 1, -1, -1):
        # 단일 메시지 토큰 추정
        msg_tokens = estimate_messages_tokens([messages[idx]])
        if curr_tokens + msg_tokens <= tail_tokens:
            curr_tokens += msg_tokens
            budget_tail_start = idx
        else:
            break

    # 테일 시작점은 필수 조건(최근 user + 최소 개수)과 토큰 예산 중 더 앞선(더 많은 메시지 포함) 쪽 선택
    tail_start = min(req_tail_start, budget_tail_start)
    if tail_start < 0:
        tail_start = 0

    # 테일 경계 정렬: 테일 시작이 tool 메시지이면 그 부모 assistant까지 테일에 포함
    while tail_start > 0 and messages[tail_start].get("role") == "tool":
        tail_start -= 1

    # 3. middle 존재 여부 판정
    if head_end >= tail_start:
        return None

    head = [dict(m) for m in messages[:head_end]]
    middle = [dict(m) for m in messages[head_end:tail_start]]
    tail = [dict(m) for m in messages[tail_start:]]

    if not middle:
        return None

    return head, middle, tail


def sanitize_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """도구 호출과 결과 사이의 고립을 정리하여 유효한 쌍만 유지.

    - 대응하는 assistant 호출 없는 tool 결과 제거.
    - 결과 없는 assistant tool_calls 항목 제거:
      - assistant의 tool_calls가 모두 사라지고 content도 비어 있으면 content = "(tool call removed)".
    - 마지막 assistant의 진행 중 호출은 보존 (뒤에 결과가 아직 없어도 마지막 메시지이면 유지).
    - 입력 messages를 변경하지 않고 새 목록을 반환.
    """
    # 1. 존재하는 모든 tool 메시지의 tool_call_id 수집
    available_tool_ids = {
        m.get("tool_call_id")
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }

    # 2. 존재하는 모든 assistant tool_calls의 id 수집
    called_tool_ids = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tc_id = tc.get("id")
                if tc_id:
                    called_tool_ids.add(tc_id)

    total_len = len(messages)
    sanitized: list[dict[str, Any]] = []

    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "tool":
            tc_id = m.get("tool_call_id")
            # 호출 assistant가 없는 고립된 tool 결과 제거
            if tc_id and tc_id in called_tool_ids:
                sanitized.append(dict(m))
        elif role == "assistant":
            m_copy = dict(m)
            tool_calls = m_copy.get("tool_calls")
            if tool_calls:
                # 마지막 메시지가 결과 없는 assistant인 경우(진행 중) 보존
                if idx == total_len - 1:
                    sanitized.append(m_copy)
                else:
                    # 결과가 존재하는 tool_calls만 보존
                    valid_tcs = [tc for tc in tool_calls if tc.get("id") in available_tool_ids]
                    if valid_tcs:
                        m_copy["tool_calls"] = valid_tcs
                        sanitized.append(m_copy)
                    else:
                        # tool_calls가 모두 제거된 경우
                        m_copy.pop("tool_calls", None)
                        if not m_copy.get("content"):
                            m_copy["content"] = "(tool call removed)"
                        sanitized.append(m_copy)
            else:
                sanitized.append(m_copy)
        else:
            sanitized.append(dict(m))

    return sanitized
