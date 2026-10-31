# [010] 도구 계층 포트(`tools/ports.py`) 신설과 `SessionDB` 의존 역전(R7)

- **로드맵**: [20-의존성-역전-도메인-분리-완결-MVP](../README.md) ([ROADMAP.md §6 · 010](../ROADMAP.md), [ADR-0020](../../../adr/0020-의존성_역전_도메인_분리_완결_MVP_범위와_결정.md) D-02, D-03)
- **선행 단계**: 없음 (코드 기준선 `bd41ec4`, `not pty` 1,137 / `pty` 11 / `test_architecture` 34)
- **성격**: 코드 구현 (`src/hermes_core/tools/ports.py` 신설, `session_db.py` 및 `tools/*` 5개 모듈 수정, 아키텍처 테스트 추가)
- **실제 API 키**: 불필요

---

## 0. 착수 전 점검 (지시서 작성 시점에 완료된 사실)

`ROADMAP.md §6 (010)` 및 `§9.2`의 지시서 작성 전 점검 항목을 실제 코드에서 대조한 결과는 다음과 같다.

1. **`tools/*`의 `SessionDB` 참조 전수 확인**:
   - [`src/hermes_core/tools/approval.py:31`](../../../../src/hermes_core/tools/approval.py): `from hermes_core.session_db import SessionDB` (`ApprovalGuard.__init__(..., session_db: Optional[SessionDB] = None)`)
     - 호출 메서드: `record_approval_decision(session_id=..., tool_name=..., pattern=..., action=..., reason=...)`, `list_approval_rules(scope=..., session_id=...)`, `save_approval_rule(pattern=..., scope=..., action=..., session_id=...)`, `delete_approval_rule(pattern=..., scope=..., session_id=...)`
   - [`src/hermes_core/tools/checkpoint.py:55`](../../../../src/hermes_core/tools/checkpoint.py): `from hermes_core.session_db import SessionDB` (`Checkpointer.__init__(self, session_db: SessionDB, session_id: str, ...)`)
     - 호출 메서드:
       - `insert_checkpoint(...) -> int`
       - `get_checkpoint(checkpoint_id: int) -> Optional[Dict[str, Any]]`
       - `list_checkpoints(session_id: str, turn_id: Optional[str] = None, status: Optional[str] = None, include_restored: bool = True) -> List[Dict[str, Any]]`
       - `update_checkpoint_status(checkpoint_id: int, status: str, restored_at: Optional[float] = None) -> bool`
       - `insert_rollback_event(...) -> int`
       - `list_rollback_events(session_id: str, unconsumed_only: bool = False) -> List[Dict[str, Any]]`
       - `mark_rollback_events_consumed(event_ids: List[int], consumed_in_turn: str) -> int`
       - `deactivate_messages_after(session_id: str, after_message_id: Optional[int], reason: str) -> int`
       - **비공개 속성 직접 접근 3곳 (`checkpoint.py:495, 500, 505`)**:
         - 495줄 (`build_rollback_plan` 내 전체 세션 롤백): `self.db._conn.execute("SELECT count(*) FROM sessions WHERE parent_session_id = ?", (self.session_id,)).fetchone()`
         - 500줄 (`build_rollback_plan` 내 `turn_id` 지정 롤백): `self.db._conn.execute("SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ?", (self.session_id, float(earliest_cp["created_at"]))).fetchone()`
         - 505줄 (`build_rollback_plan` 내 `checkpoint_id` 지정 롤백): `self.db._conn.execute("SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ?", (self.session_id, float(target_cp["created_at"]))).fetchone()`
   - [`src/hermes_core/tools/delegate_tool.py:25`](../../../../src/hermes_core/tools/delegate_tool.py): `from hermes_core.session_db import SessionDB` (`DelegateTool.__init__(..., session_db: Optional[SessionDB] = None)`)
     - 현재 `delegate_tool.py`에서 호출하는 메서드: `create_session(...)`, `end_session(...)` (020에서 자식 실행이 `assembly.py`로 이동하기 전까지 010 시점에서도 `DelegateStorePort`가 이 2개 메서드를 포함하여 타입·동작 일관성을 유지한다).
   - [`src/hermes_core/tools/session_search_tool.py:12`](../../../../src/hermes_core/tools/session_search_tool.py): `from hermes_core.session_db import SessionDB` (`session_search_tool(..., db: Optional[SessionDB] = None)`, `_format_Duplicate_Session_Result(db: SessionDB, ...)`)
     - 호출 메서드: `search_messages(query=..., role_filter=..., session_id_exclude=..., limit=...)`, `get_session(session_id)`, `get_messages(session_id, limit=...)`
   - [`src/hermes_core/tools/dispatcher.py:28`](../../../../src/hermes_core/tools/dispatcher.py): `from hermes_core.session_db import SessionDB` (`ToolDispatcher.__init__(..., session_db: Optional[SessionDB] = None, ...)`, `create_default_dispatcher(..., session_db: Optional[SessionDB] = None, ...)`, `create_child_dispatcher(parent_dispatcher, session_db: Optional[SessionDB] = None, ...)`)
     - `ToolDispatcher` 자신은 `self.session_db`를 보관하여 `session_search_tool(..., db=self.session_db)`에 전달하고, `create_default_dispatcher`/`create_child_dispatcher`는 `DelegateTool(..., session_db=...)`에 전달한다.
2. **외부 테스트의 `hermes_core.tools.*` 경유 `SessionDB` 재수출 의존 여부**:
   - `grep` 확인 결과 `tests/` 전체에서 `from hermes_core.tools... import SessionDB`를 사용하는 곳은 **0건**이다.
   - 단, [`tests/test_architecture/test_dependency_rules.py:305-307`](../../../../tests/test_architecture/test_dependency_rules.py)의 `test_scenario_9_actual_tree_scanner_smoke`에서:
     ```python
     # delegate_tool은 session_db와 kernel.contracts를 참조한다
     del_targets = {e.target_module for e in by_src.get("hermes_core.tools.delegate_tool", [])}
     assert "hermes_core.session_db" in del_targets
     ```
     로 하드코딩되어 있으므로, 010에서 `delegate_tool.py`가 `hermes_core.session_db` 대신 `hermes_core.tools.ports`를 임포트하게 되면 이 스모크 단언도 `"hermes_core.tools.ports" in del_targets` 및 `"hermes_core.session_db" not in del_targets`로 함께 갱신해야 한다.

---

## 1. 구현 내용 명세

### 1.1 `src/hermes_core/tools/ports.py` 신설

외부 구체 인프라(`session_db.py`, `agent/*`)에 의존하지 않는 도구 계층 전용 포트 프로토콜 5종을 `@runtime_checkable` `Protocol`로 정의한다. `ports.py`는 표준 라이브러리(`typing`)만 임포트한다.

```python
"""도구 계층(tools/*)이 요구하는 외부 저장소 및 자식 실행기 포트(Protocol) 정의.

도구 계층 모듈은 구체 SQLite 구현체(SessionDB)나 상위 에이전트 구현체(HermesAgent)를
직접 import하지 않고 본 모듈의 프로토콜에 의존한다 (ADR-0020 D-02, D-04).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ApprovalStorePort(Protocol):
    """ApprovalGuard가 사용하는 승인 규칙 및 감사 기록 저장소 포트."""

    def record_approval_decision(
        self,
        *,
        session_id: Optional[str],
        tool_name: str,
        pattern: str,
        action: str,
        reason: str = "",
    ) -> int: ...

    def list_approval_rules(
        self,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]: ...

    def save_approval_rule(
        self,
        pattern: str,
        scope: str = "global",
        action: str = "allow_always",
        session_id: Optional[str] = None,
    ) -> int: ...

    def delete_approval_rule(
        self,
        pattern: str,
        scope: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int: ...


@runtime_checkable
class CheckpointStorePort(Protocol):
    """Checkpointer가 사용하는 파일 체크포인트·롤백 이벤트·메시지 비활성화 저장소 포트."""

    def insert_checkpoint(
        self,
        *,
        session_id: str,
        turn_id: str,
        seq: int,
        tool_name: str,
        target_path: str,
        existed_before: bool,
        backup_path: Optional[str],
        byte_size: int = 0,
        sha256_before: Optional[str] = None,
        mtime_before: Optional[float] = None,
        mode_before: Optional[int] = None,
        summary: Optional[str] = None,
        status: str = "active",
    ) -> int: ...

    def get_checkpoint(self, checkpoint_id: int) -> Optional[Dict[str, Any]]: ...

    def list_checkpoints(
        self,
        session_id: str,
        *,
        turn_id: Optional[str] = None,
        status: Optional[str] = None,
        include_restored: bool = True,
    ) -> List[Dict[str, Any]]: ...

    def update_checkpoint_status(
        self,
        checkpoint_id: int,
        status: str,
        *,
        restored_at: Optional[float] = None,
    ) -> bool: ...

    def insert_rollback_event(
        self,
        *,
        session_id: str,
        trigger_source: str,
        target_turn_id: Optional[str] = None,
        target_checkpoint_id: Optional[int] = None,
        restored_files_json: str = "[]",
        deactivated_messages: int = 0,
        conflict_skipped_json: str = "[]",
    ) -> int: ...

    def list_rollback_events(
        self,
        session_id: str,
        *,
        unconsumed_only: bool = False,
    ) -> List[Dict[str, Any]]: ...

    def mark_rollback_events_consumed(
        self,
        event_ids: List[int],
        consumed_in_turn: str,
    ) -> int: ...

    def deactivate_messages_after(
        self,
        session_id: str,
        after_message_id: Optional[int],
        reason: str = "rollback",
    ) -> int: ...

    def count_child_sessions(
        self,
        parent_session_id: str,
        *,
        started_since: Optional[float] = None,
    ) -> int: ...


@runtime_checkable
class DelegateStorePort(Protocol):
    """DelegateTool이 자식 세션 생성·마감 기록에 사용하는 저장소 포트."""

    def create_session(
        self,
        session_id: str,
        model: str,
        system_prompt: Optional[str] = None,
        parent_session_id: Optional[str] = None,
         config_json: Optional[str] = None,
    ) -> None: ...

    def end_session(self, session_id: str, end_reason: str = "completed") -> None: ...


@runtime_checkable
class SessionSearchPort(Protocol):
    """session_search_tool 및 ToolDispatcher가 사용하는 과거 세션 검색 포트."""

    def search_messages(
        self,
        query: str,
        role_filter: Optional[List[str]] = None,
        session_id_exclude: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]: ...

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]: ...

    def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]: ...


@runtime_checkable
class ChildRunnerPort(Protocol):
    """DelegateTool이 자식 에이전트 1건을 실행할 때 호출하는 콜백 포트 (020에서 배선)."""

    def __call__(
        self,
        *,
        goal: str,
        context: str = "",
        toolsets: Optional[List[str]] = None,
        model: Optional[str] = None,
        max_iterations: Optional[int] = None,
        parent_session_id: Optional[str] = None,
        child_dispatcher: Optional[Any] = None,
        task_index: int = 0,
    ) -> Dict[str, Any]: ...
```

*(주의: `DelegateStorePort.create_session`의 시그니처는 실제 [`SessionDB.create_session`](../../../../src/hermes_core/session_db.py) 정의와 정확히 일치시킨다. 착수 시 `session_db.py`의 `create_session` 인자 목록을 확인해 맞춘다.)*

### 1.2 `src/hermes_core/session_db.py`에 `count_child_sessions` 공개 메서드 추가 (D-03)

[`src/hermes_core/session_db.py`](../../../../src/hermes_core/session_db.py)의 `SessionDB` 클래스에 자식 세션 개수를 조회하는 공개 메서드를 추가한다:

```python
    def count_child_sessions(
        self,
        parent_session_id: str,
        *,
        started_since: Optional[float] = None,
    ) -> int:
        """주어진 부모 세션에서 파생된 자식 세션 수를 반환한다.

        Args:
            parent_session_id: 부모 세션 ID.
            started_since: 지정 시 started_at >= started_since 인 자식 세션만 집계한다.
        """
        if started_since is None:
            row = self._conn.execute(
                "SELECT count(*) FROM sessions WHERE parent_session_id = ?",
                (parent_session_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT count(*) FROM sessions WHERE parent_session_id = ? AND started_at >= ?",
                (parent_session_id, float(started_since)),
            ).fetchone()
        return int(row[0]) if row else 0
```

### 1.3 `src/hermes_core/tools/checkpoint.py` 수정 (D-02, D-03)

1. `from hermes_core.session_db import SessionDB`를 제거하고 `from hermes_core.tools.ports import CheckpointStorePort`로 교체한다.
2. `Checkpointer.__init__`의 `session_db: SessionDB` 타입 힌트를 `session_db: CheckpointStorePort`로 교체한다.
3. `build_rollback_plan()` 내부의 `self.db._conn.execute(...)` 3곳(495, 500, 505줄 부근)을 `self.db.count_child_sessions(...)` 호출로 교체한다:
   - 전체 세션 롤백(`target_turn_id is None and target_checkpoint_id is None`):
     ```python
     delegate_warning = self.db.count_child_sessions(self.session_id)
     ```
   - `turn_id` 지정 롤백:
     ```python
     if target_cps:
         earliest_cp = min(target_cps, key=lambda c: c["id"])
         delegate_warning = self.db.count_child_sessions(
             self.session_id,
             started_since=float(earliest_cp["created_at"]),
         )
     ```
   - `checkpoint_id` 지정 롤백:
     ```python
     delegate_warning = self.db.count_child_sessions(
         self.session_id,
         started_since=float(target_cp["created_at"]),
     )
     ```
   - 기존 `try ... except Exception: delegate_warning = 0` 방어 블록은 그대로 유지하여, 테스트 스텁이나 예외 상황에서도 안전하게 `0`으로 폴백하도록 보존한다.

### 1.4 나머지 `src/hermes_core/tools/*` 4개 모듈의 `SessionDB` 임포트 교체 (D-02)

1. [`src/hermes_core/tools/approval.py`](../../../../src/hermes_core/tools/approval.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import ApprovalStorePort` 임포트.
   - `ApprovalGuard.__init__`의 `session_db: Optional[SessionDB] = None` → `session_db: Optional[ApprovalStorePort] = None`.
2. [`src/hermes_core/tools/session_search_tool.py`](../../../../src/hermes_core/tools/session_search_tool.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import SessionSearchPort` 임포트.
   - `_format_Duplicate_Session_Result(db: SessionSearchPort, ...)` 및 `session_search_tool(..., db: Optional[SessionSearchPort] = None)` 타입 힌트 교체.
3. [`src/hermes_core/tools/delegate_tool.py`](../../../../src/hermes_core/tools/delegate_tool.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import DelegateStorePort` 임포트.
   - `DelegateTool.__init__`의 `session_db: Optional[SessionDB] = None` → `session_db: Optional[DelegateStorePort] = None`.
4. [`src/hermes_core/tools/dispatcher.py`](../../../../src/hermes_core/tools/dispatcher.py):
   - `from hermes_core.session_db import SessionDB` 제거 → `from hermes_core.tools.ports import DelegateStorePort, SessionSearchPort` (또는 ` Union[SessionSearchPort, DelegateStorePort, Any]`, 구체적으로는 `ToolDispatcher.__init__`에서 `session_db: Optional[SessionSearchPort] = None`, `create_default_dispatcher`/`create_child_dispatcher`에서도 `session_db: Optional[ Any ] = None` 또는 포트 조합). 가장 깔끔하고 명확하게 `from hermes_core.tools.ports import DelegateStorePort, SessionSearchPort`를 임포트하고 `ToolDispatcher.__init__`에서는 `session_db: Optional[SessionSearchPort] = None`으로 선언한다.

### 1.5 아키텍처 규칙 `R7` 및 포트 적합성 테스트 추가

1. [`tests/test_architecture/test_dependency_rules.py`](../../../../tests/test_architecture/test_dependency_rules.py):
   - 금지 의존 규칙 테이블(`FORBIDDEN_RULES` 등 기존 R1~R6 정의부)에 **`R7` 규칙**을 추가한다:
     - `rule_id="R7"`: 출발지 `hermes_core.tools` (`src/hermes_core/tools/*.py`) → 금지 대상 `hermes_core.session_db`.
   - `test_scenario_9_actual_tree_scanner_smoke` 내 `del_targets` 단언(305~307줄)에서:
     - 기존 `assert "hermes_core.session_db" in del_targets`를 `assert "hermes_core.tools.ports" in del_targets` 및 `assert "hermes_core.session_db" not in del_targets`로 갱신한다.
   - `R7` 규칙에 대한 단위·실제 트리 검증 테스트 함수를 추가한다:
     - 합성 위반 픽스처(`hermes_core.tools.foo -> hermes_core.session_db`)가 `R7` 위반으로 정확히 탐지되는지 검증.
     - 실제 `src/hermes_core/tools/` 전체 파일에서 `hermes_core.session_db` 참조가 0건이고 `. _conn` 직접 접근도 0건인지 검증.
2. [`tests/test_architecture/test_ports_conformance.py`](../../../../tests/test_architecture/test_ports_conformance.py):
   - `SessionDB` 인스턴스(`tmp_path` 기반)가 `ApprovalStorePort`, `CheckpointStorePort`, `DelegateStorePort`, `SessionSearchPort` 4개 `@runtime_checkable` 프로토콜에 대해 모두 `isinstance(db, ...)`가 `True`이고 필수 메서드 시그니처가 일치하는지 검증하는 테스트를 추가한다.
   - `SessionDB.count_child_sessions(parent_id, started_since=...)`가 실제 부모·자식 세션 레코드에 대해 정확한 개수를 반환하는지 검증하는 테스트를 추가한다.

---

## 2. 인수 테스트 시나리오 (입력 → 기대 결과)

| # | 시나리오 | 입력 / 조건 | 기대 결과 |
|---|---|---|---|
| 1 | **`SessionDB`의 도구 포트 4종 적합성** | `db = SessionDB(tmp_path / "test.db")` 생성 후 `ApprovalStorePort`, `CheckpointStorePort`, `DelegateStorePort`, `SessionSearchPort`에 대해 `isinstance(db, Port)` 검사 | 4개 포트 모두 `isinstance(db, Port) is True`이며, 각 포트가 요구하는 모든 메서드가 `callable`로 존재한다 |
| 2 | **`SessionDB.count_child_sessions` 전체 집계** | 부모 세션 `"p1"` 생성 후 자식 세션 `"c1"`, `"c2"` (`parent_session_id="p1"`) 및 무관한 세션 `"other"` 생성 | `db.count_child_sessions("p1") == 2`, `db.count_child_sessions("nonexistent") == 0` |
| 3 | **`SessionDB.count_child_sessions` 시점 필터(`started_since`)** | 부모 `"p1"` 아래 자식 `"c1"` 생성 후 시각 `t_mid`를 기록하고, 이후 자식 `"c2"` 생성 | `db.count_child_sessions("p1", started_since=t_mid) == 1`, `db.count_child_sessions("p1", started_since=t_mid + 1000) == 0` |
| 4 | **`Checkpointer.build_rollback_plan`의 `count_child_sessions` 연동** | 체크포인트 생성 후 자식 세션을 1개 추가하고 `checkpointer.build_rollback_plan()` 호출 | `plan.delegate_sessions_warning == 1`이 반환되며, `checkpoint.py` 내부에서 `_conn` 속성에 접근하지 않는다 |
| 5 | **순수 인메모리 Fake 포트로 `ApprovalGuard` 구동** | `SessionDB`를 상속하지 않는 순수 인메모리 `FakeApprovalStore` 클래스를 만들어 `ApprovalGuard(session_db=fake_store)`에 주입 후 `allow_always` 결정 기록 | SQLite 파일 생성 없이 `fake_store`에 승인 감사 로그와 영구 규칙이 정상 기록·조회된다 |
| 6 | **순수 인메모리 Fake 포트로 `session_search_tool` 구동** | `SessionDB`를 상속하지 않는 `FakeSearchStore`를 `session_search_tool(query="hello", db=fake_store)`에 전달 | `success is True` 및 포맷팅된 세션 검색 결과가 정상 반환된다 |
| 7 | **아키텍처 규칙 `R7` 실제 트리 검사** | `src/hermes_core/tools/` 하위 모든 `.py` 파일의 AST import 스캔 | `hermes_core.session_db`를 import하는 간선이 **0건**이다 |
| 8 | **아키텍처 규칙 `R7` 합성 위반 탐지** | 가짜 간선 `ImportEdge(source_module="hermes_core.tools.checkpoint", target_module="hermes_core.session_db", lineno=10)`를 규칙 검사기에 입력 | `rule_id == "R7"` 위반이 정확히 검출된다 |
| 9 | **`_conn` 비공개 속성 외부 접근 금지 검사** | `src/hermes_core/` 전체(`.py`) 중 `session_db.py`를 제외한 모든 파일에서 `._conn` 속성 접근 AST/텍스트 검사 | `session_db.py` 외 `._conn` 참조가 **0건**이다 |
| 10 | **기존 전체 회귀 시험 통과** | `pytest -m "not pty"` 및 `pytest -m pty` 실행 | 기존 1,148개 테스트 + 신규 테스트 전수 통과, `src/hermes_core/kernel/` diff 0줄 |

---

## 3. 검증 명령

```bash
# 1. 아키텍처 테스트 (R1~R7 및 포트 적합성)
./.venv/bin/pytest tests/test_architecture/ -v

# 2. 체크포인트·승인·세션검색 관련 단위/통합 테스트
./.venv/bin/pytest tests/test_hermes_core/test_checkpoint_module.py tests/test_hermes_core/test_file_tools_checkpoint.py tests/test_hermes_core/test_approval_guard.py tests/test_hermes_core/test_session_search.py -v

# 3. 커널 무접촉 확인
git diff src/hermes_core/kernel/

# 4. 전체 스위트 (not pty + pty)
./.venv/bin/pytest -m "not pty" -q
./.venv/bin/pytest -m pty -q
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
  - `docs/로드맵/20-의존성-역전-도메인-분리-완결-MVP/instructions/020_...md` (010 완료 시 다음 단계 020 지시서를 함께 작성해 `Draft`로 올림 — `ROADMAP_정책.md §4-3`)
- **커밋 메시지 형식**:
  - `feat(tools): R20-010 - 도구 계층 포트(tools/ports.py) 신설 및 SessionDB 의존 역전(R7)`

---

## 5. 다음 단계(020)로 넘기는 사실

- `src/hermes_core/tools/ports.py`에 `ChildRunnerPort`와 `DelegateStorePort`가 준비되어 있으므로, 020에서는 `assembly.py`에 `run_child_session()`(`try...finally`로 자식 세션 `end_session` 보장, R-2)을 구현하고 `DelegateTool`에 `child_runner: Optional[ChildRunnerPort] = None`을 주입하여 `delegate_tool.py`의 `HermesAgent`·`build_child_system_message` 직접 임포트를 제거하고 `KNOWN_DEBT = {}`를 달성한다.

---

## 6. ROADMAP과 다른 점

- 없음 (`ROADMAP.md §6 · 010` 명세와 일치).
