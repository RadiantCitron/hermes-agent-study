"""체크포인트 순수 도메인 모듈 (20번 로드맵, ADR-0020 D-05, R-4).

[순수 도메인 원칙]
- 파일시스템(os, pathlib, tempfile), 데이터베이스(sqlite3, session_db), 경로 보안(path_guard)에 대한
  일체의 의존성을 배제한다.
- 불변 데이터 모델(RollbackItem, RollbackPlan, RollbackResult 등), 순수 판정 함수(evaluate_rollback_row,
  classify_interrupted_rows), 순수 알림 렌더러(format_*_notice)를 제공한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping, Optional, Sequence, Union

from hermes_core.constants import (
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_PENDING,
    INTERRUPT_NOTICE_BEGIN,
    INTERRUPT_NOTICE_END,
    INTERRUPT_NOTICE_INSTRUCTION,
    INTERRUPT_NOTICE_INTRO,
    INTERRUPT_NOTICE_LINE_APPLIED,
    INTERRUPT_NOTICE_LINE_FAILED,
    INTERRUPT_NOTICE_LINE_PENDING,
    INTERRUPT_NOTICE_MAX_PATHS,
    INTERRUPT_NOTICE_MORE_LINE,
    ROLLBACK_NOTICE_BEGIN,
    ROLLBACK_NOTICE_END,
    ROLLBACK_NOTICE_INSTRUCTION,
    ROLLBACK_NOTICE_INTRO,
    ROLLBACK_NOTICE_LINE_DELETED,
    ROLLBACK_NOTICE_LINE_RESTORED,
    ROLLBACK_NOTICE_LINE_SKIPPED,
    ROLLBACK_NOTICE_MAX_PATHS,
    ROLLBACK_NOTICE_MORE_LINE,
    ROLLBACK_NOTICE_TURN_LINE,
    ROLLBACK_NOTICE_UNKNOWN_REASON,
)

__all__ = [
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


class CheckpointError(Exception):
    """체크포인트 모듈 기본 예외."""


class CheckpointLimitError(CheckpointError):
    """세션당 체크포인트 누적 바이트 상한 초과 예외."""


@dataclass(frozen=True)
class RollbackItem:
    """단일 파일의 되돌리기 계획 항목."""

    checkpoint_id: int
    tool_name: str
    workspace: str
    rel_path: str
    before_exists: bool
    before_content: Optional[str]
    after_sha256: Optional[str]
    status: str
    will_skip: bool
    skip_reason: Optional[str] = None


@dataclass(frozen=True)
class RollbackPlan:
    """되돌리기 계획 (실제 파일 복원 전 검토용)."""

    session_id: str
    turn_index: int
    items: tuple[RollbackItem, ...]
    terminal_approvals_count: int = 0
    subagent_count: int = 0
    has_warnings: bool = False


@dataclass(frozen=True)
class RollbackItemResult:
    """단일 파일 복원 실행 결과."""

    checkpoint_id: int
    rel_path: str
    action: str  # "restored" | "deleted" | "skipped"
    status: str  # "rolled_back" | "skipped"
    reason: Optional[str] = None


@dataclass(frozen=True)
class RollbackResult:
    """되돌리기 실행 종합 결과."""

    session_id: str
    turn_index: int
    rolled_back_count: int
    skipped_count: int
    items: tuple[RollbackItemResult, ...]
    error: Optional[str] = None


@dataclass(frozen=True)
class InterruptedItem:
    """취소된 턴의 단일 파일 변경 상태 항목."""

    rel_path: str
    status: str  # "applied" | "pending" | "failed"


@dataclass(frozen=True)
class InterruptedTurn:
    """취소된 턴의 파일 변경 항목 묶음."""

    turn_index: int
    items: tuple[InterruptedItem, ...]


def evaluate_rollback_row(
    row: Mapping[str, Any],
    *,
    resolved_workspace_str: str,
    path_check_error: Optional[str] = None,
    current_file_state: Optional[tuple[bool, Optional[str]]] = None,
    simulated_file_state: dict[str, tuple[bool, Optional[str]]],
) -> tuple[RollbackItem, bool]:
    """단일 원장 행에 대해 롤백 복원 가능 여부 및 가상 복원 상태 전이를 판정한다.

    순수 도메인 함수로서 파일시스템 I/O를 직접 수행하지 않으며,
    사전 평가된 파일 상태(current_file_state)와 가상 추적 사전(simulated_file_state)을 기반으로 동작한다.

    Returns:
        (RollbackItem, is_pending_warning) 튜플
    """
    cid = row["id"]
    tool_name = row["tool_name"]
    ws = row["workspace"]
    rel_path = row["rel_path"]
    before_exists = bool(row["before_exists"])
    before_content = row["before_content"]
    after_sha256 = row["after_sha256"]
    status = row["status"]

    will_skip = False
    skip_reason: Optional[str] = None
    is_pending_warning = False

    if ws != resolved_workspace_str:
        will_skip = True
        skip_reason = "작업 폴더 불일치"
    elif path_check_error is not None:
        will_skip = True
        skip_reason = f"경로 재판정 실패 ({path_check_error})"
    elif status == CHECKPOINT_STATUS_PENDING:
        will_skip = True
        skip_reason = "결과 모름(pending)"
        is_pending_warning = True
    elif status == CHECKPOINT_STATUS_APPLIED:
        if rel_path not in simulated_file_state:
            if current_file_state is not None:
                simulated_file_state[rel_path] = current_file_state
            else:
                simulated_file_state[rel_path] = (False, None)

        file_exists, file_sha = simulated_file_state[rel_path]

        if not file_exists:
            will_skip = True
            skip_reason = "충돌(파일 부재)"
        elif file_sha != after_sha256:
            will_skip = True
            skip_reason = "충돌(외부 변경)"
        else:
            will_skip = False
            # 동일 파일의 선행(더 옛날) 체크포인트를 위한 가상 복원 상태 갱신
            if before_exists:
                b_bytes = (
                    before_content.encode("utf-8")
                    if before_content is not None
                    else b""
                )
                simulated_file_state[rel_path] = (
                    True,
                    hashlib.sha256(b_bytes).hexdigest(),
                )
            else:
                simulated_file_state[rel_path] = (False, None)

    item = RollbackItem(
        checkpoint_id=cid,
        tool_name=tool_name,
        workspace=ws,
        rel_path=rel_path,
        before_exists=before_exists,
        before_content=before_content,
        after_sha256=after_sha256,
        status=status,
        will_skip=will_skip,
        skip_reason=skip_reason,
    )
    return item, is_pending_warning


def classify_interrupted_rows(
    turn_index: int,
    rows: Sequence[Mapping[str, Any]],
) -> Optional[InterruptedTurn]:
    """취소(중단)된 턴의 체크포인트 원장 행 목록에서 인터럽트 대상 항목을 분류·조립한다.

    상태가 applied, pending, failed인 행만 id 오름차순으로 InterruptedItem으로 만든다.
    해당 행이 0건이면 None을 반환한다. (skipped, rolled_back 행은 제외)
    순수 함수로서 파일 시스템이나 DB를 수정하지 않는다.
    """
    target_statuses = (
        CHECKPOINT_STATUS_APPLIED,
        CHECKPOINT_STATUS_PENDING,
        CHECKPOINT_STATUS_FAILED,
    )
    items = [
        InterruptedItem(rel_path=row["rel_path"], status=row["status"])
        for row in rows
        if row.get("status") in target_statuses
    ]
    if not items:
        return None
    return InterruptedTurn(turn_index=turn_index, items=tuple(items))


def _escape_control(s: str) -> str:
    """문자열 내 제어·서식 문자를 repr 규칙에 따라 눈에 보이는 역슬래시 표기로 치환한다."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: repr(m.group(0))[1:-1], s)


def format_rollback_notice(results: Sequence[RollbackResult]) -> Optional[str]:
    """되돌리기 실행 결과 목록을 바탕으로 모델에게 전달할 고정 형식의 알림 글을 생성한다.

    - rolled_back_count == 0인 결과는 건너뛴다.
    - 남는 결과가 없으면 None을 반환한다.
    - 입력 순서대로 Turn 블록을 구성하며, 같은 턴 내 동일 (action, rel_path) 중복을 제거한다.
    - 전체 경로 줄 수가 상한(ROLLBACK_NOTICE_MAX_PATHS)을 초과하면 초과분을 생략하고 MORE 줄을 덧붙인다.
    - rel_path와 reason 내 제어 문자는 이스케이프하여 표지 줄 위조를 방지한다.
    - 순수 함수로서 입력을 변경하지 않는다.
    """
    valid_results = [r for r in results if r.rolled_back_count > 0]
    if not valid_results:
        return None

    # 1. 턴별 고유 항목 추출 (같은 턴 내 (action, rel_path) 중복 제거)
    turn_entries: list[tuple[int, list[RollbackItemResult]]] = []
    for res in valid_results:
        seen: set[tuple[str, str]] = set()
        deduped: list[RollbackItemResult] = []
        for item in res.items:
            key = (item.action, item.rel_path)
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        turn_entries.append((res.turn_index, deduped))

    # 2. 상한 적용 및 라인 구성
    lines: list[str] = [
        ROLLBACK_NOTICE_BEGIN,
        ROLLBACK_NOTICE_INTRO,
    ]

    total_paths = sum(len(items) for _, items in turn_entries)
    path_count = 0
    limit_reached = False

    for turn_idx, items in turn_entries:
        if limit_reached:
            break

        turn_lines: list[str] = [ROLLBACK_NOTICE_TURN_LINE.format(turn_index=turn_idx)]
        added_in_turn = 0

        for item in items:
            if path_count >= ROLLBACK_NOTICE_MAX_PATHS:
                limit_reached = True
                break

            safe_rel_path = _escape_control(item.rel_path)
            if item.action == "restored":
                turn_lines.append(ROLLBACK_NOTICE_LINE_RESTORED.format(rel_path=safe_rel_path))
            elif item.action == "deleted":
                turn_lines.append(ROLLBACK_NOTICE_LINE_DELETED.format(rel_path=safe_rel_path))
            elif item.action == "skipped":
                reason = item.reason if item.reason is not None else ROLLBACK_NOTICE_UNKNOWN_REASON
                safe_reason = _escape_control(reason)
                turn_lines.append(
                    ROLLBACK_NOTICE_LINE_SKIPPED.format(
                        rel_path=safe_rel_path,
                        reason=safe_reason,
                    )
                )
            else:
                continue

            path_count += 1
            added_in_turn += 1

        if added_in_turn > 0:
            lines.extend(turn_lines)
        elif not limit_reached:
            # 항목이 하나도 없는 턴 블록인 경우(이론상 없으나 안전)
            pass

    if total_paths > ROLLBACK_NOTICE_MAX_PATHS:
        overflow_count = total_paths - ROLLBACK_NOTICE_MAX_PATHS
        lines.append(ROLLBACK_NOTICE_MORE_LINE.format(count=overflow_count))

    lines.append(ROLLBACK_NOTICE_INSTRUCTION)
    lines.append(ROLLBACK_NOTICE_END)

    return "\n".join(lines)


def format_interrupt_notice(turn: InterruptedTurn) -> Optional[str]:
    """취소된 턴의 파일 변경 기록을 바탕으로 모델에게 전달할 고정 형식의 알림 글을 생성한다.

    - 항목이 0개면 None을 반환한다.
    - 같은 (status, rel_path) 중복은 첫 번째만 남긴다 (입력 순서 보존).
    - 전체 경로 줄 수가 상한(INTERRUPT_NOTICE_MAX_PATHS)을 초과하면 초과분을 생략하고 MORE 줄을 덧붙인다.
    - rel_path는 _escape_control로 이스케이프한다.
    - 턴 번호는 포함하지 않는다.
    - 순수 함수로서 입력을 변경하지 않는다.
    """
    if not turn.items:
        return None

    seen: set[tuple[str, str]] = set()
    deduped: list[InterruptedItem] = []
    for item in turn.items:
        key = (item.status, item.rel_path)
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    if not deduped:
        return None

    lines: list[str] = [
        INTERRUPT_NOTICE_BEGIN,
        INTERRUPT_NOTICE_INTRO,
    ]

    total_paths = len(deduped)
    for item in deduped[:INTERRUPT_NOTICE_MAX_PATHS]:
        safe_rel_path = _escape_control(item.rel_path)
        if item.status == CHECKPOINT_STATUS_APPLIED:
            lines.append(INTERRUPT_NOTICE_LINE_APPLIED.format(rel_path=safe_rel_path))
        elif item.status == CHECKPOINT_STATUS_PENDING:
            lines.append(INTERRUPT_NOTICE_LINE_PENDING.format(rel_path=safe_rel_path))
        elif item.status == CHECKPOINT_STATUS_FAILED:
            lines.append(INTERRUPT_NOTICE_LINE_FAILED.format(rel_path=safe_rel_path))

    if total_paths > INTERRUPT_NOTICE_MAX_PATHS:
        overflow_count = total_paths - INTERRUPT_NOTICE_MAX_PATHS
        lines.append(INTERRUPT_NOTICE_MORE_LINE.format(count=overflow_count))

    lines.append(INTERRUPT_NOTICE_INSTRUCTION)
    lines.append(INTERRUPT_NOTICE_END)

    return "\n".join(lines)


def format_pending_notices(
    items: Sequence[Union[RollbackResult, InterruptedTurn]],
) -> Optional[str]:
    """보류 목록의 되돌리기 결과 및 턴 취소 알림을 순서대로 블록화하여 하나의 텍스트로 합친다."""
    blocks: list[str] = []
    current_rollbacks: list[RollbackResult] = []

    def flush_rollbacks() -> None:
        if current_rollbacks:
            text = format_rollback_notice(current_rollbacks)
            if text:
                blocks.append(text)
            current_rollbacks.clear()

    for item in items:
        if isinstance(item, RollbackResult):
            current_rollbacks.append(item)
        elif isinstance(item, InterruptedTurn):
            flush_rollbacks()
            text = format_interrupt_notice(item)
            if text:
                blocks.append(text)

    flush_rollbacks()

    if not blocks:
        return None
    return "\n\n".join(blocks)
