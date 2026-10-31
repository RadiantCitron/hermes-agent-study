# [010] 원장 순수 도메인 분리와 SessionDB 슬림화

- **로드맵**: [21-원장-파일-도메인-분리-및-실행환경-포트-완결-MVP](../README.md) ([ROADMAP.md §6 · 010](../ROADMAP.md), [ADR-0021](../../../adr/0021-원장_파일_도메인_분리_및_실행환경_포트_완결_MVP_범위와_결정.md) D-01)
- **선행 단계**: 없음 (코드 기준선 `281d76f`, `not pty` 1,162 / `pty` 11 / `test_architecture` 37)
- **성격**: 코드 구현 (`src/hermes_core/ledger_domain.py` 신설, `session_db.py` 위임 슬림화, 단위 시험 추가)
- **실제 API 키**: 불필요

---

## 0. 착수 전 점검 (지시서 작성 시점에 완료된 사실)

`ROADMAP.md §6 (010)` 및 `§4` 사전 점검 항목을 실제 코드와 대조한 결과는 다음과 같다.

1. **`session_db.py` 실측 규모**:
   - `wc -l src/hermes_core/session_db.py` 결과: **정확히 1,001줄**.
   - 레포지토리 소스 트리(`src/`) 내에서 유일하게 1,000줄을 초과하는 거대 모듈임.
2. **`session_db.py` 내 순수 도메인 로직과 I/O 혼재 현황**:
   - DDL 및 스키마 버전 상수 (36~162줄, 약 127줄)
   - 입력 유효성 검증 로직 (승인 argv, 체크포인트 파라미터, 감사 로그 필드 등 곳곳에 분산)
   - 상태 전이 유효성 검증 (`CHECKPOINT_TRANSITIONS`, `APPROVAL_FINAL_DECISIONS`)
   - 쿼리 및 파라미터 조립 (FTS5 MATCH 쿼리, `update_file_checkpoint` 동적 SQL)
   - 데이터 가공 (`extract_tool_name_map`, hex SHA-256 검증 `_is_valid_hex_sha256`)
3. **불변식 제약 조건 확인**:
   - `tests/test_architecture/test_dependency_rules.py`의 `test_scenario_r20_no_private_conn_outside_session_db`는 `session_db.py` 외 파일에서 `._conn` 속성 참조를 엄격히 금지함.
   - 따라서 `ledger_domain.py`에는 `sqlite3.Connection`이나 커서, `_conn` 객체를 일체 전달하거나 접근하지 않아야 하며, 순수 인메모리 함수로만 구성해야 함.

---

## 1. 구현 내용 명세

### 1.1 `src/hermes_core/ledger_domain.py` 신설

`sqlite3`나 디스크 I/O 없이 순수 인메모리 검증, 문자열/자료구조 변환, SQL 상수를 담당하는 순수 도메인 모듈을 신설한다. 표준 라이브러리(`typing`, `json` 등)와 내부 상수(`hermes_core.constants`)만 임포트한다.

#### 1. 상수 정의
- `SCHEMA_VERSION = 7`
- `SCHEMA_SQL`: 기존 6개 테이블 및 8개 인덱스 DDL 문자열.
- `FTS_SQL`: 기존 FTS5 가상 테이블 및 트리거 3개 DDL 문자열.

#### 2. 순수 검증 함수
- `validate_approval_request_argv(argv: Any) -> list[str]`:
  - `argv`가 비어있지 않은 문자열 리스트인지 검증. 실패 시 `ValueError("argv must be a non-empty list of strings")`.
- `validate_approval_decision(decision: str) -> None`:
  - `decision`이 `APPROVAL_FINAL_DECISIONS`에 속하는지 검증. 실패 시 `ValueError`.
- `validate_file_checkpoint_input(session_id: str, turn_index: int, tool_name: str, workspace: str, rel_path: str, before_exists: bool, before_content: Optional[str]) -> None`:
  - `turn_index >= 1`, `tool_name in CHECKPOINT_TOOL_NAMES`, 비어있지 않은 문자열, `before_exists` 불리언 타입 및 `before_content` 존재 조건, `FILE_MAX_BYTES` 초과 검증. 실패 시 기존과 동일한 `ValueError` 메시지 발생.
- `validate_checkpoint_transition(status: str, after_sha256: Optional[str]) -> tuple[str, ...]`
  - `status in CHECKPOINT_TRANSITIONS` 검증.
  - `status == CHECKPOINT_STATUS_APPLIED`일 때 `after_sha256` 64자 소문자 16진수 검증.
  - 그 외 상태일 때 `after_sha256 is not None`이면 예외 발생.
  - 허용되는 이전 상태 튜플(`CHECKPOINT_TRANSITIONS[status]`)을 반환.
- `is_valid_hex_sha256(value: Any) -> bool`:
  - 소문자 16진수 64자 SHA-256 검증.
- `validate_control_audit_input(session_id: str, source: str, command: str, target: str, result: str, old_value: Optional[str] = None, new_value: Optional[str] = None, reason: Optional[str] = None) -> None`:
  - 제어 감사 입력값 타입 및 허용 명령어(`set_setting`, `enable_extension`, `disable_extension`), 결과값(`accepted`, `rejected`, `applied`) 검증. 실패 시 `ValueError`.
- `validate_deactivate_after_id(after_id: Any) -> None`:
  - `after_id`가 음수가 아닌 정수인지 검증. 실패 시 `ValueError("after_id must be a non-negative integer")`.

#### 3. 순수 변환 및 쿼리 빌더 함수
- `extract_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]`:
  - `assistant` 메시지의 `tool_calls`에서 `id -> name` 맵핑을 순수하게 추출하여 반환.
- `build_fts_search_query(query: str, source_filter: Optional[list[str]] = None, role_filter: Optional[list[str]] = None, limit: int = 20, offset: int = 0) -> tuple[str, list[Any]]`:
  - FTS5 매칭 SQL 문자열과 파라미터 리스트를 순수하게 조립하여 반환. 빈 쿼리 처리 및 기본 소스 필터 적용.
- `build_checkpoint_update_query(checkpoint_id: int, status: str, now: float, reason: Optional[str], after_sha256: Optional[str]) -> tuple[str, tuple[Any, ...]]`:
  - 상태 전이에 따른 `UPDATE file_checkpoints` SQL 문과 바인딩 파라미터 튜플을 조립하여 반환.

---

### 1.2 `src/hermes_core/session_db.py` 변경 (슬림화)

1. `ledger_domain`에서 상수 및 검증/조립 함수를 임포트한다.
2. 기존 `SessionDB` 메서드 내부의 장황한 유효성 검사 및 쿼리 빌딩 코드를 `ledger_domain` 함수 호출로 대체한다:
   - `_init_schema`: `SCHEMA_SQL`, `SCHEMA_VERSION`, `FTS_SQL`을 `ledger_domain` 상수로 참조.
   - `record_approval_request`: `validate_approval_request_argv(argv)` 호출.
   - `resolve_approval_request`: `validate_approval_decision(decision)` 호출.
   - `record_file_checkpoint`: `validate_file_checkpoint_input(...)` 호출.
   - `update_file_checkpoint`: `build_checkpoint_update_query(...)` 호출 및 커넥션 실행.
   - `record_control_audit`: `validate_control_audit_input(...)` 호출.
   - `deactivate_messages_after`: `validate_deactivate_after_id(after_id)` 호출.
   - `archive_and_compact`: `extract_tool_name_map(new_messages)` 호출.
   - `search_messages`: `build_fts_search_query(...)` 호출 후 `self._conn.execute(sql, params)` 실행.
3. 기존 메서드 시그니처, 반환형, 동작, 예외는 100% 동일하게 유지한다.
4. 리팩토링 후 `session_db.py` 줄 수를 600줄 내외로 슬림화한다 (목표: < 650줄, 필수: < 800줄).

---

### 1.3 `tests/test_hermes_core/test_ledger_domain.py` 신설

순수 도메인 모듈 `ledger_domain.py`를 DB 없이 인메모리로 고속 검증하는 전용 단위 테스트를 작성한다.

---

## 2. 인수 테스트 시나리오 (동어반복 방지 선기술)

구현 전에 독립적으로 도출한 인수 테스트 시나리오는 다음과 같다:

1. **시나리오 1 (`test_scenario_r21_ledger_domain_approval_validation`)**:
   - 입력: `argv = []`, `argv = ["ls", 123]`, `argv = "ls -la"`
   - 기대 결과: 모두 `ValueError` 발생 ("argv must be a non-empty list of strings"). 유효한 `["ls", "-la"]` 입력 시 정상 통과.
2. **시나리오 2 (`test_scenario_r21_ledger_domain_checkpoint_input_validation`)**:
   - 입력: `turn_index = 0` (1 미만), `tool_name = "invalid_tool"`, `before_exists=True`인데 `before_content=None`, `before_exists=False`인데 `before_content="foo"`
   - 기대 결과: 각각 명시된 `ValueError` 발생.
3. **시나리오 3 (`test_scenario_r21_ledger_domain_checkpoint_transition_validation`)**:
   - 입력: `status = "applied"`인데 `after_sha256 = "NOT_HEX_64"`
   - 기대 결과: `ValueError` 발생 ("after_sha256 must be a 64-character lowercase hex string...").
   - 입력: `status = "failed"`인데 `after_sha256 = "a" * 64`
   - 기대 결과: `ValueError` 발생 ("after_sha256 can only be set when status is 'applied'...").
4. **시나리오 4 (`test_scenario_r21_ledger_domain_fts_query_builder`)**:
   - 입력: `query = "error log"`, `source_filter = ["cli", "telegram"]`, `role_filter = ["user"]`, `limit = 10`, `offset = 5`
   - 기대 결과: 반환된 SQL에 `messages_fts MATCH ?`, `m.is_copy = 0`, `s.source IN (?,?)`, `m.role IN (?)`, `LIMIT ? OFFSET ?` 포함 및 파라미터 튜플 정합성 확인.
   - 입력: `query = "   "` (공백)
   - 기대 결과: 빈 쿼리 시 SQL `""`, params `[]` 반환.
5. **시나리오 5 (`test_scenario_r21_ledger_domain_tool_name_map_extraction`)**:
   - 입력: `[{"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "read_file"}}, {"id": "call_2", "name": "write_file"}]}]`
   - 기대 결과: `{"call_1": "read_file", "call_2": "write_file"}` 반환.
6. **시나리오 6 (`test_scenario_r21_ledger_domain_control_audit_validation`)**:
   - 입력: `command = "invalid_cmd"`, `result = "invalid_res"`
   - 기대 결과: 각각 `ValueError` 발생.
7. **시나리오 7 (`test_scenario_r21_session_db_regression_and_line_count`)**:
   - 검증: `session_db.py` 라인 수가 800줄 미만(권장 < 650줄)인지 확인.
   - 검증: 기존 `SessionDB`를 사용한 메시지 저장, 조회, FTS 검색, 체크포인트 기록/갱신, 승인 기록이 온전히 동작하는지 확인.
8. **시나리오 8 (`test_scenario_r21_no_conn_in_ledger_domain`)**:
   - 검증: `ledger_domain.py` AST를 검사하여 `_conn`, `sqlite3` 호출 및 임포트가 0건인지 확인.

---

## 3. 파급 범위 및 하위 호환성 검증

- **`SessionDB` 인터페이스 100% 하위 호환**:
  - 공개 메서드 시그니처, 매개변수 이름, 예외 타입, 예외 메시지가 온전히 유지된다.
  - 외부에서 `SessionDB.SCHEMA_VERSION`, `SCHEMA_SQL`, `FTS_SQL`을 참조하더라도 `session_db.py`에서 `from hermes_core.ledger_domain import SCHEMA_VERSION, SCHEMA_SQL, FTS_SQL`을 재수출하므로 외부 import가 전혀 깨지지 않는다.
- **기존 테스트 1,173개 무수정 통과**:
  - `tests/test_hermes_core/test_session_db.py`, `test_checkpoint.py`, `test_approval.py`, `test_fts.py` 등 기존 테스트 코드를 단 한 줄도 수정하지 않는다.

---

## 4. 완료 기준 및 셀프 체크리스트

- [ ] `src/hermes_core/ledger_domain.py` 신규 생성 완료
- [ ] `src/hermes_core/session_db.py` 리팩토링 및 800줄 이하 달성
- [ ] `tests/test_hermes_core/test_ledger_domain.py` 신규 작성 및 8개 시나리오 통과
- [ ] `tests/test_architecture/test_dependency_rules.py`의 `test_scenario_r20_no_private_conn_outside_session_db` 통과
- [ ] 기존 1,173개 회귀 시험 전수 통과 (`pytest -m "not pty"`)
- [ ] 1단계 1커밋 완료 (`feat(core): R21-010 - 원장 순수 도메인 분리와 SessionDB 슬림화`)
