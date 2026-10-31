"""루프 정체 감지기 (LoopGuard) 인수 테스트 (17 로드맵 020 단계).

[검증 범위]
- 시나리오 1: 키 순서가 다른 인자 딕셔너리의 지문 동일성
- 시나리오 2: 파싱 실패(깨진 JSON) 인자의 공백 정규화 지문 동일성
- 시나리오 3: 순수 LoopGuard 카운터 임계값 도달 시 반환 (3회 warn, 5회 block)
- 시나리오 4: 다른 도구/인자 호출 시 카운터 초기화
- 시나리오 5: LoopGuard 생성자 인자 검증 (ValueError)
- 시나리오 6: attach_loop_warning JSON 및 일반 텍스트 안전 부착
- 시나리오 7: 경고 후 자가 교정 성공 (iterations == 5, _loop_warning 3번째 포함)
- 시나리오 8: 5회째 조기 정지 (stuck_loop, final_response == "", 핸들러 4회)
- 시나리오 9: 도구 쌍 및 DB 원장 정합성 (tool_call_id 1:1, 메모리 및 DB 일치)
- 시나리오 10: 배치 내 차단 (tc1 차단 시 tc2 핸들러 미실행 및 차단 JSON)
- 시나리오 11: 턴 간 독립성 (새 턴마다 턴 칸 초기화)
- 시나리오 12: CLI 안내 출력 (CLI_STUCK_LOOP_NOTICE 출력, Assistant: 생략)
- 시나리오 13: 깨진 JSON 반복 호출 시 단일 입구 감지 및 차단
- 시나리오 14: 디스패처 공유 시 중복 계수 방지 (소유권 검증)
- 시나리오 15: 위임 자식 에이전트 내 루프 정체 차단 (부모 completed, 자식 stuck_loop)
- 시나리오 16: 설치 기록 및 GUARD 단계 순서 (POLICY 미들웨어 4회 실행)
- 시나리오 17: 턴 밖 직접 호출(handle_function_call) 시 카운터 무영향
- 시나리오 18: EVENT_LOOP_GUARD 이벤트 발행 순서 및 데이터
"""

import io
import json
from pathlib import Path
import pytest

from hermes_core import cli
from hermes_core.agent import HermesAgent, MockLLMClient
from hermes_core.agent.events import EVENT_LOOP_GUARD, AgentEvent
from hermes_core.agent.loop_guard import (
    LoopGuard,
    LoopGuardExtension,
    attach_loop_warning,
    make_fingerprint,
)
from hermes_core.constants import (
    CLI_STUCK_LOOP_NOTICE,
    LOOP_GUARD_BLOCKED_MESSAGE,
    LOOP_GUARD_WARNING_MESSAGE,
)
from hermes_core.extensions.contract import Extension, ExtensionHost, MiddlewarePhase
from hermes_core.session_db import SessionDB
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher, create_default_dispatcher


def test_scenario_01_make_fingerprint_dict_key_order():
    """시나리오 1: 키 순서만 다른 딕셔너리의 지문 동일성 검증."""
    fp1 = make_fingerprint("patch", {"path": "a.py", "old_string": "x"})
    fp2 = make_fingerprint("patch", {"old_string": "x", "path": "a.py"})
    assert fp1 == fp2
    assert fp1 == ("patch", '{"old_string":"x","path":"a.py"}')


def test_scenario_02_make_fingerprint_invalid_json():
    """시나리오 2: 파싱 실패 인자의 공백 정규화 지문 검증."""
    fp1 = make_fingerprint("patch", None, "{bad json")
    fp2 = make_fingerprint("patch", None, "  {bad json  ")
    assert fp1 == fp2
    assert fp1 == ("patch", "<invalid_json:{bad json>")


def test_scenario_03_loop_guard_counter_thresholds():
    """시나리오 3: LoopGuard에 같은 인자 5회 record 시 ok, warn, block 전이 검증."""
    guard = LoopGuard(warn_threshold=3, block_threshold=5)
    r1 = guard.record("patch", {"path": "a.py"})
    r2 = guard.record("patch", {"path": "a.py"})
    r3 = guard.record("patch", {"path": "a.py"})
    r4 = guard.record("patch", {"path": "a.py"})
    r5 = guard.record("patch", {"path": "a.py"})

    assert r1 == ("ok", 1)
    assert r2 == ("ok", 2)
    assert r3 == ("warn", 3)
    assert r4 == ("warn", 4)
    assert r5 == ("block", 5)


def test_scenario_04_loop_guard_counter_reset_on_change():
    """시나리오 4: 다른 도구 또는 다른 인자 호출 시 카운터 초기화 검증."""
    guard = LoopGuard(warn_threshold=3, block_threshold=5)
    assert guard.record("patch", {"path": "a.py"}) == ("ok", 1)
    assert guard.record("patch", {"path": "a.py"}) == ("ok", 2)
    # 다른 인자
    assert guard.record("patch", {"path": "b.py"}) == ("ok", 1)
    # 원래 인자로 복귀
    assert guard.record("patch", {"path": "a.py"}) == ("ok", 1)
    assert guard.record("patch", {"path": "a.py"}) == ("ok", 2)


def test_scenario_05_loop_guard_invalid_thresholds():
    """시나리오 5: 임계값 유효성 검사 (ValueError)."""
    with pytest.raises(ValueError, match="warn_threshold must be an integer >= 2"):
        LoopGuard(warn_threshold=1, block_threshold=3)

    with pytest.raises(ValueError, match="block_threshold must be an integer > warn_threshold"):
        LoopGuard(warn_threshold=4, block_threshold=4)

    with pytest.raises(ValueError, match="warn_threshold must be an integer >= 2"):
        LoopGuard(warn_threshold=True, block_threshold=5)


def test_scenario_06_attach_loop_warning_json_and_plain():
    """시나리오 6: attach_loop_warning의 JSON 및 일반 텍스트 안전성 검증."""
    res_json = attach_loop_warning('{"success": false, "error": "not found"}', "warn!")
    data = json.loads(res_json)
    assert data["success"] is False
    assert data["error"] == "not found"
    assert data["_loop_warning"] == "warn!"

    res_plain = attach_loop_warning("plain text", "warn!")
    assert res_plain == "plain text\nwarn!"


def test_scenario_07_warn_and_self_correction(tmp_path: Path):
    """시나리오 7: 경고 후 자가 교정 성공 (iterations == 5, _loop_warning 3번째 포함)."""
    client = MockLLMClient()
    # 1, 2, 3회: 같은 인자 (2회째는 키 순서 바꿈)
    client.queue_response(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1, "y": 2}'}}])
    client.queue_response(tool_calls=[{"id": "c2", "function": {"name": "echo", "arguments": '{"y": 2, "x": 1}'}}])
    client.queue_response(tool_calls=[{"id": "c3", "function": {"name": "echo", "arguments": '{"x": 1, "y": 2}'}}])
    # 4회: 다른 인자로 자가 교정
    client.queue_response(tool_calls=[{"id": "c4", "function": {"name": "echo", "arguments": '{"x": 99, "y": 2}'}}])
    # 5회: 최종 텍스트 응답
    client.queue_response(content="done")

    disp = ToolDispatcher()
    disp.register_tool("echo", lambda x, y: json.dumps({"echo": [x, y]}), {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=client, quiet_mode=True)

    res = agent.run_conversation("start")
    assert res["stopped_reason"] == "completed"
    assert res["iterations"] == 5
    assert res["final_response"] == "done"

    # 4번째 도구 호출 직후(즉 3번째 같은 호출이 일어난 뒤 4번째 다른 인자 호출 시)
    # messages 히스토리 상에서 3번째 tool 메시지에 _loop_warning 포함 확인
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 4

    t1 = json.loads(tool_msgs[0]["content"])
    t2 = json.loads(tool_msgs[1]["content"])
    t3 = json.loads(tool_msgs[2]["content"])
    t4 = json.loads(tool_msgs[3]["content"])

    assert "_loop_warning" not in t1
    assert "_loop_warning" not in t2
    assert "_loop_warning" in t3
    assert "consecutively with identical arguments" in t3["_loop_warning"]
    assert "_loop_warning" not in t4


def test_scenario_08_block_on_fifth_repetition(tmp_path: Path):
    """시나리오 8: 5회째 조기 정지 (stuck_loop, final_response == "", 핸들러 4회)."""
    client = MockLLMClient()
    for i in range(10):
        client.queue_response(
            tool_calls=[{"id": f"c_{i}", "function": {"name": "count_tool", "arguments": '{"k": "same"}'}}]
        )

    handler_calls = 0

    def _handler(k: str):
        nonlocal handler_calls
        handler_calls += 1
        return json.dumps({"status": "ok", "k": k})

    disp = ToolDispatcher()
    disp.register_tool("count_tool", _handler, {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(
        session_db=db,
        dispatcher=disp,
        llm_client=client,
        max_iterations=50,
        quiet_mode=True,
    )

    res = agent.run_conversation("test loop")
    assert res["stopped_reason"] == "stuck_loop"
    assert res["iterations"] == 5
    assert res["final_response"] == ""
    assert handler_calls == 4

    fifth_tool_msg = res["messages"][-1]
    assert fifth_tool_msg["role"] == "tool"
    assert fifth_tool_msg["tool_call_id"] == "c_4"
    fifth_data = json.loads(fifth_tool_msg["content"])
    assert fifth_data["success"] is False
    assert "Loop guard blocked: tool '" in fifth_data["error"]


def test_scenario_09_tool_pair_and_session_db_integrity(tmp_path: Path):
    """시나리오 9: 도구 쌍 및 DB 원장 정합성 (tool_call_id 1:1, 메모리 및 DB 일치)."""
    client = MockLLMClient()
    for i in range(10):
        client.queue_response(
            tool_calls=[{"id": f"call_{i}", "function": {"name": "echo", "arguments": '{"v": 1}'}}]
        )

    disp = ToolDispatcher()
    disp.register_tool("echo", lambda v: json.dumps({"v": v}), {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=client, quiet_mode=True)

    res = agent.run_conversation("go")
    assert res["stopped_reason"] == "stuck_loop"

    messages = res["messages"]
    # assistant tool_calls와 tool 메시지의 1:1 매칭 확인
    assistant_indices = [idx for idx, m in enumerate(messages) if m.get("role") == "assistant"]
    for a_idx in assistant_indices:
        tcs = messages[a_idx].get("tool_calls", [])
        for offset, tc in enumerate(tcs, start=1):
            tool_msg = messages[a_idx + offset]
            assert tool_msg["role"] == "tool"
            assert tool_msg["tool_call_id"] == tc["id"]

    # DB 메시지와 메모리 메시지 정합성 확인 (system 프롬프트 제외)
    db_messages = db.get_messages_as_conversation(agent.session_id)
    memory_messages = messages[1:]  # system 메시지 제외

    assert len(db_messages) == len(memory_messages)
    for m_db, m_mem in zip(db_messages, memory_messages):
        assert m_db["role"] == m_mem["role"]
        assert m_db.get("tool_call_id") == m_mem.get("tool_call_id")
        assert m_db.get("content") == m_mem.get("content")


def test_scenario_10_blocked_within_batch(tmp_path: Path):
    """시나리오 10: 배치 내 차단 (tc1 차단 시 tc2 핸들러 미실행 및 차단 JSON)."""
    client = MockLLMClient()
    # 1~4회: 단일 호출
    for i in range(4):
        client.queue_response(
            tool_calls=[{"id": f"c_{i}", "function": {"name": "main_tool", "arguments": '{"x": 1}'}}]
        )
    # 5회: 배치 호출 [tc1(같은 호출), tc2(다른 도구)]
    client.queue_response(
        tool_calls=[
            {"id": "c_5_1", "function": {"name": "main_tool", "arguments": '{"x": 1}'}},
            {"id": "c_5_2", "function": {"name": "other_tool", "arguments": '{"y": 2}'}},
        ]
    )

    other_calls = 0

    def _other(y: int):
        nonlocal other_calls
        other_calls += 1
        return "other ok"

    disp = ToolDispatcher()
    disp.register_tool("main_tool", lambda x: "main ok", {})
    disp.register_tool("other_tool", _other, {})

    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=client, quiet_mode=True)

    res = agent.run_conversation("batch test")
    assert res["stopped_reason"] == "stuck_loop"
    assert other_calls == 0  # tc2 실행 안 됨

    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 6  # 4개 + 배치 2개
    t5_1 = json.loads(tool_msgs[4]["content"])
    t5_2 = json.loads(tool_msgs[5]["content"])

    assert t5_1["success"] is False
    assert "Loop guard blocked: tool 'main_tool'" in t5_1["error"]
    assert t5_2["success"] is False
    assert "Loop guard blocked: tool 'other_tool'" in t5_2["error"]


def test_scenario_11_turn_independence(tmp_path: Path):
    """시나리오 11: 턴 간 독립성 (새 턴마다 턴 칸 초기화)."""
    client = MockLLMClient()
    # 턴 1: 같은 호출 2회 후 완료
    client.queue_response(tool_calls=[{"id": "t1_c1", "function": {"name": "echo", "arguments": '{"a": 1}'}}])
    client.queue_response(tool_calls=[{"id": "t1_c2", "function": {"name": "echo", "arguments": '{"a": 1}'}}])
    client.queue_response(content="turn 1 done")

    # 턴 2: 같은 호출 2회 후 완료
    client.queue_response(tool_calls=[{"id": "t2_c1", "function": {"name": "echo", "arguments": '{"a": 1}'}}])
    client.queue_response(tool_calls=[{"id": "t2_c2", "function": {"name": "echo", "arguments": '{"a": 1}'}}])
    client.queue_response(content="turn 2 done")

    disp = ToolDispatcher()
    disp.register_tool("echo", lambda a: json.dumps({"echo": a}), {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=client, quiet_mode=True)

    # 턴 1 실행
    res1 = agent.run_conversation("turn 1")
    assert res1["stopped_reason"] == "completed"

    # 턴 2 실행
    res2 = agent.run_conversation("turn 2")
    assert res2["stopped_reason"] == "completed"

    # 턴 2 메시지 검사
    turn2_tools = [
        json.loads(m["content"])
        for m in res2["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id", "").startswith("t2_")
    ]
    assert len(turn2_tools) == 2
    assert "_loop_warning" not in turn2_tools[0]
    assert "_loop_warning" not in turn2_tools[1]


def test_scenario_12_cli_stuck_loop_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """시나리오 12: CLI 안내 출력 (CLI_STUCK_LOOP_NOTICE 출력, Assistant: 생략)."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    class ScriptableClient:
        created_instances = []
        def __init__(self, **kwargs):
            ScriptableClient.created_instances.append(self)
        def chat_completion(self, messages, tools=None):
            # 계속 동일한 호출 반환
            return type(
                "MockResp",
                (),
                {
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "pwd"}'}}],
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                },
            )()

    monkeypatch.setattr(cli, "OpenRouterClient", ScriptableClient)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])
    monkeypatch.setattr("sys.stdin", io.StringIO("trigger loop\nexit\n"))

    cli.main()
    captured = capsys.readouterr()

    assert CLI_STUCK_LOOP_NOTICE in captured.out
    assert "Assistant: " not in captured.out


def test_scenario_13_unreadable_json_repeated(tmp_path: Path):
    """시나리오 13: 깨진 JSON 반복 호출 시 단일 입구 감지 및 차단."""
    client = MockLLMClient()
    for i in range(10):
        client.queue_response(
            tool_calls=[{"id": f"c_{i}", "function": {"name": "echo", "arguments": "{bad json"}}]
        )

    disp = ToolDispatcher()
    disp.register_tool("echo", lambda **kw: "ok", {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=client, quiet_mode=True)

    res = agent.run_conversation("test bad json")
    assert res["stopped_reason"] == "stuck_loop"
    assert res["iterations"] == 5

    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 5

    # 1, 2회: Invalid JSON 실패
    t1 = json.loads(tool_msgs[0]["content"])
    t2 = json.loads(tool_msgs[1]["content"])
    assert "Invalid JSON arguments for tool" in t1["error"]
    assert "_loop_warning" not in t1
    assert "Invalid JSON arguments for tool" in t2["error"]
    assert "_loop_warning" not in t2

    # 3, 4회: Invalid JSON 실패 + _loop_warning
    t3 = json.loads(tool_msgs[2]["content"])
    t4 = json.loads(tool_msgs[3]["content"])
    assert "Invalid JSON arguments for tool" in t3["error"]
    assert "_loop_warning" in t3
    assert "Invalid JSON arguments for tool" in t4["error"]
    assert "_loop_warning" in t4

    # 5회: 차단 JSON
    t5 = json.loads(tool_msgs[4]["content"])
    assert t5["success"] is False
    assert "Loop guard blocked: tool 'echo'" in t5["error"]


def test_scenario_14_dispatcher_shared_between_agents(tmp_path: Path):
    """시나리오 14: 디스패처 공유 시 중복 계수 방지 (D-10 소유권 검증)."""
    disp = ToolDispatcher()
    disp.register_tool("echo", lambda x: json.dumps({"echo": x}), {})

    db1 = SessionDB(tmp_path / "s1.db")
    db2 = SessionDB(tmp_path / "s2.db")

    client1 = MockLLMClient()
    client2 = MockLLMClient()

    # 두 에이전트가 동일한 disp 공유
    agent1 = HermesAgent(session_db=db1, dispatcher=disp, llm_client=client1, quiet_mode=True)
    agent2 = HermesAgent(session_db=db2, dispatcher=disp, llm_client=client2, quiet_mode=True)

    # agent2에서 같은 호출 3회 후 텍스트 응답
    client2.queue_response(tool_calls=[{"id": "c1", "function": {"name": "echo", "arguments": '{"x": 1}'}}])
    client2.queue_response(tool_calls=[{"id": "c2", "function": {"name": "echo", "arguments": '{"x": 1}'}}])
    client2.queue_response(tool_calls=[{"id": "c3", "function": {"name": "echo", "arguments": '{"x": 1}'}}])
    client2.queue_response(content="done")

    res = agent2.run_conversation("go")
    assert res["stopped_reason"] == "completed"

    tool_msgs = [json.loads(m["content"]) for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    # 2번째에는 경고가 없어야 함 (중복 계수되지 않음)
    assert "_loop_warning" not in tool_msgs[0]
    assert "_loop_warning" not in tool_msgs[1]
    assert "_loop_warning" in tool_msgs[2]


def test_scenario_15_delegate_child_stuck_loop(tmp_path: Path):
    """시나리오 15: 위임 자식 에이전트 내 루프 정체 차단 (부모 completed, 자식 stuck_loop)."""
    parent_client = MockLLMClient()
    # 부모: 1회차에 delegate_task 호출, 2회차에 텍스트 응답
    parent_client.queue_response(
        tool_calls=[
            {
                "id": "call_del",
                "function": {
                    "name": "delegate_task",
                    "arguments": '{"goal": "investigate loop"}',
                },
            }
        ]
    )
    parent_client.queue_response(content="parent received summary")

    child_client = MockLLMClient()
    # 자식: 같은 read_file 호출 10회 응답
    (tmp_path / "target.txt").write_text("content")
    for i in range(10):
        child_client.queue_response(
            tool_calls=[
                {
                    "id": f"child_c_{i}",
                    "function": {"name": "read_file", "arguments": '{"path": "target.txt"}'},
                }
            ]
        )

    db = SessionDB(tmp_path / "state.db")
    parent_agent = HermesAgent(session_db=db, llm_client=parent_client, quiet_mode=True)
    register_delegate_tool(
        parent_agent.dispatcher,
        session_db=db,
        parent_session_id=parent_agent.session_id,
        llm_client_factory=lambda: child_client,
    )

    res = parent_agent.run_conversation("delegate please")
    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "parent received summary"

    # 부모 메시지 중 delegate_task 실행 결과 확인
    del_tool_msg = next(m for m in res["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "call_del")
    del_res = json.loads(del_tool_msg["content"])
    assert del_res["status"] == "failed"
    assert del_res["stopped_reason"] == "stuck_loop"
    assert del_res["iterations"] == 5

    # 자식 세션 확인
    sessions = db._conn.execute("SELECT id, source, end_reason FROM sessions").fetchall()
    child_session = next(s for s in sessions if s["source"] == "subagent")
    assert child_session["end_reason"] == "stuck_loop"


def test_scenario_16_extension_record_and_phase_ordering(tmp_path: Path):
    """시나리오 16: 설치 기록 및 GUARD 단계 순서 (POLICY 미들웨어 4회 실행)."""
    policy_calls = 0

    class PolicyCounter(Extension):
        name = "policy_counter"
        def install(self, host: ExtensionHost) -> None:
            def _mid(call, next_fn):
                nonlocal policy_calls
                policy_calls += 1
                return next_fn(call)
            host.add_tool_middleware(MiddlewarePhase.POLICY, _mid)

    client = MockLLMClient()
    for i in range(10):
        client.queue_response(
            tool_calls=[{"id": f"c_{i}", "function": {"name": "echo", "arguments": '{"x": 1}'}}]
        )

    disp = ToolDispatcher()
    disp.register_tool("echo", lambda x: "ok", {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=client, quiet_mode=True)
    agent.install_extension(PolicyCounter())

    # 설치 기록 검증
    ext_names = [e.name for e in agent.extensions]
    assert ext_names[0] == "compression"
    assert "loop_guard" in ext_names
    lg_record = next(e for e in agent.extensions if e.name == "loop_guard")
    assert lg_record.kind == "tool_middleware"
    assert lg_record.slot == "GUARD"

    res = agent.run_conversation("test policy order")
    assert res["stopped_reason"] == "stuck_loop"
    # 차단된 5번째 호출은 GUARD 단계에서 return하므로 POLICY 미들웨어에 도달하지 않음
    assert policy_calls == 4


def test_scenario_17_direct_handle_function_call_unaffected():
    """시나리오 17: 턴 밖 직접 호출(handle_function_call) 시 카운터 무영향."""
    handler_calls = 0

    def _handler(x: int):
        nonlocal handler_calls
        handler_calls += 1
        return json.dumps({"x": x})

    disp = ToolDispatcher()
    disp.register_tool("echo", _handler, {})
    db = SessionDB(":memory:")
    agent = HermesAgent(session_db=db, dispatcher=disp, llm_client=MockLLMClient(), quiet_mode=True)

    for i in range(5):
        raw = agent.dispatcher.handle_function_call("echo", {"x": 1})
        res = json.loads(raw)
        assert res["x"] == 1
        assert "_loop_warning" not in res
        assert "error" not in res

    assert handler_calls == 5


def test_scenario_18_observation_events(tmp_path: Path):
    """시나리오 18: EVENT_LOOP_GUARD 이벤트 발행 순서 및 데이터 검증."""
    events: list[AgentEvent] = []

    def _sink(event: AgentEvent):
        if event.kind == EVENT_LOOP_GUARD:
            events.append(event)

    client = MockLLMClient()
    for i in range(10):
        client.queue_response(
            tool_calls=[{"id": f"c_{i}", "function": {"name": "target_tool", "arguments": '{"k": "v"}'}}]
        )

    disp = ToolDispatcher()
    disp.register_tool("target_tool", lambda k: "ok", {})
    db = SessionDB(tmp_path / "state.db")
    agent = HermesAgent(
        session_db=db,
        dispatcher=disp,
        llm_client=client,
        event_sink=_sink,
        quiet_mode=True,
    )

    res = agent.run_conversation("test events")
    assert res["stopped_reason"] == "stuck_loop"

    assert len(events) == 3
    assert events[0].data["status"] == "warn"
    assert events[0].data["count"] == 3
    assert events[0].data["tool"] == "target_tool"

    assert events[1].data["status"] == "warn"
    assert events[1].data["count"] == 4
    assert events[1].data["tool"] == "target_tool"

    assert events[2].data["status"] == "block"
    assert events[2].data["count"] == 5
    assert events[2].data["tool"] == "target_tool"
