"""에이전트 루프, 도구 실행 레일 및 통합 E2E 검증 테스트."""

from hermes_core.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.session_db import SessionDB
from hermes_core.tools.memory_tool import MemoryStore


def test_agent_e2e_memory_tool_cycle(tmp_path):
    """유저 입력 -> 도구 호출(memory_tool) -> 결과 수집 -> 최종 응답 -> DB 저장 전 사이클 검증."""
    mem_dir = tmp_path / "memories"
    db_file = tmp_path / "state.db"

    memory_store = MemoryStore(memory_dir=mem_dir)
    session_db = SessionDB(db_file)

    # Mock LLM 시나리오 준비
    # 1회차 호출: memory_tool 호출 요청
    # 2회차 호출: 도구 결과를 본 후 최종 응답
    mock_client = MockLLMClient()
    mock_client.queue_response(
        content=None,
        tool_calls=[{
            "id": "call_mem_1",
            "function": {
                "name": "memory",
                "arguments": '{"action": "add", "target": "memory", "content": "Project uses Python 3.14"}',
            },
        }],
    )
    mock_client.queue_response(
        content="프로젝트 환경 정보를 메모리에 안전하게 저장했습니다.",
    )

    agent = HermesAgent(
        session_db=session_db,
        memory_store=memory_store,
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("프로젝트 파이썬 버전을 기억해줘.")

    # 1. 최종 응답 확인
    assert result["final_response"] == "프로젝트 환경 정보를 메모리에 안전하게 저장했습니다."
    assert result["iterations"] == 2  # 1회 도구 호출 + 1회 최종 응답

    # 2. 실제 디스크의 MEMORY.md 파일에 기록되었는지 검증
    memory_file = mem_dir / "MEMORY.md"
    assert memory_file.exists()
    assert "Project uses Python 3.14" in memory_file.read_text(encoding="utf-8")

    # 3. SessionDB에 원장으로 모든 메시지가 Strict Role Alternation으로 저장되었는지 검증
    messages = session_db.get_messages(agent.session_id)
    # [user, assistant(tool_calls), tool, assistant] 총 4건
    assert len(messages) == 4
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "프로젝트 파이썬 버전을 기억해줘."
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"] is not None
    assert messages[2]["role"] == "tool"
    assert "Entry added" in messages[2]["content"]
    assert messages[3]["role"] == "assistant"
    assert messages[3]["content"] == "프로젝트 환경 정보를 메모리에 안전하게 저장했습니다."

    session_db.close()


def test_agent_e2e_terminal_tool_execution(tmp_path):
    """에이전트가 terminal_tool을 호출하여 실제 쉘 명령을 수행하고 결과를 반환받는지 검증."""
    session_db = SessionDB(":memory:")
    memory_store = MemoryStore(memory_dir=tmp_path)

    mock_client = MockLLMClient()
    mock_client.queue_response(
        content=None,
        tool_calls=[{
            "id": "call_term_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "echo \'Hermes Core Activated\'"}',
            },
        }],
    )
    mock_client.queue_response(
        content="쉘 명령어 실행이 완료되었습니다.",
    )

    agent = HermesAgent(
        session_db=session_db,
        memory_store=memory_store,
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("echo 명령어를 실행해줘.")

    assert result["final_response"] == "쉘 명령어 실행이 완료되었습니다."

    messages = session_db.get_messages(agent.session_id)
    assert len(messages) == 4
    tool_output_msg = messages[2]
    assert tool_output_msg["role"] == "tool"
    assert "Hermes Core Activated" in tool_output_msg["content"]

    session_db.close()


def test_agent_e2e_blocked_invalid_unreadable_and_valid_calls_in_one_conversation(tmp_path, monkeypatch):
    """로드맵 02 / 060: 차단된 명령, 잘못된 인자, 읽을 수 없는 인자, 정상 명령이 한 대화에 섞여도 루프가 정상 종료한다."""
    work = tmp_path / "work"
    work.mkdir()
    victim = work / "x"
    victim.write_text("keep", encoding="utf-8")
    monkeypatch.chdir(work)

    def call(call_id, name, arguments):
        return [{"id": call_id, "function": {"name": name, "arguments": arguments}}]

    mock_client = MockLLMClient()
    mock_client.queue_response(tool_calls=call("c1", "terminal", '{"command": "rm -rf x"}'))
    mock_client.queue_response(tool_calls=call("c2", "memory", "{}"))
    mock_client.queue_response(tool_calls=call("c3", "terminal", "{bad"))
    mock_client.queue_response(tool_calls=call("c4", "terminal", '{"command": "echo ok"}'))
    mock_client.queue_response(content="done")

    session_db = SessionDB(":memory:")
    agent = HermesAgent(
        session_db=session_db,
        memory_store=MemoryStore(memory_dir=tmp_path / "memories"),
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("섞어서 시험해줘.")

    # 1. 정상 종료
    assert result["final_response"] == "done"
    assert result["stopped_reason"] == "completed"
    assert result["iterations"] == 5

    # 2. 차단된 명령은 실행되지 않았다
    assert victim.exists()

    # 3. 도구 메시지 4개가 순서대로: 거부, 인자 오류, 인자 읽기 실패, 성공
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 4
    assert "rm" in tool_msgs[0]["content"]
    assert "action" in tool_msgs[1]["content"]
    assert "JSON" in tool_msgs[2]["content"]
    assert "ok" in tool_msgs[3]["content"]

    # 4. 세션 원장: 사용자 1 + 모델 5 + 도구 4 = 10
    assert len(session_db.get_messages(agent.session_id)) == 10

    # 5. 모델 요청마다 대응하는 도구 메시지가 있다
    requested_ids = [
        tc["id"] for m in result["messages"] if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    ]
    assert requested_ids == ["c1", "c2", "c3", "c4"]
    assert [m["tool_call_id"] for m in tool_msgs] == requested_ids

    session_db.close()
