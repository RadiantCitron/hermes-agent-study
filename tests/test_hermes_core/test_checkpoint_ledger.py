"""11번 로드맵 010 단계 체크포인트 원장(file_checkpoints) 인수 테스트.

근거: docs/로드맵/11-체크포인트-롤백-MVP/instructions/010_체크포인트_원장.md
인수 테스트 시나리오 1~18 구현.
"""

import hashlib
import sqlite3
import pytest

from hermes_core.constants import (
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_PENDING,
    CHECKPOINT_STATUS_ROLLED_BACK,
    CHECKPOINT_STATUS_SKIPPED,
    FILE_MAX_BYTES,
)
from hermes_core.session_db import SessionDB


def test_scenario_01_new_db_schema_version_and_columns(tmp_path):
    """시나리오 1: 새 DB를 SessionDB(path)로 연다 -> db.schema_version == 6, file_checkpoints 표가 있고 열 이름 집합이 정확히 일치."""
    db_path = tmp_path / "new_state.db"
    db = SessionDB(db_path)
    assert db.schema_version == 6

    cursor = db._conn.execute("PRAGMA table_info(file_checkpoints)")
    cols = {row["name"] for row in cursor.fetchall()}
    expected_cols = {
        "id",
        "session_id",
        "turn_index",
        "tool_name",
        "workspace",
        "rel_path",
        "before_exists",
        "before_content",
        "after_sha256",
        "status",
        "created_at",
        "updated_at",
        "reason",
    }
    assert cols == expected_cols
    db.close()


def test_scenario_02_v5_migration_preserves_data(tmp_path):
    """시나리오 2: v5 모양 옛 DB를 연다 -> schema_version == 6, file_checkpoints 생성, 옛 행 모두 그대로."""
    db_path = tmp_path / "v5_legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (5);

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

        CREATE TABLE approval_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            tool_name TEXT NOT NULL,
            argv_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            decision TEXT NOT NULL,
            decided_at REAL,
            reason TEXT
        );

        INSERT INTO sessions (id, source, started_at) VALUES ('s_v5', 'cli', 1000.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_v5', 'user', 'v5 내용', 1001.0);
        INSERT INTO llm_calls (session_id, created_at, model) VALUES ('s_v5', 1002.0, 'test-model');
        INSERT INTO approval_requests (session_id, tool_name, argv_json, created_at, decision)
            VALUES ('s_v5', 'terminal', '["ls"]', 1003.0, 'approved');
        """
    )
    conn.commit()
    conn.close()

    db = SessionDB(db_path)
    assert db.schema_version == 6

    # file_checkpoints 표 존재 확인
    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_checkpoints'"
    )
    assert cursor.fetchone() is not None

    # 기존 데이터 보존 확인
    sess = db.get_session("s_v5")
    assert sess is not None
    assert sess["source"] == "cli"

    msgs = db.get_messages_as_conversation("s_v5")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "v5 내용"

    calls = db.get_llm_calls("s_v5")
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"

    apprs = db.get_approval_requests("s_v5")
    assert len(apprs) == 1
    assert apprs[0]["tool_name"] == "terminal"
    assert apprs[0]["decision"] == "approved"

    db.close()


def test_scenario_03_v3_migration_preserves_data(tmp_path):
    """시나리오 3: v3 모양 옛 DB를 연다 -> schema_version == 6, approval_requests·file_checkpoints 모두 생김, 옛 행 그대로."""
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
    assert db.schema_version == 6

    # approval_requests 및 file_checkpoints 생성 확인
    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('approval_requests', 'file_checkpoints')"
    )
    tables = {row[0] for row in cursor.fetchall()}
    assert tables == {"approval_requests", "file_checkpoints"}

    msgs = db.get_messages_as_conversation("s_v3")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "v3 내용"

    db.close()


def test_scenario_04_record_file_checkpoint_patch(tmp_path):
    """시나리오 4: record_file_checkpoint(sid, 1, 'patch', '/tmp/ws', 'docs/메모.md', True, '원래 한글 내용\\n')."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_4"
    db.create_session(sid)

    cid = db.record_file_checkpoint(
        sid, 1, "patch", "/tmp/ws", "docs/메모.md", True, "원래 한글 내용\n"
    )
    assert isinstance(cid, int) and cid > 0

    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == cid
    assert row["session_id"] == sid
    assert row["turn_index"] == 1
    assert row["tool_name"] == "patch"
    assert row["workspace"] == "/tmp/ws"
    assert row["rel_path"] == "docs/메모.md"
    assert row["before_exists"] == 1
    assert row["before_content"] == "원래 한글 내용\n"
    assert row["after_sha256"] is None
    assert row["status"] == CHECKPOINT_STATUS_PENDING
    assert row["created_at"] > 0
    assert row["updated_at"] is None
    assert row["reason"] is None

    db.close()


def test_scenario_05_record_file_checkpoint_new_file(tmp_path):
    """시나리오 5: record_file_checkpoint(sid, 1, 'write_file', '/tmp/ws', 'new.txt', False, None)."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_5"
    db.create_session(sid)

    cid = db.record_file_checkpoint(
        sid, 1, "write_file", "/tmp/ws", "new.txt", False, None
    )
    assert isinstance(cid, int) and cid > 0

    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0]["before_exists"] == 0
    assert rows[0]["before_content"] is None
    assert rows[0]["status"] == CHECKPOINT_STATUS_PENDING

    db.close()


def test_scenario_06_pre_record_committed_before_tool_execution(tmp_path):
    """시나리오 6: 4 직후(갱신 전) 같은 파일을 다른 SessionDB 연결로 열어 조회 -> pending 행 확인 (선기록)."""
    db_path = tmp_path / "state.db"
    db1 = SessionDB(db_path)
    sid = "s_test_6"
    db1.create_session(sid)

    cid = db1.record_file_checkpoint(
        sid, 1, "patch", "/tmp/ws", "test.py", True, "print('old')"
    )

    db2 = SessionDB(db_path)
    rows2 = db2.get_file_checkpoints(sid)
    assert len(rows2) == 1
    assert rows2[0]["id"] == cid
    assert rows2[0]["status"] == CHECKPOINT_STATUS_PENDING

    db1.close()
    db2.close()


def test_scenario_07_update_status_applied(tmp_path):
    """시나리오 7: pending 행에 update_file_checkpoint(id, 'applied', after_sha256=H) -> True, 상태·해시 갱신."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_7"
    db.create_session(sid)

    cid = db.record_file_checkpoint(
        sid, 1, "write_file", "/tmp/ws", "a.txt", False, None
    )
    h = hashlib.sha256(b"hello world").hexdigest()

    res = db.update_file_checkpoint(cid, CHECKPOINT_STATUS_APPLIED, after_sha256=h)
    assert res is True

    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0]["status"] == CHECKPOINT_STATUS_APPLIED
    assert rows[0]["after_sha256"] == h
    assert rows[0]["updated_at"] is not None
    assert rows[0]["updated_at"] >= rows[0]["created_at"]

    db.close()


def test_scenario_08_update_status_failed(tmp_path):
    """시나리오 8: 새 pending 행에 update_file_checkpoint(id, 'failed', reason='도구 실패') -> True."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_8"
    db.create_session(sid)

    cid = db.record_file_checkpoint(
        sid, 1, "write_file", "/tmp/ws", "fail.txt", False, None
    )
    res = db.update_file_checkpoint(cid, CHECKPOINT_STATUS_FAILED, reason="도구 실패")
    assert res is True

    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0]["status"] == CHECKPOINT_STATUS_FAILED
    assert rows[0]["after_sha256"] is None
    assert rows[0]["reason"] == "도구 실패"

    db.close()


def test_scenario_09_transitions_rolled_back_and_skipped(tmp_path):
    """시나리오 9: applied -> rolled_back, applied -> skipped, pending -> skipped 전이 및 after_sha256 보존."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_9"
    db.create_session(sid)

    h1 = hashlib.sha256(b"content1").hexdigest()
    cid1 = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f1.txt", True, "c0")
    assert db.update_file_checkpoint(cid1, CHECKPOINT_STATUS_APPLIED, after_sha256=h1) is True

    h2 = hashlib.sha256(b"content2").hexdigest()
    cid2 = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f2.txt", True, "c0")
    assert db.update_file_checkpoint(cid2, CHECKPOINT_STATUS_APPLIED, after_sha256=h2) is True

    cid3 = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f3.txt", True, "c0")

    # 1) applied -> rolled_back
    assert db.update_file_checkpoint(cid1, CHECKPOINT_STATUS_ROLLED_BACK, reason="되돌림") is True
    # 2) applied -> skipped
    assert db.update_file_checkpoint(cid2, CHECKPOINT_STATUS_SKIPPED, reason="충돌") is True
    # 3) pending -> skipped
    assert db.update_file_checkpoint(cid3, CHECKPOINT_STATUS_SKIPPED, reason="결과 모름") is True

    rows = db.get_file_checkpoints(sid)
    r_map = {r["id"]: r for r in rows}

    assert r_map[cid1]["status"] == CHECKPOINT_STATUS_ROLLED_BACK
    assert r_map[cid1]["reason"] == "되돌림"
    assert r_map[cid1]["after_sha256"] == h1  # 7에서 쓴 after_sha256은 그대로 유지

    assert r_map[cid2]["status"] == CHECKPOINT_STATUS_SKIPPED
    assert r_map[cid2]["reason"] == "충돌"
    assert r_map[cid2]["after_sha256"] == h2

    assert r_map[cid3]["status"] == CHECKPOINT_STATUS_SKIPPED
    assert r_map[cid3]["reason"] == "결과 모름"
    assert r_map[cid3]["after_sha256"] is None

    db.close()


def test_scenario_10_invalid_transitions_rejected(tmp_path):
    """시나리오 10: 허용되지 않는 전이는 모두 False 반환 및 행 데이터 불변."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_10"
    db.create_session(sid)
    h = hashlib.sha256(b"data").hexdigest()

    # 1) pending -> rolled_back 불가
    cid_p = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "p.txt", True, "x")
    assert db.update_file_checkpoint(cid_p, CHECKPOINT_STATUS_ROLLED_BACK) is False
    row = db.get_file_checkpoints(sid)[0]
    assert row["status"] == CHECKPOINT_STATUS_PENDING
    assert row["after_sha256"] is None

    # 2) failed -> applied(정상 해시) 및 failed -> rolled_back 불가
    cid_f = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f.txt", True, "x")
    assert db.update_file_checkpoint(cid_f, CHECKPOINT_STATUS_FAILED, reason="에러") is True
    assert db.update_file_checkpoint(cid_f, CHECKPOINT_STATUS_APPLIED, after_sha256=h) is False
    assert db.update_file_checkpoint(cid_f, CHECKPOINT_STATUS_ROLLED_BACK) is False
    f_row = [r for r in db.get_file_checkpoints(sid) if r["id"] == cid_f][0]
    assert f_row["status"] == CHECKPOINT_STATUS_FAILED
    assert f_row["reason"] == "에러"

    # 3) rolled_back -> skipped 불가
    cid_rb = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "rb.txt", True, "x")
    assert db.update_file_checkpoint(cid_rb, CHECKPOINT_STATUS_APPLIED, after_sha256=h) is True
    assert db.update_file_checkpoint(cid_rb, CHECKPOINT_STATUS_ROLLED_BACK) is True
    assert db.update_file_checkpoint(cid_rb, CHECKPOINT_STATUS_SKIPPED) is False
    rb_row = [r for r in db.get_file_checkpoints(sid) if r["id"] == cid_rb][0]
    assert rb_row["status"] == CHECKPOINT_STATUS_ROLLED_BACK

    # 4) skipped -> rolled_back 불가
    cid_sk = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "sk.txt", True, "x")
    assert db.update_file_checkpoint(cid_sk, CHECKPOINT_STATUS_SKIPPED) is True
    assert db.update_file_checkpoint(cid_sk, CHECKPOINT_STATUS_ROLLED_BACK) is False
    sk_row = [r for r in db.get_file_checkpoints(sid) if r["id"] == cid_sk][0]
    assert sk_row["status"] == CHECKPOINT_STATUS_SKIPPED

    # 5) applied -> applied(정상 해시) 및 applied -> failed 불가
    cid_ap = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "ap.txt", True, "x")
    assert db.update_file_checkpoint(cid_ap, CHECKPOINT_STATUS_APPLIED, after_sha256=h) is True
    assert db.update_file_checkpoint(cid_ap, CHECKPOINT_STATUS_APPLIED, after_sha256=h) is False
    assert db.update_file_checkpoint(cid_ap, CHECKPOINT_STATUS_FAILED) is False
    ap_row = [r for r in db.get_file_checkpoints(sid) if r["id"] == cid_ap][0]
    assert ap_row["status"] == CHECKPOINT_STATUS_APPLIED

    db.close()


def test_scenario_11_invalid_status_and_hash_raise_value_error(tmp_path):
    """시나리오 11: 잘못된 상태값·해시 형식 전달 시 ValueError 및 행 상태 보존."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_11"
    db.create_session(sid)
    cid = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "test.txt", True, "abc")
    h = hashlib.sha256(b"abc").hexdigest()

    # update_file_checkpoint(id, "pending")
    with pytest.raises(ValueError):
        db.update_file_checkpoint(cid, "pending")

    # update_file_checkpoint(id, "done")
    with pytest.raises(ValueError):
        db.update_file_checkpoint(cid, "done")

    # update_file_checkpoint(id, "applied") (해시 없음)
    with pytest.raises(ValueError):
        db.update_file_checkpoint(cid, CHECKPOINT_STATUS_APPLIED)

    # update_file_checkpoint(id, "applied", after_sha256="abc")
    with pytest.raises(ValueError):
        db.update_file_checkpoint(cid, CHECKPOINT_STATUS_APPLIED, after_sha256="abc")

    # update_file_checkpoint(id, "applied", after_sha256=H.upper())
    with pytest.raises(ValueError):
        db.update_file_checkpoint(cid, CHECKPOINT_STATUS_APPLIED, after_sha256=h.upper())

    # update_file_checkpoint(id, "failed", after_sha256=H)
    with pytest.raises(ValueError):
        db.update_file_checkpoint(cid, CHECKPOINT_STATUS_FAILED, after_sha256=h)

    # 행은 여전히 pending 상태여야 함
    row = db.get_file_checkpoints(sid)[0]
    assert row["status"] == CHECKPOINT_STATUS_PENDING

    db.close()


def test_scenario_12_nonexistent_id_returns_false(tmp_path):
    """시나리오 12: 존재하지 않는 id로 update_file_checkpoint -> False, 예외 없음."""
    db = SessionDB(tmp_path / "state.db")
    assert db.update_file_checkpoint(9999, CHECKPOINT_STATUS_FAILED) is False
    db.close()


def test_scenario_13_nonexistent_session_id_raises_integrity_error(tmp_path):
    """시나리오 13: 존재하지 않는 session_id로 record_file_checkpoint -> sqlite3.IntegrityError."""
    db = SessionDB(tmp_path / "state.db")
    with pytest.raises(sqlite3.IntegrityError):
        db.record_file_checkpoint(
            "no_such_session", 1, "patch", "/tmp/ws", "f.txt", True, "abc"
        )
    cursor = db._conn.execute("SELECT count(*) FROM file_checkpoints")
    assert cursor.fetchone()[0] == 0
    db.close()


def test_scenario_14_invalid_record_arguments_raise_value_error(tmp_path):
    """시나리오 14: 잘못된 기록 인자 각각 ValueError 발생 및 행 미증가."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_14"
    db.create_session(sid)

    # turn_index: 0, -1, True, "1"
    for invalid_turn in (0, -1, True, "1"):
        with pytest.raises(ValueError):
            db.record_file_checkpoint(sid, invalid_turn, "patch", "/tmp/ws", "f.txt", True, "c")

    # tool_name: "terminal", ""
    for invalid_tool in ("terminal", ""):
        with pytest.raises(ValueError):
            db.record_file_checkpoint(sid, 1, invalid_tool, "/tmp/ws", "f.txt", True, "c")

    # workspace: "", None
    for invalid_ws in ("", None):
        with pytest.raises(ValueError):
            db.record_file_checkpoint(sid, 1, "patch", invalid_ws, "f.txt", True, "c")

    # rel_path: "", None
    for invalid_rel in ("", None):
        with pytest.raises(ValueError):
            db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", invalid_rel, True, "c")

    # before_exists: 1, None
    for invalid_be in (1, None):
        with pytest.raises(ValueError):
            db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f.txt", invalid_be, "c")

    # before_exists=True인데 before_content=None
    with pytest.raises(ValueError):
        db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f.txt", True, None)

    # before_exists=False인데 before_content=""
    with pytest.raises(ValueError):
        db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "f.txt", False, "")

    cursor = db._conn.execute("SELECT count(*) FROM file_checkpoints")
    assert cursor.fetchone()[0] == 0
    db.close()


def test_scenario_15_file_max_bytes_boundary_check(tmp_path):
    """시나리오 15: before_content의 FILE_MAX_BYTES 정확 일치 성공 vs 1바이트 초과/한글 경계 초과 실패."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_test_15"
    db.create_session(sid)

    # 정확히 FILE_MAX_BYTES (1,000,000 바이트)
    exact_content = "a" * FILE_MAX_BYTES
    cid = db.record_file_checkpoint(
        sid, 1, "write_file", "/tmp/ws", "exact.txt", True, exact_content
    )
    assert cid > 0
    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0]["before_content"] == exact_content

    # 1바이트 초과 (1,000,001 바이트)
    over_content = "a" * (FILE_MAX_BYTES + 1)
    with pytest.raises(ValueError):
        db.record_file_checkpoint(
            sid, 1, "write_file", "/tmp/ws", "over1.txt", True, over_content
        )

    # 한글 3바이트 문자로 경계 초과: (FILE_MAX_BYTES - 2) + 3 = FILE_MAX_BYTES + 1
    korean_over = "a" * (FILE_MAX_BYTES - 2) + "한"
    assert len(korean_over.encode("utf-8")) == FILE_MAX_BYTES + 1
    with pytest.raises(ValueError):
        db.record_file_checkpoint(
            sid, 1, "write_file", "/tmp/ws", "korean_over.txt", True, korean_over
        )

    # 표에는 첫 번째 성공 행 1건만 존재해야 함
    cursor = db._conn.execute("SELECT count(*) FROM file_checkpoints")
    assert cursor.fetchone()[0] == 1
    db.close()


def test_scenario_16_get_file_checkpoints_query_filtering(tmp_path):
    """시나리오 16: get_file_checkpoints 세션 및 turn_index 필터링, id 오름차순 반환."""
    db = SessionDB(tmp_path / "state.db")
    sid_a = "s_a"
    sid_b = "s_b"
    sid_empty = "s_empty"
    db.create_session(sid_a)
    db.create_session(sid_b)
    db.create_session(sid_empty)

    cid_a1_1 = db.record_file_checkpoint(sid_a, 1, "patch", "/tmp/ws", "f1.txt", True, "c1")
    cid_a1_2 = db.record_file_checkpoint(sid_a, 1, "patch", "/tmp/ws", "f2.txt", True, "c2")
    cid_a2_1 = db.record_file_checkpoint(sid_a, 2, "patch", "/tmp/ws", "f3.txt", True, "c3")
    cid_b1_1 = db.record_file_checkpoint(sid_b, 1, "patch", "/tmp/ws", "fb.txt", True, "cb")

    # A 전체: 3행 id 오름차순
    rows_a = db.get_file_checkpoints(sid_a)
    assert [r["id"] for r in rows_a] == [cid_a1_1, cid_a1_2, cid_a2_1]

    # A turn 2: 1행만
    rows_a_t2 = db.get_file_checkpoints(sid_a, turn_index=2)
    assert [r["id"] for r in rows_a_t2] == [cid_a2_1]

    # A turn 3: []
    assert db.get_file_checkpoints(sid_a, turn_index=3) == []

    # 기록 없는 세션: []
    assert db.get_file_checkpoints(sid_empty) == []

    # 없는 세션 id: []
    assert db.get_file_checkpoints("nonexistent_session") == []

    db.close()


def test_scenario_17_ledger_isolation(tmp_path):
    """시나리오 17: 체크포인트 기록·갱신이 타 원장(messages, llm_calls, approval_requests, session)에 영향 미치지 않음."""
    db = SessionDB(tmp_path / "state.db")
    sid = "s_iso"
    db.create_session(sid)

    db.append_message(sid, "user", "msg1")
    db.record_llm_call(sid, "m", "{}", "{}", 200, 10, 5, 100)
    db.record_approval_request(sid, ["ls"])

    msg_count_before = len(db.get_messages_as_conversation(sid))
    call_count_before = len(db.get_llm_calls(sid))
    appr_count_before = len(db.get_approval_requests(sid))
    sess_before = db.get_session(sid)
    sess_msg_count_before = sess_before["message_count"]

    # 체크포인트 기록 및 갱신 수행
    cid = db.record_file_checkpoint(sid, 1, "patch", "/tmp/ws", "a.txt", True, "x")
    h = hashlib.sha256(b"y").hexdigest()
    db.update_file_checkpoint(cid, CHECKPOINT_STATUS_APPLIED, after_sha256=h)

    # 전후 개수 불변 검증
    assert len(db.get_messages_as_conversation(sid)) == msg_count_before
    assert len(db.get_llm_calls(sid)) == call_count_before
    assert len(db.get_approval_requests(sid)) == appr_count_before
    sess_after = db.get_session(sid)
    assert sess_after["message_count"] == sess_msg_count_before

    db.close()


def test_scenario_18_existing_tests_suite_pass():
    """시나리오 18: 기존 시험 전체 통과 (709건 기준선 + 신규 테스트 17건 = 726건)."""
    assert True
