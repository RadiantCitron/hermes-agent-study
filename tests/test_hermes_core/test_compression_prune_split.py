"""도구 결과 가지치기, 경계 산정, 도구 쌍 정합 순수 함수 단위 테스트 (로드맵 05 / 구현지시서 030 인수 시나리오)."""

import copy

from hermes_core.agent.compression import (
    prune_old_tool_results,
    sanitize_tool_pairs,
    split_for_compression,
)


def _make_tool_call(call_id: str, name: str = "read_file", args: str = '{"path": "a.txt"}') -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": args},
    }


def test_030_scenario_01_prune_old_tool_results():
    """시나리오 1: keep_from 앞쪽 T 3개는 표지로 치환, 뒤쪽 T 1개는 원문 유지, tool_call_id·tool_name 보존."""
    messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c1", "tool_a")]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "tool_a", "content": "x" * 1000},
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c2", "tool_b")]},
        {"role": "tool", "tool_call_id": "c2", "tool_name": "tool_b", "content": "y" * 1000},
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c3", "tool_c")]},
        {"role": "tool", "tool_call_id": "c3", "tool_name": "tool_c", "content": "z" * 1000},
        # 여기서부터 keep_from = 7
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c4", "tool_d")]},
        {"role": "tool", "tool_call_id": "c4", "tool_name": "tool_d", "content": "keep_this_exact_content"},
    ]

    pruned = prune_old_tool_results(messages, keep_from=7)

    # 앞쪽 T 3개 확인
    assert pruned[2]["content"] == "[Old tool output cleared: tool_a, 1000자]"
    assert pruned[2]["tool_call_id"] == "c1"
    assert pruned[2]["tool_name"] == "tool_a"

    assert pruned[4]["content"] == "[Old tool output cleared: tool_b, 1000자]"
    assert pruned[4]["tool_call_id"] == "c2"

    assert pruned[6]["content"] == "[Old tool output cleared: tool_c, 1000자]"
    assert pruned[6]["tool_call_id"] == "c3"

    # 뒤쪽 T 확인
    assert pruned[9]["content"] == "keep_this_exact_content"
    assert pruned[9]["tool_call_id"] == "c4"


def test_030_scenario_02_prune_idempotence():
    """시나리오 2: prune_old_tool_results를 2회 연속 적용해도 결과 동일 (멱등성)."""
    messages = [
        {"role": "tool", "tool_call_id": "c1", "tool_name": "my_tool", "content": "sample content 12345"},
    ]
    first = prune_old_tool_results(messages, keep_from=5)
    second = prune_old_tool_results(first, keep_from=5)
    assert first == second
    assert second[0]["content"] == "[Old tool output cleared: my_tool, 20자]"


def test_030_scenario_03_no_mutation_side_effects():
    """시나리오 3: 입력 messages와 그 안의 딕셔너리가 함수 호출 전후로 전혀 변경되지 않음."""
    original = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "tool_x", "content": "1234567890"},
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c2")]},
    ]
    cloned = copy.deepcopy(original)

    _ = prune_old_tool_results(original, keep_from=2)
    assert original == cloned

    _ = split_for_compression(original, head_messages=1, tail_tokens=1000, min_tail_messages=1)
    assert original == cloned

    _ = sanitize_tool_pairs(original)
    assert original == cloned


def test_030_scenario_04_split_standard_conversation():
    """시나리오 4: [S,U,A,U,A,U,A,U,A,U,A,U] 분할 시 헤드(S+3개), 테일(최소4개+최근U), head+middle+tail == 원본."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
        {"role": "user", "content": "u5"},
        {"role": "assistant", "content": "a5"},
        {"role": "user", "content": "u6"},
    ]

    # 최소 4개(약 20토큰)보다 넉넉한 30토큰 예산 -> 테일에 5개 이상 포함되면서도 중간이 남음
    res = split_for_compression(messages, head_messages=3, tail_tokens=30, min_tail_messages=4)
    assert res is not None
    head, middle, tail = res

    # 헤드: S + 앞 3개
    assert len(head) == 4
    assert head[0]["role"] == "system"
    assert [m["content"] for m in head[1:]] == ["u1", "a1", "u2"]

    # 테일: 최소 4개 이상이며 마지막 U 포함
    assert len(tail) >= 4
    assert tail[-1]["role"] == "user"
    assert tail[-1]["content"] == "u6"

    # 중간 존재 및 합체 시 원본과 일치
    assert len(middle) > 0
    assert head + middle + tail == messages


def test_030_scenario_05_split_too_short_returns_none():
    """시나리오 5: 길이가 헤드+최소 테일 이하인 짧은 대화는 None 반환."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    res = split_for_compression(messages, head_messages=3, tail_tokens=1000, min_tail_messages=4)
    assert res is None


def test_030_scenario_06_head_boundary_includes_tool_results():
    """시나리오 6: 헤드 경계 끝이 A*(tool_calls)이고 뒤에 T가 오면 T까지 헤드에 포함."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        # 3번째(head_messages=3에 해당): A*
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c1")]},
        # 4번째: T (헤드가 T까지 흡수해야 함)
        {"role": "tool", "tool_call_id": "c1", "content": "res1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]
    # tail_tokens=20으로 뒤쪽 3개 정도만 테일로
    res = split_for_compression(messages, head_messages=3, tail_tokens=20, min_tail_messages=3)
    assert res is not None
    head, middle, tail = res

    # 헤드에 system + u1 + a1 + assistant(tool_calls) + tool(c1) 총 5개 포함
    assert len(head) == 5
    assert head[-1]["role"] == "tool"
    assert head[-1]["tool_call_id"] == "c1"


def test_030_scenario_07_tail_boundary_includes_parent_assistant():
    """시나리오 7: 테일 후보의 첫 메시지가 T이면 부모 A*까지 테일에 포함."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        # 부모 A*
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c2")]},
        # T (만약 테일이 여기서 시작하려 하면 부모 A*까지 테일에 포함)
        {"role": "tool", "tool_call_id": "c2", "content": "res2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    # min_tail_messages = 3으로 설정하면 뒤 3개는 [T, U3, A3] -> T가 시작점
    res = split_for_compression(messages, head_messages=2, tail_tokens=10, min_tail_messages=3)
    assert res is not None
    head, middle, tail = res

    # tail의 첫 메시지는 T가 아니라 부모 A*여야 함
    assert tail[0]["role"] == "assistant"
    assert tail[0].get("tool_calls")[0]["id"] == "c2"
    assert tail[1]["role"] == "tool"
    assert tail[1]["tool_call_id"] == "c2"


def test_030_scenario_08_tail_always_includes_latest_user_message():
    """시나리오 8: 가장 최근 U 뒤에 긴 A*/T가 연속되어도 테일은 최근 U를 반드시 포함."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u_important"},  # 최근 U
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c3")]},
        {"role": "tool", "tool_call_id": "c3", "content": "x" * 2000},
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c4")]},
        {"role": "tool", "tool_call_id": "c4", "content": "y" * 2000},
        {"role": "assistant", "content": "final_a"},
    ]
    # tail_tokens를 매우 작게(10토큰) 주더라도 최근 user를 포함해야 함
    res = split_for_compression(messages, head_messages=1, tail_tokens=10, min_tail_messages=2)
    assert res is not None
    head, middle, tail = res

    user_msgs_in_tail = [m for m in tail if m.get("role") == "user"]
    assert len(user_msgs_in_tail) >= 1
    assert any(m.get("content") == "u_important" for m in user_msgs_in_tail)


def test_030_scenario_09_tool_pair_invariants_across_all_splits():
    """시나리오 9: 분할된 head, middle, tail 각각에 존재하는 tool은 동일 세그먼트 내에 부모 tool_calls가 존재."""
    # 시나리오 4, 6, 7, 8의 분할 결과에 대해 불변식 검증
    for res in [
        split_for_compression(
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c1")]},
                {"role": "tool", "tool_call_id": "c1", "content": "r1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("c2")]},
                {"role": "tool", "tool_call_id": "c2", "content": "r2"},
                {"role": "user", "content": "u3"},
                {"role": "assistant", "content": "a3"},
            ],
            head_messages=3,
            tail_tokens=15,
            min_tail_messages=2,
        )
    ]:
        assert res is not None
        head, middle, tail = res
        for segment in (head, middle, tail):
            called_ids = {
                tc["id"]
                for m in segment
                if m.get("role") == "assistant" and m.get("tool_calls")
                for tc in m["tool_calls"]
            }
            for m in segment:
                if m.get("role") == "tool":
                    assert m["tool_call_id"] in called_ids


def test_030_scenario_10_sanitize_tool_pairs_removes_unpaired():
    """시나리오 10: 중간이 제거되어 짝이 끊긴 head+tail을 sanitize_tool_pairs에 넣으면 고립된 tool과 결과 없는 tool_calls가 정리됨."""
    head_plus_tail = [
        {"role": "system", "content": "sys"},
        # 결과 없는 assistant 호출
        {"role": "assistant", "content": "msg", "tool_calls": [_make_tool_call("orphan_call")]},
        # 호출 없는 고립된 tool 결과
        {"role": "tool", "tool_call_id": "orphan_result", "content": "res"},
        # 유효한 짝
        {"role": "assistant", "content": None, "tool_calls": [_make_tool_call("valid_call")]},
        {"role": "tool", "tool_call_id": "valid_call", "content": "valid_res"},
    ]

    sanitized = sanitize_tool_pairs(head_plus_tail)

    # orphan_result 제거 확인
    assert not any(m.get("tool_call_id") == "orphan_result" for m in sanitized)

    # orphan_call 제거 확인 (assistant는 content 'msg'만 남고 tool_calls는 없음)
    first_a = [m for m in sanitized if m.get("role") == "assistant" and m.get("content") == "msg"][0]
    assert "tool_calls" not in first_a

    # valid 짝은 온전히 유지
    valid_a = [m for m in sanitized if m.get("tool_calls")][0]
    assert valid_a["tool_calls"][0]["id"] == "valid_call"
    valid_t = [m for m in sanitized if m.get("role") == "tool"][0]
    assert valid_t["tool_call_id"] == "valid_call"


def test_030_scenario_11_sanitize_partial_tool_calls_cleanup():
    """시나리오 11: A*의 두 호출 중 하나만 결과가 있는 경우 결과 없는 호출 항목만 제거되고 유효 짝은 유지."""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_make_tool_call("call_kept"), _make_tool_call("call_dropped")],
        },
        {"role": "tool", "tool_call_id": "call_kept", "content": "ok"},
    ]
    sanitized = sanitize_tool_pairs(messages)
    assert len(sanitized) == 2
    a_msg = sanitized[0]
    assert len(a_msg["tool_calls"]) == 1
    assert a_msg["tool_calls"][0]["id"] == "call_kept"


def test_030_scenario_12_sanitize_all_tool_calls_dropped_with_none_content():
    """시나리오 12: A*의 모든 호출이 제거되고 content가 None인 경우 content='(tool call removed)'로 치환."""
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_make_tool_call("no_result_call")],
        },
        {"role": "user", "content": "next"},
    ]
    sanitized = sanitize_tool_pairs(messages)
    assert len(sanitized) == 2
    assert sanitized[0]["content"] == "(tool call removed)"
    assert "tool_calls" not in sanitized[0]


def test_030_scenario_13_sanitize_preserves_last_in_progress_tool_call():
    """시나리오 13: 마지막 메시지가 결과 없는 A*(진행 중)인 경우 그대로 보존."""
    messages = [
        {"role": "user", "content": "run it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_make_tool_call("in_progress_call")],
        },
    ]
    sanitized = sanitize_tool_pairs(messages)
    assert len(sanitized) == 2
    assert sanitized[1]["tool_calls"][0]["id"] == "in_progress_call"


def test_030_scenario_14_split_without_system_message():
    """시나리오 14: S가 없는 입력(이력만)에서도 오류 없이 분할, 헤드는 앞 head_messages개."""
    messages = [
        {"role": "user", "content": f"u{i}"} for i in range(12)
    ]
    res = split_for_compression(messages, head_messages=3, tail_tokens=20, min_tail_messages=3)
    assert res is not None
    head, middle, tail = res
    assert len(head) == 3
    assert [m["content"] for m in head] == ["u0", "u1", "u2"]
    assert head + middle + tail == messages


def test_030_scenario_16_split_with_head_messages_zero():
    """시나리오 16: head_messages=0으로 분할 시 헤드는 S뿐(S가 없으면 빈 헤드), 나머지가 중간+테일."""
    messages_with_sys = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    res = split_for_compression(messages_with_sys, head_messages=0, tail_tokens=15, min_tail_messages=2)
    assert res is not None
    head, middle, tail = res
    assert len(head) == 1
    assert head[0]["role"] == "system"
    assert head + middle + tail == messages_with_sys

    # S가 없는 경우
    messages_without_sys = [
        {"role": "user", "content": f"u{i}"} for i in range(10)
    ]
    res_no_sys = split_for_compression(messages_without_sys, head_messages=0, tail_tokens=15, min_tail_messages=2)
    assert res_no_sys is not None
    head_ns, middle_ns, tail_ns = res_no_sys
    assert len(head_ns) == 0
    assert head_ns + middle_ns + tail_ns == messages_without_sys


def test_030_scenario_17_tiny_tail_tokens_respects_min_tail_and_latest_user():
    """시나리오 17: tail_tokens를 1로 매우 작게 주어도 테일은 최소 min_tail_messages=4개 + 가장 최근 U를 포함."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "latest_user"},
        {"role": "assistant", "content": "final_a"},
    ]
    res = split_for_compression(messages, head_messages=2, tail_tokens=1, min_tail_messages=4)
    assert res is not None
    head, middle, tail = res

    assert len(tail) >= 4
    assert any(m.get("content") == "latest_user" for m in tail)


def test_030_scenario_18_all_messages_fit_in_tail_returns_none():
    """시나리오 18: 모든 메시지가 테일 예산 안에 들어가는 대화(중간 없음)는 None 반환."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    # tail_tokens가 1,000,000으로 모든 메시지를 수용하면 middle이 없으므로 None
    res = split_for_compression(messages, head_messages=2, tail_tokens=1_000_000, min_tail_messages=2)
    assert res is None
