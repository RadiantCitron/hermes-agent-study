# [구현지시서 050] 에이전트 연동 · 스키마 v4 · in-place 기록

> **상위 대시보드**: [05-README.md](../README.md)  
> **선행 조건**: 040 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §6-5, §4 위험 2·6·7·12, [ADR-0005](../../../adr/0005-컨텍스트_압축_MVP_범위와_세션_연결_방식.md) §2-2, [WHY.md](../WHY.md) 가정 A·F·G·H

---

## 0. 결정 기록: 보관 단위 = **가(원본 방식)**

사용자가 「판단 필요한 건 제안대로」로 위임하여 에이전트의 제안(2026-10-28)대로 확정했다. 근거: 원본 구조(`archive_and_compact`)를 그대로 익히는 것이 학습 목적에 맞고, 활성 대화 복원이 「활성 행 시간순」 한 줄로 끝난다. 검색 중복 문제는 아래 `is_copy` 열로 푼다.

| 항목 | 확정 내용 |
|---|---|
| 보관 | 압축 시점에 `active=1`인 세션의 모든 행(system은 DB에 없음)을 `active=0, compacted=1`로 바꾼다 |
| 삽입 | 압축본(헤드·요약·테일, system 제외)을 새 행으로 INSERT(`active=1, compacted=0`). 요약 행은 `is_copy=0`, **원본에서 복제한 행은 `is_copy=1`**(병합으로 내용이 바뀐 테일 첫 행도 복제 행) |
| 검색 | `search_messages`는 `is_copy = 0`인 행만 대상(원문 + 요약). 복제 행은 같은 내용이 두 번 나오지 않도록 제외 |
| 복제 시 필드 | `tool_name`은 짝이 되는 assistant `tool_calls`의 `function.name`을 `tool_call_id`로 찾아 복원(메모리 속 tool 메시지에는 `tool_name`이 없다). `finish_reason`·`token_count`는 복제하지 않는다(NULL) |
| 카운터 | `sessions.message_count`·`tool_call_count`는 에이전트가 `append_message`로 추가한 행만 센다. 압축 삽입은 직접 INSERT라 변하지 않는다(의미: 「대화 중 실제로 오간 메시지 수」) |
| 원천 | 05에서 대화의 원천은 **메모리의 `messages`**다. DB의 활성 행은 그 거울이며(세션 재개를 만들 때의 기반) 시나리오 9가 둘의 일치를 시험한다 |

---

## 1. 구현 내용 명세

1. **스키마 v4** ([`session_db.py`](../../../../src/hermes_core/session_db.py)):
   - `SCHEMA_VERSION = 4`. `messages`에 `active INTEGER DEFAULT 1`, `compacted INTEGER DEFAULT 0`, `is_copy INTEGER DEFAULT 0`를 `SCHEMA_SQL`의 `CREATE TABLE`에 추가(새 DB). 기존 DB는 `if current_version < 4` 블록에서 `ALTER TABLE ... ADD COLUMN`(이미 있으면 무시)하고 **그 블록에서** `schema_version`을 4로 올린다(현재 마이그레이션은 블록마다 버전을 직접 올리는 방식). 기존 행은 `active=1, compacted=0, is_copy=0`이 된다.
   - `get_messages_as_conversation(session_id)`: `active = 1`만 시간순. `get_messages(session_id)`: 보관 행 포함 전체 원장(열 포함).
   - `search_messages(...)`: `m.is_copy = 0` 조건 추가(다른 동작·반환 형식 불변).
   - `archive_and_compact(session_id, new_messages: list[dict], summary_index: Optional[int]) -> None`: **단일 트랜잭션**(`BEGIN`~`COMMIT`)에서 위 표대로 보관·삽입. `summary_index`가 가리키는 행만 `is_copy=0`. 중간에 실패하면 전부 롤백하고 예외를 올린다. 삽입 시각은 단조 증가가 보장되도록 `time.time()` 호출 간 순서를 유지(같은 시각이면 `id` 오름차순이 순서를 결정).
2. **에이전트** ([`agent.py`](../../../../src/hermes_core/agent/agent.py)):
   - 생성자에 `context_window_tokens: int = CONTEXT_WINDOW_TOKENS`, `compression_threshold_ratio: float = COMPRESSION_THRESHOLD_RATIO`, `compression_enabled: bool = True`를 추가(기존 인자 순서·의미 불변).
   - 상태: `_last_input_tokens`, `_last_message_count`, `_rejected_at_tokens`. (이전 요약은 상태로 두지 않고 040의 `find_previous_summary`가 메시지에서 찾는다.)
   - `run_conversation` 루프에서 `llm_client.chat_completion` 호출 **직전**에 한 번 판정: `compression_enabled`이고, 이 호출 안에서 요약 호출을 시도한 횟수 < `COMPRESSION_MAX_PER_RUN`이고, `should_compress(estimate_current_tokens(...), threshold)`이고, `may_retry_after_reject(현재 토큰, _rejected_at_tokens)`이면 `compress_messages(…, tail_tokens=compression_tail_budget(threshold))`를 호출한다.
     - 결과 있음: `messages[:] = 결과.messages`, `archive_and_compact` 기록, `_last_input_tokens = None`, `_rejected_at_tokens = None`, 요약 호출 `usage`를 `update_token_counts`로 누적.
     - 결과 `None`: 분할 불가(짧음)이면 아무 상태도 바꾸지 않는다. 요약 호출까지 갔다가 **거부**된 경우에는 `_rejected_at_tokens = 현재 추정 토큰`을 기록하고 이 호출 안에서는 더 시도하지 않는다.
     - DB 기록 실패: 경고 로그를 남기고 **메모리의 `messages`는 바꾸지 않으며**(압축 포기) 대화를 계속한다. 이 시도도 시도 횟수에 센다.
   - 모델 응답의 `usage.input_tokens`와 호출에 보낸 `len(messages)`(호출 직전 길이)를 `_last_input_tokens`/`_last_message_count`에 보관.
   - 압축이 한 번이라도 있었으면 결과 사전에 `"compression": {"count": 성공한 압축 수, "fallback": 정적 대체 요약을 쓴 횟수}`를 **추가**한다(없으면 키 없음, 기존 키·의미 불변). 압축마다 `logger.info`로 전·후 추정 토큰을 남긴다. `compression_enabled=False`로 끌 수 있다.
   - 시스템 프롬프트(`messages[0]`)와 `_cached_system_prompt`는 건드리지 않는다.
3. **CLI** ([`cli.py`](../../../../src/hermes_core/cli.py)): `--context-window <정수>` 옵션(기본 상수)을 추가하고 에이전트에 전달. 1 미만이거나 정수가 아니면 안내 후 종료 코드 2(에이전트·DB를 만들기 전).
4. **상수** ([`constants.py`](../../../../src/hermes_core/constants.py)): `COMPRESSION_MAX_PER_RUN = 3`.
5. **작동원리 갱신**: 없음 (060에서 일괄 반영).

---

## 2. 변경할 파일 목록

- `src/hermes_core/session_db.py`, `src/hermes_core/agent/agent.py`, `src/hermes_core/cli.py`, `src/hermes_core/constants.py` (수정)
- `tests/test_hermes_core/test_session_db.py` (시나리오 추가), `tests/test_hermes_core/test_compression_agent.py` (신규), `tests/test_hermes_core/test_cli.py` (옵션 시나리오 추가)
- `docs/로드맵/05-컨텍스트-압축-MVP/README.md`, `docs/인계.md` (갱신)

> **사전 점검 (착수 시)**: ① 기존 시험이 `messages` 열 개수·`schema_version == 3`에 의존하는지 `grep`. 의존하면 임의로 고치지 않고 멈춰서 보고한다. ② `session_search_tool.py`가 `search_messages`를 어떻게 호출하는지(필터 추가가 반환 형식을 바꾸지 않는지) 확인. ③ 마이그레이션 v4 블록이 버전을 4로 올리는지 확인(누락 시 버전이 3에 머무는 결함 방지). ④ `MockLLMClient`의 `call_history`가 `messages` 참조를 저장하므로, 압축 전후 비교 시험은 호출 시점에 사본을 떠 둔다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험용 합성 대화: 임계값을 낮춘 에이전트(`context_window_tokens`를 작게)와 큐를 채운 `MockLLMClient`(요약 응답 포함). DB는 `:memory:` 또는 `tmp_path`.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | v3 DB(파일, 세션 1·메시지 2)를 `SessionDB(path)`로 열기 | 오류 없이 열림, `schema_version == 4`, 기존 메시지 `active=1, compacted=0, is_copy=0`, 조회 결과 불변 |
| 2 | 새 DB | `schema_version == 4`, `messages`에 `active`·`compacted`·`is_copy` 열 존재. v3에서 마이그레이션한 DB와 열 구성 일치 |
| 3 | 마이그레이션 두 번 실행(멱등) | 오류 없음, 버전 4 유지 |
| 4 | `archive_and_compact` 정상 호출 | 호출 전 활성 행 전부가 `active=0, compacted=1`, 새 행이 `active=1, compacted=0`, 요약 행만 `is_copy=0`·나머지 새 행 `is_copy=1`, `get_messages`(원장)에 옛 행과 새 행이 모두 있음 |
| 5 | `get_messages_as_conversation` | 활성 행만 시간순, 보관 행 제외 |
| 6 | `archive_and_compact` 중간 예외(삽입 단계에서 실패하도록 유도) | 롤백되어 활성/보관 상태가 호출 전과 완전히 동일 |
| 7 | 임계값 미만의 짧은 대화 | 압축 발동 없음, 모델 호출 수 불변, `compacted` 행 0, 결과에 `compression` 키 없음 |
| 8 | 임계값을 넘는 긴 대화(도구 호출 포함) 한 턴 | 압축 발동, 모델 호출 순서: 요약 호출 → 본 호출, `result["messages"]`의 첫 요소 `system` 내용이 압축 전과 바이트 동일, 요약 메시지 1개, `result["compression"] == {"count": 1, "fallback": 0}` |
| 9 | 8번 이후 `get_messages_as_conversation` | `(role, content, tool_call_id, tool_calls)` 기준으로 `result["messages"][1:]`(system 제외)과 일치(DB 활성 거울 = 메모리) |
| 10 | 8번 이후 `sessions` | `session_id` 불변, `message_count`/`tool_call_count`는 압축 전후 같음(압축 삽입이 세지 않음) |
| 11 | 8번의 `llm_calls` | 요약 호출 1행(요청에 `tools` 없음), 본 호출도 기록됨 |
| 12 | 8번 이후 복제된 `tool` 행 | `tool_name`이 짝 assistant 호출의 도구 이름과 같음 |
| 13 | 8번 이후 테일 메시지의 고유 문구를 `search_messages`로 검색 | 정확히 **1건**(원본 행, `compacted=1`), 복제 행은 나오지 않음 |
| 14 | 8번 이후 중간 구간에만 있던 고유 문구 검색 | 원본 보관 행이 검색됨(원문이 사라지지 않음) |
| 15 | 요약 모델 응답에서 `LLMError` | 정적 대체 요약으로 압축, `stopped_reason == "completed"`, `result["compression"]["fallback"] == 1` |
| 16 | 압축 후 또 길어져 같은 `run_conversation`에서 재압축 | 프롬프트에 이전 요약 포함(메시지에서 찾음), 요약 메시지는 여전히 1개, 요약 호출 시도가 `COMPRESSION_MAX_PER_RUN`을 넘지 않음 |
| 17 | 요약이 원본보다 커서 거부 | 압축 없음, DB 불변, 대화 계속, 같은 `run_conversation`에서 요약 호출이 **더 나가지 않음** |
| 18 | 17번 직후 다음 `run_conversation`(토큰이 거의 늘지 않음) | 요약 호출 없음. 토큰이 거부 시점의 1.25배 이상으로 늘면 다시 시도 |
| 19 | DB 기록 실패(`archive_and_compact`가 예외) | 경고 로그, 메모리 `messages` 불변, 대화 계속, 예외가 밖으로 나오지 않음 |
| 20 | `compression_enabled=False` | 어떤 길이에도 압축 없음 |
| 21 | 새 `HermesAgent` 인스턴스로 압축된 이력을 `conversation_history`로 이어서 호출 | 이전 요약이 프롬프트에 반영됨(에이전트 인스턴스 상태에 의존하지 않음) |
| 22 | `MockLLMClient`(usage 없음) 대화 | 거친 추정으로 트리거가 판정됨. 실제 usage가 있는 가짜 모델에서는 `input_tokens + 신규 추정`이 쓰임 |
| 23 | 압축 후 시스템 프롬프트 캐시 | `_cached_system_prompt` 불변, `sessions.system_prompt` 불변 |
| 24 | CLI `--context-window 2000` | 에이전트가 해당 창 크기로 생성되고 임계값이 `1000`으로 계산됨 |
| 25 | CLI `--context-window 0`, `abc`, `-5` | 안내 메시지 후 종료 코드 2, 세션 DB 생성 없음 |
| 26 | 기존 코어 테스트 | **수정 없이** 통과(압축은 기본 창에서 발동하지 않음) |

---

## 4. 검증 기준 (Done 조건)

1. 시나리오 1~25가 통과한다.
2. 기존 테스트가 모두 통과한다. `git diff <단계 시작 커밋>..HEAD -- tests/`에서 **삭제된 줄이 0**이다.
3. `git diff <단계 시작 커밋>..HEAD -- src/hermes_core/tools src/hermes_core/llm_client.py src/hermes_core/agent/prompt_builder.py`가 비어 있다.
4. `grep -rn "SCHEMA_VERSION = " src/hermes_core`의 정의가 한 곳뿐이다.
