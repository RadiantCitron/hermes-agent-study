"""11번 로드맵 020 단계 체크포인트 모듈(Checkpointer) 단위 및 인수 테스트.

근거: docs/로드맵/11-체크포인트-롤백-MVP/instructions/020_체크포인트_모듈.md
인수 테스트 시나리오 1~22 구현.
"""

import hashlib
import os
from pathlib import Path
import pytest

from hermes_core.constants import (
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_PENDING,
    CHECKPOINT_STATUS_ROLLED_BACK,
    CHECKPOINT_STATUS_SKIPPED,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.checkpoint import (
    CheckpointLimitError,
    Checkpointer,
    RollbackPlan,
)


@pytest.fixture
def test_env(tmp_path):
    """임시 DB와 작업 폴더를 준비하는 픽스처."""
    db_path = tmp_path / "state.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path)
    session_id = "s_test_cp"
    db.create_session(session_id)
    return db, session_id, workspace


def test_scenario_01_init_and_begin_turn(test_env):
    """시나리오 1: Checkpointer 초기화 및 begin_turn() 호출 -> turn_index == 1, turn_started_at > 0."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    assert cp.turn_index == 0

    t1 = cp.begin_turn()
    assert t1 == 1
    assert cp.turn_index == 1
    assert cp.turn_started_at > 0

    t2 = cp.begin_turn(5)
    assert t2 == 5
    assert cp.turn_index == 5

    with pytest.raises(ValueError):
        cp.begin_turn(0)
    with pytest.raises(ValueError):
        cp.begin_turn(-1)
    with pytest.raises(ValueError):
        cp.begin_turn(True)


def test_scenario_02_record_before_requires_turn_begun(test_env):
    """시나리오 2: turn_index 시작 전 record_before 호출 -> ValueError('Turn has not begun')."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    with pytest.raises(ValueError, match="Turn has not begun"):
        cp.record_before("patch", "a.txt")


def test_scenario_03_record_before_invalid_tool_name(test_env):
    """시나리오 3: 허용 외 도구('terminal')로 record_before -> ValueError."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    with pytest.raises(ValueError, match="tool_name must be one of"):
        cp.record_before("terminal", "a.txt")


def test_scenario_04_record_before_outside_workspace(test_env):
    """시나리오 4: 작업 폴더 밖 경로('../../secret.txt')로 record_before -> None 반환 및 행 미생성."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("patch", "../../secret.txt")
    assert cid is None
    assert len(db.get_file_checkpoints(sid)) == 0


def test_scenario_05_record_before_protected_files(test_env):
    """시나리오 5: 보호 파일('.env', '.git/config')로 record_before -> None 반환 및 행 미생성."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    for protected in (".env", ".env.local", ".git/config"):
        cid = cp.record_before("write_file", protected)
        assert cid is None
    assert len(db.get_file_checkpoints(sid)) == 0


def test_scenario_06_record_before_instruction_files(test_env):
    """시나리오 6: 쓰기 금지 파일('AGENTS.md', 'SOUL.md', '.cursorrules')로 record_before -> None 반환 및 행 미생성."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    for forbidden in ("AGENTS.md", "SOUL.md", ".cursorrules"):
        cid = cp.record_before("write_file", forbidden)
        assert cid is None
    assert len(db.get_file_checkpoints(sid)) == 0


def test_scenario_07_record_before_existing_file(test_env):
    """시나리오 7: 정상 기존 파일 수정 전 record_before -> cid 반환, DB에 pending, before_content 일치."""
    db, sid, ws = test_env
    target = ws / "a.txt"
    target.write_text("원래 한글 텍스트\n", encoding="utf-8")

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("patch", "a.txt")
    assert cid is not None and cid > 0

    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0]["id"] == cid
    assert rows[0]["status"] == CHECKPOINT_STATUS_PENDING
    assert rows[0]["before_exists"] == 1
    assert rows[0]["before_content"] == "원래 한글 텍스트\n"
    assert rows[0]["rel_path"] == "a.txt"


def test_scenario_08_record_before_new_file(test_env):
    """시나리오 8: 새 파일 생성 전 record_before -> cid 반환, DB에 pending, before_exists=0, content=None."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("write_file", "new.txt")
    assert cid is not None and cid > 0

    rows = db.get_file_checkpoints(sid)
    assert len(rows) == 1
    assert rows[0]["id"] == cid
    assert rows[0]["status"] == CHECKPOINT_STATUS_PENDING
    assert rows[0]["before_exists"] == 0
    assert rows[0]["before_content"] is None


def test_scenario_09_session_bytes_limit_exceeded(test_env):
    """시나리오 9: 세션 바이트 상한 초과 파일 수정 시도 -> CheckpointLimitError 발생 및 DB 행 미증가."""
    db, sid, ws = test_env
    target = ws / "large.txt"
    target.write_text("123456789012345", encoding="utf-8")  # 15 bytes

    # 상한을 10바이트로 작게 주입
    cp = Checkpointer(db, sid, ws, bytes_limit=10)
    cp.begin_turn()

    with pytest.raises(CheckpointLimitError):
        cp.record_before("patch", "large.txt")

    assert len(db.get_file_checkpoints(sid)) == 0


def test_scenario_10_finalize_new_file_success(test_env):
    """시나리오 10: finalize: 새 파일 생성 성공 확인 -> applied, after_sha256 기록."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("write_file", "created.txt")

    # 도구 실행 시뮬레이션: 파일 생성
    target = ws / "created.txt"
    target.write_text("생성된 내용", encoding="utf-8")
    expected_sha = hashlib.sha256("생성된 내용".encode("utf-8")).hexdigest()

    status = cp.finalize(cid, "write_file", "created.txt", result_success=True)
    assert status == CHECKPOINT_STATUS_APPLIED

    rows = db.get_file_checkpoints(sid)
    assert rows[0]["status"] == CHECKPOINT_STATUS_APPLIED
    assert rows[0]["after_sha256"] == expected_sha


def test_scenario_11_finalize_modify_file_success(test_env):
    """시나리오 11: finalize: 파일 내용 변경 성공 확인 -> applied, 새 해시 기록."""
    db, sid, ws = test_env
    target = ws / "mod.txt"
    target.write_text("old", encoding="utf-8")

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("patch", "mod.txt")

    # 도구 실행 시뮬레이션: 내용 변경
    target.write_text("new content", encoding="utf-8")
    expected_sha = hashlib.sha256(b"new content").hexdigest()

    status = cp.finalize(cid, "patch", "mod.txt", result_success=True)
    assert status == CHECKPOINT_STATUS_APPLIED

    rows = db.get_file_checkpoints(sid)
    assert rows[0]["status"] == CHECKPOINT_STATUS_APPLIED
    assert rows[0]["after_sha256"] == expected_sha


def test_scenario_12_finalize_file_unchanged_failure(test_env):
    """시나리오 12: finalize: 파일 내용 미변경 (도구 실패) 확인 -> failed, reason='내용 미변경'."""
    db, sid, ws = test_env
    target = ws / "unmod.txt"
    target.write_text("same", encoding="utf-8")

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("patch", "unmod.txt")

    # 도구 실행 실패 시뮬레이션: 내용 바꾸지 않음
    status = cp.finalize(cid, "patch", "unmod.txt", result_success=False)
    assert status == CHECKPOINT_STATUS_FAILED

    rows = db.get_file_checkpoints(sid)
    assert rows[0]["status"] == CHECKPOINT_STATUS_FAILED
    assert "내용 미변경" in (rows[0]["reason"] or "")


def test_scenario_13_finalize_broken_output_or_exception_follows_filesystem(test_env):
    """시나리오 13: finalize: 깨진 JSON / 도구 예외 상황에서도 파일 상태로 확정."""
    db, sid, ws = test_env
    target = ws / "robust.txt"
    target.write_text("v1", encoding="utf-8")

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    cid = cp.record_before("patch", "robust.txt")

    # 도구가 예외를 냈으나 파일은 실제 바뀌었음
    target.write_text("v2", encoding="utf-8")
    status = cp.finalize(cid, "patch", "robust.txt", result_success=False, exception=RuntimeError("crash"))
    assert status == CHECKPOINT_STATUS_APPLIED

    rows = db.get_file_checkpoints(sid)
    assert rows[0]["status"] == CHECKPOINT_STATUS_APPLIED


def test_scenario_14_plan_rollback_reverse_order(test_env):
    """시나리오 14: plan_rollback: 대상 턴의 변경 파일 역순(id 내림차순) 정렬 수집."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    cid1 = cp.record_before("write_file", "f1.txt")
    (ws / "f1.txt").write_text("c1", encoding="utf-8")
    cp.finalize(cid1, "write_file", "f1.txt", result_success=True)

    cid2 = cp.record_before("write_file", "f2.txt")
    (ws / "f2.txt").write_text("c2", encoding="utf-8")
    cp.finalize(cid2, "write_file", "f2.txt", result_success=True)

    plan = cp.plan_rollback(1)
    assert len(plan.items) == 2
    assert plan.items[0].checkpoint_id == cid2
    assert plan.items[1].checkpoint_id == cid1


def test_scenario_15_plan_rollback_conflict_detection(test_env):
    """시나리오 15: plan_rollback: 외부에서 변경된 파일 충돌 감지 -> will_skip=True."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    cid = cp.record_before("write_file", "conf.txt")
    (ws / "conf.txt").write_text("agent content", encoding="utf-8")
    cp.finalize(cid, "write_file", "conf.txt", result_success=True)

    # 사람이 외부에서 파일 수정
    (ws / "conf.txt").write_text("human changed this", encoding="utf-8")

    plan = cp.plan_rollback(1)
    assert len(plan.items) == 1
    assert plan.items[0].will_skip is True
    assert "충돌(외부 변경)" in plan.items[0].skip_reason


def test_scenario_16_plan_rollback_workspace_mismatch(test_env):
    """시나리오 16: plan_rollback: 작업 폴더가 변경된 경우 건너뜀."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    cid = cp.record_before("write_file", "ws_test.txt")
    (ws / "ws_test.txt").write_text("agent content", encoding="utf-8")
    cp.finalize(cid, "write_file", "ws_test.txt", result_success=True)

    # 다른 작업 폴더로 Checkpointer 생성
    other_ws = ws.parent / "other_ws"
    other_ws.mkdir(parents=True, exist_ok=True)
    cp_other = Checkpointer(db, sid, other_ws)

    plan = cp_other.plan_rollback(1)
    assert len(plan.items) == 1
    assert plan.items[0].will_skip is True
    assert plan.items[0].skip_reason == "작업 폴더 불일치"


def test_scenario_17_plan_rollback_pending_row_skipped_and_warning(test_env):
    """시나리오 17: plan_rollback: pending 잔존 행 건너뜀 및 경고 -> will_skip=True, has_warnings=True."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    # finalize되지 않고 남은 pending 행
    cid = cp.record_before("write_file", "pending.txt")

    plan = cp.plan_rollback(1)
    assert len(plan.items) == 1
    assert plan.items[0].will_skip is True
    assert "결과 모름(pending)" in plan.items[0].skip_reason
    assert plan.has_warnings is True


def test_scenario_18_plan_rollback_detects_terminal_approvals_warning(test_env):
    """시나리오 18: plan_rollback: 턴 내 승인된 터미널 실행 감지 시 경고."""
    db, sid, ws = test_env
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    cid = cp.record_before("write_file", "norm.txt")
    (ws / "norm.txt").write_text("agent content", encoding="utf-8")
    cp.finalize(cid, "write_file", "norm.txt", result_success=True)

    # 턴 시작 이후 터미널 명령 승인 기록 추가
    req_id = db.record_approval_request(sid, ["rm", "-rf", "temp"])
    db.resolve_approval_request(req_id, "approved")

    plan = cp.plan_rollback(1)
    assert plan.terminal_approvals_count >= 1
    assert plan.has_warnings is True


def test_scenario_19_execute_rollback_new_file_deleted(test_env):
    """시나리오 19: execute_rollback: 새 파일 생성 턴 되돌리기 -> 파일 삭제, 부모 폴더 보존, rolled_back."""
    db, sid, ws = test_env
    sub_dir = ws / "sub"
    sub_dir.mkdir(parents=True, exist_ok=True)

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)
    cid = cp.record_before("write_file", "sub/del.txt")
    (sub_dir / "del.txt").write_text("newly created", encoding="utf-8")
    cp.finalize(cid, "write_file", "sub/del.txt", result_success=True)

    plan = cp.plan_rollback(1)
    res = cp.execute_rollback(plan)

    assert res.rolled_back_count == 1
    assert res.skipped_count == 0
    assert not (sub_dir / "del.txt").exists()
    assert sub_dir.exists()  # 부모 폴더는 보존됨

    rows = db.get_file_checkpoints(sid)
    assert rows[0]["status"] == CHECKPOINT_STATUS_ROLLED_BACK


def test_scenario_20_execute_rollback_modified_file_restored(test_env):
    """시나리오 20: execute_rollback: 기존 파일 수정 턴 되돌리기 -> 원래 내용 복원, 권한 보존, rolled_back."""
    db, sid, ws = test_env
    target = ws / "restore.txt"
    target.write_text("original content\n", encoding="utf-8")
    os.chmod(target, 0o644)

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)
    cid = cp.record_before("patch", "restore.txt")
    target.write_text("agent modified this\n", encoding="utf-8")
    cp.finalize(cid, "patch", "restore.txt", result_success=True)

    plan = cp.plan_rollback(1)
    res = cp.execute_rollback(plan)

    assert res.rolled_back_count == 1
    assert res.skipped_count == 0
    assert target.read_text(encoding="utf-8") == "original content\n"

    rows = db.get_file_checkpoints(sid)
    assert rows[0]["status"] == CHECKPOINT_STATUS_ROLLED_BACK


def test_scenario_21_execute_rollback_skips_conflict_and_restores_others(test_env):
    """시나리오 21: execute_rollback: 충돌 파일은 건너뛰고 정상 파일만 복원."""
    db, sid, ws = test_env
    f1 = ws / "f1.txt"
    f2 = ws / "f2.txt"
    f1.write_text("orig1", encoding="utf-8")
    f2.write_text("orig2", encoding="utf-8")

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    cid1 = cp.record_before("patch", "f1.txt")
    f1.write_text("agent1", encoding="utf-8")
    cp.finalize(cid1, "patch", "f1.txt", result_success=True)

    cid2 = cp.record_before("patch", "f2.txt")
    f2.write_text("agent2", encoding="utf-8")
    cp.finalize(cid2, "patch", "f2.txt", result_success=True)

    # f1만 사람이 변경 (충돌 발생)
    f1.write_text("human changed f1", encoding="utf-8")

    plan = cp.plan_rollback(1)
    res = cp.execute_rollback(plan)

    assert res.rolled_back_count == 1  # f2
    assert res.skipped_count == 1      # f1
    assert f1.read_text(encoding="utf-8") == "human changed f1"  # 사람 변경 보존
    assert f2.read_text(encoding="utf-8") == "orig2"             # 정상 복원

    rows = {r["rel_path"]: r["status"] for r in db.get_file_checkpoints(sid)}
    assert rows["f1.txt"] == CHECKPOINT_STATUS_SKIPPED
    assert rows["f2.txt"] == CHECKPOINT_STATUS_ROLLED_BACK


def test_scenario_22_same_file_modified_twice_in_one_turn(test_env):
    """시나리오 22: 한 턴에 같은 파일 2회 수정 후 되돌리기 -> 원래 내용으로 완벽 복원."""
    db, sid, ws = test_env
    target = ws / "twice.txt"
    target.write_text("initial 0", encoding="utf-8")

    cp = Checkpointer(db, sid, ws)
    cp.begin_turn(1)

    # 1차 수정
    cid1 = cp.record_before("patch", "twice.txt")
    target.write_text("step 1", encoding="utf-8")
    cp.finalize(cid1, "patch", "twice.txt", result_success=True)

    # 2차 수정
    cid2 = cp.record_before("patch", "twice.txt")
    target.write_text("step 2", encoding="utf-8")
    cp.finalize(cid2, "patch", "twice.txt", result_success=True)

    plan = cp.plan_rollback(1)
    res = cp.execute_rollback(plan)

    assert res.rolled_back_count == 2
    assert res.skipped_count == 0
    assert target.read_text(encoding="utf-8") == "initial 0"

    rows = db.get_file_checkpoints(sid)
    assert all(r["status"] == CHECKPOINT_STATUS_ROLLED_BACK for r in rows)
