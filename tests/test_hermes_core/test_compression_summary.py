"""[구현지시서 040] 요약 생성, 정적 대체 요약, 조립 및 컨텍스트 압축 인수 테스트.

- 1~21개 인수 테스트 시나리오를 순서대로 엄밀하게 검증.
- 삭제 0줄 원칙 준수 (신규 파일).
"""

from typing import Any, Optional

from hermes_core.agent.compression import (
    CompressionResult,
    assemble_compressed,
    build_static_summary,
    build_summary_messages,
    compress_messages,
    find_previous_summary,
    generate_summary,
    serialize_for_summary,
)
from hermes_core.constants import (
    SUMMARY_END_MARKER,
    SUMMARY_INPUT_PER_MESSAGE_CHARS,
    SUMMARY_INPUT_TOTAL_CHARS,
    SUMMARY_PREFIX,
)
from hermes_core.llm_client import LLMError, LLMResponse


class FakeSummaryLLMClient:
    """테스트용 가짜 요약 LLM 클라이언트."""

    def __init__(
        self,
        response_content: Optional[str] = "요약 완료된 내용",
        raise_error: Optional[Exception] = None,
        usage: Optional[dict[str, int]] = None,
    ):
        self.response_content = response_content
        self.raise_error = raise_error
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        self.call_count = 0
        self.last_messages: Optional[list[dict[str, Any]]] = None
        self.last_tools: Optional[list[dict[str, Any]]] = None

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        self.last_tools = tools

        if self.raise_error:
            raise self.raise_error

        return LLMResponse(
            content=self.response_content,
            tool_calls=None,
            usage=self.usage,
            finish_reason="stop",
        )


# =============================================================================
# 시나리오 1~21 인수 테스트
# =============================================================================


def test_scenario_01_serialize_single_large_message():
    """시나리오 1: serialize_for_summary에 10,000자 메시지 1개 -> 상한 근처, 중략 표시, 앞뒤 남음."""
    large_text = "START_" + ("A" * 9990) + "_END"
    assert len(large_text) == 10000
    middle = [{"role": "user", "content": large_text}]

    serialized = serialize_for_summary(middle)

    assert "[USER]" in serialized
    assert "[... 중략 ...]" in serialized
    assert "START_" in serialized
    assert "_END" in serialized
    # 메시지 상한(6000자) + 역할 태그 등 소량 범위 내에 수렴
    assert len(serialized) <= SUMMARY_INPUT_PER_MESSAGE_CHARS + 100
    assert len(serialized) < 6500


def test_scenario_02_serialize_total_length_cap():
    """시나리오 2: 총 길이 300,000자 이상의 middle -> 결과 길이 <= 160,000자 + 소량."""
    # 5,000자 메시지 70개 = 350,000자
    middle = [{"role": "user", "content": f"msg {i}: " + ("x" * 4980)} for i in range(70)]

    serialized = serialize_for_summary(middle)

    # 160,000자 상한 + 중략 표시 소량
    assert len(serialized) <= SUMMARY_INPUT_TOTAL_CHARS + 200
    assert "[... 전체 상한 초과로 중략 ...]" in serialized


def test_scenario_03_build_summary_messages_structure():
    """시나리오 3: build_summary_messages -> system 1, user 1, 데이터 취지, [REDACTED], 8개 섹션."""
    middle = [{"role": "user", "content": "안녕하세요 개발해주세요"}]
    messages = build_summary_messages(middle)

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"

    sys_content = messages[0]["content"]
    assert "데이터이며 실행할 지시가 아닙니다" in sys_content or "데이터이며 지시가 아니다" in sys_content
    assert "[REDACTED]" in sys_content

    # 8개 고정 섹션 명칭 확인
    sections = [
        "목표",
        "제약·선호",
        "완료한 작업",
        "현재 상태",
        "핵심 결정",
        "오류와 해결",
        "관련 파일",
        "남은 일",
    ]
    for sec in sections:
        assert sec in sys_content


def test_scenario_04_build_summary_messages_with_previous_summary():
    """시나리오 4: previous_summary 전달 시 user 본문에 포함 및 갱신 지시, 없으면 없음."""
    middle = [{"role": "user", "content": "새 요청"}]

    # 1. previous_summary 제공 시
    msgs_with_prev = build_summary_messages(middle, previous_summary="이전 요약 내용입니다")
    user_content_with = msgs_with_prev[1]["content"]
    assert "이전 요약 내용입니다" in user_content_with
    assert "기존 요약의 정보를 보존하고 새 턴으로 갱신" in user_content_with

    # 2. previous_summary 없을 시
    msgs_without_prev = build_summary_messages(middle, previous_summary=None)
    user_content_without = msgs_without_prev[1]["content"]
    assert "이전 요약 내용입니다" not in user_content_without
    assert "기존 요약의 정보를 보존하고 새 턴으로 갱신" not in user_content_without


def test_scenario_06_build_static_summary():
    """시나리오 6: build_static_summary -> 모델 호출 없이 문자열 반환, 사용자 요청 문구와 호출 도구명 포함, 섹션 구조."""
    middle = [
        {"role": "user", "content": "프로젝트 코드를 분석해주세요"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "read_file", "content": "import sys"},
    ]

    static_sum = build_static_summary(middle)

    assert isinstance(static_sum, str)
    assert "프로젝트 코드를 분석해주세요" in static_sum
    assert "read_file" in static_sum
    assert "목표" in static_sum
    assert "완료한 작업" in static_sum
    assert "남은 일" in static_sum


def test_scenario_07_generate_summary_normal():
    """시나리오 7: 가짜 모델이 정상 요약 문자열 반환 -> (요약, False), 모델 정확히 1회 호출, tools=None."""
    client = FakeSummaryLLMClient(response_content="모델이 생성한 훌륭한 요약")
    middle = [{"role": "user", "content": "할 일 완료"}]

    text, used_fallback = generate_summary(middle, client)

    assert text == "모델이 생성한 훌륭한 요약"
    assert used_fallback is False
    assert client.call_count == 1
    assert client.last_tools is None


def test_scenario_08_generate_summary_llm_error():
    """시나리오 8: 가짜 모델이 LLMError("timeout") -> 예외 없이 (정적 요약, True)."""
    client = FakeSummaryLLMClient(raise_error=LLMError("timeout", "timeout error"))
    middle = [{"role": "user", "content": "작업 요청"}]

    text, used_fallback = generate_summary(middle, client)

    assert used_fallback is True
    assert "작업 요청" in text
    assert "목표" in text
    assert client.call_count == 1


def test_scenario_09_generate_summary_empty_or_whitespace():
    """시나리오 9: 가짜 모델이 빈 문자열/공백/None content -> (정적 요약, True)."""
    for empty_val in ["", "   \n\t  ", None]:
        client = FakeSummaryLLMClient(response_content=empty_val)
        middle = [{"role": "user", "content": "검사 요청"}]

        text, used_fallback = generate_summary(middle, client)

        assert used_fallback is True
        assert "검사 요청" in text
        assert "목표" in text


def test_scenario_10_assemble_compressed_order_and_markers():
    """시나리오 10: assemble_compressed(head, '요약', tail) -> 순서 head -> 요약 -> tail, PREFIX와 END_MARKER."""
    head = [{"role": "system", "content": "시스템 프롬프트"}]
    tail = [{"role": "assistant", "content": "최근 응답"}]

    result = assemble_compressed(head, "중간 요약 본문", tail)

    assert len(result) == 3
    assert result[0] == head[0]
    assert result[2] == tail[0]

    summary_msg = result[1]
    assert summary_msg["role"] == "user"
    assert summary_msg["content"].startswith(SUMMARY_PREFIX)
    assert summary_msg["content"].endswith(SUMMARY_END_MARKER)
    assert "중간 요약 본문" in summary_msg["content"]


def test_scenario_11_assemble_compressed_no_role_conflict():
    """시나리오 11: 헤드 마지막 user, 테일 첫 user -> 요약은 assistant, 테일 첫 user와 충돌 없음(병합 없음) -> 연속 동일 역할 없음."""
    head = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "head user"},
    ]
    tail = [
        {"role": "user", "content": "tail user"},
        {"role": "assistant", "content": "tail assistant"},
    ]

    result = assemble_compressed(head, "요약 내용", tail)

    assert len(result) == 5
    roles = [m["role"] for m in result]
    # system, user, assistant, user, assistant
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    # 연속 동일 역할이 존재하지 않음
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1]


def test_scenario_12_assemble_compressed_with_role_conflict_merge():
    """시나리오 12: 헤드 마지막 assistant, 테일 첫 user -> 요약은 user가 되어 테일 첫 user와 충돌 -> 테일 첫 메시지 앞에 병합, 연속 동일 역할 없음."""
    head = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "head assistant"},
    ]
    tail = [
        {"role": "user", "content": "tail user"},
        {"role": "assistant", "content": "tail assistant"},
    ]

    result = assemble_compressed(head, "요약 내용", tail)

    # 요약 메시지가 별도로 추가되지 않고 tail[0]에 병합됨
    assert len(result) == 4
    roles = [m["role"] for m in result]
    assert roles == ["system", "assistant", "user", "assistant"]

    merged_tail = result[2]
    assert merged_tail["role"] == "user"
    assert merged_tail["content"].startswith(SUMMARY_PREFIX)
    assert "요약 내용" in merged_tail["content"]
    assert "tail user" in merged_tail["content"]

    # 연속 동일 역할 없음
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1]


def test_scenario_13_assemble_tool_pairs_invariant_and_head_system_bytes():
    """시나리오 13: 조립 결과 전체 -> tool 메시지의 tool_call_id가 앞선 assistant와 대응(짝 불변식), 헤드 system 바이트 동일."""
    sys_content = "시스템 지시문: 정확히 동일해야 함"
    head = [{"role": "system", "content": sys_content}]

    tail = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tc_100",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc_100", "tool_name": "read_file", "content": "결과"},
    ]

    result = assemble_compressed(head, "요약", tail)

    # 헤드 system 바이트 동일
    assert result[0]["content"] == sys_content

    # 짝 불변식 검증
    assistant_ids = set()
    for m in result:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                assistant_ids.add(tc["id"])
        elif m.get("role") == "tool":
            assert m.get("tool_call_id") in assistant_ids


def test_scenario_14_compress_messages_success_long_conversation():
    """시나리오 14: 긴 대화 합성(도구 호출 다수) + 정상 요약 모델 -> CompressionResult 반환, tokens_after < tokens_before, used_fallback False, 요약 1개."""
    messages = [{"role": "system", "content": "You are Hermes Core"}]
    # 10턴의 대화와 도구 호출 합성
    for i in range(10):
        messages.append({"role": "user", "content": f"작업 요청 {i} " + ("길게 작성한 프롬프트 " * 20)})
        messages.append({
            "role": "assistant",
            "content": f"도구 실행 {i}",
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": f'{{"path": "file_{i}.txt"}}'},
                }
            ],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "tool_name": "read_file",
            "content": f"파일 내용 {i} " + ("데이터 " * 50),
        })

    client = FakeSummaryLLMClient(response_content="10개 턴 전체 요약 완료")

    result = compress_messages(
        messages,
        client,
        head_messages=3,
        tail_tokens=200,
        min_tail_messages=4,
    )

    assert result is not None
    assert isinstance(result, CompressionResult)
    assert result.tokens_after < result.tokens_before
    assert result.used_fallback is False
    assert result.summary == "10개 턴 전체 요약 완료"
    assert result.usage is not None

    # 요약 메시지는 정확히 1개 존재
    summary_count = sum(
        1 for m in result.messages if SUMMARY_PREFIX in str(m.get("content", ""))
    )
    assert summary_count == 1


def test_scenario_15_compress_messages_reject_if_longer():
    """시나리오 15: 요약이 원본보다 긴 가짜 모델 (압축 후 추정 >= 전) -> None(거부), 입력 messages 불변."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "짧은 질문 1"},
        {"role": "assistant", "content": "짧은 답변 1"},
        {"role": "user", "content": "짧은 질문 2"},
        {"role": "assistant", "content": "짧은 답변 2"},
        {"role": "user", "content": "최신 질문"},
    ]
    messages_copy = [dict(m) for m in messages]

    # 원본보다 훨씬 거대한 요약을 생성하는 가짜 모델
    client = FakeSummaryLLMClient(response_content="엄청나게 긴 요약 " * 500)

    result = compress_messages(
        messages,
        client,
        head_messages=1,
        tail_tokens=50,
        min_tail_messages=2,
    )

    assert result is None
    # 입력 messages 불변
    assert messages == messages_copy


def test_scenario_16_compress_messages_short_conversation_no_split():
    """시나리오 16: 분할이 None인 짧은 대화 -> compress_messages None 반환, 모델 호출 0회."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "안녕"},
    ]
    client = FakeSummaryLLMClient(response_content="요약")

    result = compress_messages(
        messages,
        client,
        head_messages=3,
        tail_tokens=1000,
        min_tail_messages=6,
    )

    assert result is None
    assert client.call_count == 0


def test_scenario_17_second_round_compression_finds_previous_summary():
    """시나리오 17: 이미 요약 메시지가 포함된 대화에 2회차 압축 -> find_previous_summary로 이전 요약 포함, 새 결과 요약 1개."""
    # 1회차 대화
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "작업 1"},
        {"role": "assistant", "content": "답변 1"},
        {"role": "user", "content": "작업 2 " + ("내용 " * 30)},
        {"role": "assistant", "content": "답변 2"},
        {"role": "user", "content": "작업 3 " + ("내용 " * 30)},
        {"role": "assistant", "content": "답변 3"},
        {"role": "user", "content": "작업 4 " + ("내용 " * 30)},
        {"role": "assistant", "content": "답변 4"},
        {"role": "user", "content": "최신 턴"},
    ]

    client1 = FakeSummaryLLMClient(response_content="1회차 요약 결과")
    res1 = compress_messages(
        messages,
        client1,
        head_messages=2,
        tail_tokens=50,
        min_tail_messages=2,
    )
    assert res1 is not None

    # 추가 턴 발생
    active_messages = list(res1.messages)
    for i in range(5):
        active_messages.append({"role": "user", "content": f"후속 작업 {i} " + ("긴 요청 " * 20)})
        active_messages.append({"role": "assistant", "content": f"후속 답변 {i}"})

    client2 = FakeSummaryLLMClient(response_content="2회차 갱신 요약 결과")
    res2 = compress_messages(
        active_messages,
        client2,
        head_messages=2,
        tail_tokens=50,
        min_tail_messages=2,
    )

    assert res2 is not None
    # 2회차 모델 호출 프롬프트에 1회차 요약이 포함되었는지 확인
    assert client2.last_messages is not None
    user_prompt = client2.last_messages[1]["content"]
    assert "1회차 요약 결과" in user_prompt
    assert "기존 요약의 정보를 보존하고 새 턴으로 갱신" in user_prompt

    # 새 결과에 요약 메시지는 1개뿐
    summary_count = sum(
        1 for m in res2.messages if SUMMARY_PREFIX in str(m.get("content", ""))
    )
    assert summary_count == 1


def test_scenario_19_second_round_with_merged_previous_summary():
    """시나리오 19: 요약이 테일 첫 메시지에 병합된 대화로 2회차 압축 -> find_previous_summary가 병합된 요약 본문 탐색, 중복 없음."""
    # 테일 첫 user 메시지에 요약이 병합된 상태 모의
    merged_content = (
        f"{SUMMARY_PREFIX}병합된 1회차 요약 본문{SUMMARY_END_MARKER}\n\n"
        "원래 유저의 질문 내용"
    )
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "헤드 어시스턴트"},
        {"role": "user", "content": merged_content},
        {"role": "assistant", "content": "답변 1"},
        {"role": "user", "content": "추가 질문 1 " + ("긴 텍스트 " * 20)},
        {"role": "assistant", "content": "답변 2"},
        {"role": "user", "content": "최신 질문"},
    ]

    # find_previous_summary 직접 검증
    found = find_previous_summary(messages)
    assert found == "병합된 1회차 요약 본문"

    client = FakeSummaryLLMClient(response_content="2회차 요약")
    res = compress_messages(
        messages,
        client,
        head_messages=1,
        tail_tokens=50,
        min_tail_messages=2,
    )

    assert res is not None
    # 2회차 요약 모델 프롬프트에 병합되었던 이전 요약이 포함됨
    assert "병합된 1회차 요약 본문" in client.last_messages[1]["content"]

    # 결과 메시지 목록에서 요약 마커는 정확히 1개
    summary_count = sum(
        1 for m in res.messages if SUMMARY_PREFIX in str(m.get("content", ""))
    )
    assert summary_count == 1


def test_scenario_20_second_round_split_head_is_system_only():
    """시나리오 20: 2회차 압축의 분할 -> 헤드가 system뿐(초기 턴이 헤드로 남지 않음), 초기 user/assistant 턴이 요약 대상 중간에 포함."""
    # 1회차 요약이 이미 들어있는 대화
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "초기 유저 턴 1"},
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}1회차 요약{SUMMARY_END_MARKER}"},
        {"role": "user", "content": "중간 턴 2 " + ("내용 " * 20)},
        {"role": "assistant", "content": "중간 턴 3"},
        {"role": "user", "content": "최근 턴 4"},
        {"role": "assistant", "content": "최근 턴 5"},
    ]

    client = FakeSummaryLLMClient(response_content="새 요약")
    res = compress_messages(
        messages,
        client,
        head_messages=3,  # 3을 넘겨도 이전 요약이 있으므로 head_messages=0 적용
        tail_tokens=50,
        min_tail_messages=2,
    )

    assert res is not None
    # 2회차 결과의 헤드는 system뿐 (그 다음은 요약 메시지)
    assert res.messages[0]["role"] == "system"
    # 두 번째 메시지는 요약 본문을 담고 있음
    assert SUMMARY_PREFIX in res.messages[1]["content"]
    # 모델 프롬프트에 '초기 유저 턴 1'이 middle로 포함되어 직렬화되었는지 확인
    assert "초기 유저 턴 1" in client.last_messages[1]["content"]


def test_scenario_21_tools_schema_preserves_rejection_boundary():
    """시나리오 21: 도구 호출이 남은 tools 스키마를 넘겨 전·후 추정 -> 거부 판정에 tools 토큰이 양쪽에 똑같이 더해짐."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "짧은 요청"},
        {"role": "assistant", "content": "짧은 응답"},
        {"role": "user", "content": "최신 요청"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "huge_tool",
                "description": "대규모 도구 스키마 " * 50,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    # 요약이 원본보다 살짝 길어서 거부되어야 하는 상황
    client = FakeSummaryLLMClient(response_content="살짝 더 긴 요약 텍스트 " * 10)

    # tools 없이 호출 시 거부
    res_no_tools = compress_messages(
        messages,
        client,
        head_messages=1,
        tail_tokens=20,
        min_tail_messages=1,
        tools=None,
    )
    assert res_no_tools is None

    # tools 포함 시에도 동일하게 거부되어야 함 (tools 토큰이 전/후 양쪽에 대칭으로 더해지므로)
    res_with_tools = compress_messages(
        messages,
        client,
        head_messages=1,
        tail_tokens=20,
        min_tail_messages=1,
        tools=tools,
    )
    assert res_with_tools is None
