"""승인 요청 원장(approval_requests 표, 스키마 v5, SessionDB 메서드) 단위 테스트.

근거: 로드맵 10 [구현지시서 010] 인수 테스트 시나리오 1~13
"""

import json
import sqlite3
import time

import pytest

from hermes_core.session_db import SessionDB


def test_scenario_01_new_db_schema_version_and_columns(tmp_path):
    """시나리오 1: 새 DB를 SessionDB(path)로 연다 -> db.schema_version == 5, approval_requests 표가 있고 열 이름 집합이 정확히 일치."""
    db_path = tmp_path / "new_state.db"
    db = SessionDB(db_path)
    assert db.schema_version == 5

    cursor = db._conn.execute("PRAGMA table_info(approval_requests)")
    cols = {row["name"] for row in cursor.fetchall()}
    expected_cols = {
        "id",
        "session_id",
        "tool_name",
        "argv_json",
        "created_at",
        "decision",
        "decided_at",
        "reason",
    }
    assert cols == expected_cols
    db.close()


def test_scenario_02_v4_migration_preserves_data(tmp_path):
    """시나리오 2: v4 모양 옛 DB를 연다 -> schema_version == 5, approval_requests 생성, 옛 행 모두 그대로."""
    db_path = tmp_path / "v4_legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (4);

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
            finish_reason TEXT,
            active INTEGER DEFAULT 1,
            compacted INTEGER DEFAULT 0,
            is_copy INTEGER DEFAULT 0
        );

        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            created_at REAL NOT NULL,
            model TEXT,
            request_json TEXT,
            response_json TEXT,
            status_code INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms INTEGER,
            error_kind TEXT,
            error_message TEXT
        );

        INSERT INTO sessions (id, source, started_at) VALUES ('s_v4', 'cli', 1000.0);
        INSERT INTO messages (session_id, role, content, timestamp, active) VALUES ('s_v4', 'user', 'v4 user message', 1001.0, 1);
        INSERT INTO llm_calls (session_id, created_at, model, status_code) VALUES ('s_v4', 1002.0, 'test-model', 200);
        """
    )
    conn.commit()
    conn.close()

    db = SessionDB(db_path)
    assert db.schema_version == 5

    # approval_requests 표 존재 확인
    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='approval_requests'"
    )
    assert cursor.fetchone() is not None

    # 기존 행 보존 확인
    session = db.get_session("s_v4")
    assert session is not None
    assert session["started_at"] == 1000.0

    msgs = db.get_messages("s_v4")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "v4 user message"

    calls = db.get_llm_calls("s_v4")
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"

    db.close()


def test_scenario_03_v3_migration_preserves_data(tmp_path):
    """시나리오 3: v3 모양 옛 DB(test_agent_e2e.py 형태)를 연다 -> schema_version == 5, approval_requests 생성, 옛 행 그대로."""
    db_path = tmp_path / "v3_legacy.db"
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

        INSERT INTO sessions (id, source, started_at) VALUES ('s_v3', 'cli', 1000.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_v3', 'user', 'v3 내용', 1001.0);
        """
    )
    conn.commit()
    conn.close()

    db = SessionDB(db_path)
    assert db.schema_version == 5

    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='approval_requests'"
    )
    assert cursor.fetchone() is not None

    msgs = db.get_messages("s_v3")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "v3 내용"
    assert msgs[0]["active"] == 1  # v4 migration executed
    db.close()


def test_scenario_04_record_approval_request_pending(tmp_path):
    """시나리오 4: record_approval_request(sid, ["python", "-c", "print('한글')"]) -> 양의 정수 id, pending 행."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid = "sess_04"
    db.create_session(sid, source="cli")

    t_before = time.time()
    req_id = db.record_approval_request(sid, ["python", "-c", "print('한글')"])
    t_after = time.time()

    assert isinstance(req_id, int)
    assert req_id > 0

    rows = db.get_approval_requests(sid)
    assert len(rows) == 1
    row = rows[0]

    assert row["id"] == req_id
    assert row["session_id"] == sid
    assert row["tool_name"] == "terminal"
    assert row["decision"] == "pending"
    assert row["decided_at"] is None
    assert row["reason"] is None
    assert t_before <= row["created_at"] <= t_after

    # json 역직렬화 일치 및 한글 문자열 보존(\\u 이스케이프 아님)
    assert json.loads(row["argv_json"]) == ["python", "-c", "print('한글')"]
    assert "한글" in row["argv_json"]
    assert "\\u" not in row["argv_json"]

    db.close()


def test_scenario_05_pre_record_visible_in_different_connection(tmp_path):
    """시나리오 5: 4 직후(결정 전) 같은 파일을 다른 SessionDB 연결로 열어 조회 -> pending 행 보임 (선기록)."""
    db_path = tmp_path / "state.db"
    db1 = SessionDB(db_path)
    sid = "sess_05"
    db1.create_session(sid, source="cli")
    req_id = db1.record_approval_request(sid, ["git", "status"])

    # 결정 전 다른 연결로 열기
    db2 = SessionDB(db_path)
    rows = db2.get_approval_requests(sid)
    assert len(rows) == 1
    assert rows[0]["id"] == req_id
    assert rows[0]["decision"] == "pending"

    db1.close()
    db2.close()


def test_scenario_06_resolve_final_decisions(tmp_path):
    """시나리오 6: 최종 결정 5개 각각에 대해 resolve_approval_request 성공 및 값 갱신 검증."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid = "sess_06"
    db.create_session(sid, source="cli")

    final_decisions = ["approved", "denied", "cancelled", "auto_denied", "limit_exceeded"]
    for decision in final_decisions:
        req_id = db.record_approval_request(sid, ["echo", decision])
        ok = db.resolve_approval_request(req_id, decision, reason=f"사유_{decision}")
        assert ok is True

        requests = [r for r in db.get_approval_requests(sid) if r["id"] == req_id]
        assert len(requests) == 1
        r = requests[0]
        assert r["decision"] == decision
        assert r["reason"] == f"사유_{decision}"
        assert r["decided_at"] is not None
        assert r["decided_at"] >= r["created_at"]

    db.close()


def test_scenario_07_immutable_decision_no_reversal(tmp_path):
    """시나리오 7: approved 된 행에 resolve_approval_request(id, 'denied', '번복') -> False, 행 불변."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid = "sess_07"
    db.create_session(sid, source="cli")

    req_id = db.record_approval_request(sid, ["rm", "-rf", "target"])
    ok1 = db.resolve_approval_request(req_id, "approved", reason="최초 승인")
    assert ok1 is True

    # 번복 시도
    ok2 = db.resolve_approval_request(req_id, "denied", reason="번복")
    assert ok2 is False

    row = db.get_approval_requests(sid)[0]
    assert row["decision"] == "approved"
    assert row["reason"] == "최초 승인"

    db.close()


def test_scenario_08_invalid_decision_raises_value_error(tmp_path):
    """시나리오 8: 새 요청에 resolve_approval_request(id, 'pending') / 'maybe' -> ValueError, 행은 pending 유지."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid = "sess_08"
    db.create_session(sid, source="cli")

    req_id = db.record_approval_request(sid, ["ls"])

    with pytest.raises(ValueError):
        db.resolve_approval_request(req_id, "pending")

    with pytest.raises(ValueError):
        db.resolve_approval_request(req_id, "maybe")

    row = db.get_approval_requests(sid)[0]
    assert row["decision"] == "pending"

    db.close()


def test_scenario_09_resolve_non_existent_id(tmp_path):
    """시나리오 9: 존재하지 않는 id(예: 9999)로 resolve_approval_request(9999, 'approved') -> False, 예외 없음."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    ok = db.resolve_approval_request(9999, "approved")
    assert ok is False
    db.close()


def test_scenario_10_foreign_key_violation_for_missing_session(tmp_path):
    """시나리오 10: 존재하지 않는 session_id로 record_approval_request -> sqlite3.IntegrityError, 표에 행 늘지 않음."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)

    with pytest.raises(sqlite3.IntegrityError):
        db.record_approval_request("non_existent_sid", ["ls"])

    cursor = db._conn.execute("SELECT COUNT(*) FROM approval_requests")
    assert cursor.fetchone()[0] == 0
    db.close()


def test_scenario_11_invalid_argv_raises_value_error(tmp_path):
    """시나리오 11: argv가 [] / 'ls -la' / ['ls', 1] / None 인 경우 -> 모두 ValueError, 표에 행 안 늘어남."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid = "sess_11"
    db.create_session(sid, source="cli")

    invalid_argvs = [[], "ls -la", ["ls", 1], None]
    for invalid in invalid_argvs:
        with pytest.raises(ValueError):
            db.record_approval_request(sid, invalid)  # type: ignore

    cursor = db._conn.execute("SELECT COUNT(*) FROM approval_requests")
    assert cursor.fetchone()[0] == 0
    db.close()


def test_scenario_12_get_approval_requests_filtering_and_order(tmp_path):
    """시나리오 12: 세션 2개에 요청 섞어 생성 뒤 get_approval_requests(세션A) / 요청 없는 세션 / 없는 세션 id."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid_a = "sess_12_a"
    sid_b = "sess_12_b"
    sid_empty = "sess_12_empty"
    db.create_session(sid_a, source="cli")
    db.create_session(sid_b, source="cli")
    db.create_session(sid_empty, source="cli")

    id_a1 = db.record_approval_request(sid_a, ["cmd", "a1"])
    id_b1 = db.record_approval_request(sid_b, ["cmd", "b1"])
    id_a2 = db.record_approval_request(sid_a, ["cmd", "a2"])

    reqs_a = db.get_approval_requests(sid_a)
    assert [r["id"] for r in reqs_a] == [id_a1, id_a2]

    reqs_b = db.get_approval_requests(sid_b)
    assert [r["id"] for r in reqs_b] == [id_b1]

    assert db.get_approval_requests(sid_empty) == []
    assert db.get_approval_requests("totally_non_existent") == []

    db.close()


def test_scenario_13_ledger_isolation(tmp_path):
    """시나리오 13: 요청 기록·결정 갱신 전후로 messages 개수, llm_calls 개수, 세션의 message_count 변하지 않음 (가정 I)."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    sid = "sess_13"
    db.create_session(sid, source="cli")
    db.append_message(sid, role="user", content="hello")
    db.record_llm_call(sid, model="test-model", status_code=200)

    # 기준선
    sess_before = db.get_session(sid)
    msgs_before = len(db.get_messages(sid))
    calls_before = len(db.get_llm_calls(sid))

    # 승인 요청 기록 및 결정
    req_id = db.record_approval_request(sid, ["python", "run.py"])
    db.resolve_approval_request(req_id, "approved")

    # 사후 검증
    sess_after = db.get_session(sid)
    msgs_after = len(db.get_messages(sid))
    calls_after = len(db.get_llm_calls(sid))

    assert msgs_after == msgs_before
    assert calls_after == calls_before
    assert sess_after["message_count"] == sess_before["message_count"]

    db.close()
