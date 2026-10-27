"""SQLite 기반 세션 원장 및 FTS5 전문 검색 엔진.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/hermes_state.py (SessionDB 클래스)
- 연구 및 학습 포인트:
  1. 에피소딕 세션 원장(Episodic Session Store):
     - 모든 대화 턴과 도구 호출/응답을 SQLite에 원장으로 누적 보존.
     - 컨텍스트 압축 발생 시 `parent_session_id` 체인으로 부모-자식 세션 연결.
  2. SQLite FTS5(Full-Text Search) 전문 검색:
     - 대화 내용이 messages 테이블에 INSERT될 때 트리거를 통해 FTS5 가상 테이블에 자동 인덱싱.
     - `search_messages()`를 통해 과거 대화 내용의 키워드 및 스니펫 고속 검색.
  3. WAL 모드 및 메모리 모드 지원:
     - 실서비스에서는 WAL(Write-Ahead Logging)로 동시 읽기/쓰기 지원.
     - 테스트 시에는 `:memory:` 모드를 지원하여 테스트 간 상태 격리 보장.
"""

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional, Union

from hermes_core.constants import get_hermes_home

SCHEMA_VERSION = 2

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
    finish_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
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


class SessionDB:
    """SQLite + FTS5 세션 데이터베이스 관리 클래스."""

    def __init__(self, db_path: Optional[Union[Path, str]] = None):
        if db_path is None:
            self.db_path = get_hermes_home() / "state.db"
        elif str(db_path) == ":memory:":
            self.db_path = ":memory:"
        else:
            self.db_path = Path(db_path)

        if isinstance(self.db_path, Path):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row

        # 메모리 DB가 아닐 때만 WAL 모드 적용
        if str(self.db_path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    def _init_schema(self) -> None:
        """테이블 스키마 및 FTS5 가상 테이블 초기화."""
        cursor = self._conn.cursor()
        cursor.executescript(SCHEMA_SQL)

        # 스키마 버전 초기화
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass
                cursor.execute("UPDATE schema_version SET version = 2")

        # FTS5 가상 테이블 초기화
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        self._conn.commit()

    def close(self) -> None:
        """DB 커넥션 종료."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # =========================================================================
    # 세션 수명주기 메서드
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        source: str = "cli",
        model: Optional[str] = None,
        model_config: Optional[dict[str, Any]] = None,
        system_prompt: Optional[str] = None,
        user_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
    ) -> str:
        """새 세션 레코드 생성 및 session_id 반환."""
        self._conn.execute(
            """INSERT INTO sessions (id, source, user_id, model, model_config,
               system_prompt, parent_session_id, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                source,
                user_id,
                model,
                json.dumps(model_config) if model_config else None,
                system_prompt,
                parent_session_id,
                time.time(),
            ),
        )
        self._conn.commit()
        return session_id

    def end_session(self, session_id: str, end_reason: str = "completed") -> None:
        """세션 종료 기록."""
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), end_reason, session_id),
        )
        self._conn.commit()

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """시스템 프롬프트 스냅샷 갱신."""
        self._conn.execute(
            "UPDATE sessions SET system_prompt = ? WHERE id = ?",
            (system_prompt, session_id),
        )
        self._conn.commit()

    def update_token_counts(
        self, session_id: str, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """세션의 토큰 사용량 카운터 누적."""
        self._conn.execute(
            """UPDATE sessions SET
               input_tokens = input_tokens + ?,
               output_tokens = output_tokens + ?
               WHERE id = ?""",
            (input_tokens, output_tokens, session_id),
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """세션 ID로 단일 세션 정보 조회."""
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    # =========================================================================
    # 메시지 원장 추가 및 조회 메서드
    # =========================================================================

    def append_message(
        self,
        session_id: str,
        role: str,
        content: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_calls: Optional[Any] = None,
        tool_call_id: Optional[str] = None,
        token_count: Optional[int] = None,
        finish_reason: Optional[str] = None,
    ) -> int:
        """메시지를 추가하고 FTS5 동기화 및 세션 카운터 갱신. 생성된 row ID 반환."""
        cursor = self._conn.execute(
            """INSERT INTO messages (session_id, role, content, tool_call_id,
               tool_calls, tool_name, timestamp, token_count, finish_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                tool_call_id,
                json.dumps(tool_calls) if tool_calls else None,
                tool_name,
                time.time(),
                token_count,
                finish_reason,
            ),
        )
        msg_id = cursor.lastrowid

        is_tool_related = role == "tool" or tool_calls is not None
        if is_tool_related:
            self._conn.execute(
                """UPDATE sessions SET message_count = message_count + 1,
                   tool_call_count = tool_call_count + 1 WHERE id = ?""",
                (session_id,),
            )
        else:
            self._conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )

        self._conn.commit()
        return msg_id

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """세션의 모든 메시지 원장을 타임스탬프 순서대로 반환."""
        cursor = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        )
        result = []
        for row in cursor.fetchall():
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(msg)
        return result

    def get_messages_as_conversation(self, session_id: str) -> list[dict[str, Any]]:
        """LLM API에 즉시 전달 가능한 OpenAI 규격 대화 메시지 포맷으로 반환."""
        cursor = self._conn.execute(
            "SELECT role, content, tool_call_id, tool_calls, tool_name "
            "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        )
        messages = []
        for row in cursor.fetchall():
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            messages.append(msg)
        return messages

    # =========================================================================
    # FTS5 전문 검색 메서드
    # =========================================================================

    def search_messages(
        self,
        query: str,
        source_filter: Optional[list[str]] = None,
        role_filter: Optional[list[str]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """FTS5 전문 검색을 통해 키워드가 일치하는 메시지와 하이라이트 스니펫 반환."""
        if not query or not query.strip():
            return []

        if source_filter is None:
            source_filter = ["cli", "telegram", "discord", "whatsapp", "slack"]

        where_clauses = ["messages_fts MATCH ?"]
        params: list[Any] = [query]

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

        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def search_sessions(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """간편 세션 검색 인터페이스."""
        return self.search_messages(query=query, limit=limit)
