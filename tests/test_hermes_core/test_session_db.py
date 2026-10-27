"""SQLite 세션 DB 및 FTS5 전문 검색 단위 테스트."""

from hermes_core.session_db import SessionDB


def test_session_db_in_memory_lifecycle():
    """:memory: 모드에서 세션 생성, 종료 및 토큰 카운터 갱신 검증."""
    db = SessionDB(":memory:")

    session_id = "test-session-001"
    db.create_session(
        session_id=session_id,
        source="cli",
        model="gpt-4o",
        model_config={"temperature": 0.7},
        system_prompt="You are Hermes",
    )

    session = db.get_session(session_id)
    assert session is not None
    assert session["id"] == session_id
    assert session["source"] == "cli"
    assert session["model"] == "gpt-4o"
    assert session["message_count"] == 0

    # 토큰 카운트 업데이트
    db.update_token_counts(session_id, input_tokens=150, output_tokens=50)
    session = db.get_session(session_id)
    assert session["input_tokens"] == 150
    assert session["output_tokens"] == 50

    # 세션 종료
    db.end_session(session_id, end_reason="completed")
    session = db.get_session(session_id)
    assert session["end_reason"] == "completed"
    assert session["ended_at"] is not None

    db.close()


def test_messages_append_and_conversation_format():
    """메시지 추가 시 카운터 증가 및 OpenAI 대화 포맷 변환 검증."""
    db = SessionDB(":memory:")
    sid = "turn-session-002"
    db.create_session(sid, source="cli")

    # 1. User 메시지 추가
    db.append_message(sid, role="user", content="Deploy PostgreSQL using Docker")
    # 2. Assistant 도구 호출 메시지 추가
    tool_calls = [{"id": "call_1", "function": {"name": "terminal", "arguments": '{"cmd": "docker run"}'}}]
    db.append_message(sid, role="assistant", content=None, tool_calls=tool_calls)
    # 3. Tool 응답 메시지 추가
    db.append_message(sid, role="tool", content="Container started: 5432", tool_call_id="call_1")

    session = db.get_session(sid)
    assert session["message_count"] == 3
    assert session["tool_call_count"] == 2  # assistant(tool_calls) + tool(role)

    # 대화 포맷 검증
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 3
    assert conv[0]["role"] == "user"
    assert conv[0]["content"] == "Deploy PostgreSQL using Docker"
    assert conv[1]["role"] == "assistant"
    assert conv[1]["tool_calls"] == tool_calls
    assert conv[2]["role"] == "tool"
    assert conv[2]["tool_call_id"] == "call_1"

    db.close()


def test_fts5_full_text_search(tmp_path):
    """파일 기반 SQLite에서 FTS5 전문 검색 및 스니펫 하이라이트 정상 반환 검증."""
    db_file = tmp_path / "test_state.db"
    db = SessionDB(db_file)

    sid1 = "session-infra-01"
    sid2 = "session-frontend-02"
    db.create_session(sid1, source="cli", model="gpt-4o")
    db.create_session(sid2, source="cli", model="gpt-4o")

    # 세션 1: Docker 및 PostgreSQL 관련 대화
    db.append_message(sid1, role="user", content="How do I configure PostgreSQL 16 replication in Docker?")
    db.append_message(sid1, role="assistant", content="Set up primary and standby nodes in docker-compose.yml.")

    # 세션 2: React 관련 대화
    db.append_message(sid2, role="user", content="Create a responsive navbar using Tailwind CSS and React.")

    # FTS5 검색 실행: "PostgreSQL"
    results = db.search_messages("PostgreSQL")
    assert len(results) == 1
    assert results[0]["session_id"] == sid1
    assert "PostgreSQL" in results[0]["content"]
    assert ">>>PostgreSQL<<<" in results[0]["snippet"]  # FTS5 snippet 하이라이트 마커

    # FTS5 검색 실행: "Docker"
    docker_results = db.search_messages("Docker")
    assert len(docker_results) == 2  # sid1의 user와 assistant 메시지 2건 모두 매칭
    assert all(r["session_id"] == sid1 for r in docker_results)

    # FTS5 검색 실행: 없는 키워드
    assert len(db.search_messages("Kubernetes")) == 0

    db.close()
