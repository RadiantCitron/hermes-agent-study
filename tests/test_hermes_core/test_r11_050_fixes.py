"""로드맵 11 050 발견 수정(Fixes) 인수 테스트: F1 턴별 터미널·위임 경고 어긋남 수정 검증.

인수 테스트 시나리오 22, 23, 24.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hermes_core.session_db import SessionDB
from hermes_core.tools.checkpoint import Checkpointer


@pytest.fixture
def f1_env(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    session_id = db.create_session("parent_model")
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.chdir(ws)

    # 시간 모킹 헬퍼
    current_time = [100.0]

    def mock_time():
        return current_time[0]

    monkeypatch.setattr("hermes_core.tools.checkpoint.time.time", mock_time)

    cp = Checkpointer(db=db, session_id=session_id, workspace=ws)
    return {
        "db": db,
        "session_id": session_id,
        "workspace": ws,
        "cp": cp,
        "current_time": current_time,
    }


def test_scenario_22_f1_terminal_warning_per_turn_interval(f1_env):
    """시나리오 22: F1 경고 누락 및 오탐 제거 (턴별 터미널 승인 구간 분리)."""
    db = f1_env["db"]
    session_id = f1_env["session_id"]
    ws = f1_env["workspace"]
    cp = f1_env["cp"]
    current_time = f1_env["current_time"]

    # 턴 1: 시각 100
    current_time[0] = 100.0
    cp.begin_turn()

    # 턴 1 구간: 승인된 터미널 1건 (시각 150)
    req_id = db.record_approval_request(
        session_id=session_id,
        argv=["echo", "1"],
        tool_name="terminal",
    )
    db.resolve_approval_request(req_id, "approved")
    db._conn.execute(
        "UPDATE approval_requests SET decided_at = 150.0 WHERE id = ?",
        (req_id,),
    )
    db._conn.commit()

    # 턴 1 파일 쓰기
    cid_1 = cp.record_before("write_file", "a.txt")
    (ws / "a.txt").write_text("v1", encoding="utf-8")
    cp.finalize(cid_1, "write_file", "a.txt", result_success=True)

    # 턴 2: 시각 200
    current_time[0] = 200.0
    cp.begin_turn()

    # 턴 2 파일 쓰기
    cid_2 = cp.record_before("write_file", "b.txt")
    (ws / "b.txt").write_text("v2", encoding="utf-8")
    cp.finalize(cid_2, "write_file", "b.txt", result_success=True)

    # 턴 2 계획 확인: 터미널 승인은 턴 1 구간이었으므로 턴 2 계획에는 경고가 없어야 함
    plan_2 = cp.plan_rollback()
    assert plan_2.turn_index == 2
    assert plan_2.terminal_approvals_count == 0
    assert not plan_2.has_warnings

    # 턴 2 되돌리기 실행
    res_2 = cp.execute_rollback(plan_2)
    assert res_2.rolled_back_count == 1
    assert not (ws / "b.txt").exists()

    # 턴 1 계획 확인: 대상 턴 1. 승인된 터미널 1건이 정확히 잡히고 has_warnings가 True여야 함
    plan_1 = cp.plan_rollback()
    assert plan_1.turn_index == 1
    assert plan_1.terminal_approvals_count == 1
    assert plan_1.has_warnings


def test_scenario_23_f1_subagent_warning_per_turn_interval(f1_env):
    """시나리오 23: F1 위임 자식 세션 턴별 구간 분리 검증."""
    db = f1_env["db"]
    session_id = f1_env["session_id"]
    ws = f1_env["workspace"]
    cp = f1_env["cp"]
    current_time = f1_env["current_time"]

    # 턴 1: 시각 100
    current_time[0] = 100.0
    cp.begin_turn()

    # 턴 1 구간: 자식 세션 생성 (시각 150)
    child_sid = db.create_session("child_model")
    db._conn.execute(
        "UPDATE sessions SET parent_session_id = ?, started_at = 150.0 WHERE id = ?",
        (session_id, child_sid),
    )
    db._conn.commit()

    # 턴 1 파일 쓰기
    cid_1 = cp.record_before("write_file", "a.txt")
    (ws / "a.txt").write_text("v1", encoding="utf-8")
    cp.finalize(cid_1, "write_file", "a.txt", result_success=True)

    # 턴 2: 시각 200
    current_time[0] = 200.0
    cp.begin_turn()

    # 턴 2 파일 쓰기
    cid_2 = cp.record_before("write_file", "b.txt")
    (ws / "b.txt").write_text("v2", encoding="utf-8")
    cp.finalize(cid_2, "write_file", "b.txt", result_success=True)

    # 턴 2 계획 확인
    plan_2 = cp.plan_rollback()
    assert plan_2.turn_index == 2
    assert plan_2.subagent_count == 0
    assert not plan_2.has_warnings

    # 턴 2 되돌리기
    cp.execute_rollback(plan_2)

    # 턴 1 계획 확인
    plan_1 = cp.plan_rollback()
    assert plan_1.turn_index == 1
    assert plan_1.subagent_count == 1
    assert plan_1.has_warnings


def test_scenario_24_f1_invariants_and_fallbacks(f1_env):
    """시나리오 24: F1 마지막 턴, begin_turn 미호출, turn_started_at 속성 등 불변 확인."""
    db = f1_env["db"]
    session_id = f1_env["session_id"]
    ws = f1_env["workspace"]
    cp = f1_env["cp"]
    current_time = f1_env["current_time"]

    # begin_turn 미호출 상태 불변 확인
    assert cp.turn_started_at == 0.0
    plan_empty = cp.plan_rollback()
    assert plan_empty.terminal_approvals_count == 0
    assert plan_empty.subagent_count == 0

    # 턴 1 시작: turn_started_at이 최신 시각으로 갱신됨
    current_time[0] = 123.45
    cp.begin_turn()
    assert cp.turn_started_at == 123.45

    # 마지막 턴(끝 없음)에서는 시작 이후 전체 카운트 확인
    req_id = db.record_approval_request(
        session_id=session_id,
        argv=["echo", "test"],
        tool_name="terminal",
    )
    db.resolve_approval_request(req_id, "approved")
    db._conn.execute(
        "UPDATE approval_requests SET decided_at = 130.0 WHERE id = ?",
        (req_id,),
    )
    db._conn.commit()

    cid = cp.record_before("write_file", "f.txt")
    (ws / "f.txt").write_text("hi", encoding="utf-8")
    cp.finalize(cid, "write_file", "f.txt", result_success=True)

    plan_last = cp.plan_rollback()
    assert plan_last.terminal_approvals_count == 1
    assert plan_last.has_warnings

    # plan_rollback(turn_index=N) 호출 시그니처 유지 확인
    plan_explicit = cp.plan_rollback(turn_index=1)
    assert plan_explicit.turn_index == 1
    assert plan_explicit.terminal_approvals_count == 1
