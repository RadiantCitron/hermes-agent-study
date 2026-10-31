"""파일 도구 체크포인트 및 롤백 모듈 (11번 로드맵).

[연구 및 설계 포인트]
- 원장(010) 위에 탑재되는 파일 변경 추적 및 복원 제어기.
- 도구 실행 전 상태 읽기 및 선기록(`record_before`).
- 도구 실행 후 파일 상태 직접 검증 및 결과 확정(`finalize`).
- 턴 단위 안전한 되돌리기 계획 수립(`plan_rollback`) 및 실행(`execute_rollback`).
- 충돌 시 건너뜀(외부 변경, 파일 부재, pending 잔존 행 차단).
- 원자적 복원 쓰기(`os.replace`) 및 파일 권한 보존.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import time
from typing import Optional, Sequence, Union

from hermes_core.constants import (
    CHECKPOINT_LIMIT_ERROR_MESSAGE,
    CHECKPOINT_ROLLBACK_ABORTED_HERE_REASON,
    CHECKPOINT_ROLLBACK_NOT_REACHED_REASON,
    CHECKPOINT_SESSION_BYTES_LIMIT,
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_PENDING,
    CHECKPOINT_STATUS_ROLLED_BACK,
    CHECKPOINT_STATUS_SKIPPED,
    CHECKPOINT_TOOL_NAMES,
    FILE_MAX_BYTES,
    get_default_workspace,
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
    _escape_control,
    classify_interrupted_rows,
    evaluate_rollback_row,
    format_interrupt_notice,
    format_pending_notices,
    format_rollback_notice,
)
from hermes_core.tools.ports import CheckpointStorePort
from hermes_core.tools.path_guard import check_write_path, resolve_workspace


class Checkpointer:
    """파일 도구 실행 전후 체크포인트 기록 및 롤백 제어기."""

    def __init__(
        self,
        db: CheckpointStorePort,
        session_id: str,
        workspace: Optional[Path] = None,
        *,
        bytes_limit: int = CHECKPOINT_SESSION_BYTES_LIMIT,
    ):
        self.db = db
        self.session_id = session_id
        self.workspace = workspace if workspace is not None else get_default_workspace()
        self.bytes_limit = bytes_limit

        self.turn_index = 0
        self.turn_started_at = 0.0
        self._turn_start_times: dict[int, float] = {}
        self._session_before_bytes = 0

    def restore_from_db(self) -> int:
        """DB의 file_checkpoints 원장에서 기존 세션의 마지막 turn_index, 누적 바이트, 턴별 시작 시각을 복원하고 복원된 turn_index를 반환한다."""
        rows = self.db.get_file_checkpoints(self.session_id)
        if not rows:
            self.turn_index = 0
            self.turn_started_at = 0.0
            self._turn_start_times = {}
            self._session_before_bytes = 0
            return 0

        self.turn_index = max(r["turn_index"] for r in rows)
        self._session_before_bytes = sum(
            len(r["before_content"].encode("utf-8"))
            for r in rows
            if r["before_exists"] and isinstance(r["before_content"], str)
        )
        turn_times: dict[int, float] = {}
        for r in rows:
            t = r["turn_index"]
            c = float(r["created_at"] or 0.0)
            if t in turn_times:
                turn_times[t] = min(turn_times[t], c)
            else:
                turn_times[t] = c

        self._turn_start_times = turn_times
        self.turn_started_at = self._turn_start_times.get(self.turn_index, 0.0)
        return self.turn_index

    def begin_turn(self, turn_index: Optional[int] = None) -> int:
        """턴 경계를 설정하고 시작 시각을 기록한다."""
        if turn_index is not None:
            if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 1:
                raise ValueError(f"turn_index must be an integer >= 1, got {turn_index!r}")
            self.turn_index = turn_index
        else:
            self.turn_index += 1

        self.turn_started_at = time.time()
        self._turn_start_times[self.turn_index] = self.turn_started_at
        return self.turn_index

    def record_before(self, tool_name: str, path: str) -> Optional[int]:
        """도구 실행 전 상태를 읽고 원장에 선기록(pending)한다."""
        if self.turn_index < 1:
            raise ValueError("Turn has not begun")
        if tool_name not in CHECKPOINT_TOOL_NAMES:
            raise ValueError(
                f"tool_name must be one of {CHECKPOINT_TOOL_NAMES}, got {tool_name!r}"
            )

        resolved_ws = resolve_workspace(self.workspace)
        resolved_path, err = check_write_path(path, resolved_ws)
        if err or resolved_path is None:
            return None

        # 파일 상태 읽기
        if resolved_path.exists():
            if not resolved_path.is_file():
                return None
            try:
                raw_bytes = resolved_path.read_bytes()
                if len(raw_bytes) > FILE_MAX_BYTES:
                    return None
                text = raw_bytes.decode("utf-8")
            except Exception:
                return None

            before_exists = True
            before_content = text
            bytes_count = len(raw_bytes)
        else:
            before_exists = False
            before_content = None
            bytes_count = 0

        # 세션 누적 상한 검사
        if self._session_before_bytes + bytes_count > self.bytes_limit:
            raise CheckpointLimitError(
                CHECKPOINT_LIMIT_ERROR_MESSAGE.format(limit=self.bytes_limit)
            )

        rel_path = resolved_path.relative_to(resolved_ws).as_posix()
        cid = self.db.record_file_checkpoint(
            session_id=self.session_id,
            turn_index=self.turn_index,
            tool_name=tool_name,
            workspace=str(resolved_ws),
            rel_path=rel_path,
            before_exists=before_exists,
            before_content=before_content,
        )
        self._session_before_bytes += bytes_count
        return cid

    def finalize(
        self,
        checkpoint_id: Optional[int],
        tool_name: str,
        path: str,
        result_success: Optional[bool] = None,
        exception: Optional[Exception] = None,
    ) -> str:
        """도구 실행 후 파일 상태를 직접 확인하여 원장을 확정한다."""
        if checkpoint_id is None:
            return "none"

        resolved_ws = resolve_workspace(self.workspace)
        resolved_path, err = check_write_path(path, resolved_ws)
        if err or resolved_path is None:
            self.db.update_file_checkpoint(
                checkpoint_id,
                CHECKPOINT_STATUS_SKIPPED,
                reason=f"결과 확인 실패(경로 판정 불가: {err})",
            )
            return CHECKPOINT_STATUS_SKIPPED

        # 원장 행 조회
        rows = [
            r
            for r in self.db.get_file_checkpoints(self.session_id, turn_index=self.turn_index)
            if r["id"] == checkpoint_id
        ]
        if not rows:
            rows = [
                r
                for r in self.db.get_file_checkpoints(self.session_id)
                if r["id"] == checkpoint_id
            ]
        if not rows or rows[0]["status"] != CHECKPOINT_STATUS_PENDING:
            return "invalid"

        row = rows[0]
        before_exists = bool(row["before_exists"])
        before_content = row["before_content"]

        if resolved_path.exists():
            try:
                raw_bytes = resolved_path.read_bytes()
            except Exception as e:
                self.db.update_file_checkpoint(
                    checkpoint_id,
                    CHECKPOINT_STATUS_SKIPPED,
                    reason=f"결과 확인 실패(읽기 오류: {e})",
                )
                return CHECKPOINT_STATUS_SKIPPED

            after_sha256 = hashlib.sha256(raw_bytes).hexdigest()

            if not before_exists:
                # 없던 파일이 생성됨
                final_status = CHECKPOINT_STATUS_APPLIED
                reason = "도구 결과 False이나 파일 생성됨" if result_success is False else None
                self.db.update_file_checkpoint(
                    checkpoint_id, final_status, after_sha256=after_sha256, reason=reason
                )
                return final_status
            else:
                # 기존 파일 내용 비교
                before_bytes = (
                    before_content.encode("utf-8") if before_content is not None else b""
                )
                if raw_bytes != before_bytes:
                    final_status = CHECKPOINT_STATUS_APPLIED
                    reason = "도구 결과 False이나 파일 변경됨" if result_success is False else None
                    self.db.update_file_checkpoint(
                        checkpoint_id, final_status, after_sha256=after_sha256, reason=reason
                    )
                    return final_status
                else:
                    final_status = CHECKPOINT_STATUS_FAILED
                    reason = (
                        "도구 결과 True이나 내용 미변경"
                        if result_success is True
                        else "내용 미변경"
                    )
                    self.db.update_file_checkpoint(
                        checkpoint_id, final_status, reason=reason
                    )
                    return final_status
        else:
            if not before_exists:
                final_status = CHECKPOINT_STATUS_FAILED
                reason = "파일 미생성"
                self.db.update_file_checkpoint(checkpoint_id, final_status, reason=reason)
                return final_status
            else:
                final_status = CHECKPOINT_STATUS_FAILED
                reason = "파일 소실"
                self.db.update_file_checkpoint(checkpoint_id, final_status, reason=reason)
                return final_status

    def plan_rollback(self, turn_index: Optional[int] = None) -> RollbackPlan:
        """되돌리기 계획을 수립한다. 파일 시스템을 변경하지 않는다."""
        resolved_ws = resolve_workspace(self.workspace)

        if turn_index is None:
            all_rows = self.db.get_file_checkpoints(self.session_id)
            candidate_turns = {
                r["turn_index"]
                for r in all_rows
                if r["status"] in (CHECKPOINT_STATUS_APPLIED, CHECKPOINT_STATUS_PENDING)
            }
            if not candidate_turns:
                return RollbackPlan(
                    session_id=self.session_id,
                    turn_index=0,
                    items=(),
                    terminal_approvals_count=0,
                    subagent_count=0,
                    has_warnings=False,
                )
            target_turn = max(candidate_turns)
        else:
            target_turn = turn_index

        turn_rows = self.db.get_file_checkpoints(self.session_id, turn_index=target_turn)
        target_rows = [
            r
            for r in turn_rows
            if r["status"] in (CHECKPOINT_STATUS_APPLIED, CHECKPOINT_STATUS_PENDING)
        ]
        # id 내림차순 (역순 복원)
        target_rows.sort(key=lambda r: r["id"], reverse=True)

        items = []
        has_warnings = False
        # 한 턴 내 동일 파일 다중 수정 시 앞선 역순 롤백의 가상 상태 추적 (위험 7, 가정 C)
        simulated_file_state: dict[str, tuple[bool, Optional[str]]] = {}

        for r in target_rows:
            ws = r["workspace"]
            rel_path = r["rel_path"]
            status = r["status"]

            path_check_error: Optional[str] = None
            current_file_state: Optional[tuple[bool, Optional[str]]] = None

            if ws == str(resolved_ws):
                resolved_path, err = check_write_path(rel_path, resolved_ws)
                if err or resolved_path is None:
                    path_check_error = err or "경로 해석 실패"
                elif (
                    status == CHECKPOINT_STATUS_APPLIED
                    and rel_path not in simulated_file_state
                ):
                    if resolved_path.exists():
                        try:
                            curr_bytes = resolved_path.read_bytes()
                            curr_sha = hashlib.sha256(curr_bytes).hexdigest()
                            current_file_state = (True, curr_sha)
                        except Exception:
                            current_file_state = (False, None)
                    else:
                        current_file_state = (False, None)

            item, is_pending_warning = evaluate_rollback_row(
                r,
                resolved_workspace_str=str(resolved_ws),
                path_check_error=path_check_error,
                current_file_state=current_file_state,
                simulated_file_state=simulated_file_state,
            )
            if is_pending_warning:
                has_warnings = True
            items.append(item)

        start_time: Optional[float] = None
        end_time: Optional[float] = None
        if target_turn in self._turn_start_times:
            start_time = self._turn_start_times[target_turn]
            later_turns = [t for t in self._turn_start_times if t > target_turn]
            if later_turns:
                next_turn = min(later_turns)
                end_time = self._turn_start_times[next_turn]
        else:
            if self.turn_started_at > 0:
                start_time = self.turn_started_at

        terminal_approvals_count = 0
        try:
            apprs = self.db.get_approval_requests(self.session_id)
            terminal_approvals_count = sum(
                1
                for a in apprs
                if a.get("decision") == "approved"
                and (
                    start_time is None
                    or (a.get("decided_at") or 0) >= start_time
                )
                and (
                    end_time is None
                    or (a.get("decided_at") or 0) < end_time
                )
            )
        except Exception:
            pass

        subagent_count = 0
        try:
            subagent_count = self.db.count_child_sessions(
                self.session_id,
                started_at_min=start_time,
                started_at_max=end_time,
            )
        except Exception:
            pass

        if terminal_approvals_count > 0 or subagent_count > 0:
            has_warnings = True

        return RollbackPlan(
            session_id=self.session_id,
            turn_index=target_turn,
            items=tuple(items),
            terminal_approvals_count=terminal_approvals_count,
            subagent_count=subagent_count,
            has_warnings=has_warnings,
        )

    def execute_rollback(self, plan: RollbackPlan) -> RollbackResult:
        """계획에 따라 실제 파일을 복원하고 원장 상태를 갱신한다."""
        resolved_ws = resolve_workspace(self.workspace)
        rolled_back_count = 0
        skipped_count = 0
        item_results = []
        error: Optional[str] = None

        try:
            for item in plan.items:
                if item.will_skip:
                    self.db.update_file_checkpoint(
                        item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=item.skip_reason
                    )
                    skipped_count += 1
                    item_results.append(
                        RollbackItemResult(
                            checkpoint_id=item.checkpoint_id,
                            rel_path=item.rel_path,
                            action="skipped",
                            status=CHECKPOINT_STATUS_SKIPPED,
                            reason=item.skip_reason,
                        )
                    )
                    continue

                target_path = resolved_ws / item.rel_path
                resolved_path, err = check_write_path(item.rel_path, resolved_ws)
                if err or resolved_path is None:
                    reason = f"복원 직전 경로 검사 실패 ({err})"
                    self.db.update_file_checkpoint(
                        item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=reason
                    )
                    skipped_count += 1
                    item_results.append(
                        RollbackItemResult(
                            checkpoint_id=item.checkpoint_id,
                            rel_path=item.rel_path,
                            action="skipped",
                            status=CHECKPOINT_STATUS_SKIPPED,
                            reason=reason,
                        )
                    )
                    continue

                if not resolved_path.exists():
                    reason = "복원 직전 충돌(파일 부재)"
                    self.db.update_file_checkpoint(
                        item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=reason
                    )
                    skipped_count += 1
                    item_results.append(
                        RollbackItemResult(
                            checkpoint_id=item.checkpoint_id,
                            rel_path=item.rel_path,
                            action="skipped",
                            status=CHECKPOINT_STATUS_SKIPPED,
                            reason=reason,
                        )
                    )
                    continue

                try:
                    curr_bytes = resolved_path.read_bytes()
                    curr_sha = hashlib.sha256(curr_bytes).hexdigest()
                    if curr_sha != item.after_sha256:
                        reason = "복원 직전 충돌(외부 변경 감지)"
                        self.db.update_file_checkpoint(
                            item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=reason
                        )
                        skipped_count += 1
                        item_results.append(
                            RollbackItemResult(
                                checkpoint_id=item.checkpoint_id,
                                rel_path=item.rel_path,
                                action="skipped",
                                status=CHECKPOINT_STATUS_SKIPPED,
                                reason=reason,
                            )
                        )
                        continue
                except Exception as e:
                    reason = f"복원 직전 파일 읽기 실패 ({e})"
                    self.db.update_file_checkpoint(
                        item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=reason
                    )
                    skipped_count += 1
                    item_results.append(
                        RollbackItemResult(
                            checkpoint_id=item.checkpoint_id,
                            rel_path=item.rel_path,
                            action="skipped",
                            status=CHECKPOINT_STATUS_SKIPPED,
                            reason=reason,
                        )
                    )
                    continue

                # 복원 실행
                if item.before_exists:
                    try:
                        st_mode = resolved_path.stat().st_mode
                        parent_dir = resolved_path.parent
                        parent_dir.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(
                            "w", dir=parent_dir, delete=False, encoding="utf-8"
                        ) as tf:
                            tf.write(item.before_content if item.before_content is not None else "")
                            temp_name = tf.name
                        os.chmod(temp_name, st_mode)
                        os.replace(temp_name, resolved_path)
                        action = "restored"
                    except Exception as e:
                        reason = f"복원 쓰기 실패 ({e})"
                        self.db.update_file_checkpoint(
                            item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=reason
                        )
                        skipped_count += 1
                        item_results.append(
                            RollbackItemResult(
                                checkpoint_id=item.checkpoint_id,
                                rel_path=item.rel_path,
                                action="skipped",
                                status=CHECKPOINT_STATUS_SKIPPED,
                                reason=reason,
                            )
                        )
                        continue
                else:
                    try:
                        resolved_path.unlink(missing_ok=True)
                        action = "deleted"
                    except Exception as e:
                        reason = f"복원 삭제 실패 ({e})"
                        self.db.update_file_checkpoint(
                            item.checkpoint_id, CHECKPOINT_STATUS_SKIPPED, reason=reason
                        )
                        skipped_count += 1
                        item_results.append(
                            RollbackItemResult(
                                checkpoint_id=item.checkpoint_id,
                                rel_path=item.rel_path,
                                action="skipped",
                                status=CHECKPOINT_STATUS_SKIPPED,
                                reason=reason,
                            )
                        )
                        continue

                self.db.update_file_checkpoint(
                    item.checkpoint_id, CHECKPOINT_STATUS_ROLLED_BACK, reason="되돌림"
                )
                rolled_back_count += 1
                item_results.append(
                    RollbackItemResult(
                        checkpoint_id=item.checkpoint_id,
                        rel_path=item.rel_path,
                        action=action,
                        status=CHECKPOINT_STATUS_ROLLED_BACK,
                        reason="되돌림",
                    )
                )
        except Exception as e:
            error = str(e)
            aborted_idx = len(item_results)
            for i, rem_item in enumerate(plan.items[aborted_idx:]):
                if i == 0:
                    r_reason = CHECKPOINT_ROLLBACK_ABORTED_HERE_REASON.format(error=error)
                else:
                    r_reason = CHECKPOINT_ROLLBACK_NOT_REACHED_REASON
                skipped_count += 1
                item_results.append(
                    RollbackItemResult(
                        checkpoint_id=rem_item.checkpoint_id,
                        rel_path=rem_item.rel_path,
                        action="skipped",
                        status=CHECKPOINT_STATUS_SKIPPED,
                        reason=r_reason,
                    )
                )

        return RollbackResult(
            session_id=self.session_id,
            turn_index=plan.turn_index,
            rolled_back_count=rolled_back_count,
            skipped_count=skipped_count,
            items=tuple(item_results),
            error=error,
        )

    def collect_interrupted_turn(
        self, turn_index: Optional[int] = None
    ) -> Optional[InterruptedTurn]:
        """취소된 턴의 파일 변경 기록을 수집한다.

        turn_index가 지정되지 않으면 self.turn_index를 사용한다.
        상태가 applied, pending, failed인 행만 id 오름차순으로 InterruptedItem으로 만든다.
        해당 행이 0건이면 None을 반환한다. (skipped, rolled_back 행은 제외)
        파일 시스템이나 DB를 수정하지 않는다.
        """
        idx = self.turn_index if turn_index is None else turn_index
        rows = self.db.get_file_checkpoints(self.session_id, turn_index=idx)
        return classify_interrupted_rows(idx, rows)


__all__ = [
    "Checkpointer",
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
