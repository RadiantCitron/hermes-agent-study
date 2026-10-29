"""승인 모듈 단위 테스트 (로드맵 10 - 020단계).

[근거 문서]
- 지시서: docs/로드맵/10-승인-흐름-MVP/instructions/020_승인_모듈.md
- ADR-0010: §2-3, §2-7, §2-8, §2-9
- ROADMAP: §2-4, §4 (위험 2·4·6·7·9), §6-2
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_core.constants import (
    APPROVAL_DECISION_APPROVED,
    APPROVAL_DECISION_AUTO_DENIED,
    APPROVAL_DECISION_CANCELLED,
    APPROVAL_DECISION_DENIED,
    APPROVAL_DECISION_LIMIT_EXCEEDED,
    APPROVAL_DECISION_PENDING,
    APPROVAL_DISPLAY_MAX_CHARS,
    APPROVAL_HEADER,
    APPROVAL_PROMPT,
    APPROVAL_REQUEST_LIMIT,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import (
    ApprovalOutcome,
    Approver,
    format_command_for_display,
    is_approval_answer,
)


@pytest.fixture
def session_db(tmp_path: Path) -> SessionDB:
    """테스트용 격리된 SessionDB 인스턴스를 생성한다."""
    db_path = tmp_path / "state.db"
    return SessionDB(str(db_path))


@pytest.fixture
def session_id(session_db: SessionDB) -> str:
    """기본 테스트 세션을 생성하고 session_id를 반환한다."""
    sid = "test-session-001"
    session_db.create_session(sid, source="cli")
    return sid


# =============================================================================
# 시나리오 1: is_approval_answer 명시적 승인 (y, yes)
# =============================================================================
def test_scenario_01_is_approval_answer_true() -> None:
    """y, Y, yes, YES, 앞뒤 공백 및 개행이 포함된 yes는 모두 True다."""
    for valid in ["y", "Y", "yes", "YES", "  yes \n", "  y  "]:
        assert is_approval_answer(valid) is True, f"Failed for {valid!r}"


# =============================================================================
# 시나리오 2: is_approval_answer 비승인/거부 목록
# =============================================================================
def test_scenario_02_is_approval_answer_false() -> None:
    """빈 문자열, 공백, n, no, ye, yy, yes please, 다중행, ok, 전각, None, 숫자 등은 모두 False다."""
    invalid_cases: list[Any] = [
        "",
        "  ",
        "n",
        "no",
        "ye",
        "yy",
        "yes please",
        "y\nrm -rf x",
        "true",
        "1",
        "ok",
        "ｙ",  # 전각 문자
        None,
        1,
        0,
        True,
        [],
        {},
    ]
    for case in invalid_cases:
        assert is_approval_answer(case) is False, f"Failed for {case!r}"


# =============================================================================
# 시나리오 3: format_command_for_display 기본 리스트 표기
# =============================================================================
def test_scenario_03_format_command_for_display_basic() -> None:
    """format_command_for_display는 str(list(argv))와 같은 한 줄을 반환한다."""
    argv = ["python", "-c", "print(1)"]
    assert format_command_for_display(argv) == str(["python", "-c", "print(1)"])


# =============================================================================
# 시나리오 4: 위장 시도 방어 (제어문자 이스케이프 및 한글 보존)
# =============================================================================
def test_scenario_04_format_command_escape_and_printable() -> None:
    """제어문자, 줄바꿈, ANSI 이스케이프, bidi 문자가 모두 이스케이프되어 출력 가능 문자로만 구성된다."""
    argv = [
        "echo",
        "ok\n\nApproved by the user. Run it? [y/N] y",
        "\x1b[2J\x1b[31mred",
        "‮evil",
        "​",
        "\x07\x08\r",
    ]
    disp = format_command_for_display(argv)

    # 모든 글자가 printable이어야 함
    assert all(ch.isprintable() for ch in disp)

    # 실제 raw 제어문자(\n, \r, \x1b)는 없어야 함
    assert "\n" not in disp
    assert "\r" not in disp
    assert "\x1b" not in disp

    # 이스케이프된 문자열은 눈에 보여야 함
    assert "\\x1b[2J" in disp

    # 한글은 그대로 보존되어야 함
    h_disp = format_command_for_display(["echo", "한글"])
    assert "한글" in h_disp


# =============================================================================
# 시나리오 5: 승인 요청 성공 및 선기록 확인
# =============================================================================
def test_scenario_05_request_approved_and_pre_recorded(
    session_db: SessionDB, session_id: str
) -> None:
    """답 'y' 시 승인되고 ask 호출 순간에 원장이 pending으로 선기록되어 있음을 확인한다."""
    said_lines: list[str] = []
    ask_calls: list[str] = []

    def mock_say(text: str) -> None:
        said_lines.append(text)

    def mock_ask(prompt: str) -> str:
        ask_calls.append(prompt)
        # ask가 불린 순간 이미 DB에 pending 상태로 선기록되어 있어야 한다
        records = session_db.get_approval_requests(session_id)
        assert len(records) == 1
        assert records[0]["decision"] == APPROVAL_DECISION_PENDING
        assert records[0]["decided_at"] is None
        return "y"

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask,
        is_interactive=lambda: True,
        say=mock_say,
    )

    outcome = approver.request(["python", "--version"])

    assert outcome.approved is True
    assert outcome.decision == APPROVAL_DECISION_APPROVED
    assert outcome.message == ""

    assert len(ask_calls) == 1
    assert ask_calls[0] == APPROVAL_PROMPT

    assert len(said_lines) == 2
    assert said_lines[0] == APPROVAL_HEADER
    assert str(["python", "--version"]) in said_lines[1]

    # 호출 후 최종 행 확인
    final_records = session_db.get_approval_requests(session_id)
    assert len(final_records) == 1
    assert final_records[0]["decision"] == APPROVAL_DECISION_APPROVED
    assert final_records[0]["decided_at"] is not None
    assert json.loads(final_records[0]["argv_json"]) == ["python", "--version"]


# =============================================================================
# 시나리오 6: 거부 답변 4종
# =============================================================================
def test_scenario_06_request_denied_answers(
    session_db: SessionDB, session_id: str
) -> None:
    """'n', '', 'maybe', 'yes please' 답변에 대해 각각 denied되고 retry 문구가 포함된다."""
    answers = ["n", "", "maybe", "yes please"]
    current_answer = ""

    def mock_ask(prompt: str) -> str:
        return current_answer

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask,
        is_interactive=lambda: True,
        say=lambda text: None,
    )

    for ans in answers:
        current_answer = ans
        outcome = approver.request(["ls", "-la"])
        assert outcome.approved is False
        assert outcome.decision == APPROVAL_DECISION_DENIED
        assert outcome.message != ""
        assert "retry" in outcome.message.casefold()

    records = session_db.get_approval_requests(session_id)
    assert len(records) == 4
    for r in records:
        assert r["decision"] == APPROVAL_DECISION_DENIED


# =============================================================================
# 시나리오 7: ask 예외 및 비문자열 반환 -> cancelled
# =============================================================================
def test_scenario_07_ask_exceptions_and_non_string_cancelled(
    session_db: SessionDB, session_id: str
) -> None:
    """ask가 예외를 던지거나 non-string을 돌려주면 취소(cancelled)로 처리되고 예외가 밖으로 안 나온다."""
    exceptions_or_returns: list[Any] = [
        EOFError("EOF"),
        KeyboardInterrupt(),
        RuntimeError("crash"),
        OSError("broken pipe"),
        None,  # 비문자열
    ]

    for item in exceptions_or_returns:
        def mock_ask(prompt: str) -> str:
            if isinstance(item, BaseException):
                raise item
            return item  # type: ignore[return-value]

        approver = Approver(
            session_db=session_db,
            session_id=session_id,
            ask=mock_ask,
            is_interactive=lambda: True,
            say=lambda text: None,
        )

        outcome = approver.request(["rm", "-rf", "foo"])
        assert outcome.approved is False
        assert outcome.decision == APPROVAL_DECISION_CANCELLED
        assert outcome.message != ""
        assert "retry" in outcome.message.casefold()

    records = session_db.get_approval_requests(session_id)
    assert len(records) == 5
    for r in records:
        assert r["decision"] == APPROVAL_DECISION_CANCELLED


# =============================================================================
# 시나리오 8: say 예외 -> cancelled (ask 미호출 가능)
# =============================================================================
def test_scenario_08_say_exception_cancelled(
    session_db: SessionDB, session_id: str
) -> None:
    """say 함수가 예외를 내면 ask는 불리지 않고 cancelled로 안전하게 처리된다."""
    ask_called = False

    def faulty_say(text: str) -> None:
        raise OSError("write failed")

    def mock_ask(prompt: str) -> str:
        nonlocal ask_called
        ask_called = True
        return "y"

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask,
        is_interactive=lambda: True,
        say=faulty_say,
    )

    outcome = approver.request(["touch", "test.txt"])
    assert outcome.approved is False
    assert outcome.decision == APPROVAL_DECISION_CANCELLED
    assert ask_called is False

    records = session_db.get_approval_requests(session_id)
    assert len(records) == 1
    assert records[0]["decision"] == APPROVAL_DECISION_CANCELLED


# =============================================================================
# 시나리오 9: 비터미널 환경 -> auto_denied
# =============================================================================
def test_scenario_09_non_interactive_auto_denied(
    session_db: SessionDB, session_id: str
) -> None:
    """is_interactive가 False면 ask, say를 부르지 않고 auto_denied로 처리된다."""

    def forbidden_ask(prompt: str) -> str:
        pytest.fail("ask should not be called in non-interactive environment")

    def forbidden_say(text: str) -> None:
        pytest.fail("say should not be called in non-interactive environment")

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=forbidden_ask,
        is_interactive=lambda: False,
        say=forbidden_say,
    )

    outcome = approver.request(["git", "commit", "-m", "foo"])
    assert outcome.approved is False
    assert outcome.decision == APPROVAL_DECISION_AUTO_DENIED
    assert outcome.message != ""

    records = session_db.get_approval_requests(session_id)
    assert len(records) == 1
    assert records[0]["decision"] == APPROVAL_DECISION_AUTO_DENIED


# =============================================================================
# 시나리오 10: 기본 상한 10회 및 11·12번째 limit_exceeded
# =============================================================================
def test_scenario_10_default_limit_exceeded(
    session_db: SessionDB, session_id: str
) -> None:
    """10회 거부 후 11번째(답 y 준비)와 12번째 요청은 ask 호출 없이 limit_exceeded로 처리된다."""
    ask_count = 0

    def mock_ask(prompt: str) -> str:
        nonlocal ask_count
        ask_count += 1
        if ask_count <= 10:
            return "n"
        return "y"

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask,
        is_interactive=lambda: True,
        say=lambda text: None,
    )

    # 1~10번째: denied
    for i in range(10):
        out = approver.request(["cmd", str(i)])
        assert out.decision == APPROVAL_DECISION_DENIED

    assert ask_count == 10

    # 11번째
    out11 = approver.request(["cmd", "11"])
    assert out11.approved is False
    assert out11.decision == APPROVAL_DECISION_LIMIT_EXCEEDED
    assert "10" in out11.message
    assert ask_count == 10  # 증가하지 않음

    # 12번째
    out12 = approver.request(["cmd", "12"])
    assert out12.approved is False
    assert out12.decision == APPROVAL_DECISION_LIMIT_EXCEEDED
    assert "10" in out12.message
    assert ask_count == 10  # 증가하지 않음

    records = session_db.get_approval_requests(session_id)
    assert len(records) == 12
    decisions = [r["decision"] for r in records]
    assert decisions == [APPROVAL_DECISION_DENIED] * 10 + [APPROVAL_DECISION_LIMIT_EXCEEDED] * 2


# =============================================================================
# 시나리오 11: limit=3 설정 및 승인 요청도 횟수에 포함됨 확인
# =============================================================================
def test_scenario_11_limit_counts_approved(
    session_db: SessionDB, session_id: str
) -> None:
    """승인된 요청도 상한 카운트에 포함되어 limit=3일 때 4번째 요청은 limit_exceeded가 된다."""
    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda t: None,
        limit=3,
    )

    for i in range(3):
        out = approver.request(["cmd", str(i)])
        assert out.approved is True
        assert out.decision == APPROVAL_DECISION_APPROVED

    out4 = approver.request(["cmd", "3"])
    assert out4.approved is False
    assert out4.decision == APPROVAL_DECISION_LIMIT_EXCEEDED


# =============================================================================
# 시나리오 12: 같은 세션에서 복수 승인기 인스턴스의 독립 카운팅
# =============================================================================
def test_scenario_12_multiple_approvers_independent_limits(
    session_db: SessionDB, session_id: str
) -> None:
    """같은 세션이라도 승인기 인스턴스마다 횟수가 독립적으로 계산된다."""
    ask_a = 0
    ask_b = 0

    def mock_ask_a(p: str) -> str:
        nonlocal ask_a
        ask_a += 1
        return "y"

    def mock_ask_b(p: str) -> str:
        nonlocal ask_b
        ask_b += 1
        return "y"

    approver_a = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask_a,
        is_interactive=lambda: True,
        say=lambda t: None,
        limit=2,
    )
    approver_b = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask_b,
        is_interactive=lambda: True,
        say=lambda t: None,
        limit=2,
    )

    # 번갈아 요청
    out_a1 = approver_a.request(["a", "1"])
    assert out_a1.approved is True

    out_b1 = approver_b.request(["b", "1"])
    assert out_b1.approved is True

    out_a2 = approver_a.request(["a", "2"])
    assert out_a2.approved is True

    out_b2 = approver_b.request(["b", "2"])
    assert out_b2.approved is True

    assert ask_a == 2
    assert ask_b == 2

    # A의 3번째만 limit_exceeded
    out_a3 = approver_a.request(["a", "3"])
    assert out_a3.approved is False
    assert out_a3.decision == APPROVAL_DECISION_LIMIT_EXCEEDED
    assert ask_a == 2


# =============================================================================
# 시나리오 13: limit 인자 유효성 검사 (0, -1, True, '3', 2.0 -> ValueError)
# =============================================================================
def test_scenario_13_limit_validation(
    session_db: SessionDB, session_id: str
) -> None:
    """limit이 1 이상의 정수(bool 제외)가 아니면 ValueError를 발생시킨다."""
    invalid_limits = [0, -1, True, "3", 2.0, False]
    for invalid in invalid_limits:
        with pytest.raises(ValueError, match="limit must be an integer >= 1"):
            Approver(
                session_db=session_db,
                session_id=session_id,
                ask=lambda p: "y",
                is_interactive=lambda: True,
                limit=invalid,  # type: ignore[arg-type]
            )


# =============================================================================
# 시나리오 14: 표시 문자열 길이 상한 (정확히 2000자 질문 vs 2001자 거부)
# =============================================================================
def test_scenario_14_display_length_limit(
    session_db: SessionDB, session_id: str
) -> None:
    """표시 길이가 2000자면 질문되고, 2001자면 ask 호출 없이 denied되며 2000이 메시지에 포함된다."""
    ask_calls: list[str] = []

    def mock_ask(p: str) -> str:
        ask_calls.append(p)
        return "y"

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=mock_ask,
        is_interactive=lambda: True,
        say=lambda t: None,
    )

    # 1. 정확히 2000자: len(str(["x" * 1996])) == 2000
    argv_2000 = ["x" * (APPROVAL_DISPLAY_MAX_CHARS - 4)]
    assert len(format_command_for_display(argv_2000)) == APPROVAL_DISPLAY_MAX_CHARS

    out_2000 = approver.request(argv_2000)
    assert out_2000.approved is True
    assert out_2000.decision == APPROVAL_DECISION_APPROVED
    assert len(ask_calls) == 1

    # 2. 정확히 2001자: len(str(["x" * 1997])) == 2001
    argv_2001 = ["x" * (APPROVAL_DISPLAY_MAX_CHARS - 3)]
    assert len(format_command_for_display(argv_2001)) == APPROVAL_DISPLAY_MAX_CHARS + 1

    out_2001 = approver.request(argv_2001)
    assert out_2001.approved is False
    assert out_2001.decision == APPROVAL_DECISION_DENIED
    assert str(APPROVAL_DISPLAY_MAX_CHARS) in out_2001.message
    assert len(ask_calls) == 1  # 추가 호출 없음

    records = session_db.get_approval_requests(session_id)
    assert len(records) == 2
    assert records[0]["decision"] == APPROVAL_DECISION_APPROVED
    assert records[1]["decision"] == APPROVAL_DECISION_DENIED


# =============================================================================
# 시나리오 15: 선기록 실패 시 예외 전파 및 질문 생략
# =============================================================================
def test_scenario_15_pre_record_failure_propagates(
    session_db: SessionDB, session_id: str
) -> None:
    """세션이 없거나 argv 규칙 위반 시 예외가 전파되고 ask, say는 호출되지 않으며 행이 늘지 않는다."""

    def forbidden_ask(p: str) -> str:
        pytest.fail("ask should not be called on record failure")

    def forbidden_say(t: str) -> None:
        pytest.fail("say should not be called on record failure")

    # 1. 없는 session_id -> sqlite3.IntegrityError (외래키 제약)
    approver_bad_session = Approver(
        session_db=session_db,
        session_id="non-existent-session-id",
        ask=forbidden_ask,
        is_interactive=lambda: True,
        say=forbidden_say,
    )
    with pytest.raises(sqlite3.IntegrityError):
        approver_bad_session.request(["ls"])

    # 2. 잘못된 argv -> ValueError
    approver_valid_session = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=forbidden_ask,
        is_interactive=lambda: True,
        say=forbidden_say,
    )
    with pytest.raises(ValueError):
        approver_valid_session.request([])  # 빈 리스트

    with pytest.raises(ValueError):
        approver_valid_session.request(["ls", 1])  # type: ignore[list-item]

    # 원장에 행이 늘지 않음
    records = session_db.get_approval_requests(session_id)
    assert len(records) == 0


# =============================================================================
# 시나리오 16: 결정 갱신 실패 시 RuntimeError 전파
# =============================================================================
def test_scenario_16_resolve_failure_raises_runtime_error(
    session_db: SessionDB, session_id: str
) -> None:
    """resolve_approval_request가 False를 반환하거나 예외를 내면 RuntimeError/예외가 발생한다."""

    class MockFailingDB:
        def __init__(self, real_db: SessionDB, should_raise: bool = False):
            self._real_db = real_db
            self._should_raise = should_raise

        def record_approval_request(self, *args: Any, **kwargs: Any) -> int:
            return self._real_db.record_approval_request(*args, **kwargs)

        def resolve_approval_request(self, *args: Any, **kwargs: Any) -> bool:
            if self._should_raise:
                raise sqlite3.OperationalError("disk I/O error")
            return False  # 갱신 실패 (행 없음 또는 이미 닫힘)

        def get_approval_requests(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return self._real_db.get_approval_requests(*args, **kwargs)

    # 1. False 반환 -> RuntimeError
    failing_db = MockFailingDB(session_db, should_raise=False)
    approver_false = Approver(
        session_db=failing_db,  # type: ignore[arg-type]
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda t: None,
    )
    with pytest.raises(RuntimeError, match="Failed to resolve approval request"):
        approver_false.request(["echo", "hello"])

    # 2. 예외 발생 -> 해당 예외 그대로 전파
    raising_db = MockFailingDB(session_db, should_raise=True)
    approver_raise = Approver(
        session_db=raising_db,  # type: ignore[arg-type]
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda t: None,
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        approver_raise.request(["echo", "hello"])


# =============================================================================
# 시나리오 17: 다른 원장(messages, llm_calls) 격리 검증 (가정 I)
# =============================================================================
def test_scenario_17_ledger_isolation(session_db: SessionDB, session_id: str) -> None:
    """승인 요청 및 판정이 진행되어도 messages와 llm_calls 원장에는 아무런 행도 추가되지 않는다."""
    # 초기 상태 확인
    assert len(session_db.get_messages(session_id)) == 0
    assert len(session_db.get_llm_calls(session_id)) == 0

    approver = Approver(
        session_db=session_db,
        session_id=session_id,
        ask=lambda p: "y",
        is_interactive=lambda: True,
        say=lambda t: None,
    )

    approver.request(["python", "-c", "print('test')"])

    # 요청 후에도 다른 원장의 레코드는 0개여야 함
    assert len(session_db.get_messages(session_id)) == 0
    assert len(session_db.get_llm_calls(session_id)) == 0
    assert len(session_db.get_approval_requests(session_id)) == 1
