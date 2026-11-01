"""원장(Ledger) 순수 도메인 모듈.

I/O(SQLite 커넥션, 파일 시스템) 의존성 없이
스키마 DDL, 순수 유효성 검증, 데이터 변환, 쿼리 조립을 담당한다.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from hermes_core.constants import (
    APPROVAL_FINAL_DECISIONS,
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_TOOL_NAMES,
    CHECKPOINT_TRANSITIONS,
    FILE_MAX_BYTES,
)

SCHEMA_VERSION = 7

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
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
    mode TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
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

CREATE TABLE IF NOT EXISTS llm_calls (
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

CREATE TABLE IF NOT EXISTS approval_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    tool_name TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    decision TEXT NOT NULL,
    decided_at REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS file_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    turn_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    workspace TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    before_exists INTEGER NOT NULL,
    before_content TEXT,
    after_sha256 TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS control_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at REAL NOT NULL,
    source TEXT NOT NULL,
    command TEXT NOT NULL,
    target TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    result TEXT NOT NULL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_approval_requests_session ON approval_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_file_checkpoints_session_turn ON file_checkpoints(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_control_audit_session ON control_audit(session_id, id);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def is_valid_hex_sha256(value: Any) -> bool:
    """소문자 16진수 64자 SHA-256 해시 문자열인지 검증한다."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def validate_approval_request_argv(argv: Any) -> list[str]:
    """승인 요청 argv가 비어있지 않은 문자열 리스트인지 검증하고 반환한다."""
    if not isinstance(argv, list) or len(argv) == 0 or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    return argv


def validate_approval_decision(decision: str) -> None:
    """승인 결정이 최종 결정 값 중 하나인지 검증한다."""
    if decision not in APPROVAL_FINAL_DECISIONS:
        raise ValueError(
            f"decision must be one of {APPROVAL_FINAL_DECISIONS}, got {decision!r}"
        )


def validate_file_checkpoint_input(
    session_id: str,
    turn_index: int,
    tool_name: str,
    workspace: str,
    rel_path: str,
    before_exists: bool,
    before_content: Optional[str] = None,
) -> None:
    """파일 도구 실행 전 체크포인트 입력값의 유효성을 검증한다."""
    if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 1:
        raise ValueError(f"turn_index must be an integer >= 1, got {turn_index!r}")
    if not isinstance(tool_name, str) or tool_name not in CHECKPOINT_TOOL_NAMES:
        raise ValueError(
            f"tool_name must be one of {CHECKPOINT_TOOL_NAMES}, got {tool_name!r}"
        )
    if not isinstance(workspace, str) or len(workspace) == 0:
        raise ValueError("workspace must be a non-empty string")
    if not isinstance(rel_path, str) or len(rel_path) == 0:
        raise ValueError("rel_path must be a non-empty string")
    if not isinstance(before_exists, bool):
        raise ValueError(f"before_exists must be a boolean, got {before_exists!r}")

    if before_exists:
        if not isinstance(before_content, str):
            raise ValueError("before_content must be a string when before_exists is True")
        encoded_len = len(before_content.encode("utf-8"))
        if encoded_len > FILE_MAX_BYTES:
            raise ValueError(
                f"before_content exceeds FILE_MAX_BYTES ({encoded_len} > {FILE_MAX_BYTES})"
            )
    else:
        if before_content is not None:
            raise ValueError("before_content must be None when before_exists is False")


def validate_checkpoint_transition(
    status: str,
    after_sha256: Optional[str] = None,
) -> tuple[str, ...]:
    """체크포인트 상태 전이 및 after_sha256 조건의 유효성을 검증하고 허용된 이전 상태 튜플을 반환한다."""
    if status not in CHECKPOINT_TRANSITIONS:
        raise ValueError(
            f"status must be one of {tuple(CHECKPOINT_TRANSITIONS.keys())}, got {status!r}"
        )

    if status == CHECKPOINT_STATUS_APPLIED:
        if not is_valid_hex_sha256(after_sha256):
            raise ValueError(
                f"after_sha256 must be a 64-character lowercase hex string, got {after_sha256!r}"
            )
    else:
        if after_sha256 is not None:
            raise ValueError(
                f"after_sha256 can only be set when status is '{CHECKPOINT_STATUS_APPLIED}', got {after_sha256!r}"
            )

    return CHECKPOINT_TRANSITIONS[status]


def validate_control_audit_input(
    session_id: str,
    source: str,
    command: str,
    target: str,
    result: str,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """제어 감사 입력값의 유효성을 검증한다."""
    if not isinstance(session_id, str) or session_id.strip() == "":
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(source, str) or source.strip() == "":
        raise ValueError("source must be a non-empty string")
    if not isinstance(target, str) or target.strip() == "":
        raise ValueError("target must be a non-empty string")
    if command not in ("set_setting", "enable_extension", "disable_extension"):
        raise ValueError(f"Invalid audit command: {command!r}")
    if result not in ("accepted", "rejected", "applied"):
        raise ValueError(f"Invalid audit result: {result!r}")
    if old_value is not None and not isinstance(old_value, str):
        raise ValueError("old_value must be None or a string")
    if new_value is not None and not isinstance(new_value, str):
        raise ValueError("new_value must be None or a string")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("reason must be None or a string")


def validate_deactivate_after_id(after_id: Any) -> None:
    """deactivate_messages_after의 after_id 유효성을 검증한다."""
    if not isinstance(after_id, int) or isinstance(after_id, bool) or after_id < 0:
        raise ValueError("after_id must be a non-negative integer")


def extract_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    """메시지 목록의 assistant tool_calls에서 tool_call_id -> tool_name 맵핑을 추출한다."""
    tool_name_map: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tc_id = tc.get("id")
                fn = tc.get("function", {})
                fn_name = fn.get("name") or tc.get("name")
                if tc_id and fn_name:
                    tool_name_map[tc_id] = fn_name
    return tool_name_map


_FTS5_RESERVED = frozenset({"AND", "OR", "NOT", "NEAR"})
_NON_WORD_PATTERN = re.compile(r"[^\w]")


def sanitize_fts_query(query: str) -> str:
    """FTS5 전문 검색 쿼리를 안전화하여 SQLite OperationalError 크래시를 방지한다.

    - 빈 문자열이거나 공백뿐이면 "" 반환.
    - 공백 기준으로 개별 토큰 분리.
    - FTS5 예약어(AND, OR, NOT, NEAR)는 대소문자 무관하게 큰따옴표로 감쌈.
    - 영숫자, 언더스코어, 유니코드 문자/숫자 외의 특수문자가 포함된 토큰은
      내부의 "를 ""로 이스케이프한 뒤 큰따옴표로 감쌈.
    - 순수 일반 단어 토큰은 원형을 그대로 유지하여 기존 단위 테스트 및 평문 검색 계약을 보존함.
    """
    if not isinstance(query, str) or not query.strip():
        return ""

    tokens = query.strip().split()
    sanitized_tokens: list[str] = []

    for token in tokens:
        if token.upper() in _FTS5_RESERVED:
            sanitized_tokens.append(f'"{token}"')
        elif _NON_WORD_PATTERN.search(token):
            escaped = token.replace('"', '""')
            sanitized_tokens.append(f'"{escaped}"')
        else:
            sanitized_tokens.append(token)

    return " ".join(sanitized_tokens)


def build_fts_search_query(
    query: str,
    source_filter: Optional[list[str]] = None,
    role_filter: Optional[list[str]] = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[str, list[Any]]:
    """FTS5 전문 검색을 위한 SQL 문과 바인딩 파라미터 리스트를 순수하게 조립하여 반환한다."""
    sanitized = sanitize_fts_query(query)
    if not sanitized:
        return ("", [])

    if source_filter is None:
        source_filter = ["cli", "telegram", "discord", "whatsapp", "slack"]

    where_clauses = ["messages_fts MATCH ?", "m.is_copy = 0"]
    params: list[Any] = [sanitized]

    source_placeholders = ",".join("?" for _ in source_filter)
    where_clauses.append(f"s.source IN ({source_placeholders})")
    params.extend(source_filter)

    if role_filter:
        role_placeholders = ",".join("?" for _ in role_filter)
        where_clauses.append(f"m.role IN ({role_placeholders})")
        params.extend(role_filter)

    where_sql = " AND ".join(where_clauses)
    params.extend([limit, offset])

    sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """
    return (sql, params)


def build_checkpoint_update_query(
    checkpoint_id: int,
    status: str,
    now: float,
    reason: Optional[str] = None,
    after_sha256: Optional[str] = None,
) -> tuple[str, tuple[Any, ...]]:
    """체크포인트 상태 전이 및 update SQL과 바인딩 파라미터를 조립하여 반환한다."""
    allowed_previous = validate_checkpoint_transition(status, after_sha256)
    placeholders = ", ".join("?" for _ in allowed_previous)

    if status == CHECKPOINT_STATUS_APPLIED:
        sql = f"""UPDATE file_checkpoints
               SET status = ?, updated_at = ?, reason = ?, after_sha256 = ?
               WHERE id = ? AND status IN ({placeholders})"""
        params: tuple[Any, ...] = (status, now, reason, after_sha256, checkpoint_id, *allowed_previous)
    else:
        sql = f"""UPDATE file_checkpoints
               SET status = ?, updated_at = ?, reason = ?
               WHERE id = ? AND status IN ({placeholders})"""
        params = (status, now, reason, checkpoint_id, *allowed_previous)

    return (sql, params)
