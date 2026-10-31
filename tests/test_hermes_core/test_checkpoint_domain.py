"""체크포인트 순수 도메인 모듈 단위 시험 (20번 로드맵, 030 단계, R-4).

[검증 원칙]
- 디스크 I/O나 SQLite DB 없이 순수 인메모리 객체만으로 판정 함수, 분류 함수, 알림 렌더러를 검증한다.
- 030 지시서의 8대 인수 테스트 시나리오를 엄밀히 검증한다.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import hermes_core.tools.checkpoint as cp_mod
import hermes_core.tools.checkpoint_domain as cpd_mod
from hermes_core.constants import (
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_PENDING,
    CHECKPOINT_STATUS_ROLLED_BACK,
    CHECKPOINT_STATUS_SKIPPED,
)
from hermes_core.tools.checkpoint_domain import (
    CheckpointError,
    CheckpointLimitError,
    InterruptedItem,
    InterruptedTurn,
    RollbackItem,
    RollbackItemResult,
    RollbackPlan,
    RollbackResult,
    classify_interrupted_rows,
    evaluate_rollback_row,
    format_interrupt_notice,
    format_pending_notices,
    format_rollback_notice,
)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_scenario_1_evaluate_rollback_row_six_branches():
    """시나리오 1: evaluate_rollback_row 단일 행 판정 6대 분기 검증."""
    sim_state: dict[str, tuple[bool, str | None]] = {}

    # 분기 1: 작업 폴더 불일치
    row1 = {
        "id": 1,
        "tool_name": "write_file",
        "workspace": "/other/ws",
        "rel_path": "a.txt",
        "before_exists": 0,
        "before_content": None,
        "after_sha256": _sha("content"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }
    item1, warn1 = evaluate_rollback_row(
        row1,
        resolved_workspace_str="/my/ws",
        path_check_error=None,
        current_file_state=(True, _sha("content")),
        simulated_file_state=sim_state,
    )
    assert item1.will_skip is True
    assert item1.skip_reason == "작업 폴더 불일치"
    assert warn1 is False

    # 분기 2: 경로 재판정 실패
    row2 = {
        "id": 2,
        "tool_name": "write_file",
        "workspace": "/my/ws",
        "rel_path": "../escape.txt",
        "before_exists": 0,
        "before_content": None,
        "after_sha256": _sha("content"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }
    item2, warn2 = evaluate_rollback_row(
        row2,
        resolved_workspace_str="/my/ws",
        path_check_error="상위 탈출 금지",
        current_file_state=(True, _sha("content")),
        simulated_file_state=sim_state,
    )
    assert item2.will_skip is True
    assert item2.skip_reason == "경로 재판정 실패 (상위 탈출 금지)"
    assert warn2 is False

    # 분기 3: status == pending -> skip 및 is_pending_warning == True
    row3 = {
        "id": 3,
        "tool_name": "write_file",
        "workspace": "/my/ws",
        "rel_path": "pending.txt",
        "before_exists": 0,
        "before_content": None,
        "after_sha256": None,
        "status": CHECKPOINT_STATUS_PENDING,
    }
    item3, warn3 = evaluate_rollback_row(
        row3,
        resolved_workspace_str="/my/ws",
        path_check_error=None,
        current_file_state=None,
        simulated_file_state=sim_state,
    )
    assert item3.will_skip is True
    assert item3.skip_reason == "결과 모름(pending)"
    assert warn3 is True

    # 분기 4: applied 상태이나 파일 부재
    row4 = {
        "id": 4,
        "tool_name": "write_file",
        "workspace": "/my/ws",
        "rel_path": "missing.txt",
        "before_exists": 0,
        "before_content": None,
        "after_sha256": _sha("content"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }
    item4, warn4 = evaluate_rollback_row(
        row4,
        resolved_workspace_str="/my/ws",
        path_check_error=None,
        current_file_state=(False, None),
        simulated_file_state=sim_state,
    )
    assert item4.will_skip is True
    assert item4.skip_reason == "충돌(파일 부재)"
    assert warn4 is False

    # 분기 5: applied 상태이나 외부 변경 (해시 불일치)
    row5 = {
        "id": 5,
        "tool_name": "write_file",
        "workspace": "/my/ws",
        "rel_path": "modified.txt",
        "before_exists": 0,
        "before_content": None,
        "after_sha256": _sha("expected"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }
    item5, warn5 = evaluate_rollback_row(
        row5,
        resolved_workspace_str="/my/ws",
        path_check_error=None,
        current_file_state=(True, _sha("different")),
        simulated_file_state=sim_state,
    )
    assert item5.will_skip is True
    assert item5.skip_reason == "충돌(외부 변경)"
    assert warn5 is False

    # 분기 6: applied 상태 정상 일치 -> 복원 가능
    row6 = {
        "id": 6,
        "tool_name": "write_file",
        "workspace": "/my/ws",
        "rel_path": "good.txt",
        "before_exists": 1,
        "before_content": "old_content",
        "after_sha256": _sha("new_content"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }
    item6, warn6 = evaluate_rollback_row(
        row6,
        resolved_workspace_str="/my/ws",
        path_check_error=None,
        current_file_state=(True, _sha("new_content")),
        simulated_file_state=sim_state,
    )
    assert item6.will_skip is False
    assert item6.skip_reason is None
    assert warn6 is False
    # 가상 상태가 before 상태로 전이되었는지 확인
    assert sim_state["good.txt"] == (True, _sha("old_content"))


def test_scenario_2_evaluate_rollback_row_multi_mutation_reverse_order():
    """시나리오 2: evaluate_rollback_row 동일 파일 다중 수정 시 역순 가상 복원 상태 추적 검증."""
    sim_state: dict[str, tuple[bool, str | None]] = {}

    row2 = {
        "id": 102,
        "tool_name": "edit_file",
        "workspace": "/ws",
        "rel_path": "doc.txt",
        "before_exists": 1,
        "before_content": "v1",
        "after_sha256": _sha("v2"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }
    row1 = {
        "id": 101,
        "tool_name": "write_file",
        "workspace": "/ws",
        "rel_path": "doc.txt",
        "before_exists": 1,
        "before_content": "v0",
        "after_sha256": _sha("v1"),
        "status": CHECKPOINT_STATUS_APPLIED,
    }

    # row2 평가: 최신 수정 롤백 판정
    item2, warn2 = evaluate_rollback_row(
        row2,
        resolved_workspace_str="/ws",
        path_check_error=None,
        current_file_state=(True, _sha("v2")),
        simulated_file_state=sim_state,
    )
    assert item2.will_skip is False
    assert warn2 is False
    assert sim_state["doc.txt"] == (True, _sha("v1"))

    # row1 평가: 앞선 row2 덕분에 simulated_file_state에 sha("v1")이 이미 들어있음
    # current_file_state를 None으로 넘겨도 simulated_file_state를 참조해야 함
    item1, warn1 = evaluate_rollback_row(
        row1,
        resolved_workspace_str="/ws",
        path_check_error=None,
        current_file_state=None,
        simulated_file_state=sim_state,
    )
    assert item1.will_skip is False
    assert warn1 is False
    assert sim_state["doc.txt"] == (True, _sha("v0"))


def test_scenario_3_classify_interrupted_rows():
    """시나리오 3: classify_interrupted_rows 상태 필터링 및 조립 검증."""
    # 케이스 A: 섞인 상태들
    rows_mixed = [
        {"id": 1, "rel_path": "a.txt", "status": CHECKPOINT_STATUS_APPLIED},
        {"id": 2, "rel_path": "b.txt", "status": CHECKPOINT_STATUS_SKIPPED},
        {"id": 3, "rel_path": "c.txt", "status": CHECKPOINT_STATUS_PENDING},
        {"id": 4, "rel_path": "d.txt", "status": CHECKPOINT_STATUS_ROLLED_BACK},
        {"id": 5, "rel_path": "e.txt", "status": CHECKPOINT_STATUS_FAILED},
    ]
    turn_mixed = classify_interrupted_rows(1, rows_mixed)
    assert turn_mixed is not None
    assert turn_mixed.turn_index == 1
    assert len(turn_mixed.items) == 3
    assert turn_mixed.items[0] == InterruptedItem(rel_path="a.txt", status=CHECKPOINT_STATUS_APPLIED)
    assert turn_mixed.items[1] == InterruptedItem(rel_path="c.txt", status=CHECKPOINT_STATUS_PENDING)
    assert turn_mixed.items[2] == InterruptedItem(rel_path="e.txt", status=CHECKPOINT_STATUS_FAILED)

    # 케이스 B: 대상 외 상태만 존재
    rows_none = [
        {"id": 1, "rel_path": "b.txt", "status": CHECKPOINT_STATUS_SKIPPED},
        {"id": 2, "rel_path": "d.txt", "status": CHECKPOINT_STATUS_ROLLED_BACK},
    ]
    assert classify_interrupted_rows(2, rows_none) is None

    # 케이스 C: 빈 리스트
    assert classify_interrupted_rows(3, []) is None


def test_scenario_4_notice_formatting():
    """시나리오 4: 알림 포맷 순수 함수 검증."""
    # 제어문자 및 상한선 초과 테스트
    items: list[RollbackItemResult] = []
    for i in range(25):
        items.append(
            RollbackItemResult(
                checkpoint_id=i,
                rel_path=f"file_\x00_{i}.txt",
                action="restored",
                status="rolled_back",
            )
        )
    res = RollbackResult(
        session_id="s1",
        turn_index=1,
        rolled_back_count=25,
        skipped_count=0,
        items=tuple(items),
    )
    notice = format_rollback_notice([res])
    assert notice is not None
    # \x00이 이스케이프되었는지 확인
    assert "\\x00" in notice
    assert "\x00" not in notice
    # 20개 초과분이 more 줄로 요약되었는지 확인
    assert "... and 5 more file(s)" in notice

    # InterruptedTurn 포맷팅
    int_items: list[InterruptedItem] = []
    for i in range(25):
        int_items.append(
            InterruptedItem(
                rel_path=f"int_\x1b_{i}.txt",
                status=CHECKPOINT_STATUS_APPLIED,
            )
        )
    int_turn = InterruptedTurn(turn_index=1, items=tuple(int_items))
    int_notice = format_interrupt_notice(int_turn)
    assert int_notice is not None
    assert "\\x1b" in int_notice
    assert "... and 5 more file(s)" in int_notice

    # pending notices 병합
    pending_combined = format_pending_notices([res, int_turn])
    assert pending_combined is not None
    assert notice in pending_combined
    assert int_notice in pending_combined
    assert "\n\n" in pending_combined


def test_scenario_5_reexport_identity():
    """시나리오 5: 재수출 심볼 객체 동일성(is) 검증."""
    symbols = [
        "CheckpointError",
        "CheckpointLimitError",
        "RollbackItem",
        "RollbackPlan",
        "RollbackItemResult",
        "RollbackResult",
        "InterruptedItem",
        "InterruptedTurn",
        "evaluate_rollback_row",
        "classify_interrupted_rows",
        "_escape_control",
        "format_rollback_notice",
        "format_interrupt_notice",
        "format_pending_notices",
    ]
    for sym in symbols:
        assert getattr(cp_mod, sym) is getattr(cpd_mod, sym), f"{sym} identity mismatch"


def test_scenario_6_checkpoint_domain_purity():
    """시나리오 6: checkpoint_domain.py 순수성 AST 정적 분석 검증."""
    domain_path = Path(cpd_mod.__file__)
    source = domain_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {
        "os",
        "pathlib",
        "tempfile",
        "sqlite3",
        "hermes_core.session_db",
        "hermes_core.tools.path_guard",
        "hermes_core.tools.checkpoint",
    }

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)

    for mod in imported_modules:
        for f in forbidden:
            assert mod != f and not mod.startswith(f + "."), f"금지된 모듈 {mod} import 발견"
