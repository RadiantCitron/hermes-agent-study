# [구현지시서 030] `delegate_task` 도구와 디스패처 등록

> **상위 대시보드**: [07-README.md](../README.md)  
> **선행 조건**: 010·020 `Done`  
> **근거**: [ROADMAP.md](../ROADMAP.md) §6-3·§6-5, §4 위험 1·3·10, [ADR-0007](../../../adr/0007-서브에이전트_MVP_범위와_격리_방식.md) §2-2~§2-6, [WHY.md](../WHY.md) 가정 C·D·H

---

## 1. 구현 내용 명세

### 1-0. 이 지시서에서 확정한 열린 항목 (코드 대조 결과)

| 열린 항목 | 확정 | 근거 |
|---|---|---|
| 클라이언트 복제 수단 (위험 10) | **복제하지 않는다.** 호출하는 쪽이 `llm_client_factory`(인자 없이 새 클라이언트를 돌려주는 함수)를 넘긴다. `OpenRouterClient`·`llm_client.py`는 무변경 | 기록기는 클라이언트 인스턴스당 하나이고 `HermesAgent.__init__`이 `set_call_recorder`로 묶는다([agent.py:168-171](../../../../src/hermes_core/agent/agent.py)). 새 인스턴스만 있으면 된다. 키·모델·전송 계층을 아는 곳은 호출부뿐이라 팩토리가 가장 적은 지식으로 해결한다. `MockLLMClient`도 같은 방식으로 쓸 수 있다 |
| 부모 연결 방식 | **값 주입.** 등록 함수가 `session_db`와 `parent_session_id`(문자열)를 받아 클로저로 쥔다. 부모 `HermesAgent` 객체를 참조하지 않는다 | 도구 모듈이 에이전트 클래스에 거꾸로 매이지 않는다. 시험에서 가짜 값으로 바로 쓸 수 있다 |
| 위임 도구를 켜는 방식 | **별도 등록 함수.** `register_delegate_tool(dispatcher, ...)`을 부모 생성 **뒤에** 호출한다. 에이전트 생성자와 `create_default_dispatcher`는 무변경 | 기존 시험이 기본 도구를 8종으로 단언한다(`test_skills_guidance.py:125`). 켜지 않은 에이전트는 이전과 완전히 같다. 이 결정으로 **030은 `agent.py`를 바꾸지 않는다** |

### 1-1. 상수 ([`constants.py`](../../../../src/hermes_core/constants.py))

`DELEGATE_CHILD_MAX_ITERATIONS = 25`, `DELEGATE_SUMMARY_MAX_CHARS = 8000`, `SUBAGENT_SOURCE = "subagent"` ([ROADMAP §6-5](../ROADMAP.md)의 값. 이 단계 착수 승인으로 갈음).

### 1-2. 신규 모듈 `src/hermes_core/tools/delegate_tool.py`

```
register_delegate_tool(
    dispatcher: ToolDispatcher, *,
    session_db: SessionDB, parent_session_id: str,
    llm_client_factory: Callable[[], Any],
    max_iterations: int = DELEGATE_CHILD_MAX_ITERATIONS,
    summary_max_chars: int = DELEGATE_SUMMARY_MAX_CHARS,
) -> None
```

`dispatcher.register_tool("delegate_task", handler, DELEGATE_TASK_SCHEMA)`만 한다. 인자는 모두 키워드 전용이다. 같은 디스패처에 두 번 부르면 `register_tool`의 기존 동작대로 뒤의 것이 앞의 것을 덮는다(별도 처리 없음).

**스키마**: 이름 `delegate_task`. `goal`(string, 필수), `context`(string, 선택). 정의되지 않은 인자와 타입 오류는 디스패처의 기존 인자 검증이 막는다. 설명은 영어로, 다음 문장을 반드시 포함한다: `The subagent knows nothing about your conversation. Put everything it needs in goal and context.` 그 밖에 순차 단일 자식이 도구(터미널·파일·스킬 읽기)로 일하고 **요약만** 돌려준다는 사실을 한두 문장으로 쓴다.

**핸들러 `delegate_task(goal, context=None)`** — 순서가 곧 설계다:

1. **입력 검증**: `goal.strip()`이 비면 `{"success": false, "error": "goal must be a non-empty string"}`를 돌려주고 끝낸다. 클라이언트도 세션도 만들지 않는다.
2. **클라이언트**: `llm_client_factory()`를 부른다. 예외가 나면 `{"success": false, "error": "Failed to create child client: ..."}`로 끝낸다(세션을 아직 만들지 않았다).
3. **자식 생성**: `HermesAgent(session_db=session_db, dispatcher=create_child_dispatcher(), llm_client=<새 클라이언트>, max_iterations=max_iterations, quiet_mode=True, system_message=context, parent_session_id=parent_session_id, source=SUBAGENT_SOURCE, skip_memory=True, skip_context_files=True)`. `session_id`는 지정하지 않아 새로 생성된다. (`context`를 시스템 프롬프트의 호출자 지시 칸에 싣는 것은 이 단계의 최소 형태다. 「집중된 하위 작업자」 프롬프트로 다듬는 일은 040.)
4. **실행**: `try`로 `run_conversation(goal)`을 부른다. `goal`은 자식의 **첫 사용자 메시지** 한 곳에만 들어간다. `LLMError`는 에이전트가 이미 `stopped_reason="llm_error"`로 돌려주므로 여기서 잡지 않는다. 그 밖의 예외는 잡아 `stopped_reason="exception"`, `error={"kind": "exception", "message": str(e)}`로 바꾼다(예외가 부모 루프로 번지지 않는다).
5. **종료 기록**: `finally`로 `session_db.end_session(child_session_id, end_reason)`. `end_reason`은 성공이면 `"completed"`, 실패이면 `stopped_reason`(`llm_error`/`max_iterations`/`exception`), 빈 응답이면 `"empty_response"`.
6. **결과 판정**: `status = "completed"`는 `stopped_reason == "completed"`이고 `final_response.strip()`이 비어 있지 않을 때만이다(원본과 같은 기준). 그 밖에는 `"failed"`. 빈 응답이면 `error={"kind": "empty_response", "message": "Subagent returned an empty response."}`를 붙인다.
7. **요약 절삭**: `summary`는 자식 `final_response` 앞 `summary_max_chars`자. 잘렸으면 `truncated: true`.
8. **토큰**: 결과의 `tokens`는 `session_db.get_session(child_session_id)`의 `input_tokens`/`output_tokens`다(자식 세션 원장 값). 부모 세션 합계에는 아무것도 더하지 않는다(D-07).
9. **반환**: JSON 문자열(`ensure_ascii=False`). 키는 항상 `success`(= `status == "completed"`), `status`, `summary`, `child_session_id`, `iterations`, `stopped_reason`, `tokens`(`{"input": n, "output": n}`), `truncated`(불리언)이고, 실패일 때만 `error`(`{"kind", "message"}`)가 더해진다. 자식의 `messages`는 결과에 싣지 않는다.

**하지 않는 것**: 재시도, 타임아웃, 병렬, 부모 도구 목록·메모리·대화 접근, 결과 요약 가공. `session_db`와 `memory_store`를 자식에게 건네는 일도 없다(자식 도구 통로는 010에서 두 객체에 닿지 못하게 만들었다).

### 1-3. 하지 않는 것 (이 단계 밖)

- 시스템 지시문의 위임 안내문, 자식 프롬프트 다듬기: 040.
- 실제 앱(`cli.py`)에서 위임을 켜는 연결: **미배정**. 현재 대시보드의 어느 단계에도 없다. 060 실제 키 스모크가 성립하려면 필요하므로 040 지시서 작성 때 소속을 정해 보고한다.
- 작동원리 갱신: 050.

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py` (수정), `src/hermes_core/tools/delegate_tool.py` (신규)
- `tests/test_hermes_core/test_delegate_tool.py` (신규)
- `docs/로드맵/07-서브에이전트-MVP/README.md`, `docs/인계.md` (갱신)

> **사전 점검 (착수 시)**: ① 전체 테스트가 모두 통과하는지(010·020 완료 시점의 개수) 먼저 실행해 기록한다. ② `constants.py`에 세 상수가 없는지, `delegate_tool.py`가 없는지 확인한다. ③ `HermesAgent`의 `parent_session_id`·`source`·`skip_memory`·`skip_context_files`(020)와 `create_child_dispatcher`(010)가 실제로 있는지 확인한다. 없으면 착수하지 않고 보고한다. ④ `agent.py`, `dispatcher.py`, `llm_client.py`, `session_db.py`는 수정하지 않는다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험 방식: 부모는 `MockLLMClient`로 `delegate_task` 도구 호출을 스크립트한 `HermesAgent`(임시 `SessionDB`, 임시 `MemoryStore`). 자식 클라이언트는 팩토리가 돌려주는 `MockLLMClient`이거나(원장·토큰 시나리오 8만) 가짜 전송 계층을 쓰는 `OpenRouterClient`. 모델·네트워크 없음. 기대하는 도구 이름 집합·상수 값·마커는 시험 안에 **문자열·숫자로 직접 적는다**(`constants`를 import해 기대값으로 쓰지 않는다). 부모 마커는 `PARENTMARK`, 자식 내부 마커는 `CHILDINNER`로 한다. 팩토리는 호출 횟수와 돌려준 객체를 기록하는 함수로 만든다. 가짜 키는 `test-key-not-real`로 한다.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | 등록 전후 `parent.dispatcher`의 도구 이름 / 새 `create_default_dispatcher()`의 도구 이름 | 등록 전 정확히 기존 8종, 등록 후 9종(8종 + `delegate_task`). 새로 만든 기본 디스패처는 여전히 8종(자동 등록 안 함). 스키마: 이름 `delegate_task`, `required == ["goal"]`, `properties` 키 정확히 `{"goal","context"}`(둘 다 `string`), 설명에 `knows nothing about your conversation` 포함 |
| 2 | 부모 사용자 메시지 `PARENTMARK ...` → 부모 모델이 `delegate_task(goal="G1", context="C1")` 호출 → 자식 모델이 최종 응답 `child summary` | 부모 `run_conversation`이 정상 종료. 부모 `messages`의 도구 메시지 JSON: `success: true`, `status: "completed"`, `summary: "child summary"`, `child_session_id`는 비어 있지 않고 부모 세션 id와 다름, `iterations: 1`, `stopped_reason: "completed"`, `tokens: {"input": 0, "output": 0}`, `truncated: false`, `error` 키 없음 |
| 3 | 시나리오 2에서 자식 클라이언트가 받은 첫 호출 | 마지막 `user` 메시지의 내용이 정확히 `G1`. 시스템 메시지에 `C1` 포함. 어떤 메시지에도 `PARENTMARK`가 없음(부모 대화 비전달). 함께 받은 도구 이름 집합이 정확히 `{"terminal","read_file","write_file","patch","skills_list","skill_view"}` |
| 4 | 시나리오 2 이후 DB의 자식 세션 | `parent_session_id`가 부모 세션 id, `source == "subagent"`, `ended_at`이 비어 있지 않고 `end_reason == "completed"`. 부모 세션은 `source == "cli"`이고 `ended_at`이 비어 있음(자식이 부모를 닫지 않음) |
| 5 | 자식 모델이 `read_file` 도구를 한 번 부르며 중간 응답에 `CHILDINNER`를 포함한 뒤 최종 응답 | `CHILDINNER`가 자식 세션의 저장된 메시지에는 있고, 부모 세션의 저장된 메시지와 부모 `messages` 전체에는 없음. 부모 `session_search("CHILDINNER")` 결과는 `success: true`에 결과 목록이 비어 있음 |
| 6 | 자식 모델이 `delegate_task`, `memory`, `session_search`를 각각 호출 | 세 도구 모두 자식 쪽 도구 결과가 `Unknown tool`. 팩토리 호출은 총 1회, DB의 세션은 부모 + 자식 둘뿐(손자 없음). 부모의 `MemoryStore` 내용 불변 |
| 7 | 같은 부모가 `delegate_task`를 두 번 연속 호출 | 팩토리 2회 호출, 돌려준 클라이언트 두 객체가 서로 다름(`is not`), 두 `child_session_id`가 서로 다르고 둘 다 `parent_session_id`가 같은 부모 |
| 8 | 부모·자식 모두 `OpenRouterClient`(각자 별도의 가짜 전송 계층). 부모 전송: 1회차 `delegate_task` 호출 응답, 2회차 최종 응답(각 호출 입력 10·출력 5). 자식 전송: 최종 응답 1회(입력 33·출력 7) | DB `llm_calls`: 부모 세션 id로 정확히 2건, 자식 세션 id로 정확히 1건, 서로 섞이지 않음. 결과 JSON `tokens == {"input": 33, "output": 7}`. 자식 세션 토큰 합계는 33/7, 부모 세션 토큰 합계는 20/10(자식 값을 합산하지 않음) |
| 9 | `goal=""`, `goal="   "` | 둘 다 `success: false`, 오류에 `goal` 포함. 팩토리 호출 0회, DB 세션은 부모 하나뿐 |
| 10 | `goal` 누락, `context=123`(숫자), 정의되지 않은 인자 `foo=1` | 각각 디스패처의 기존 검증 메시지(`missing required argument 'goal'`, `argument 'context' must be of type string`, `unexpected argument 'foo'`)로 거부. 팩토리 호출 0회 |
| 11 | 팩토리가 `ValueError("bad key")`를 던짐 | `success: false`, 오류에 `Failed to create child client` 포함. DB 세션은 부모 하나뿐. 부모 루프는 예외 없이 이어서 최종 응답까지 도달 |
| 12 | 자식 클라이언트가 호출 시 `LLMError`(kind `server`)를 던짐 | `success: false`, `status: "failed"`, `stopped_reason: "llm_error"`, `summary: ""`, `error.kind` 존재. 자식 세션 `end_reason == "llm_error"`. 부모 루프는 이어서 최종 응답까지 도달 |
| 13 | 자식 클라이언트가 `LLMError`가 아닌 `RuntimeError("boom")`를 던짐 | `success: false`, `status: "failed"`, `stopped_reason: "exception"`, `error.kind == "exception"`, 오류 메시지에 `boom` 포함. 자식 세션 `ended_at`이 비어 있지 않고 `end_reason == "exception"`. 예외가 부모 밖으로 번지지 않음 |
| 14 | 등록 시 `max_iterations=2`, 자식 모델이 계속 `read_file`(없는 파일)을 호출 | `status: "failed"`, `stopped_reason: "max_iterations"`, `iterations: 2`, 자식 세션 `end_reason == "max_iterations"` |
| 15 | 자식 최종 응답이 빈 문자열 | `success: false`, `status: "failed"`, `stopped_reason: "completed"`, `error.kind == "empty_response"`, 자식 세션 `end_reason == "empty_response"` |
| 16 | 등록 시 `summary_max_chars=10`, 자식 최종 응답이 16자(`0123456789ABCDEF`) | `summary == "0123456789"`, `truncated: true`, `status: "completed"` |
| 17 | 기본값 그대로 등록. (a) 자식이 30회 연속 `read_file` 도구 호출을 큐 (b) 자식 최종 응답이 `x` 9,000자 | (a) `iterations == 25`, `stopped_reason == "max_iterations"`. (b) `len(summary) == 8000`, `truncated: true` |
| 18 | 기존 코어 테스트(010·020 완료 시점 전체) | **수정 없이** 통과 |

---

## 4. 검증 기준 (Done 조건)

1. 시나리오 1~17이 통과한다 (`test_delegate_tool.py`).
2. 기존 테스트가 모두 통과한다 (사전 점검 기준선 + 신규). 기존 시험 파일의 변경이 0건이다.
3. `git diff <단계 시작 커밋>..HEAD --stat`의 `src/` 변경이 `constants.py`와 `tools/delegate_tool.py`뿐이다 (`agent/`, `dispatcher.py`, `llm_client.py`, `session_db.py` 무변경, 가정 H).
4. `grep -rn "delegate" src/`의 결과가 `constants.py`와 `tools/delegate_tool.py`에만 있다 (`agent/`와 `dispatcher.py`에는 없음 — 부모 에이전트 쪽에 위임이 하드코딩되지 않았다는 뜻).
