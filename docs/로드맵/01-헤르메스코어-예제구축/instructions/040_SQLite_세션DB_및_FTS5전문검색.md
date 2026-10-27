# [구현지시서 040] SQLite 세션 DB 및 FTS5 전문 검색

> **상위 대시보드**: [01-README.md](file:///Users/<user>/Programming/hermes-agent-study/docs/로드맵/01-헤르메스코어-예제구축/README.md)  
> **선행 조건**: 030 단계 완료 (`Done`)  
> **기준 레퍼런스**: `hermes-agent` commit `6134939882`의 [`hermes_state.py`](file:///Users/<user>/Programming/hermes-agent-study/hermes-agent/hermes_state.py) 및 `tools/session_search_tool.py`

---

## 1. 구현 내용 명세

1. **SQLite SessionDB 모듈 (`src/hermes_core/hermes_state.py`)**:
   - 원본 `hermes_state.py`의 `SessionDB` 클래스 1:1 미러링
   - `sessions` 테이블: `id` (session_id), `created_at`, `model`, `parent_session_id`, `status`
   - `messages` 테이블: 대화 턴 히스토리 (`session_id`, `role`, `content`, `tool_calls`, `tool_call_id`)
   - `messages_fts` 가상 테이블: SQLite **FTS5**를 활용한 전체 대화 내용 및 도구 호출 텍스트 전문 검색 인덱스
2. **세션 수명주기 및 검색 메서드**:
   - `create_session(session_id, source="cli", model=None, parent_session_id=None)`: 세션 시작
   - `end_session(session_id, end_reason="completed")`: 세션 종료
   - `append_message(session_id, role, content, tool_calls=None, tool_call_id=None)`: 메시지 원장 추가 및 FTS5 동기화
   - `search_sessions(query, limit=20)`: FTS5 MATCH 쿼리를 통한 이전 대화 세션 고속 검색
3. **단위 테스트 작성**:
   - `tests/test_hermes_core/test_hermes_state.py`:
     - 테이블 자동 생성 및 세션 라이프사이클 검증
     - 유저 메시지 및 툴 호출 결과 저장 정합성 검증
     - FTS5 키워드 전문 검색 및 랭킹 조회 검증

---

## 2. 변경할 파일 목록

- `src/hermes_core/hermes_state.py` (신규)
- `tests/test_hermes_core/test_hermes_state.py` (신규)

---

## 3. 검증 기준 (Done 조건)

1. `pytest tests/test_hermes_core/test_hermes_state.py` 100% 통과.
2. 특정 키워드(예: "PostgreSQL", "Docker")가 포함된 이전 대화 턴을 FTS5 `search_sessions()`로 검색 시 해당 세션 ID와 매칭된 스니펫이 정상 반환됨을 단언.
3. In-memory SQLite (`:memory:`) 및 파일 기반 SQLite 모드 모두에서 정상 구동됨을 단언.
