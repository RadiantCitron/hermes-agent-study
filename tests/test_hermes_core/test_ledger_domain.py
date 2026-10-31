"""R21-010: 원장 순수 도메인(ledger_domain) 및 SessionDB 슬림화 검증 테스트.

인수 테스트 시나리오 1~8:
1. 승인 요청 및 결정 순수 검증
2. 파일 체크포인트 입력값 순수 검증
3. 체크포인트 상태 전이 및 SHA-256 검증
4. FTS5 전문 검색 쿼리 빌더
5. 도구 이름 맵핑 추출
6. 제어 감사 입력값 순수 검증
7. SessionDB 라인 수(< 800) 및 기본 원장 회귀 검증
8. ledger_domain 순수성(sqlite3, _conn 무참조) AST 검증
"""

import ast
from pathlib import Path
import pytest

from hermes_core.ledger_domain import (
    FTS_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    build_checkpoint_update_query,
    build_fts_search_query,
    extract_tool_name_map,
    is_valid_hex_sha256,
    validate_approval_decision,
    validate_approval_request_argv,
    validate_checkpoint_transition,
    validate_control_audit_input,
    validate_deactivate_after_id,
    validate_file_checkpoint_input,
)
from hermes_core.session_db import SessionDB


def test_scenario_r21_ledger_domain_approval_validation() -> None:
    """시나리오 1: 승인 요청 argv 및 결정 검증."""
    # 비어있는 리스트
    with pytest.raises(ValueError, match="argv must be a non-empty list of strings"):
        validate_approval_request_argv([])

    # 문자열이 아닌 원소 포함
    with pytest.raises(ValueError, match="argv must be a non-empty list of strings"):
        validate_approval_request_argv(["ls", 123])

    # 리스트가 아닌 입력
    with pytest.raises(ValueError, match="argv must be a non-empty list of strings"):
        validate_approval_request_argv("ls -la")

    # 유효한 argv
    assert validate_approval_request_argv(["ls", "-la"]) == ["ls", "-la"]

    # 승인 결정 유효성
    with pytest.raises(ValueError, match="decision must be one of"):
        validate_approval_decision("invalid_decision")

    validate_approval_decision("approved")
    validate_approval_decision("denied")
    validate_approval_decision("cancelled")


def test_scenario_r21_ledger_domain_checkpoint_input_validation() -> None:
    """시나리오 2: 체크포인트 입력값 유효성 검증."""
    valid_args = {
        "session_id": "sess_1",
        "turn_index": 1,
        "tool_name": "write_file",
        "workspace": "/tmp/test",
        "rel_path": "a.txt",
        "before_exists": True,
        "before_content": "hello",
    }

    # 정상 통과
    validate_file_checkpoint_input(**valid_args)

    # turn_index < 1
    with pytest.raises(ValueError, match="turn_index must be an integer >= 1"):
        validate_file_checkpoint_input(**{**valid_args, "turn_index": 0})

    # tool_name 불일치
    with pytest.raises(ValueError, match="tool_name must be one of"):
        validate_file_checkpoint_input(**{**valid_args, "tool_name": "invalid_tool"})

    # workspace 공백
    with pytest.raises(ValueError, match="workspace must be a non-empty string"):
        validate_file_checkpoint_input(**{**valid_args, "workspace": ""})

    # rel_path 공백
    with pytest.raises(ValueError, match="rel_path must be a non-empty string"):
        validate_file_checkpoint_input(**{**valid_args, "rel_path": ""})

    # before_exists가 bool이 아님
    with pytest.raises(ValueError, match="before_exists must be a boolean"):
        validate_file_checkpoint_input(**{**valid_args, "before_exists": "True"})

    # before_exists=True인데 before_content=None
    with pytest.raises(ValueError, match="before_content must be a string when before_exists is True"):
        validate_file_checkpoint_input(**{**valid_args, "before_content": None})

    # before_exists=False인데 before_content가 None이 아님
    with pytest.raises(ValueError, match="before_content must be None when before_exists is False"):
        validate_file_checkpoint_input(**{**valid_args, "before_exists": False, "before_content": "foo"})


def test_scenario_r21_ledger_domain_checkpoint_transition_validation() -> None:
    """시나리오 3: 체크포인트 상태 전이 및 after_sha256 검증."""
    # 유효하지 않은 status
    with pytest.raises(ValueError, match="status must be one of"):
        validate_checkpoint_transition("unknown_status")

    # status == applied인데 sha256 불일치
    with pytest.raises(ValueError, match="after_sha256 must be a 64-character lowercase hex string"):
        validate_checkpoint_transition("applied", after_sha256="NOT_HEX_64")

    valid_sha = "a" * 64
    allowed = validate_checkpoint_transition("applied", after_sha256=valid_sha)
    assert "pending" in allowed

    # status != applied인데 after_sha256이 전달됨
    with pytest.raises(ValueError, match="after_sha256 can only be set when status is 'applied'"):
        validate_checkpoint_transition("failed", after_sha256=valid_sha)

    # 쿼리 빌더 검증
    sql, params = build_checkpoint_update_query(
        checkpoint_id=42,
        status="applied",
        now=1234567.89,
        reason="success",
        after_sha256=valid_sha,
    )
    assert "UPDATE file_checkpoints" in sql
    assert "after_sha256 = ?" in sql
    assert params[0] == "applied"
    assert params[3] == valid_sha
    assert params[4] == 42


def test_scenario_r21_ledger_domain_fts_query_builder() -> None:
    """시나리오 4: FTS5 전문 검색 쿼리 빌더."""
    # 공백 쿼리
    empty_sql, empty_params = build_fts_search_query("   ")
    assert empty_sql == ""
    assert empty_params == []

    # 일반 쿼리
    sql, params = build_fts_search_query(
        query="error log",
        source_filter=["cli", "telegram"],
        role_filter=["user"],
        limit=10,
        offset=5,
    )
    assert "messages_fts MATCH ?" in sql
    assert "m.is_copy = 0" in sql
    assert "s.source IN (?,?)" in sql
    assert "m.role IN (?)" in sql
    assert "LIMIT ? OFFSET ?" in sql
    assert params == ["error log", "cli", "telegram", "user", 10, 5]


def test_scenario_r21_ledger_domain_tool_name_map_extraction() -> None:
    """시나리오 5: tool_calls에서 tool_name 맵핑 추출."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "read_file"}},
                {"id": "call_2", "name": "write_file"},
            ],
        },
        {"role": "user", "content": "hello"},
    ]
    mapping = extract_tool_name_map(messages)
    assert mapping == {"call_1": "read_file", "call_2": "write_file"}


def test_scenario_r21_ledger_domain_control_audit_validation() -> None:
    """시나리오 6: 제어 감사 입력값 유효성 검증."""
    # 정상 케이스
    validate_control_audit_input(
        session_id="s1",
        source="cli",
        command="set_setting",
        target="theme",
        result="applied",
        old_value="dark",
        new_value="light",
        reason="user request",
    )

    # 비어있는 session_id
    with pytest.raises(ValueError, match="session_id must be a non-empty string"):
        validate_control_audit_input("   ", "cli", "set_setting", "t", "applied")

    # 유효하지 않은 command
    with pytest.raises(ValueError, match="Invalid audit command"):
        validate_control_audit_input("s1", "cli", "invalid_cmd", "t", "applied")

    # 유효하지 않은 result
    with pytest.raises(ValueError, match="Invalid audit result"):
        validate_control_audit_input("s1", "cli", "set_setting", "t", "invalid_res")

    # deactivate_messages_after after_id 검증
    with pytest.raises(ValueError, match="after_id must be a non-negative integer"):
        validate_deactivate_after_id(-1)
    with pytest.raises(ValueError, match="after_id must be a non-negative integer"):
        validate_deactivate_after_id(True)
    validate_deactivate_after_id(0)
    validate_deactivate_after_id(10)


def test_scenario_r21_session_db_regression_and_line_count(tmp_path: Path) -> None:
    """시나리오 7: SessionDB 라인 수(< 800) 및 기본 원장 회귀 검증."""
    session_db_path = Path(__file__).resolve().parents[2] / "src" / "hermes_core" / "session_db.py"
    lines = session_db_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 800, f"session_db.py exceeds 800 lines ({len(lines)} lines)"

    # 실제 DB 동작 회귀 테스트
    db = SessionDB(":memory:")
    sess_id = db.create_session(session_id="s_test", source="cli")
    assert sess_id == "s_test"
    sess = db.get_session("s_test")
    assert sess is not None
    assert sess["id"] == "s_test"

    # 메시지 및 FTS
    db.append_message("s_test", "user", "authentication error occurred")
    db.append_message("s_test", "assistant", "I will fix the authentication issue.")
    results = db.search_messages("authentication")
    assert len(results) == 2

    # 승인 요청
    req_id = db.record_approval_request("s_test", ["rm", "-rf", "/tmp/junk"])
    assert req_id > 0
    resolved = db.resolve_approval_request(req_id, "approved", reason="allowed by test")
    assert resolved is True
    requests = db.get_approval_requests("s_test")
    assert len(requests) == 1
    assert requests[0]["decision"] == "approved"

    # 파일 체크포인트
    cp_id = db.record_file_checkpoint(
        session_id="s_test",
        turn_index=1,
        tool_name="write_file",
        workspace=str(tmp_path),
        rel_path="foo.txt",
        before_exists=False,
    )
    assert cp_id > 0
    updated = db.update_file_checkpoint(
        cp_id,
        "applied",
        after_sha256="b" * 64,
        reason="file written",
    )
    assert updated is True
    cps = db.get_file_checkpoints("s_test")
    assert len(cps) == 1
    assert cps[0]["status"] == "applied"

    # 제어 감사
    audit_id = db.record_control_audit(
        session_id="s_test",
        source="cli",
        command="set_setting",
        target="editor",
        old_value="nano",
        new_value="vim",
        result="applied",
    )
    assert audit_id > 0
    audits = db.list_control_audit("s_test")
    assert len(audits) == 1
    assert audits[0]["command"] == "set_setting"


def test_scenario_r21_no_conn_in_ledger_domain() -> None:
    """시나리오 8: ledger_domain 순수성(sqlite3, _conn 무참조) AST 검증."""
    domain_path = Path(__file__).resolve().parents[2] / "src" / "hermes_core" / "ledger_domain.py"
    source = domain_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(domain_path))

    for node in ast.walk(tree):
        # sqlite3 import 검사
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "sqlite3" not in alias.name, f"sqlite3 imported in ledger_domain: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "sqlite3", "sqlite3 imported in ledger_domain"

        # _conn 속성 참조 검사
        if isinstance(node, ast.Attribute):
            assert node.attr != "_conn", "._conn attribute accessed in ledger_domain"
