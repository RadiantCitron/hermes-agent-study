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


# =============================================================================
# 로드맵 04: 구현지시서 030 인수 테스트 시나리오 (시나리오 13, 14)
# =============================================================================


def test_030_scenario_13_schema_v2_to_v3_migration(tmp_path):
    """시나리오 13: 스키마 v2 DB를 SessionDB(path)로 열면 schema_version == 3, llm_calls 생성, 기존 데이터 보존."""
    import sqlite3

    db_path = tmp_path / "v2_legacy.db"
    conn = sqlite3.connect(str(db_path))

    # v2 형태 스키마 구성 (mode 없음, llm_calls 없음)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (2);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT
        );

        INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1000.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'user', 'hello', 1001.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'assistant', 'world', 1002.0);
        """
    )
    conn.commit()
    conn.close()

    # SessionDB로 열어 마이그레이션 검증
    db = SessionDB(db_path)
    assert db.schema_version >= 3

    # 기존 데이터 보존 확인
    s = db.get_session("s1")
    assert s is not None
    assert s["id"] == "s1"
    assert "mode" in s  # mode 컬럼 추가됨

    msgs = db.get_messages_as_conversation("s1")
    assert len(msgs) == 2
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == "world"

    # llm_calls 테이블 생성 확인
    calls = db.get_llm_calls("s1")
    assert calls == []

    db.close()


def test_030_scenario_14_new_database_schema_v3():
    """시나리오 14: 새 DB 생성 시 schema_version >= 3, sessions.mode와 llm_calls 존재."""
    db = SessionDB(":memory:")
    assert db.schema_version >= 3

    sid = "s_new"
    db.create_session(sid, source="cli", mode="real")
    s = db.get_session(sid)
    assert s is not None
    assert s["mode"] == "real"

    # llm_calls 존재 확인
    assert db.get_llm_calls(sid) == []
    db.close()


# =============================================================================
# 로드맵 05: 구현지시서 050 인수 테스트 시나리오 (시나리오 1~6)
# =============================================================================


def test_050_scenario_01_schema_v3_to_v4_migration(tmp_path):
    """시나리오 1: v3 DB를 SessionDB(path)로 열면 schema_version == 4, active/compacted/is_copy 컬럼 추가 및 기존 행 기본값 설정."""
    import sqlite3

    db_path = tmp_path / "v3_legacy.db"
    conn = sqlite3.connect(str(db_path))

    # v3 형태 스키마 구성 (active/compacted/is_copy 없음)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (3);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            mode TEXT
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT
        );

        INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1000.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'user', 'hello', 1001.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s1', 'assistant', 'world', 1002.0);
        """
    )
    conn.commit()
    conn.close()

    # SessionDB로 열어 마이그레이션 검증
    db = SessionDB(db_path)
    assert db.schema_version >= 4

    # 기존 메시지 컬럼 값 확인
    raw_msgs = db.get_messages("s1")
    assert len(raw_msgs) == 2
    for m in raw_msgs:
        assert m["active"] == 1
        assert m["compacted"] == 0
        assert m["is_copy"] == 0

    # 조회 결과 불변 확인
    conv = db.get_messages_as_conversation("s1")
    assert len(conv) == 2
    assert conv[0]["content"] == "hello"
    assert conv[1]["content"] == "world"

    db.close()


def test_050_scenario_02_new_db_schema_v4_column_match(tmp_path):
    """시나리오 2: 새 DB의 schema_version == 4 및 v3 마이그레이션 DB와 messages 테이블 열 구성 일치."""
    import sqlite3

    db_new = SessionDB(":memory:")
    assert db_new.schema_version >= 4

    # 새 DB의 messages 열 조회
    cursor_new = db_new._conn.execute("PRAGMA table_info(messages)")
    cols_new = [row["name"] for row in cursor_new.fetchall()]
    assert "active" in cols_new
    assert "compacted" in cols_new
    assert "is_copy" in cols_new

    # 마이그레이션 DB 준비
    mig_path = tmp_path / "v3_mig.db"
    conn = sqlite3.connect(str(mig_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (3);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            mode TEXT
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    db_mig = SessionDB(mig_path)
    cursor_mig = db_mig._conn.execute("PRAGMA table_info(messages)")
    cols_mig = [row["name"] for row in cursor_mig.fetchall()]

    for c in ["active", "compacted", "is_copy"]:
        assert c in cols_mig

    db_new.close()
    db_mig.close()


def test_050_scenario_03_migration_idempotent(tmp_path):
    """시나리오 3: v3 레거시 DB에서 마이그레이션 두 번 실행(멱등성) 시 오류 없이 버전 4 유지."""
    import sqlite3

    db_path = tmp_path / "v3_idempotent.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (3);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            mode TEXT
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    # 1차 마이그레이션 실행
    db1 = SessionDB(db_path)
    assert db1.schema_version >= 4
    cursor1 = db1._conn.execute("PRAGMA table_info(messages)")
    cols1 = [row["name"] for row in cursor1.fetchall()]
    for c in ["active", "compacted", "is_copy"]:
        assert c in cols1
    db1.close()

    # 2차 마이그레이션 재실행 (멱등성 검증)
    db2 = SessionDB(db_path)
    assert db2.schema_version >= 4
    cursor2 = db2._conn.execute("PRAGMA table_info(messages)")
    cols2 = [row["name"] for row in cursor2.fetchall()]
    for c in ["active", "compacted", "is_copy"]:
        assert c in cols2
    db2.close()


def test_050_scenario_04_archive_and_compact_normal():
    """시나리오 4: archive_and_compact 정상 호출 시 기존 활성 행 보관 및 새 행 삽입."""
    db = SessionDB(":memory:")
    sid = "sess_04"
    db.create_session(sid, source="cli")

    db.append_message(sid, role="user", content="turn 1")
    db.append_message(sid, role="assistant", content="turn 2")

    new_messages = [
        {"role": "system", "content": "시스템 프롬프트 (DB 미저장)"},
        {"role": "user", "content": "[대화 요약: ...]\n요약 본문\n[대화 요약 끝]"},
        {"role": "assistant", "content": "새 최근 답변"},
    ]

    # summary_index는 new_messages 중 요약 행인 index 1
    db.archive_and_compact(sid, new_messages, summary_index=1)

    raw_msgs = db.get_messages(sid)
    # 기존 2개 + 새 2개(system 제외) = 총 4개
    assert len(raw_msgs) == 4

    # 1. 기존 행: active=0, compacted=1
    old_msgs = raw_msgs[:2]
    for om in old_msgs:
        assert om["active"] == 0
        assert om["compacted"] == 1
        assert om["is_copy"] == 0

    # 2. 새 행: active=1, compacted=0
    new_rows = raw_msgs[2:]
    # 요약 행 (index 1 -> new_rows[0])
    summary_row = new_rows[0]
    assert summary_row["active"] == 1
    assert summary_row["compacted"] == 0
    assert summary_row["is_copy"] == 0
    assert "요약 본문" in summary_row["content"]

    # 복제/새 응답 행 (index 2 -> new_rows[1])
    copied_row = new_rows[1]
    assert copied_row["active"] == 1
    assert copied_row["compacted"] == 0
    assert copied_row["is_copy"] == 1
    assert copied_row["content"] == "새 최근 답변"

    db.close()


def test_050_scenario_05_get_messages_as_conversation_filters_active():
    """시나리오 5: get_messages_as_conversation은 활성 행(active=1)만 시간순 반환, 보관 행 제외."""
    db = SessionDB(":memory:")
    sid = "sess_05"
    db.create_session(sid, source="cli")

    db.append_message(sid, role="user", content="과거 요청")
    db.append_message(sid, role="assistant", content="과거 답변")

    new_messages = [
        {"role": "user", "content": "압축 요약"},
        {"role": "assistant", "content": "최근 답변"},
    ]
    db.archive_and_compact(sid, new_messages, summary_index=0)

    conv = db.get_messages_as_conversation(sid)
    # 과거 요청/답변(active=0)은 제외되고 활성 2개만 반환
    assert len(conv) == 2
    assert conv[0]["content"] == "압축 요약"
    assert conv[1]["content"] == "최근 답변"

    db.close()


def test_050_scenario_06_archive_and_compact_rollback_on_error():
    """시나리오 6: archive_and_compact 중간 예외 시 롤백되어 호출 전 상태 완전히 보존."""
    db = SessionDB(":memory:")
    sid = "sess_06"
    db.create_session(sid, source="cli")

    db.append_message(sid, role="user", content="원래 요청")
    db.append_message(sid, role="assistant", content="원래 답변")

    # 잘못된 데이터로 예외 유도 (예: role이 None이면 NOT NULL 제약 위반)
    invalid_messages = [
        {"role": None, "content": "잘못된 행"},
    ]

    import pytest
    with pytest.raises(Exception):
        db.archive_and_compact(sid, invalid_messages, summary_index=0)

    # 롤백 확인: 원래 활성 행이 그대로 active=1로 유지됨
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 2
    assert conv[0]["content"] == "원래 요청"
    assert conv[1]["content"] == "원래 답변"

    raw = db.get_messages(sid)
    assert len(raw) == 2
    assert raw[0]["active"] == 1
    assert raw[0]["compacted"] == 0

    db.close()
