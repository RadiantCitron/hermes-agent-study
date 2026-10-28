# [구현지시서 050] 에이전트 연동 · 스키마 v4 · in-place 기록

> **상위 대시보드**: [05-README.md](../README.md)  
> **선행 조건**: 040 단계 완료 (`Done`) **그리고 아래 「착수 전 결정 항목」의 사용자 확정**  
> **근거**: [ROADMAP.md](../ROADMAP.md) §6-5, §4 위험 2·6·7, [ADR-0005](../../../adr/0005-컨텍스트_압축_MVP_범위와_세션_연결_방식.md) §2-2, [WHY.md](../WHY.md) 가정 A·F·G·H

---

## 0. 착수 전 결정 항목 (미확정 블로커 — 사용자 확정 필요)

이 단계는 설계 결정 하나가 열려 있다. 착수 전 3대 점검의 ② 「미확정 블로커 없음」을 만족하려면 아래를 사용자와 확정하고 이 절 끝에 결정을 기록해야 한다. 확정 전에는 착수하지 않고 질문한다.

**보관 단위**: 압축할 때 DB의 활성 행을 어떻게 다룰 것인가.

| 안 | 동작 | 장점 | 단점 |
|---|---|---|---|
| 가 | 원본 방식: 현재 활성 행(system 제외)을 **전부** `compacted`로 보관하고, 압축본(헤드·요약·테일)을 새 활성 행으로 **복제 삽입** | 원본과 같은 구조, 활성 대화 복원이 단순(활성 행 시간순) | 헤드·테일 내용이 보관 행과 활성 행에 두 번 존재 → `session_search` 중복. 복제 행을 검색에서 제외할 표지(`origin_id` 등)가 필요 |
| 나 | 대체된 **중간 행만** `compacted`로 표시하고 헤드·테일 행은 제자리 유지. 요약 행을 삽입하되 시간순 복원에서 중간 자리에 오도록 정렬 방법이 필요 | 복제 없음, 검색 중복 없음 | 요약 행의 정렬 위치를 정하는 추가 열/규칙(`sort_key` 등)이 필요하고 복원 쿼리가 복잡해짐 |

(에이전트의 의견: 학습 목적상 원본 구조를 따르는 **가**를 기본으로 보되, 검색 중복 처리 비용이 얼마인지 `search_messages`·`session_search_tool`을 읽고 착수 시 구체화한다. 최종 결정은 사용자.)

**기타 점검(결정은 아니고 확인할 것)**: `message_count`/`tool_call_count`는 `append_message`가 올리는 누적값이다. 압축 후에는 삽입 행도 `append_message`를 거치면 값이 부풀 수 있으므로, 삽입 방식(직접 INSERT vs `append_message`)과 카운터 의미를 착수 시 확인해 시나리오 9에 맞춘다.

**결정 기록**: (미확정)

---

## 1. 구현 내용 명세

1. **스키마 v4** ([`session_db.py`](../../../../src/hermes_core/session_db.py)):
   - `SCHEMA_VERSION = 4`. `messages`에 `active INTEGER DEFAULT 1`, `compacted INTEGER DEFAULT 0`를 `SCHEMA_SQL`의 `CREATE TABLE`에 추가한다(새 DB). 기존 DB는 `if current_version < 4` 블록에서 `ALTER TABLE ... ADD COLUMN`(이미 있으면 무시)하고 **그 블록에서** `schema_version`을 4로 올린다. 기존 행은 `active=1, compacted=0`으로 간주된다.
   - 결정 항목에 따라 필요한 추가 열(예: 복제 표지)은 같은 마이그레이션에 넣는다.
   - `get_messages_as_conversation(session_id)`: 활성 행(`active = 1`)만 시간순. `get_messages(session_id)`: 보관 행 포함 전체 원장(열 `active`/`compacted` 포함).
   - `archive_and_compact(session_id, ...)`: **단일 트랜잭션**에서 결정 항목대로 활성 행을 보관 표시하고 압축본을 삽입. 중간에 실패하면 전부 롤백하고 예외를 올린다. 검색(`search_messages`)의 대상 규칙은 결정 항목대로 확정한다.
2. **에이전트** ([`agent.py`](../../../../src/hermes_core/agent/agent.py)):
   - 생성자에 `context_window_tokens: int = CONTEXT_WINDOW_TOKENS`, `compression_threshold_ratio: float = COMPRESSION_THRESHOLD_RATIO`, `compression_enabled: bool = True`를 추가(기존 인자 순서·의미 불변).
   - 상태: `_previous_summary`, `_last_input_tokens`, `_last_message_count`.
   - `run_conversation` 루프에서 `llm_client.chat_completion` 호출 **직전**에 한 번 판정: `compression_enabled`이고 한 번의 `run_conversation`당 압축 횟수 < `COMPRESSION_MAX_PER_RUN`이며 `should_compress(estimate_current_tokens(...), threshold)`이면 `compress_messages`를 호출한다. 결과가 있으면 `messages[:] = 결과.messages`, `session_db.archive_and_compact`로 기록, `_previous_summary` 갱신, `_last_input_tokens = None`, 요약 호출 `usage`를 `update_token_counts`로 누적. 결과가 `None`(거부·짧음)이면 그대로 진행. DB 기록이 실패하면 경고 로그를 남기고 **메모리의 messages는 바꾸지 않는다**(압축 포기, 대화 계속).
   - 모델 응답의 `usage.input_tokens`와 그때의 `len(messages)`를 `_last_input_tokens`/`_last_message_count`에 보관.
   - 시스템 프롬프트(`messages[0]`)와 `_cached_system_prompt`는 건드리지 않는다.
3. **CLI** ([`cli.py`](../../../../src/hermes_core/cli.py)): `--context-window <정수>` 옵션(기본 상수)을 추가하고 에이전트에 전달.
4. **작동원리 갱신**: 없음 (060에서 일괄 반영).

---

## 2. 변경할 파일 목록

- `src/hermes_core/session_db.py`, `src/hermes_core/agent/agent.py`, `src/hermes_core/cli.py`, `src/hermes_core/constants.py`(`COMPRESSION_MAX_PER_RUN` 추가) (수정)
- `tests/test_hermes_core/test_session_db.py` (시나리오 추가), `tests/test_hermes_core/test_compression_agent.py` (신규), `tests/test_hermes_core/test_cli.py` (옵션 시나리오 추가)
- `docs/로드맵/05-컨텍스트-압축-MVP/README.md`, `docs/인계.md` (갱신)

> **사전 점검 (착수 시)**: ① 기존 시험이 `messages` 열 개수·`schema_version == 3`에 의존하는지 `grep`. 의존하면 임의로 고치지 않고 멈춰서 보고한다. ② `session_search_tool.py`와 `search_messages`가 보관/활성 행을 어떻게 다뤄야 하는지 결정 항목과 대조. ③ 마이그레이션이 블록마다 `UPDATE schema_version`을 직접 하는 현재 방식 확인(v4 블록 누락 시 버전이 3에 머무는 결함 방지).

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지 — 결정 항목 확정 후 DB 관련 행의 세부만 구체화 가능)

시험용 합성 대화: 임계값을 낮춘 에이전트(`context_window_tokens`를 작게)와 큐를 채운 `MockLLMClient`(요약 응답 포함). DB는 `:memory:` 또는 `tmp_path`.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | v3 DB(파일, 세션 1·메시지 2)를 `SessionDB(path)`로 열기 | 오류 없이 열림, `schema_version == 4`, 기존 메시지 `active=1, compacted=0`, 조회 결과 불변 |
| 2 | 새 DB | `schema_version == 4`, `messages`에 `active`·`compacted` 열 존재. v3에서 마이그레이션한 DB와 열 구성 일치 |
| 3 | 마이그레이션을 두 번 실행(멱등) | 오류 없음, 버전 4 유지 |
| 4 | `archive_and_compact` 정상 호출 | 활성 행 = 압축본(결정 항목 규칙대로), 대체된 원문은 `compacted=1, active=0`로 `get_messages`에 남음 |
| 5 | `get_messages_as_conversation` | 활성 행만 시간순, 보관 행 제외 |
| 6 | `archive_and_compact` 중간 예외(삽입 단계에서 실패하도록 유도) | 롤백되어 활성/보관 상태가 호출 전과 동일 |
| 7 | 임계값 미만의 짧은 대화 | 압축 발동 없음, 모델 호출 수 불변, `compacted` 행 0 |
| 8 | 임계값을 넘는 긴 대화(도구 호출 포함) 한 턴 | 압축 발동, 모델 호출 순서: 요약 호출 → 본 호출, `result["messages"]`의 첫 요소 `system` 내용이 압축 전과 바이트 동일, 요약 메시지 1개 |
| 9 | 8번 이후 세션 | `session_id` 불변, `message_count`/`tool_call_count`는 결정 항목에서 정한 의미와 일치 |
| 10 | 8번의 `llm_calls` | 요약 호출이 1행으로 기록됨(도구 없음), 본 호출도 기록됨 |
| 11 | 요약 모델 응답에서 `LLMError` | 정적 대체 요약으로 압축, `stopped_reason == "completed"`, 대화 계속 |
| 12 | 압축 후 또 길어져 같은 `run_conversation`에서 재압축 | `_previous_summary`가 프롬프트에 포함, 요약 메시지는 여전히 1개, `COMPRESSION_MAX_PER_RUN` 초과 시 더는 압축하지 않음 |
| 13 | 요약이 원본보다 커서 거부 | 압축 없음, DB 불변, 대화 계속 |
| 14 | DB 기록 실패(`archive_and_compact`가 예외) | 경고 로그, 메모리 `messages` 불변, 대화 계속, 예외가 밖으로 나오지 않음 |
| 15 | `compression_enabled=False` | 어떤 길이에도 압축 없음 |
| 16 | `MockLLMClient`(usage 없음) 대화 | 거친 추정으로 트리거가 판정됨. 실제 usage 있는 가짜 모델에서는 `input_tokens + 신규 추정`이 쓰임 |
| 17 | 압축 후 시스템 프롬프트 캐시 | `_cached_system_prompt` 불변, `sessions.system_prompt` 불변 |
| 18 | 압축 후 보관 원문 검색 | 결정 항목의 검색 규칙대로 `session_search`가 보관된 중간 원문을 찾음(중복 처리 포함) |
| 19 | CLI `--context-window 2000` | 에이전트가 해당 창 크기로 생성되고 임계값이 `1000`으로 계산됨 |
| 20 | 기존 코어 테스트 | **수정 없이** 통과(압축은 기본 창에서 발동하지 않음) |

---

## 4. 검증 기준 (Done 조건)

1. 시나리오 1~19가 통과한다.
2. 기존 테스트가 모두 통과한다. `git diff <단계 시작 커밋>..HEAD -- tests/`에서 **삭제된 줄이 0**이다.
3. `git diff <단계 시작 커밋>..HEAD -- src/hermes_core/tools src/hermes_core/llm_client.py src/hermes_core/agent/prompt_builder.py`가 비어 있다.
4. `grep -rn "SCHEMA_VERSION = " src/hermes_core`의 정의가 한 곳뿐이다.
