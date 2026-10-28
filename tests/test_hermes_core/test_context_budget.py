"""토큰 추정 및 압축 예산 판정 순수 함수 단위 테스트 (로드맵 05 / 구현지시서 020 인수 시나리오)."""

import copy

from hermes_core.agent.context_budget import (
    compression_tail_budget,
    compression_threshold,
    estimate_current_tokens,
    estimate_messages_tokens,
    estimate_tokens_rough,
    may_retry_after_reject,
    should_compress,
)
from hermes_core.constants import MESSAGE_TOKEN_OVERHEAD


def test_020_scenario_01_empty_and_none_text():
    """시나리오 1: 빈 문자열과 None은 토큰 추정치 0."""
    assert estimate_tokens_rough("") == 0
    assert estimate_tokens_rough(None) == 0


def test_020_scenario_02_ascii_text():
    """시나리오 2: ASCII 400자는 100토큰 (4바이트당 1토큰)."""
    assert estimate_tokens_rough("a" * 400) == 100


def test_020_scenario_03_cjk_text():
    """시나리오 3: 한글 100자는 100토큰 (CJK 1자=1토큰)."""
    assert estimate_tokens_rough("가" * 100) == 100


def test_020_scenario_04_mixed_cjk_and_ascii():
    """시나리오 4: 한글 10자 + ASCII 40자는 20토큰 (10 + 40/4)."""
    assert estimate_tokens_rough("가" * 10 + "a" * 40) == 20


def test_020_scenario_05_emoji_4bytes():
    """시나리오 5: 이모지(4바이트 UTF-8) 1개는 1 이상의 정수(예외 없음)."""
    val = estimate_tokens_rough("😀")
    assert isinstance(val, int)
    assert val >= 1


def test_020_scenario_06_empty_messages():
    """시나리오 6: 빈 메시지 목록은 0토큰."""
    assert estimate_messages_tokens([]) == 0


def test_020_scenario_07_single_user_message():
    """시나리오 7: user 메시지 'a'*400 1개는 100 + MESSAGE_TOKEN_OVERHEAD."""
    msgs = [{"role": "user", "content": "a" * 400}]
    assert estimate_messages_tokens(msgs) == 100 + MESSAGE_TOKEN_OVERHEAD


def test_020_scenario_08_assistant_with_tool_calls_no_content():
    """시나리오 8: tool_calls가 있는 assistant 메시지(content 없음)는 직렬화 길이만큼 증가, 예외 없음."""
    tool_call = {
        "id": "call_123",
        "function": {
            "name": "read_file",
            "arguments": '{"path": "sample.txt"}',
        },
    }
    msgs = [{"role": "assistant", "content": None, "tool_calls": [tool_call]}]
    tokens = estimate_messages_tokens(msgs)
    assert isinstance(tokens, int)
    assert tokens > MESSAGE_TOKEN_OVERHEAD


def test_020_scenario_09_messages_with_tools_schema():
    """시나리오 9: 같은 메시지에 tools 스키마 추가 시 추정치가 tools 없을 때보다 큼."""
    msgs = [{"role": "user", "content": "hello"}]
    tokens_without_tools = estimate_messages_tokens(msgs)

    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]
    tokens_with_tools = estimate_messages_tokens(msgs, tools=tools)
    assert tokens_with_tools > tokens_without_tools


def test_020_scenario_10_current_tokens_with_exact_last_message_count():
    """시나리오 10: msgs 길이 3, last_input_tokens=1000, last_message_count=3 -> 1000."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    tokens = estimate_current_tokens(
        messages=msgs,
        tools=None,
        last_input_tokens=1000,
        last_message_count=3,
    )
    assert tokens == 1000


def test_020_scenario_11_current_tokens_with_additional_messages():
    """시나리오 11: msgs 길이 5(뒤 2개 추가) -> 1000 + estimate_messages_tokens(msgs[3:])."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    tokens = estimate_current_tokens(
        messages=msgs,
        tools=None,
        last_input_tokens=1000,
        last_message_count=3,
    )
    expected = 1000 + estimate_messages_tokens(msgs[3:])
    assert tokens == expected


def test_020_scenario_12_current_tokens_fallback_when_last_input_none():
    """시나리오 12: last_input_tokens=None일 때 전체 추정치와 같음."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    tokens = estimate_current_tokens(
        messages=msgs,
        tools=None,
        last_input_tokens=None,
        last_message_count=None,
    )
    assert tokens == estimate_messages_tokens(msgs, tools=None)


def test_020_scenario_13_14_compression_threshold():
    """시나리오 13 & 14: 임계값 계산 (1M창 50% -> 524288, 1000창 50% -> 500)."""
    assert compression_threshold(1_048_576, 0.5) == 524288
    assert compression_threshold(1000, 0.5) == 500


def test_020_scenario_15_should_compress():
    """시나리오 15: should_compress(500, 500) -> True, should_compress(499, 500) -> False."""
    assert should_compress(500, 500) is True
    assert should_compress(499, 500) is False


def test_020_scenario_16_no_mutation_side_effects():
    """시나리오 16: 입력 messages를 함수 호출 전후로 비교하여 변경(부수 효과) 없음 검증."""
    original = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"id": "1", "function": {"name": "f"}}]},
    ]
    cloned = copy.deepcopy(original)

    _ = estimate_messages_tokens(original)
    assert original == cloned

    _ = estimate_current_tokens(original, tools=None, last_input_tokens=100, last_message_count=1)
    assert original == cloned


def test_020_scenario_17_compression_tail_budget():
    """시나리오 17: 테일 예산 (524288 -> 상한 20000, 2000 -> 25% 비율인 500)."""
    assert compression_tail_budget(524_288) == 20000
    assert compression_tail_budget(2_000) == 500


def test_020_scenario_18_19_may_retry_after_reject():
    """시나리오 18 & 19: 압축 거부 후 재시도 허용 판정 (None -> True, 1249/1000 -> False, 1250/1000 -> True)."""
    assert may_retry_after_reject(1000, None) is True
    assert may_retry_after_reject(1249, 1000) is False
    assert may_retry_after_reject(1250, 1000) is True
