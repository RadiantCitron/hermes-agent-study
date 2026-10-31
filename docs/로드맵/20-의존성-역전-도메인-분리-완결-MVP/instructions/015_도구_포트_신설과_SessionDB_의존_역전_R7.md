# [015] 도구 계층 포트(`tools/ports.py`) 신설과 `SessionDB` 의존 역전(R7)

- **로드맵**: [20-의존성-역전-도메인-분리-완결-MVP](../README.md) ([ROADMAP.md §6-1](../ROADMAP.md), [ADR-0020](../../../adr/0020-의존성_역전_도메인_분리_완결_MVP_범위와_결정.md) D-02, D-03)
- **선행 단계**: 없음 (`010` 지시서는 컨텍스트 요약 직후 심볼명 오기로 `Dropped` 처리되었으며, 본 `015` 지시서가 첫 실행 단계임. 코드 기준선 `bd41ec4`, `not pty` 1,137 / `pty` 11 / `test_architecture` 34)
- **성격**: 코드 구현 (`src/hermes_core/tools/ports.py` 신설, `session_db.py` 및 `tools/*` 5개 모듈 수정, 아키텍처 테스트 추가)
- **실제 API 키**: 불필요

---

## 0. 착수 전 점검 (지시서 작성 시점에 완료된 사실)

`ROADMAP.md §6-1` 및 `§9`의 지시서 작성 전 탐침 항목을 실제 코드와 `inspect.signature`로 대조한 결과는 다음과 같다.

1. **`tools/*` 5개 모듈의 `SessionDB` 참조 및 실제 호출 시그니처 전수 확인**:
   - [`src/hermes_core/tools/approval.py:31,75,118,131`](../../../../src/hermes_core/tools/approval.py):
     - 현재 import: `from hermes_core.session_db import SessionDB`
     - 클래스 및 생성자: `Approver.__init__(self, *, session_db: SessionDB, session_id: str, ask: Callable[[str], str], is_interactive: Callable[[], bool], say: Callable[[str], None] = print, limit: int = APPROVAL_REQUEST_LIMIT) -> None`
     - 실제 호출하는 `SessionDB` 메서드 2종:
       - `record_approval_request(self, session_id: str, argv: list[str], tool_name: str = 'terminal') -> int` (118행)
       - `resolve_approval_request(self, request_id: int, decision: str, reason: str | None = None) -> bool` (131, 141, 152, 165, 179행)
   - [`src/hermes_core/tools/checkpoint.py:55,139,494-512`](../../../../src/hermes_core/tools/checkpoint.py):
     - 현재 import: `from hermes_core.session_db import SessionDB`
     - 클래스 및 생성자: `Checkpointer.__init__(self, db: SessionDB, session_id: str, workspace: Optional[Path] = None, *, bytes_limit: int = CHECKPOINT_SESSION_BYTES_LIMIT)`
     - 실제 호출하는 `SessionDB` 공개 메서드 4종:
       - `get_file_checkpoints(self, session_id: str, turn_index: int | None = None) -> list[dict[str, Any]]` (157, 255, 352, 371, 728행)
       - `record_file_checkpoint(self, session_id: str, turn_index: int, tool_name: str, workspace: str, rel_path: str, before_exists: bool, before_content: str | None = None) -> int` (242행)
       - `update_file_checkpoint(self, checkpoint_id: int, status: str, *, after_sha256: str | None = None, reason: str | None = None) -> bool` (285, 300, 308, 320, 331, 339, 344, 538, 557, 574, 594, 676행)
       - `get_approval_requests(self, session_id: str) -> list[dict[str, Any]]` (475행)
     - **비공개 속성 직접 접근 3분기 (`checkpoint.py:494-512`, `plan_rollback` 내부)**:
       - `start_time is None`: `self.db._conn.execute("SELECT count(*) FROM sessions WHERE parent_session_id = ?", (self.session_id,))`
       - `end_time is None`: `self.db._conn.execute("SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ?", (self.session_id, start_time))`
       - 둘 다 지정: `self.db._conn.execute("SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ? AND started_at < ?", (self.session_id, start_time, end_time))`
   - [`src/hermes_core/tools/delegate_tool.py:25,59,144,153`](../../../../src/hermes_core/tools/delegate_tool.py):
     - 현재 import: `from hermes_core.session_db import SessionDB`
     - 함수 시그니처: `register_delegate_tool(dispatcher: ToolDispatcher, *, session_db: SessionDB, parent_session_id: str, llm_client_factory: Callable[[], Any], max_iterations: int = DELEGATE_CHILD_MAX_ITERATIONS, summary_max_chars: int = DELEGATE_SUMMARY_MAX_CHARS) -> None`
     - 실제 호출하는 `SessionDB` 메서드 2종:
       - `end_session(self, session_id: str, end_reason: str = 'completed') -> None` (144행)
       - `get_session(self, session_id: str) -> dict[str, Any] | None` (153행)
       *(참고: 015 시점에서는 `delegate_tool.py`가 아직 `HermesAgent(session_db=session_db, ...)`에도 `session_db`를 넘기며, 020에서 `HermesAgent` 생성이 `assembly.run_child_session`으로 이동한다.)*
   - [`src/hermes_core/tools/session_search_tool.py:12,41,51`](../../../../src/hermes_core/tools/session_search_tool.py):
     - 현재 import: `from hermes_core.session_db import SessionDB`
     - 함수 시그니처: `session_search_tool(query: str, limit: int = 3, db: Optional[SessionDB] = None) -> str`
     - 실제 호출하는 `SessionDB` 메서드 1종:
       - `search_messages(self, query: str, source_filter: list[str] | None = None, role_filter: list[str] | None = None, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]` (51행)
   - [`src/hermes_core/tools/dispatcher.py:28,341,377`](../../../../src/hermes_core/tools/dispatcher.py):
     - 현재 import: `from hermes_core.session_db import SessionDB`
     - 함수 시그니처: `create_default_dispatcher(memory_store: Optional[MemoryStore] = None, session_db: Optional[SessionDB] = None, include: Optional[Collection[str]] = None, approver: Optional[Approver] = None, checkpointer: Optional[Checkpointer] = None) -> ToolDispatcher`
     - `session_search_tool(db=session_db, **kwargs)`로 전달함.
2. **기존 테스트의 `tools/*` 경유 `SessionDB` 참조 여부**:
   - `tests/` 전체에서 `from hermes_core.tools... import SessionDB`는 **0건**이다.
   - 단, [`tests/test_architecture/test_dependency_rules.py:305-307`](../../../../tests/test_architecture/test_dependency_rules.py)의 `test_scenario_9_actual_tree_scanner_smoke`가:
     ```python
     # hermes_core.tools.delegate_tool이 hermes_core.session_db를 참조하는지 확인
     del_targets = all_imports.get("hermes_core.tools.delegate_tool", set())
     assert any(t == "hermes_core.session_db" or t.startswith("hermes_core.session_db.") for t in del_targets)
     ```
     를 단언하고 있으므로, 본 단계에서 `hermes_core.tools.ports`를 확인하도록 함께 갱신한다.

---

## 1. 구현 내용 명세

### 1.1 `src/hermes_core/tools/ports.py` 신설 (D-02)

표준 라이브러리(`from __future__ import annotations`, `typing.Any`, `Optional`, `Protocol`, `runtime_checkable`)만 import하여 도구 계층 전용 포트 프로토콜 5종을 정의한다. (`test_annotations_resolve.py`의 `typing.get_type_hints()` 검사를 100% 통과하도록 외부 모듈 타입을 쓰지 않는다.)

```python
"""도구 계층(tools/*)이 요구하는 외부 저장소 및 자식 실행기 포트(Protocol) 정의.

도구 계층 모듈은 구체 SQLite 구현체(SessionDB)나 상위 에이전트 구현체(HermesAgent)를
직접 import하지 않고 본 모듈의 프로토콜에 의존한다 (ADR-0020 D-02, D-04).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class ApprovalStorePort(Protocol):
    """Approver가 사용하는 승인 요청 선기록 및 결정 갱신 저장소 포트."""

    def record_approval_request(
        self,
        session_id: str,
        argv: list[str],
        tool_name: str = "terminal",
    ) -> int: ...

    def resolve_approval_request(
        self,
        request_id: int,
        decision: str,
        reason: Optional[str] = None,
    ) -> bool: ...


@runtime_checkable
class CheckpointStorePort(Protocol):
    """Checkpointer가 사용하는 파일 체크포인트·승인 이력·자식 세션 수 조회 포트."""

    def record_file_checkpoint(
        self,
        session_id: str,
        turn_index: int,
        tool_name: str,
        workspace: str,
        rel_path: str,
        before_exists: bool,
        before_content: Optional[str] = None,
    ) -> int: ...

    def update_file_checkpoint(
        self,
        checkpoint_id: int,
        status: str,
        *,
        after_sha256: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool: ...

    def get_file_checkpoints(
        self,
        session_id: str,
        turn_index: Optional[int] = None,
    ) -> list[dict[str, Any]]: ...

    def get_approval_requests(self, session_id: str) -> list[dict[str, Any]]: ...

    def count_child_sessions(
        self,
        parent_session_id: str,
        *,
        started_at_min: Optional[float] = None,
        started_at_max: Optional[float] = None,
    ) -> int: ...


@runtime_checkable
class DelegateStorePort(Protocol):
    """register_delegate_tool이 자식 세션 마감 및 토큰 조회에 사용하는 저장소 포트."""

    def end_session(self, session_id: str, end_reason: str = "completed") -> None: ...

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]: ...


@runtime_checkable
class SessionSearchPort(Protocol):
    """session_search_tool 및 create_default_dispatcher가 사용하는 과거 대화 검색 포트."""

    def search_messages(
        self,
        query: str,
        source_filter: Optional[list[str]] = None,
        role_filter: Optional[list[str]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class ChildRunnerPort(Protocol):
    """delegate_task가 자식 에이전트 세션을 실행할 때 호출하는 콜백 포트 (020에서 연결)."""

    def __call__(
        self,
        goal: str,
        context: Optional[str] = None,
        *,
        session_db: Any,
        parent_session_id: str,
        llm_client: Any,
        max_iterations: int,
        dispatcher: Optional[Any] = None,
    ) -> dict[str, Any]: ...
```

### 1.2 `src/hermes_core/session_db.py`에 `count_child_sessions` 공개 메서드 추가 (D-03)

[`src/hermes_core/session_db.py`](../../../../src/hermes_core/session_db.py)의 `SessionDB` 클래스에 자식 세션 수를 조회하는 공개 메서드를 추가한다:

```python
    def count_child_sessions(
        self,
        parent_session_id: str,
        *,
        started_at_min: Optional[float] = None,
        started_at_max: Optional[float] = None,
    ) -> int:
        """부모 세션에서 파생된 자식 세션 수를 반환한다.

        Args:
            parent_session_id: 부모 세션 ID.
            started_at_min: 지정 시 started_at >= started_at_min 조건 추가.
            started_at_max: 지정 시 started_at < started_at_max 조건 추가 (started_at_min과 함께 사용).
        """
        if started_at_min is None:
            cursor = self._conn.execute(
                "SELECT count(*) FROM sessions WHERE parent_session_id = ?",
                (parent_session_id,),
            )
        elif started_at_max is None:
            cursor = self._conn.execute(
                "SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ?",
                (parent_session_id, started_at_min),
            )
        else:
            cursor = self._conn.execute(
                "SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ? AND started_at < ?",
                (parent_session_id, started_at_min, started_at_max),
            )
        row = cursor.fetchone()
        return int(row[0]) if row else 0
```

### 1.3 `src/hermes_core/tools/checkpoint.py` 수정 (D-02, D-03)

1. `from hermes_core.session_db import SessionDB`를 제거하고 `from hermes_core.tools.ports import CheckpointStorePort`로 교체한다.
2. `Checkpointer.__init__`의 `db: SessionDB` 타입 힌트를 `db: CheckpointStorePort`로 교체한다.
3. `Checkpointer.plan_rollback` 내부(492~514행)의 `self.db._conn.execute(...)` 3분기 블록을 `self.db.count_child_sessions` 호출로 교체한다:
   ```python
        subagent_count = 0
        try:
            subagent_count = self.db.count_child_sessions(
                self.session_id,
                started_at_min=start_time,
                started_at_max=end_time,
            )
        except Exception:
            pass
   ```

### 1.4 나머지 `src/hermes_core/tools/*` 4개 모듈의 `SessionDB` 임포트 교체 (D-02)

1. [`src/hermes_core/tools/approval.py`](../../../../src/hermes_core/tools/approval.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import ApprovalStorePort` 임포트.
   - `Approver.__init__`의 `session_db: SessionDB` → `session_db: ApprovalStorePort`.
2. [`src/hermes_core/tools/session_search_tool.py`](../../../../src/hermes_core/tools/session_search_tool.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import SessionSearchPort` 임포트.
   - `session_search_tool(query: str, limit: int = 3, db: Optional[SessionSearchPort] = None) -> str`로 타입 힌트 교체.
3. [`src/hermes_core/tools/delegate_tool.py`](../../../../src/hermes_core/tools/delegate_tool.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import DelegateStorePort` 임포트.
   - `register_delegate_tool`의 `session_db: SessionDB` → `session_db: DelegateStorePort`.
4. [`src/hermes_core/tools/dispatcher.py`](../../../../src/hermes_core/tools/dispatcher.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import SessionSearchPort` 임포트.
   - `create_default_dispatcher`의 `session_db: Optional[SessionDB] = None` → `session_db: Optional[SessionSearchPort] = None`.

### 1.5 아키텍처 규칙 `R7` 및 포트 적합성 시험 추가

1. [`tests/test_architecture/test_dependency_rules.py`](../../../../tests/test_architecture/test_dependency_rules.py):
   - `find_violations` 내부 `if is_importer_tools:` 블록(128~132행 부근) 아래에 **R7 규칙**을 추가한다:
     ```python
            # R7: importer가 hermes_core.tools 하위이면 대상이 hermes_core.session_db(하위 포함)이면 위반
            if is_importer_tools:
                target_is_session_db = (
                    target == "hermes_core.session_db"
                    or target.startswith("hermes_core.session_db.")
                )
                if target_is_session_db:
                    violations.add((importer, target, "R7"))
     ```
   - `test_scenario_9_actual_tree_scanner_smoke`(305~307행)의 `del_targets` 확인을 `hermes_core.tools.ports` 참조 확인 및 `hermes_core.session_db` 미참조 확인으로 갱신한다.
   - `R7` 단위 검증 함수(`test_scenario_r7_tools_cannot_import_session_db`)를 추가하여:
     - 합성 위반 `find_violations({"hermes_core.tools.checkpoint": {"hermes_core.session_db"}})`가 `{("hermes_core.tools.checkpoint", "hermes_core.session_db", "R7")}`을 반환하고,
     - `find_violations({"hermes_core.tools.checkpoint": {"hermes_core.tools.ports"}})`는 `set()`임을 검증하며,
     - 실제 소스 트리에서 `hermes_core.tools.*` 모듈 중 `hermes_core.session_db`를 import하는 모듈이 0개임을 검증한다.
2. [`tests/test_architecture/test_ports_conformance.py`](../../../../tests/test_architecture/test_ports_conformance.py):
   - `ApprovalStorePort`, `CheckpointStorePort`, `DelegateStorePort`, `SessionSearchPort`를 import하여:
     - `issubclass(SessionDB, ApprovalStorePort)` 등 4개 포트 모두 `True`임을 단언.
     - `inspect.signature(SessionDB.<method>).bind(...)`로 4개 포트의 모든 메서드(`record_approval_request`, `resolve_approval_request`, `record_file_checkpoint`, `update_file_checkpoint`, `get_file_checkpoints`, `get_approval_requests`, `count_child_sessions`, `end_session`, `get_session`, `search_messages`) 바인딩이 일치함을 검증.
     - `SessionDB` 인스턴스(`tmp_path`)로 `count_child_sessions`의 3가지 분기(`started_at_min=None`, `started_at_max=None`, 구간 지정) 및 순수 인메모리 Fake 포트로 `Approver`·`session_search_tool`이 정상 동작함을 검증.

---

## 2. 인수 테스트 시나리오 (입력 → 기대 결과)

| # | 시나리오 | 입력 / 조건 | 기대 결과 |
|---|---|---|---|
| 1 | **`SessionDB`의 도구 포트 4종 적합성 및 시그니처 바인딩** | `ApprovalStorePort`, `CheckpointStorePort`, `DelegateStorePort`, `SessionSearchPort`에 대해 `issubclass(SessionDB, Port)` 및 `inspect.signature().bind()` 실행 | 4개 포트 모두 `issubclass`가 `True`이고 모든 메서드의 인자 바인딩이 예외 없이 통과한다 |
| 2 | **`SessionDB.count_child_sessions` 전체 집계 (`started_at_min=None`)** | 부모 세션 `"p1"` 생성 후 자식 `"c1"`, `"c2"`(`parent_session_id="p1"`) 및 무관한 세션 `"other"` 생성 | `db.count_child_sessions("p1") == 2`, `db.count_child_sessions("none") == 0` |
| 3 | **`SessionDB.count_child_sessions` 구간 집계 (`started_at_min`, `started_at_max`)** | 자식 `"c1"`(시각 `t1`), `"c2"`(시각 `t2`)를 시간차로 생성하거나 `started_at`을 설정한 뒤 조회 | `started_at_min=t2`이면 `1`, `started_at_min=t1, started_at_max=t2`이면 `1`을 정확히 반환한다 |
| 4 | **`Checkpointer.plan_rollback`의 `count_child_sessions` 연동** | 체크포인트 턴 시작 후 자식 세션을 생성하고 `checkpointer.plan_rollback()` 호출 | `plan.subagent_count == 1`, `plan.has_warnings is True`가 반환되며 `checkpoint.py`에 `_conn` 참조가 존재하지 않는다 |
| 5 | **순수 인메모리 Fake 포트로 `Approver` 구동** | `SessionDB`를 상속하지 않는 순수 인메모리 `FakeApprovalStore`를 `Approver(session_db=fake_store, ...)`에 주입하여 `request(["rm", "-rf", "foo"])` 호출 | SQLite DB 파일 없이 `fake_store`에 pending 선기록과 최종 결정(`approved`/`denied`)이 정상 기록된다 |
| 6 | **순수 인메모리 Fake 포트로 `session_search_tool` 구동** | `SessionDB`를 상속하지 않는 `FakeSearchStore`를 `session_search_tool(query="hello", db=fake_store)`에 전달 | JSON 응답의 `success is True`, `count == 1` 및 스니펫 결과가 정상 반환된다 |
| 7 | **아키텍처 규칙 `R7` 합성 위반 탐지** | `find_violations({"hermes_core.tools.checkpoint": {"hermes_core.session_db"}})` 호출 | `{("hermes_core.tools.checkpoint", "hermes_core.session_db", "R7")}` 위반이 검출된다 |
| 8 | **아키텍처 규칙 `R7` 실제 소스 트리 검사** | `scan_source_tree`로 `src/hermes_core/tools/` 하위 전 모듈 스캔 | `hermes_core.session_db`를 import하는 모듈이 **0개**이며 시나리오 10 래칫 시험이 통과한다 |
| 9 | **전 모듈 타입 주석 해석(`get_type_hints`) 통과** | `pytest tests/test_architecture/test_annotations_resolve.py` 실행 | `tools/ports.py` 및 수정된 5개 도구 모듈을 포함해 미해결 타입 주석이 **0건**이다 |
| 10 | **기존 전체 회귀 시험 통과 및 커널 무접촉** | `pytest -m "not pty"` 및 `pytest -m pty` 실행, `git diff src/hermes_core/kernel/` 확인 | 기존 1,148개 시험 + 신규 시험 전수 통과, `src/hermes_core/kernel/` diff 0줄 |

---

## 3. 검증 명령

```bash
# 1. 아키텍처 테스트 (R1~R7, 주석 해석, 포트 적합성)
./.venv/bin/pytest tests/test_architecture/ -v

# 2. 승인·체크포인트·세션검색·위임 관련 기존 테스트 회귀 확인
./.venv/bin/pytest tests/test_hermes_core/test_approval.py tests/test_hermes_core/test_approval_ledger.py tests/test_hermes_core/test_checkpoint_module.py tests/test_hermes_core/test_file_tools_checkpoint.py tests/test_hermes_core/test_delegate_tool.py -v

# 3. 커널 무접촉 확인
git diff src/hermes_core/kernel/

# 4. 전체 스위트 (not pty + pty)
./.venv/bin/pytest -n 4 -q -m "not pty"
./.venv/bin/pytest -q -m pty
```

---

## 4. 커밋 규칙

- **선행 확인**: 커밋 전 `git fetch` 및 `git status`로 원격 정합성 확인, `git diff src/hermes_core/kernel/`이 0줄인지 확인.
- **스테이징 (`git add -A` 금지)**: 변경한 파일 경로만 명시하여 스테이징한다.
  - `src/hermes_core/tools/ports.py`
  - `src/hermes_core/session_db.py`
  - `src/hermes_core/tools/approval.py`
  - `src/hermes_core/tools/checkpoint.py`
  - `src/hermes_core/tools/delegate_tool.py`
  - `src/hermes_core/tools/session_search_tool.py`
  - `src/hermes_core/tools/dispatcher.py`
  - `tests/test_architecture/test_dependency_rules.py`
  - `tests/test_architecture/test_ports_conformance.py`
  - `docs/로드맵/20-의존성-역전-도메인-분리-완결-MVP/README.md`
  - `docs/로드맵/20-의존성-역전-도메인-분리-완결-MVP/instructions/020_...md` (`015` 완료 시 다음 단계 `020` 지시서를 함께 작성해 `Draft`로 등재)
- **커밋 메시지 형식**:
  - `feat(tools): R20-015 - 도구 계층 포트(tools/ports.py) 신설 및 SessionDB 의존 역전(R7)`

---

## 5. 다음 단계(020)로 넘기는 사실

- `src/hermes_core/tools/ports.py`에 `ChildRunnerPort`와 `DelegateStorePort`가 준비되었고 `tools/*`의 `SessionDB` 직접 의존이 모두 제거(R7)되었으므로, `020`에서는 [`ROADMAP.md §6-2`](../ROADMAP.md) 명세에 따라 `assembly.py`에 `run_child_session()`과 `create_child_runner()`를 추가하고, `delegate_tool.py`의 `HermesAgent`·`build_child_system_message` import를 제거하여 `KNOWN_DEBT = {}`를 달성하며 자식 세션 `end_session` 보장(`try...finally`, R-2)을 완성한다.

---

## 6. ROADMAP과 다른 점

- 없음 (`ROADMAP.md §6-1` 명세와 일치).
