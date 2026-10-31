"""루프 정체 감지 및 자가 교정 확장기 (LoopGuard Extension).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/file_tools.py (동일 파일/인자 연속 호출 추적)
- 핵심 차이점:
  1. 원본의 전역 모듈 수준 딕셔너리 대신 턴 문맥(TurnScope) 턴 칸에 격리 보관.
  2. 디스패처 GUARD 단계의 내장 확장으로 동작하여 코어 루프 수정 없이 fail-open으로 보호.
  3. 1차(WARN_THRESHOLD=3) 경고 주입, 2차(BLOCK_THRESHOLD=5) 실행 생략 및 stuck_loop 정지 요청.
"""

from collections.abc import Callable
import json
import logging
from typing import Any, Optional

from hermes_core.agent.events import EVENT_LOOP_GUARD
from hermes_core.constants import (
    LOOP_GUARD_BLOCK_THRESHOLD,
    LOOP_GUARD_BLOCKED_MESSAGE,
    LOOP_GUARD_WARN_THRESHOLD,
    LOOP_GUARD_WARNING_MESSAGE,
)
from hermes_core.extensions.contract import ExtensionHost, MiddlewarePhase
from hermes_core.tools.dispatcher import ToolCall

logger = logging.getLogger(__name__)


def make_fingerprint(
    func_name: str,
    parsed_args: Optional[dict[str, Any]],
    raw_args: Any = "",
) -> tuple[str, str]:
    """도구 호출의 정규화된 지문 (이름, 정규화된 인자 문자열) 생성."""
    name = str(func_name or "unknown")
    if isinstance(parsed_args, dict):
        canonical = json.dumps(
            parsed_args,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        canonical = f"<invalid_json:{str(raw_args or '').strip()}>"
    return name, canonical


class LoopGuard:
    """순수 연속 호출 카운터."""

    def __init__(
        self,
        warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD,
        block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD,
    ) -> None:
        if isinstance(warn_threshold, bool) or not isinstance(warn_threshold, int) or warn_threshold < 2:
            raise ValueError("warn_threshold must be an integer >= 2")
        if (
            isinstance(block_threshold, bool)
            or not isinstance(block_threshold, int)
            or block_threshold <= warn_threshold
        ):
            raise ValueError("block_threshold must be an integer > warn_threshold")

        self.warn_threshold = warn_threshold
        self.block_threshold = block_threshold
        self.last_fingerprint: Optional[tuple[str, str]] = None
        self.consecutive_count = 0

    def record(
        self,
        func_name: str,
        parsed_args: Optional[dict[str, Any]],
        raw_args: Any = "",
    ) -> tuple[str, int]:
        """도구 호출 지문을 기록하고 (상태, 연속 호출 횟수) 반환."""
        fp = make_fingerprint(func_name, parsed_args, raw_args)
        if fp == self.last_fingerprint:
            self.consecutive_count += 1
        else:
            self.last_fingerprint = fp
            self.consecutive_count = 1

        count = self.consecutive_count
        if count >= self.block_threshold:
            return "block", count
        if count >= self.warn_threshold:
            return "warn", count
        return "ok", count


def attach_loop_warning(result_str: str, warning_message: str) -> str:
    """도구 실행 결과 문자열에 자가 교정 경고 메시지 부착."""
    try:
        data = json.loads(result_str)
        if isinstance(data, dict):
            data["_loop_warning"] = warning_message
            return json.dumps(data, ensure_ascii=False)
    except Exception:
        pass
    return f"{result_str}\n{warning_message}"


def _blocked_result(tool_name: str, count: int) -> str:
    """루프 차단 시 도구 실행 결과 JSON 생성."""
    return json.dumps(
        {
            "success": False,
            "error": LOOP_GUARD_BLOCKED_MESSAGE.format(tool_name=tool_name, count=count),
        },
        ensure_ascii=False,
    )


class LoopGuardExtension:
    """디스패처 GUARD 단계의 루프 정체 감지 내장 확장."""

    name = "loop_guard"

    def __init__(
        self,
        warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD,
        block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD,
    ) -> None:
        self.warn_threshold = warn_threshold
        self.block_threshold = block_threshold
        self._host: Optional[ExtensionHost] = None

    def install(self, host: ExtensionHost) -> None:
        self._host = host
        host.add_tool_middleware(MiddlewarePhase.GUARD, self._middleware)

    def _middleware(self, call: ToolCall, next_fn: Callable[[ToolCall], str]) -> str:
        # 1. 턴 밖 직접 호출(call.scope is None)은 세지 않는다.
        if call.scope is None:
            return next_fn(call)

        # 2. 턴 칸 소유권 확인 (ADR-0017 D-10: 디스패처 공유 시 중복 등록 인스턴스는 통과만 수행)
        slot = call.scope.state("loop_guard")
        if slot.setdefault("owner", self) is not self:
            return next_fn(call)

        # 3. 턴 칸의 카운터 획득 또는 생성
        guard: LoopGuard = slot.setdefault(
            "guard",
            LoopGuard(self.warn_threshold, self.block_threshold),
        )

        # 4. 이미 차단 상태면 동일 배치의 나머지 호출도 핸들러 미실행 및 차단 반환
        if slot.get("blocked"):
            return _blocked_result(call.name, guard.consecutive_count)

        # 5. 지문 기록 및 상태 판정
        parsed_args = call.args if call.parse_error is None else None
        status, count = guard.record(call.name, parsed_args, call.raw_arguments or "")

        # 6. block 상태: 정지 요청 및 차단 반환 (next_fn 미호출)
        if status == "block":
            slot["blocked"] = True
            call.scope.request_stop("stuck_loop")
            logger.warning("LoopGuard blocked repeated tool call '%s' (count=%d).", call.name, count)
            if self._host is not None:
                self._host.emit(EVENT_LOOP_GUARD, status="block", tool=call.name, count=count)
            return _blocked_result(call.name, count)

        # 7. 실행 및 경고 부착
        result = next_fn(call)
        if status == "warn":
            if self._host is not None:
                self._host.emit(EVENT_LOOP_GUARD, status="warn", tool=call.name, count=count)
            warning_msg = LOOP_GUARD_WARNING_MESSAGE.format(tool_name=call.name, count=count)
            return attach_loop_warning(result, warning_msg)

        return result
