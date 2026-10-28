# [구현지시서 020] OpenRouter 클라이언트 (가짜 통신 장치로 시험)

> **상위 대시보드**: [04-README.md](../README.md)  
> **선행 조건**: 010 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §4 (위험 1·2·4·5), §6-1, [ADR-0004](../../../adr/0004-실제_모델_연결_MVP_범위와_안전_정책.md) §2-5·§2-7·§2-8, [WHY.md](../WHY.md) 가정 A·C·F  
> **원칙**: 이 단계도 실제 키·실제 통신이 필요 없다. 시험은 전부 `httpx.MockTransport`다.

---

## 1. 구현 내용 명세

1. **상수** ([`constants.py`](../../../../src/hermes_core/constants.py)): 기존 `OPENROUTER_CHAT_URL`을 쓰고, 다음을 추가한다.
   - `DEFAULT_REAL_MODEL = "deepseek/deepseek-v4-flash-0731"`
   - `OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"` (이름은 **미확인 가정**)
   - `LLM_TIMEOUT_SECONDS = 60`, `LLM_MAX_OUTPUT_TOKENS = 4096`, `LLM_ERROR_BODY_MAX_CHARS = 500`
2. **신규 모듈** `src/hermes_core/llm_client.py`:
   - `LLMError(Exception)`: 속성 `kind`(`rate_limited`, `timeout`, `network`, `http_error`, `bad_response`)와 `message`. 문자열 표현에 키가 들어가지 않는다.
   - `LLMResponse`(dataclass): `content: Optional[str]`, `tool_calls: Optional[list[dict]]`, `usage: Optional[dict]`(`input_tokens`, `output_tokens`), `finish_reason: Optional[str]`.
   - `OpenRouterClient(api_key, model, base_url=OPENROUTER_CHAT_URL, timeout=LLM_TIMEOUT_SECONDS, transport=None, on_call=None)`:
     - `api_key`가 빈 문자열·공백이면 `ValueError`. `__repr__`에 키가 나오지 않는다. 속성 `model`을 노출한다.
     - `chat_completion(messages, tools=None) -> LLMResponse`: `POST`로 `{"model", "messages", "tools"(있을 때만), "max_tokens": LLM_MAX_OUTPUT_TOKENS}`를 보낸다. 헤더는 `Authorization: Bearer <키>`, `Content-Type: application/json`. 한 번만 시도한다(재시도 없음).
     - 보내기 전에 assistant 메시지가 `tool_calls`만 있고 `content`가 없으면 `content`를 `None`으로 명시한다(원본 메시지 목록은 바꾸지 않고 복사본을 보낸다).
     - 응답 해석: `choices[0].message`의 `content`, `tool_calls`(없거나 빈 목록이면 `None`, 있으면 제공자가 보낸 사전을 그대로), `choices[0].finish_reason`, `usage.prompt_tokens`/`completion_tokens`.
     - 실패 변환: 상태 429 → `rate_limited`; 그 밖의 4xx·5xx → `http_error`(메시지에 상태 코드와 본문 앞 `LLM_ERROR_BODY_MAX_CHARS`자); `httpx.TimeoutException` → `timeout`; 그 밖의 `httpx.TransportError` → `network`; JSON이 아니거나 `choices`가 비었거나 형식이 맞지 않음 → `bad_response`.
     - **키 마스킹**: 오류 메시지, `on_call`로 넘기는 요청·응답·오류의 모든 문자열에서 키 값을 `***`로 치환한다.
     - `on_call`: 호출마다(성공·실패 모두) 한 번 `on_call(record)`를 부른다. `record`는 사전: `model`, `request`(본문 사전, **헤더 없음**), `response`(본문 사전 또는 `None`), `status_code`, `input_tokens`, `output_tokens`, `latency_ms`, `error_kind`, `error_message`. 콜백이 예외를 내도 호출 결과에 영향이 없다(삼키고 경고 로그).
     - `set_call_recorder(fn)`: `on_call`을 나중에 지정하는 메서드(030에서 에이전트가 사용).
3. **시험 안전장치** (신규 `tests/test_hermes_core/conftest.py`): 코어 시험 전체에 자동 적용되는 fixture 두 가지.
   - 환경변수 `OPENROUTER_API_KEY`를 제거한다(사용자 셸의 실제 키가 시험에 새지 않게).
   - `httpx.HTTPTransport.handle_request`를 호출하면 `RuntimeError("real network is blocked in tests")`를 내도록 막는다(`MockTransport`는 영향 없음).
4. **작동원리 갱신**: 없음 (이 단계는 시스템의 영구 사실이 아직 연결되지 않은 부품만 추가한다. 연결되는 040에서 한꺼번에 반영한다).

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py` (수정), `src/hermes_core/llm_client.py` (신규)
- `tests/test_hermes_core/test_llm_client.py` (신규), `tests/test_hermes_core/conftest.py` (신규)
- `docs/로드맵/04-실제모델-연결-MVP/README.md`, `docs/인계.md` (갱신)

`agent.py`, `cli.py`, `session_db.py`는 **수정하지 않는다.**

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

키는 시험용 가짜 값 `K = "test-key-1234567890"`. 모든 통신은 `httpx.MockTransport`. `client = OpenRouterClient(K, "x/y", transport=...)`.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | 서버가 `{"choices":[{"message":{"content":"hi"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":3}}` 응답 | `content=="hi"`, `tool_calls is None`, `finish_reason=="stop"`, `usage`는 입력 10·출력 3 |
| 2 | 서버가 `message`에 `content: null`과 `tool_calls: [{"id":"c1","type":"function","function":{"name":"terminal","arguments":"{\"command\":\"ls\"}"}}]` | `content is None`, `tool_calls == 그 목록 그대로`(id, function.name, function.arguments 문자열 보존) |
| 3 | `chat_completion([{"role":"user","content":"a"}], tools=[T])`의 요청 | 메서드 POST, URL `OPENROUTER_CHAT_URL`, 본문 `model=="x/y"`, `messages` 그대로, `tools==[T]`, `max_tokens==LLM_MAX_OUTPUT_TOKENS`. `tools=None`이면 본문에 `tools` 키가 없음 |
| 4 | 요청 헤더·본문 | `Authorization == "Bearer " + K`. 본문 JSON 문자열에 `K`가 없음 |
| 5 | 서버가 429 | `LLMError.kind == "rate_limited"` |
| 6 | 서버가 500과 본문 `"boom"` | `kind == "http_error"`, 메시지에 `500` 포함, 예외·`str(예외)`에 `K` 없음 |
| 7 | 서버가 500, 본문이 `K`를 되풀이(`"bad key test-key-1234567890"`) | `LLMError` 메시지와 `on_call` 기록의 `error_message`에 `K`가 없고 `***`가 있음 |
| 8 | 본문이 `LLM_ERROR_BODY_MAX_CHARS`보다 긴 오류 | 메시지에 포함된 본문이 최대 500자 |
| 9 | 전송이 `httpx.TimeoutException`을 냄 | `kind == "timeout"` |
| 10 | 전송이 `httpx.ConnectError`를 냄 | `kind == "network"` |
| 11 | 200이지만 본문이 JSON이 아님 / `{"choices":[]}` / `{}` | 각각 `kind == "bad_response"` |
| 12 | `OpenRouterClient("", "x/y")`, `OpenRouterClient("   ", "x/y")` | `ValueError` |
| 13 | `repr(client)`, `str(client)` | `K`를 포함하지 않음 |
| 14 | `on_call`을 지정하고 성공 1회 + 실패(429) 1회 | 콜백이 정확히 2번 호출. 성공 기록: `status_code 200`, 토큰, `latency_ms >= 0`, `error_kind None`. 실패 기록: `status_code 429`, `error_kind "rate_limited"`. 두 기록 모두 `request`에 `Authorization`이 없고 전체 문자열에 `K`가 없음 |
| 15 | `on_call`이 예외를 던짐 | `chat_completion` 결과는 정상 반환 |
| 16 | assistant 메시지 `{"role":"assistant","tool_calls":[...]}`(content 없음)를 보냄 | 요청 본문의 해당 메시지에 `"content": null`이 있고, 호출자가 넘긴 원본 목록은 변경되지 않음 |
| 17 | 전송을 지정하지 않고 `chat_completion` 호출 (시험 환경) | `RuntimeError`(실제 통신 차단). 어떤 시험도 실제 네트워크에 나가지 않음 |
| 18 | `os.environ`에 키 변수가 있는 상태로 코어 시험 시작 | 시험 안에서는 `OPENROUTER_API_KEY`가 없음 |
| 19 | 기존 코어 테스트 182건 | **수정 없이** 통과 |

---

## 4. 검증 기준 (Done 조건)

1. 시나리오 1~18이 `test_llm_client.py`에서 통과한다 (17·18은 `conftest.py`가 지킴을 확인).
2. 기존 테스트가 모두 통과한다. `git diff <단계 시작 커밋>..HEAD -- src/hermes_core/agent src/hermes_core/cli.py src/hermes_core/session_db.py`가 비어 있다.
3. `grep -rn "shell=True\|requests\." src/hermes_core`에 새 결과가 없고, 새 의존성이 `pyproject.toml`에 추가되지 않았다(`httpx`는 이미 있음).
4. 소스와 시험 어디에도 실제 키 형태 문자열(`sk-or-`)이 없다.

> **사전 점검 (착수 시)**: 기존 테스트가 환경변수에 기대지 않는지, `conftest.py`가 기존 시험의 fixture(`hermes_home` 등)와 이름이 겹치지 않는지 확인한다. 문제가 있으면 임의로 바꾸지 않고 멈춰서 보고한다.
