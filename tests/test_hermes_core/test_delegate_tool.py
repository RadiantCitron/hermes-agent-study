"""서브에이전트 위임 도구 (`delegate_task`) 테스트 (로드맵 07 / 구현지시서 030 인수 시나리오)."""

import json
from pathlib import Path

import httpx
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.llm_client import LLMError, OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore


@pytest.fixture
def base_env(tmp_path: Path):
    """기본 SessionDB 및 MemoryStore 준비 픽스처."""
    db = SessionDB(tmp_path / "state.db")
    store = MemoryStore(memory_dir=tmp_path / "memories")
    return {"tmp_path": tmp_path, "db": db, "store": store}


def test_scenario_01_registration_and_schema(base_env):
    """시나리오 1: 등록 전후 도구 목록 및 delegate_task OpenAI 스키마 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    names_before = [s["function"]["name"] for s in parent.dispatcher.get_tools_schema()]
    expected_8 = [
        "memory",
        "session_search",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "skills_list",
        "skill_view",
    ]
    assert names_before == expected_8

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
    )

    names_after = [s["function"]["name"] for s in parent.dispatcher.get_tools_schema()]
    assert names_after == expected_8 + ["delegate_task"]

    fresh_default = create_default_dispatcher()
    assert [s["function"]["name"] for s in fresh_default.get_tools_schema()] == expected_8

    delegate_schema = next(
        s for s in parent.dispatcher.get_tools_schema() if s["function"]["name"] == "delegate_task"
    )
    fn = delegate_schema["function"]
    assert fn["parameters"]["required"] == ["goal"]
    assert set(fn["parameters"]["properties"].keys()) == {"goal", "context"}
    assert fn["parameters"]["properties"]["goal"]["type"] == "string"
    assert fn["parameters"]["properties"]["context"]["type"] == "string"
    assert (
        "The subagent knows nothing about your conversation. Put everything it needs in goal and context."
        in fn["description"]
    )


def test_scenario_02_03_04_successful_delegation(base_env):
    """시나리오 2, 3, 4: 위임 호출 성공 순환, 격리된 프롬프트/도구 전달, 세션 레코드 및 상태 검증."""
    db = base_env["db"]
    store = base_env["store"]

    parent_client = MockLLMClient()
    parent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=parent_client,
        quiet_mode=True,
    )

    child_created = []

    def factory():
        c = MockLLMClient([MockLLMResponse(content="child summary")])
        child_created.append(c)
        return c

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    # 부모 1턴: delegate_task 호출 후, 2턴: 최종 응답
    parent_client.queue_response(
        tool_calls=[
            {
                "id": "call_p1",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "G1", "context": "C1"}),
                },
            }
        ]
    )
    parent_client.queue_response(content="Parent finished after delegation.")

    res = parent.run_conversation("PARENTMARK please solve this")
    assert res["final_response"] == "Parent finished after delegation."

    # 시나리오 2 검증: 부모 messages의 tool 결과 메시지
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["success"] is True
    assert payload["status"] == "completed"
    assert payload["summary"] == "child summary"
    assert payload["child_session_id"]
    assert payload["child_session_id"] != parent.session_id
    assert payload["iterations"] == 1
    assert payload["stopped_reason"] == "completed"
    assert payload["tokens"] == {"input": 0, "output": 0}
    assert payload["truncated"] is False
    assert "error" not in payload

    # 시나리오 3 검증: 자식 클라이언트가 받은 첫 호출 내용 및 격리
    assert len(child_created) == 1
    child_client = child_created[0]
    first_child_call = child_client.call_history[0]
    child_msgs = first_child_call["messages"]

    # 마지막 user 메시지는 정확히 G1
    user_msgs = [m for m in child_msgs if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "G1"

    # 시스템 메시지에 C1 포함, 어떤 메시지에도 PARENTMARK 없음
    sys_msg = child_msgs[0]["content"]
    assert "C1" in sys_msg
    assert not any("PARENTMARK" in (m.get("content") or "") for m in child_msgs)

    # 자식에게 전달된 도구는 정확히 허용 목록 6종
    child_tool_names = {t["function"]["name"] for t in first_child_call["tools"]}
    assert child_tool_names == {"terminal", "read_file", "write_file", "patch", "search_files", "skills_list", "skill_view"}

    # 시나리오 4 검증: DB 세션 레코드
    child_sess = db.get_session(payload["child_session_id"])
    assert child_sess["parent_session_id"] == parent.session_id
    assert child_sess["source"] == "subagent"
    assert child_sess["ended_at"] is not None
    assert child_sess["end_reason"] == "completed"

    parent_sess = db.get_session(parent.session_id)
    assert parent_sess["source"] == "cli"
    assert parent_sess["ended_at"] is None


def test_scenario_05_child_tool_output_isolation(base_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """시나리오 5: 자식의 중간 도구 호출 및 내부 마커(CHILDINNER)가 부모 대화/세션/검색에 미노출 검증."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "target.txt").write_text("file content", encoding="utf-8")

    db = base_env["db"]
    store = base_env["store"]
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    def factory():
        return MockLLMClient(
            [
                MockLLMResponse(
                    content="Thinking about CHILDINNER marker",
                    tool_calls=[
                        {
                            "id": "c_call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": json.dumps({"path": "target.txt"})},
                        }
                    ],
                ),
                MockLLMResponse(content="Child final response without marker"),
            ]
        )

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p_call_1",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "Read and report"}),
                },
            }
        ]
    )
    parent_client.queue_response(content="Parent done.")

    res = parent.run_conversation("start")
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    child_session_id = payload["child_session_id"]

    # 자식 세션 DB에는 CHILDINNER가 있음
    child_msgs = db.get_messages(child_session_id)
    assert any("CHILDINNER" in json.dumps(m) for m in child_msgs)

    # 부모 세션 DB 및 부모 대화 messages에는 CHILDINNER가 없음
    parent_msgs = db.get_messages(parent.session_id)
    assert not any("CHILDINNER" in json.dumps(m) for m in parent_msgs)
    assert not any("CHILDINNER" in json.dumps(m) for m in res["messages"])

    # 부모 session_search 결과 목록 비어 있음
    search_raw = parent.dispatcher.handle_function_call("session_search", {"query": "CHILDINNER"})
    search_res = json.loads(search_raw)
    assert search_res["success"] is True
    assert search_res["results"] == []


def test_scenario_06_child_recursion_and_forbidden_tools_blocked(base_env):
    """시나리오 6: 자식 모델이 delegate_task, memory, session_search 호출 시 Unknown tool 실패 및 손자 세션 미생성 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return MockLLMClient(
            [
                MockLLMResponse(
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "nested"})},
                        }
                    ]
                ),
                MockLLMResponse(
                    tool_calls=[
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {
                                "name": "memory",
                                "arguments": json.dumps({"action": "add", "content": "leak"}),
                            },
                        }
                    ]
                ),
                MockLLMResponse(
                    tool_calls=[
                        {
                            "id": "c3",
                            "type": "function",
                            "function": {"name": "session_search", "arguments": json.dumps({"query": "q"})},
                        }
                    ]
                ),
                MockLLMResponse(content="Child finished after tool errors"),
            ]
        )

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p1",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "try forbidden"})},
            }
        ]
    )
    parent_client.queue_response(content="Parent done.")

    res = parent.run_conversation("test")
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    child_session_id = payload["child_session_id"]

    # 자식 세션의 tool 응답들에서 Unknown tool 확인
    child_msgs = db.get_messages(child_session_id)
    child_tool_msgs = [m for m in child_msgs if m["role"] == "tool"]
    assert len(child_tool_msgs) == 3
    for tm in child_tool_msgs:
        err = json.loads(tm["content"])
        assert err["success"] is False
        assert "Unknown tool" in err["error"]

    # 팩토리 호출은 1회뿐이고, DB 세션은 부모와 자식 둘뿐
    assert factory_calls == 1
    session_count = db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert session_count == 2
    assert store.memory_entries == []


def test_scenario_07_multiple_sequential_delegations(base_env):
    """시나리오 7: 두 번 연속 delegate_task 호출 시 각각 별도 클라이언트 및 세션 독립성 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    created_clients = []

    def factory():
        c = MockLLMClient([MockLLMResponse(content=f"Summary {len(created_clients) + 1}")])
        created_clients.append(c)
        return c

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p_call_1",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "Task 1"})},
            }
        ]
    )
    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p_call_2",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "Task 2"})},
            }
        ]
    )
    parent_client.queue_response(content="Both done.")

    res = parent.run_conversation("go")
    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    p1 = json.loads(tool_msgs[0]["content"])
    p2 = json.loads(tool_msgs[1]["content"])

    assert len(created_clients) == 2
    assert created_clients[0] is not created_clients[1]
    assert p1["child_session_id"] != p2["child_session_id"]

    s1 = db.get_session(p1["child_session_id"])
    s2 = db.get_session(p2["child_session_id"])
    assert s1["parent_session_id"] == parent.session_id
    assert s2["parent_session_id"] == parent.session_id


def test_scenario_08_ledger_and_token_separation(base_env):
    """시나리오 8: OpenRouterClient 연동 시 부모와 자식의 llm_calls 원장 및 세션 토큰 합계 격리 검증."""
    db = base_env["db"]
    store = base_env["store"]

    parent_call_count = 0

    def parent_transport_handler(request: httpx.Request) -> httpx.Response:
        nonlocal parent_call_count
        parent_call_count += 1
        if parent_call_count == 1:
            body = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_p_real",
                                    "type": "function",
                                    "function": {
                                        "name": "delegate_task",
                                        "arguments": json.dumps({"goal": "compute"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        else:
            body = {
                "choices": [{"message": {"role": "assistant", "content": "parent final"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        return httpx.Response(200, json=body)

    parent_transport = httpx.MockTransport(parent_transport_handler)
    parent_client = OpenRouterClient(
        api_key="test-key-not-real",
        model="deepseek/deepseek-v4-flash-0731",
        transport=parent_transport,
    )
    parent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=parent_client,
        quiet_mode=True,
    )

    def child_transport_handler(request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"role": "assistant", "content": "child answer"}}],
            "usage": {"prompt_tokens": 33, "completion_tokens": 7, "total_tokens": 40},
        }
        return httpx.Response(200, json=body)

    def factory():
        t = httpx.MockTransport(child_transport_handler)
        return OpenRouterClient(
            api_key="test-key-not-real",
            model="deepseek/deepseek-v4-flash-0731",
            transport=t,
        )

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    res = parent.run_conversation("start")
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    child_id = payload["child_session_id"]

    # DB llm_calls 분리 검증
    p_calls = db.get_llm_calls(parent.session_id)
    c_calls = db.get_llm_calls(child_id)
    assert len(p_calls) == 2
    assert len(c_calls) == 1

    # 토큰 합계 검증
    assert payload["tokens"] == {"input": 33, "output": 7}
    c_sess = db.get_session(child_id)
    assert c_sess["input_tokens"] == 33
    assert c_sess["output_tokens"] == 7

    p_sess = db.get_session(parent.session_id)
    assert p_sess["input_tokens"] == 20
    assert p_sess["output_tokens"] == 10


def test_scenario_09_empty_goal_rejected(base_env):
    """시나리오 9: goal='' 또는 공백 문자열 시 거부 및 팩토리 미호출/세션 미생성 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return MockLLMClient()

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    for empty_val in ("", "   "):
        raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": empty_val})
        res = json.loads(raw)
        assert res["success"] is False
        assert "goal" in res["error"]

    assert factory_calls == 0
    session_count = db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert session_count == 1


def test_scenario_11_factory_failure(base_env):
    """시나리오 11: llm_client_factory 예외 발생 시 안전한 실패 처리 및 부모 루프 지속 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    def bad_factory():
        raise ValueError("bad key")

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=bad_factory,
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p1",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "try"})},
            }
        ]
    )
    parent_client.queue_response(content="Parent recovered after factory failure.")

    res = parent.run_conversation("test")
    assert res["final_response"] == "Parent recovered after factory failure."

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["success"] is False
    assert "Failed to create child client" in payload["error"]

    # 자식 세션 생성되지 않음
    session_count = db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert session_count == 1


def test_scenario_12_child_llm_error(base_env):
    """시나리오 12: 자식 LLM 호출이 LLMError를 반환할 때의 안전한 status='failed' 변환 및 end_reason 기록 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    class FailingClient:
        def chat_completion(self, *args, **kwargs):
            raise LLMError(kind="server", message="upstream server 500 error")

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: FailingClient(),
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p1",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "fail"})},
            }
        ]
    )
    parent_client.queue_response(content="Parent recovered from child LLMError.")

    res = parent.run_conversation("test")
    assert res["final_response"] == "Parent recovered from child LLMError."

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["stopped_reason"] == "llm_error"
    assert payload["summary"] == ""
    assert payload["error"]["kind"] == "server"

    child_sess = db.get_session(payload["child_session_id"])
    assert child_sess["end_reason"] == "llm_error"


def test_scenario_13_child_runtime_exception(base_env):
    """시나리오 13: 자식 실행 중 일반 예외(RuntimeError) 발생 시 stopped_reason='exception' 포획 및 격리 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent_client = MockLLMClient()
    parent = HermesAgent(session_db=db, memory_store=store, llm_client=parent_client, quiet_mode=True)

    class BoomClient:
        def chat_completion(self, *args, **kwargs):
            raise RuntimeError("boom")

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: BoomClient(),
    )

    parent_client.queue_response(
        tool_calls=[
            {
                "id": "p1",
                "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "boom"})},
            }
        ]
    )
    parent_client.queue_response(content="Parent alive.")

    res = parent.run_conversation("test")
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["stopped_reason"] == "exception"
    assert payload["error"]["kind"] == "exception"
    assert "boom" in payload["error"]["message"]

    child_sess = db.get_session(payload["child_session_id"])
    assert child_sess["ended_at"] is not None
    assert child_sess["end_reason"] == "exception"


def test_scenario_14_child_max_iterations(base_env):
    """시나리오 14: max_iterations=2 지정 시 2회 후 중단 및 stopped_reason='max_iterations' 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    def factory():
        # 끝없이 read_file 호출 시도
        return MockLLMClient(
            [
                MockLLMResponse(
                    tool_calls=[
                        {
                            "id": f"c_{i}",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": json.dumps({"path": f"none_{i}.txt"})},
                        }
                    ]
                )
                for i in range(10)
            ]
        )

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
        max_iterations=2,
    )

    raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": "loop forever"})
    payload = json.loads(raw)
    assert payload["status"] == "failed"
    assert payload["stopped_reason"] == "max_iterations"
    assert payload["iterations"] == 2

    child_sess = db.get_session(payload["child_session_id"])
    assert child_sess["end_reason"] == "max_iterations"


def test_scenario_15_child_empty_response(base_env):
    """시나리오 15: 자식이 빈 문자열 응답 반환 시 status='failed', error.kind='empty_response' 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    def factory():
        return MockLLMClient([MockLLMResponse(content="   ")])

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
    )

    raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": "empty"})
    payload = json.loads(raw)
    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["stopped_reason"] == "completed"
    assert payload["error"]["kind"] == "empty_response"

    child_sess = db.get_session(payload["child_session_id"])
    assert child_sess["end_reason"] == "empty_response"


def test_scenario_16_summary_truncation(base_env):
    """시나리오 16: summary_max_chars=10 설정 시 16자 응답이 10자로 절삭되고 truncated=True 반환 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    def factory():
        return MockLLMClient([MockLLMResponse(content="0123456789ABCDEF")])

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory,
        summary_max_chars=10,
    )

    raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": "truncate"})
    payload = json.loads(raw)
    assert payload["summary"] == "0123456789"
    assert payload["truncated"] is True
    assert payload["status"] == "completed"


def test_scenario_17_default_limits(base_env):
    """시나리오 17: 기본 한도(25회 반복, 8,000자 요약) 동작 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    # (a) 30회 도구 호출 큐 -> 25회 상한 중단
    def factory_loop():
        return MockLLMClient(
            [
                MockLLMResponse(
                    tool_calls=[
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": json.dumps({"path": f"f_{i}.txt"})},
                        }
                    ]
                )
                for i in range(30)
            ]
        )

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory_loop,
    )

    raw_a = parent.dispatcher.handle_function_call("delegate_task", {"goal": "loop default"})
    payload_a = json.loads(raw_a)
    assert payload_a["iterations"] == 25
    assert payload_a["stopped_reason"] == "max_iterations"

    # (b) 9,000자 응답 -> 8,000자 절삭
    def factory_long():
        return MockLLMClient([MockLLMResponse(content="x" * 9000)])

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=factory_long,
    )

    raw_b = parent.dispatcher.handle_function_call("delegate_task", {"goal": "long response"})
    payload_b = json.loads(raw_b)
    assert len(payload_b["summary"]) == 8000
    assert payload_b["truncated"] is True


def test_scenario_r20_020_custom_child_runner_injection(base_env):
    """시나리오 20-020-3: 커스텀 child_runner 주입을 통한 독립 구동 및 토큰 집계 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    def stub_runner(
        goal: str,
        context: str | None = None,
        *,
        session_db: Any,
        parent_session_id: str,
        llm_client: Any,
        max_iterations: int,
        dispatcher: Any = None,
    ) -> dict[str, Any]:
        cid = "stub_child_123"
        session_db.create_session(cid, source="subagent", parent_session_id=parent_session_id)
        session_db.update_token_counts(cid, input_tokens=42, output_tokens=21)
        return {
            "child_session_id": cid,
            "run_result": {
                "final_response": "stub response",
                "iterations": 1,
                "stopped_reason": "completed",
            },
            "caught_exception": None,
        }

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
        child_runner=stub_runner,
    )

    raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": "do something"})
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["status"] == "completed"
    assert payload["summary"] == "stub response"
    assert payload["child_session_id"] == "stub_child_123"
    assert payload["iterations"] == 1
    assert payload["stopped_reason"] == "completed"
    assert payload["tokens"] == {"input": 42, "output": 21}
    assert payload["truncated"] is False
    assert "error" not in payload

    # DB 원장 마감 확인
    sess = db.get_session("stub_child_123")
    assert sess is not None
    assert sess["end_reason"] == "completed"
    assert sess["ended_at"] is not None


def test_scenario_r20_020_child_execution_exception_end_session(base_env):
    """시나리오 20-020-4: 자식 실행 중 예외 포획 시 원장 마감 및 실패 응답 반환 검증."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    def failing_runner(
        goal: str,
        context: str | None = None,
        *,
        session_db: Any,
        parent_session_id: str,
        llm_client: Any,
        max_iterations: int,
        dispatcher: Any = None,
    ) -> dict[str, Any]:
        cid = "child_crash_456"
        session_db.create_session(cid, source="subagent", parent_session_id=parent_session_id)
        return {
            "child_session_id": cid,
            "run_result": None,
            "caught_exception": RuntimeError("child crashed during execution"),
        }

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
        child_runner=failing_runner,
    )

    raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": "crash"})
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["stopped_reason"] == "exception"
    assert payload["error"]["kind"] == "exception"
    assert "child crashed during execution" in payload["error"]["message"]

    # 원장 세션 마감 보장 확인
    sess = db.get_session("child_crash_456")
    assert sess is not None
    assert sess["end_reason"] == "exception"
    assert sess["ended_at"] is not None


def test_scenario_r20_020_orphan_prevention_on_agent_init_failure(base_env):
    """시나리오 20-020-5: 자식 에이전트 생성/초기화 도중 실패 시 고아 세션 방지 (R-2)."""
    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    def init_failed_runner(
        goal: str,
        context: str | None = None,
        *,
        session_db: Any,
        parent_session_id: str,
        llm_client: Any,
        max_iterations: int,
        dispatcher: Any = None,
    ) -> dict[str, Any]:
        cid = "child_init_fail_789"
        session_db.create_session(cid, source="subagent", parent_session_id=parent_session_id)
        return {
            "child_session_id": cid,
            "run_result": None,
            "caught_exception": RuntimeError("failed during agent init"),
        }

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
        child_runner=init_failed_runner,
    )

    raw = parent.dispatcher.handle_function_call("delegate_task", {"goal": "init fail"})
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["child_session_id"] == "child_init_fail_789"

    # 고아 세션(ended_at IS NULL)이 존재하지 않고 정상 마감되었는지 확인
    sess = db.get_session("child_init_fail_789")
    assert sess is not None
    assert sess["end_reason"] == "exception"
    assert sess["ended_at"] is not None


def test_scenario_r20_020_create_child_dispatcher_monkeypatch_contract(base_env, monkeypatch):
    """시나리오 20-020-6: create_child_dispatcher 몽키패치가 child_runner에 누락 없이 전달됨을 검증."""
    import hermes_core.tools.delegate_tool as dt_mod
    from hermes_core.tools.dispatcher import ToolDispatcher

    db = base_env["db"]
    store = base_env["store"]
    parent = HermesAgent(session_db=db, memory_store=store, quiet_mode=True)

    custom_dispatcher = ToolDispatcher()
    monkeypatch.setattr(dt_mod, "create_child_dispatcher", lambda: custom_dispatcher)

    passed_dispatcher: list[Any] = []

    def spy_runner(
        goal: str,
        context: str | None = None,
        *,
        session_db: Any,
        parent_session_id: str,
        llm_client: Any,
        max_iterations: int,
        dispatcher: Any = None,
    ) -> dict[str, Any]:
        passed_dispatcher.append(dispatcher)
        cid = "spy_child_001"
        session_db.create_session(cid, source="subagent", parent_session_id=parent_session_id)
        return {
            "child_session_id": cid,
            "run_result": {"final_response": "ok", "iterations": 1, "stopped_reason": "completed"},
            "caught_exception": None,
        }

    register_delegate_tool(
        parent.dispatcher,
        session_db=db,
        parent_session_id=parent.session_id,
        llm_client_factory=lambda: MockLLMClient(),
        child_runner=spy_runner,
    )

    parent.dispatcher.handle_function_call("delegate_task", {"goal": "spy test"})
    assert len(passed_dispatcher) == 1
    assert passed_dispatcher[0] is custom_dispatcher
