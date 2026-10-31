"""승인 흐름 판정 및 표시 정제 모듈.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/approval.py
- 핵심 역할:
  - 허용 목록 밖의 터미널 명령에 대해 사람에게 승인을 요청하고 단일 여부를 판정.
  - fail-closed 원칙: 불확실하거나 모호한 입력, 비터미널 환경, 오류 시 안전하게 거부/취소.
  - 화면 표시 정제(제어문자 이스케이프 노출)를 통해 모델의 질문 위장 방어.
  - 세션 원장(approval_requests) 선기록 및 결정 갱신 보장.
"""

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from hermes_core.constants import (
    APPROVAL_AUTO_DENIED_MESSAGE,
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_AUTO_DENIED,
    APPROVAL_DECISION_CANCELLED,
    APPROVAL_DECISION_DENIED,
    APPROVAL_DECISION_LIMIT_EXCEEDED,
    APPROVAL_DENIED_MESSAGE,
    APPROVAL_DISPLAY_MAX_CHARS,
    APPROVAL_HEADER,
    APPROVAL_LIMIT_MESSAGE,
    APPROVAL_PROMPT,
    APPROVAL_REQUEST_LIMIT,
    APPROVAL_TOO_LONG_MESSAGE,
    APPROVAL_YES_ANSWERS,
)
from hermes_core.tools.ports import ApprovalStorePort


def format_command_for_display(argv: Sequence[str]) -> str:
    """실행될 argv를 파이썬 리스트 표기 한 줄로 변환한다.

    제어·서식 문자는 repr 규칙에 따라 눈에 보이는 이스케이프 문자(\\n, \\x1b 등)로 바뀌므로,
    모델이 화면을 지우거나 질문을 위장할 수 없다. 화면에 보이는 것이 곧 실제 argv다.
    """
    return str(list(argv))


def is_approval_answer(answer: Any) -> bool:
    """사람의 답변이 명시적 승인(y, yes)인지 판정한다.

    대소문자를 무시하고 앞뒤 공백을 제거한 결과가 APPROVAL_YES_ANSWERS에 속할 때만 True.
    그 외의 모든 값(빈 문자열, n, no, 다른 문장 결합 등)은 False를 반환한다.
    """
    if not isinstance(answer, str):
        return False
    return answer.strip().casefold() in APPROVAL_YES_ANSWERS


@dataclass(frozen=True)
class ApprovalOutcome:
    """승인 요청 결과 객체.

    Attributes:
        approved: 명령 실행이 승인되었는지 여부.
        decision: 원장에 남은 최종 결정 상태 (APPROVAL_FINAL_DECISIONS 중 하나).
        message: 거부/취소 시 모델에게 전달할 안내 문구 (승인이면 빈 문자열).
    """

    approved: bool
    decision: str
    message: str


class Approver:
    """사람에게 명령을 제시하고 승인 여부를 판정하는 승인기 클래스."""

    def __init__(
        self,
        *,
        session_db: ApprovalStorePort,
        session_id: str,
        ask: Callable[[str], str],
        is_interactive: Callable[[], bool],
        say: Callable[[str], None] = print,
        limit: int = APPROVAL_REQUEST_LIMIT,
    ) -> None:
        """승인기 초기화.

        Args:
            session_db: 요청을 기록하고 결정을 갱신할 저장소 포트 인스턴스 (ApprovalStorePort).
            session_id: 요청이 귀속될 세션 ID (이미 DB에 존재해야 함).
            ask: 사람에게 한 줄 입력을 요청하는 함수.
            is_interactive: 현재 대화형 터미널 환경인지 확인하는 함수.
            say: 사람에게 안내를 출력하는 함수 (기본값 print).
            limit: 승인기 인스턴스당 허용되는 최대 요청 횟수 (1 이상의 int, bool 제외).
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be an integer >= 1, got {limit!r}")

        self._session_db = session_db
        self._session_id = session_id
        self._ask = ask
        self._is_interactive = is_interactive
        self._say = say
        self._limit = limit
        self._request_count = 0

    def request(self, argv: list[str]) -> ApprovalOutcome:
        """명령 실행에 대한 승인을 요청하고 판정 결과를 반환한다.

        계약 순서:
        1. 선기록: DB에 pending 행을 기록한다. 실패 시 예외를 그대로 전파한다.
        2. 횟수 세기: 요청 횟수를 1 증가시킨다.
        3. 상한 검사: 횟수가 limit을 초과하면 limit_exceeded로 즉시 거부한다.
        4. 비터미널 검사: 대화형 환경이 아니면 auto_denied로 즉시 거부한다.
        5. 길이 검사: 표시 문자열이 APPROVAL_DISPLAY_MAX_CHARS를 초과하면 denied로 거부한다.
        6. 질문: say로 헤더와 명령을 출력하고 ask로 답변을 받는다. 예외/취소 시 cancelled로 처리한다.
        7. 해석: 답변이 is_approval_answer를 통과하면 approved, 아니면 denied로 판정한다.
        8. 결정 갱신: DB 원장에 최종 결정을 기록한다. 실패 시 RuntimeError를 발생시킨다.
        9. 결과 반환: 판정 결과(ApprovalOutcome)를 반환한다.
        """
        # 1. 선기록 (기록 실패 시 예외 전파, 횟수 증가 및 질문 없음)
        request_id = self._session_db.record_approval_request(self._session_id, argv)

        # 2. 횟수 세기
        self._request_count += 1

        # 3. 상한 검사
        if self._request_count > self._limit:
            decision = APPROVAL_DECISION_LIMIT_EXCEEDED
            reason = f"Request limit ({self._limit}) exceeded"
            ok = self._session_db.resolve_approval_request(request_id, decision, reason)
            if not ok:
                raise RuntimeError(
                    f"Failed to resolve approval request {request_id} as {decision}"
                )
            return ApprovalOutcome(
                approved=False,
                decision=decision,
                message=APPROVAL_LIMIT_MESSAGE.format(limit=self._limit),
            )

        # 4. 비터미널 검사
        if not self._is_interactive():
            decision = APPROVAL_DECISION_AUTO_DENIED
            reason = "Non-interactive environment"
            ok = self._session_db.resolve_approval_request(request_id, decision, reason)
            if not ok:
                raise RuntimeError(
                    f"Failed to resolve approval request {request_id} as {decision}"
                )
            return ApprovalOutcome(
                approved=False,
                decision=decision,
                message=APPROVAL_AUTO_DENIED_MESSAGE,
            )

        # 5. 길이 검사
        display_cmd = format_command_for_display(argv)
        if len(display_cmd) > APPROVAL_DISPLAY_MAX_CHARS:
            decision = APPROVAL_DECISION_DENIED
            reason = (
                f"Command display length ({len(display_cmd)}) exceeds "
                f"{APPROVAL_DISPLAY_MAX_CHARS}"
            )
            ok = self._session_db.resolve_approval_request(request_id, decision, reason)
            if not ok:
                raise RuntimeError(
                    f"Failed to resolve approval request {request_id} as {decision}"
                )
            return ApprovalOutcome(
                approved=False,
                decision=decision,
                message=APPROVAL_TOO_LONG_MESSAGE.format(
                    max_chars=APPROVAL_DISPLAY_MAX_CHARS
                ),
            )

        # 6. 질문 (say / ask)
        cancelled = False
        cancel_reason = None
        raw_answer = None

        try:
            self._say(APPROVAL_HEADER)
            self._say(display_cmd)
            raw_answer = self._ask(APPROVAL_PROMPT)
            if not isinstance(raw_answer, str):
                cancelled = True
                cancel_reason = f"Non-string answer: {type(raw_answer).__name__}"
        except (Exception, KeyboardInterrupt) as exc:
            cancelled = True
            cancel_reason = f"{type(exc).__name__}: {exc}"

        if cancelled:
            decision = APPROVAL_DECISION_CANCELLED
            reason = cancel_reason
            ok = self._session_db.resolve_approval_request(request_id, decision, reason)
            if not ok:
                raise RuntimeError(
                    f"Failed to resolve approval request {request_id} as {decision}"
                )
            return ApprovalOutcome(
                approved=False,
                decision=decision,
                message=APPROVAL_DENIED_MESSAGE,
            )

        # 7. 해석
        if is_approval_answer(raw_answer):
            decision = APPROVAL_DECISION_APPROVED
            reason = "Approved by user"
            message = ""
            approved = True
        else:
            decision = APPROVAL_DECISION_DENIED
            reason = f"Denied by user (answer: {raw_answer!r})"
            message = APPROVAL_DENIED_MESSAGE
            approved = False

        # 8. 결정 갱신
        ok = self._session_db.resolve_approval_request(request_id, decision, reason)
        if not ok:
            raise RuntimeError(
                f"Failed to resolve approval request {request_id} as {decision}"
            )

        # 9. 결과 반환
        return ApprovalOutcome(
            approved=approved,
            decision=decision,
            message=message,
        )
