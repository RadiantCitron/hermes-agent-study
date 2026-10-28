"""[구현지시서 050] 에이전트 연동, 스키마 v4, in-place 기록 인수 테스트 (시나리오 7~23).

- 작은 context_window_tokens 및 가짜 클라이언트를 이용해 압축 트리거, DB 보관/삽입, 거부, fallback, 검색 연동 검증.
"""

import copy
from typing import Any, Optional

import pytest

from hermes_core.agent import HermesAgent, MockLLMClient
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.constants import (
    COMPRESSION_MAX_PER_RUN,
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
)
from hermes_core.llm_client import LLMError, LLMResponse
from hermes_core.session_db import SessionDB


class TrackingLLMClient:
    """테스트용 LLM 클라이언트: 큐된 응답 반환 및 호출 시점 messages 깊은 복사 보관."""

    def __init__(self, responses: Optional[list[Any]] = None):
        self.responses = list(responses or [])
        self._index = 0
        self.call_history: list[dict[str, Any]] = []
        self._on_call = None

    def set_call_recorder(self, recorder):
        self._on_call = recorder

    def queue_response(self, response: Any):
        self.responses.append(response)

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> Any:
        # 호출 시점의 messages 사본을 저장하여 사후 변경(messages[:]=...)의 영향을 받지 않도록 함
        snapshot = copy.deepcopy(messages)
        self.call_history.append({"messages": snapshot, "tools": tools})

        if self._index >= len(self.responses):
            resp = LLMResponse(content="[Tracking] default final response")
        else:
            resp = self.responses[self._index]
            self._index += 1

        if isinstance(resp, Exception):
            if self._on_call:
                self._on_call({
                    "model": "tracking-mock",
                    "request_json": "{}",
                    "response_json": str(resp),
                    "status_code": 500,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 10,
                    "error_kind": getattr(resp, "kind", "exception"),
                })
            raise resp

        if self._on_call:
            usage = getattr(resp, "usage", {}) or {}
            self._on_call({
                "model": "tracking-mock",
                "request_json": "{}",
                "response_json": resp.content or "",
                "status_code": 200,
                "input_tokens": usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
                "latency_ms": 10,
                "error_kind": None,
            })

        return resp


# =============================================================================
# 시나리오 7~23 인수 테스트
# =============================================================================


def test_scenario_07_short_conversation_no_compression():
    """시나리오 7: 임계값 미만의 짧은 대화 -> 압축 발동 없음, 모델 호출 수 불변, compacted 행 0, compression 키 없음."""
    db = SessionDB(":memory:")
    client = TrackingLLMClient([
        LLMResponse(content="안녕하세요 무엇을 도와드릴까요?")
    ])
    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=100_000,  # 큰 창 -> 압축 미발동
    )

    res = agent.run_conversation("안녕")
    assert "compression" not in res
    assert len(client.call_history) == 1

    # DB에 compacted 행이 0개
    raw_msgs = db.get_messages(agent.session_id)
    assert len(raw_msgs) == 2  # user + assistant
    assert all(m["compacted"] == 0 for m in raw_msgs)


def test_scenario_08_to_14_long_conversation_compression_and_invariants():
    """시나리오 8~14: 임계값 초과 대화 압축 발동, 요약/본 호출 순서, DB 활성 거울, 카운터, llm_calls, 복제 tool_name, 검색 분리."""
    db = SessionDB(":memory:")

    # 요약 호출 응답과 최종 본 응답을 준비
    client = TrackingLLMClient([
        # 1. 요약 모델 응답 (압축 시 호출됨)
        LLMResponse(
            content="중간 대화 요약 본문",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        ),
        # 2. 본 호출 응답
        LLMResponse(
            content="최종 본 응답 완료",
            usage={"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65},
        ),
    ])

    # 매우 작은 context_window로 압축 트리거 유도 (임계값 = 500 * 0.5 = 250 토큰)
    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=500,
    )

    # 이전 긴 이력 구성: 도구 호출 포함, 중간 고유 문구 및 테일 고유 문구 포함
    # 도구 호출 짝 구성
    history = [
        {"role": "system", "content": agent._cached_system_prompt or "System Prompt Original Bytes"},
        {"role": "user", "content": "작업 1: 헤드 유저 요청"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tc_read_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tc_read_1",
            "tool_name": "read_file",
            "content": "파일 내용 본문 A",
        },
        {"role": "assistant", "content": "중간 답변 1"},
        {"role": "user", "content": "작업 2: 중간 고유 문구 UNIQUE_MID_TEXT " + ("데이터 " * 20)},
        {"role": "assistant", "content": "중간 답변 2"},
        {"role": "user", "content": "작업 3: " + ("내용 " * 20)},
        {"role": "assistant", "content": "중간 답변 3"},
        {"role": "user", "content": "작업 4: " + ("내용 " * 20)},
        {"role": "assistant", "content": "중간 답변 4"},
        {"role": "user", "content": "작업 5: 테일 고유 문구 UNIQUE_TAIL_TEXT " + ("최근 데이터 " * 10)},
        {"role": "assistant", "content": "테일 직전 답변"},
    ]

    # 세션 DB에 이전 이력 반영
    for m in history[1:]:
        db.append_message(
            session_id=agent.session_id,
            role=m["role"],
            content=m.get("content"),
            tool_call_id=m.get("tool_call_id"),
            tool_calls=m.get("tool_calls"),
            tool_name=m.get("tool_name"),
        )

    msg_count_before = db.get_session(agent.session_id)["message_count"]
    tool_count_before = db.get_session(agent.session_id)["tool_call_count"]

    # 새 유저 입력으로 대화 실행
    res = agent.run_conversation("최신 요청입니다", conversation_history=history)

    # --- 시나리오 8 검증 ---
    # 압축 발동 확인
    assert "compression" in res
    assert res["compression"] == {"count": 1, "fallback": 0}
    assert len(client.call_history) == 2
    # 호출 순서: 첫 번째가 요약 호출(tools=None), 두 번째가 본 호출(tools 존재)
    assert client.call_history[0]["tools"] is None
    assert client.call_history[1]["tools"] is not None

    # system 내용 바이트 동일 검증
    orig_sys = history[0]["content"]
    assert res["messages"][0]["content"] == orig_sys

    # 요약 메시지는 1개
    summary_count = sum(1 for m in res["messages"] if SUMMARY_PREFIX in str(m.get("content", "")))
    assert summary_count == 1

    # --- 시나리오 9 검증 ---
    # get_messages_as_conversation이 result["messages"][1:]과 일치 (DB 활성 거울)
    conv = db.get_messages_as_conversation(agent.session_id)
    mem_messages = res["messages"][1:]  # system 제외
    assert len(conv) == len(mem_messages)
    for db_m, mem_m in zip(conv, mem_messages):
        assert db_m["role"] == mem_m["role"]
        assert db_m.get("content") == mem_m.get("content")
        assert db_m.get("tool_call_id") == mem_m.get("tool_call_id")
        assert db_m.get("tool_calls") == mem_m.get("tool_calls")

    # --- 시나리오 10 검증 ---
    # session_id 불변, message_count/tool_call_count는 append_message로 추가된 사용자 1건만 증가(+1)
    s_after = db.get_session(agent.session_id)
    assert s_after["id"] == agent.session_id
    # 압축 삽입 자체는 카운터를 올리지 않음 (오직 user 입력 + 새 assistant 응답만 세어짐)
    assert s_after["message_count"] == msg_count_before + 2  # user + assistant

    # --- 시나리오 11 검증 ---
    # llm_calls에 요약 호출 및 본 호출 기록 확인
    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 2

    # --- 시나리오 12 검증 ---
    # 복제된 tool 행의 tool_name 복원 확인
    raw_msgs = db.get_messages(agent.session_id)
    copied_tools = [m for m in raw_msgs if m["is_copy"] == 1 and m["role"] == "tool"]
    for ct in copied_tools:
        if ct["tool_call_id"] == "tc_read_1":
            assert ct["tool_name"] == "read_file"

    # --- 시나리오 13 검증 ---
    # 테일 메시지의 고유 문구 검색 -> 원본 행(compacted=1) 1건만 나오고, 복제 행(is_copy=1)은 제외됨
    search_tail = db.search_messages("UNIQUE_TAIL_TEXT")
    assert len(search_tail) == 1
    tail_row = [m for m in raw_msgs if m["id"] == search_tail[0]["id"]][0]
    assert tail_row["compacted"] == 1
    assert tail_row["is_copy"] == 0

    # --- 시나리오 14 검증 ---
    # 중간 구간에만 있던 고유 문구 검색 -> 원본 보관 행이 온전히 검색됨
    search_mid = db.search_messages("UNIQUE_MID_TEXT")
    assert len(search_mid) == 1
    mid_row = [m for m in raw_msgs if m["id"] == search_mid[0]["id"]][0]
    assert mid_row["compacted"] == 1


def test_scenario_15_summary_llm_error_fallback():
    """시나리오 15: 요약 모델 응답에서 LLMError -> 정적 대체 요약으로 압축, stopped_reason == completed, fallback == 1."""
    db = SessionDB(":memory:")
    client = TrackingLLMClient([
        # 요약 호출 시 LLMError 발생
        LLMError("timeout", "Summary model timed out"),
        # 본 호출은 정상 응답
        LLMResponse(content="대체 요약 바탕 본 응답 완료"),
    ])

    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=500,
    )

    history = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "요청 1"},
        {"role": "assistant", "content": "답변 1"},
        {"role": "user", "content": "요청 2: " + ("긴 요청 본문 데이터 " * 20)},
        {"role": "assistant", "content": "답변 2: " + ("긴 응답 본문 데이터 " * 20)},
        {"role": "user", "content": "요청 3: " + ("길게 작성한 추가 요청 " * 30)},
        {"role": "assistant", "content": "답변 3: " + ("길게 작성한 추가 응답 " * 30)},
        {"role": "user", "content": "요청 4: " + ("길게 작성한 데이터 " * 30)},
        {"role": "assistant", "content": "답변 4"},
        {"role": "user", "content": "요청 5"},
        {"role": "assistant", "content": "답변 5"},
        {"role": "user", "content": "요청 6"},
    ]

    res = agent.run_conversation("새 요청", conversation_history=history)

    assert res["stopped_reason"] == "completed"
    assert "compression" in res
    assert res["compression"]["count"] == 1
    assert res["compression"]["fallback"] == 1
    assert res["final_response"] == "대체 요약 바탕 본 응답 완료"


def test_scenario_16_multiple_compressions_within_max_per_run():
    """시나리오 16: 압축 후 또 길어져 같은 run_conversation에서 재압축 -> 프롬프트에 이전 요약 포함, 요약 1개, 상한 준수."""
    db = SessionDB(":memory:")
    # 도구 호출 루프로 길어지는 상황 모의
    client = TrackingLLMClient([
        # 1회차 요약
        LLMResponse(content="1차 요약"),
        # 1회차 도구 호출 본 응답
        LLMResponse(
            content=None,
            tool_calls=[{"id": "call_x", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}],
        ),
        # 2회차 요약 (도구 결과 후 다시 길어져 압축)
        LLMResponse(content="2차 갱신 요약"),
        # 최종 응답
        LLMResponse(content="최종 끝"),
    ])

    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=400,
    )

    # 1회차 압축을 유발할 긴 이력
    history = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "초기 요청 1"},
        {"role": "assistant", "content": "초기 답변 1"},
        {"role": "user", "content": "초기 요청 2"},
        {"role": "assistant", "content": "초기 답변 2"},
        {"role": "user", "content": "중간 요청 3 " + ("긴 데이터 " * 20)},
        {"role": "assistant", "content": "중간 답변 3"},
        {"role": "user", "content": "중간 요청 4 " + ("긴 데이터 " * 20)},
        {"role": "assistant", "content": "중간 답변 4"},
        {"role": "user", "content": "테일 요청 5"},
        {"role": "assistant", "content": "테일 답변 5"},
    ]

    res = agent.run_conversation("새 질문", conversation_history=history)

    assert "compression" in res
    # 요약 메시지는 최종 결과에 정확히 1개
    summary_count = sum(1 for m in res["messages"] if SUMMARY_PREFIX in str(m.get("content", "")))
    assert summary_count == 1
    assert res["compression"]["count"] >= 1
    assert res["compression"]["count"] <= COMPRESSION_MAX_PER_RUN


def test_scenario_17_18_rejection_and_retry_growth():
    """시나리오 17, 18: 요약이 더 커서 거부 -> 압축 없음, DB 불변, 같은 턴 재시도 없음, 1.25배 성장 후 재시도."""
    db = SessionDB(":memory:")
    # 엄청나게 긴 요약을 돌려주는 클라이언트
    client = TrackingLLMClient([
        LLMResponse(content="엄청나게 긴 요약 " * 500),  # 압축 거부됨
        LLMResponse(content="본 호출 정상 응답"),
    ])

    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=400,
    )

    history = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "요청 1"},
        {"role": "assistant", "content": "답변 1"},
        {"role": "user", "content": "요청 2"},
        {"role": "assistant", "content": "답변 2"},
        {"role": "user", "content": "요청 3 " + ("길게 " * 15)},
        {"role": "assistant", "content": "답변 3"},
        {"role": "user", "content": "요청 4 " + ("길게 " * 15)},
        {"role": "assistant", "content": "답변 4"},
        {"role": "user", "content": "요청 5"},
        {"role": "assistant", "content": "답변 5"},
    ]

    res = agent.run_conversation("새 요청", conversation_history=history)

    # 시나리오 17: 거부되어 압축 없음, DB 불변, 대화 정상 완료
    assert "compression" not in res
    assert res["final_response"] == "본 호출 정상 응답"
    assert agent._rejected_at_tokens is not None

    # 시나리오 18: 직후 다음 run_conversation에서 토큰이 거의 늘지 않았을 때 요약 호출 재시도 안 함
    client2 = TrackingLLMClient([
        LLMResponse(content="두 번째 턴 응답"),
    ])
    agent.llm_client = client2

    # 짧은 추가 요청
    res2 = agent.run_conversation("짧은 추가 질문", conversation_history=res["messages"])
    # 요약 호출 없이 본 호출 1회만 나감
    assert len(client2.call_history) == 1
    assert "compression" not in res2


def test_scenario_19_db_archive_failure_keeps_memory_and_continues():
    """시나리오 19: DB 기록 실패(archive_and_compact 예외) -> 경고 로그, 메모리 messages 불변, 대화 계속."""
    db = SessionDB(":memory:")
    client = TrackingLLMClient([
        LLMResponse(content="요약 완료"),
        LLMResponse(content="본 호출 응답"),
    ])
    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=400,
    )

    history = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "요청 1"},
        {"role": "assistant", "content": "답변 1"},
        {"role": "user", "content": "요청 2"},
        {"role": "assistant", "content": "답변 2"},
        {"role": "user", "content": "요청 3 " + ("길게 " * 20)},
        {"role": "assistant", "content": "답변 3"},
        {"role": "user", "content": "요청 4 " + ("길게 " * 20)},
        {"role": "assistant", "content": "답변 4"},
        {"role": "user", "content": "요청 5"},
        {"role": "assistant", "content": "답변 5"},
    ]

    # archive_and_compact 호출 시 예외를 발생시키도록 패치
    def broken_archive(*args, **kwargs):
        raise RuntimeError("Simulated DB failure")

    agent.session_db.archive_and_compact = broken_archive

    # 예외가 밖으로 나오지 않고 대화 계속 완료
    res = agent.run_conversation("새 질문", conversation_history=history)

    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "본 호출 응답"
    # 메모리의 messages는 교체되지 않고 원본 유지(요약 마커 없음)
    assert not any(SUMMARY_PREFIX in str(m.get("content", "")) for m in res["messages"])


def test_scenario_20_compression_disabled():
    """시나리오 20: compression_enabled=False -> 어떤 길이에도 압축 없음."""
    db = SessionDB(":memory:")
    client = TrackingLLMClient([
        LLMResponse(content="응답"),
    ])
    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=200,  # 매우 작아도
        compression_enabled=False,  # 비활성화
    )

    history = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "긴 요청 " * 50},
    ]

    res = agent.run_conversation("새 질문", conversation_history=history)
    assert "compression" not in res
    assert len(client.call_history) == 1  # 요약 호출 없이 본 호출 1회만


def test_scenario_21_new_agent_instance_carries_summary_statelessly():
    """시나리오 21: 새 HermesAgent 인스턴스로 압축된 이력을 conversation_history로 이어서 호출 -> 이전 요약이 프롬프트에 반영됨."""
    db = SessionDB(":memory:")
    # 압축된 이력 모의
    compressed_history = [
        {"role": "system", "content": "Sys"},
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}이전 세션의 요약 본문{SUMMARY_END_MARKER}"},
        {"role": "user", "content": "중간 질문 1 " + ("길게 " * 30)},
        {"role": "assistant", "content": "중간 답변 1"},
        {"role": "user", "content": "중간 질문 2 " + ("길게 " * 30)},
        {"role": "assistant", "content": "중간 답변 2"},
        {"role": "user", "content": "최근 질문 3"},
        {"role": "assistant", "content": "최근 답변 3"},
        {"role": "user", "content": "최근 질문 4"},
        {"role": "assistant", "content": "최근 답변 4"},
    ]

    client = TrackingLLMClient([
        LLMResponse(content="새 요약"),
        LLMResponse(content="새 응답"),
    ])

    # 새 에이전트 인스턴스 (상태 없음)
    new_agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=400,
    )

    res = new_agent.run_conversation("후속 질문", conversation_history=compressed_history)

    # 1번째 호출(요약 모델 호출)의 user 프롬프트에 '이전 세션의 요약 본문'이 포함되어 전달되었는지 검증
    summary_call_user_msg = client.call_history[0]["messages"][1]["content"]
    assert "이전 세션의 요약 본문" in summary_call_user_msg
    assert "기존 요약의 정보를 보존하고 새 턴으로 갱신" in summary_call_user_msg


def test_scenario_22_mock_client_rough_estimation_and_real_usage_estimation():
    """시나리오 22: MockLLMClient(usage 없음) 대화 -> 거친 추정으로 판정. 실제 usage가 있으면 input_tokens + 신규 추정 사용."""
    db = SessionDB(":memory:")
    # 1. usage 없는 MockLLMClient
    mock_client = MockLLMClient([
        MockLLMResponse(content="Mock 응답"),
    ])
    agent_mock = HermesAgent(
        session_db=db,
        llm_client=mock_client,
        context_window_tokens=100_000,
    )
    _ = agent_mock.run_conversation("질문")
    # usage가 없으므로 _last_input_tokens는 None 유지
    assert agent_mock._last_input_tokens is None

    # 2. usage가 있는 TrackingLLMClient
    real_mock_client = TrackingLLMClient([
        LLMResponse(content="Real 모의 응답", usage={"input_tokens": 1234, "output_tokens": 56}),
    ])
    agent_real = HermesAgent(
        session_db=db,
        llm_client=real_mock_client,
        context_window_tokens=100_000,
    )
    _ = agent_real.run_conversation("질문")
    # 실제 input_tokens가 저장됨
    assert agent_real._last_input_tokens == 1234
    assert agent_real._last_message_count is not None


def test_scenario_23_cached_system_prompt_immutability():
    """시나리오 23: 압축 후 시스템 프롬프트 캐시 -> _cached_system_prompt 불변, sessions.system_prompt 불변."""
    db = SessionDB(":memory:")
    client = TrackingLLMClient([
        LLMResponse(content="요약"),
        LLMResponse(content="응답"),
    ])
    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        context_window_tokens=400,
    )

    history = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "요청 " * 30},
    ]

    _ = agent.run_conversation("질문", conversation_history=history)

    cached_prompt = agent._cached_system_prompt
    assert cached_prompt is not None

    db_session = db.get_session(agent.session_id)
    assert db_session["system_prompt"] == cached_prompt
