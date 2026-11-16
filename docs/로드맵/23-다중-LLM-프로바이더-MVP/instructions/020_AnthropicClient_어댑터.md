# [020] `AnthropicClient` Messages API 어댑터 구현

- **단계 번호**: `020`
- **단계명**: `AnthropicClient` Messages API 어댑터 구현
- **상태**: `Done`
- **의존성**: `010` (`constants.py`의 `ANTHROPIC_MESSAGES_URL`, `ANTHROPIC_API_VERSION` 등)
- **관련 도해**: [ROADMAP.md §2~§3](../ROADMAP.md#2-폴더-및-변경-파일-구조)
- **변경 파일**:
  - `[변경]` `src/hermes_core/llm_client.py` (마스킹 헬퍼 및 `on_call` 안전 호출 공용화, `OpenRouterClient` 호환 유지)
  - `[신규]` `src/hermes_core/anthropic_client.py` (`AnthropicClient` 구현)
  - `[신규]` `tests/test_hermes_core/test_anthropic_client.py` (단위 및 인수 테스트)
- **커밋 메시지**: `feat(core): R23-020 - Anthropic Messages API 어댑터(AnthropicClient) 구현 및 검증`

---

## 1. 배경 및 목적

[ADR-0023 §2-1](../../../adr/0023-다중_LLM_프로바이더_MVP_범위와_결정.md)에 따라 Anthropic Claude는 OpenAI Chat Completions 규격과 다른 Messages API(`/v1/messages`)를 사용합니다. 외부 SDK(`anthropic`)를 추가하지 않고 기존 `httpx`만으로 [`LLMPort`](../../../src/hermes_core/kernel/ports.py)와 [`CallRecordable`](../../../src/hermes_core/kernel/ports.py)을 구현하는 `AnthropicClient`를 작성합니다.

---

## 2. 상세 구현 명세

### 2.1 `src/hermes_core/llm_client.py` 공용 헬퍼 정리
- 기존 `_mask_str`, `_mask_obj`의 동작을 유지한 채 `mask_str`, `mask_obj` 공개 별칭(또는 공용 함수)과 `invoke_on_call_safe(on_call, record, logger)` 헬퍼를 제공해 `OpenRouterClient`와 `AnthropicClient`가 마스킹·콜백 예외 삼킴 로직을 중복하지 않도록 정리한다.
- `OpenRouterClient`의 생성자 시그니처, 속성, 메서드, 기존 테스트 호환성은 100% 유지한다.

### 2.2 `src/hermes_core/anthropic_client.py` 신규 작성
`AnthropicClient` 클래스는 다음 규격을 만족한다:

1. **생성자 및 문자열 표현**:
   - `__init__(self, api_key: str, model: str, base_url: str = ANTHROPIC_MESSAGES_URL, timeout: int = LLM_TIMEOUT_SECONDS, transport: Optional[httpx.BaseTransport] = None, on_call: Optional[Callable[[dict[str, Any]], None]] = None)`
   - `api_key`가 비어 있거나 공백뿐이면 `ValueError("API key cannot be empty.")` 발생.
   - `__repr__` / `__str__`에 API 키가 절대 노출되지 않는다.
   - `set_call_recorder(fn)`으로 `on_call` 콜백을 설정한다 (`CallRecordable` 준수).

2. **요청 변환 (`OpenAI messages/tools` → `Anthropic Messages API`)**:
   - **헤더**: `x-api-key: <api_key>`, `anthropic-version: ANTHROPIC_API_VERSION`, `Content-Type: application/json`. (`Authorization: Bearer` 미사용)
   - **`system` 분리**: `messages` 중 `role == "system"`인 항목의 텍스트를 추출해 `\n\n`으로 결합하고, 비어 있지 않으면 payload 최상위 `"system"` 필드로 전달한다.
   - **`assistant` 변환**:
     - `content`가 비어 있지 않은 문자열이면 `{"type": "text", "text": content}` 블록 추가.
     - `tool_calls`의 각 항목을 `{"type": "tool_use", "id": id, "name": name, "input": parsed_dict}` 블록으로 변환.
     - `arguments`가 JSON 문자열이면 `json.loads`로 파싱(객체 dict가 아니거나 파싱 실패 시 `{"raw": arguments}` 안전 폴백; dict면 그대로 사용).
   - **`tool` 결과 병합**:
     - `role == "tool"` 메시지는 `{"type": "tool_result", "tool_use_id": tool_call_id, "content": content or ""}` 블록으로 변환해 `role == "user"` 메시지로 적재한다.
     - 연속된 `role == "tool"` 또는 직전 메시지가 `role == "user"`인 경우, 새 메시지를 추가하지 않고 직전 `user` 메시지의 `content` 블록 리스트에 병합한다 (Anthropic 연속 `user` 금지 규격 준수).
   - **도구 스키마 변환**:
     - `tools`가 `None`이 아니고 비어 있지 않을 때만 payload에 `"tools"` 포함 (`tools=None` 또는 `[]`이면 생략).
     - `{"type": "function", "function": {"name": n, "description": d, "parameters": p}}` → `{"name": n, "description": d, "input_schema": p or {"type": "object", "properties": {}}}`.
   - **공통 바운디드**: `"model": self.model`, `"max_tokens": LLM_MAX_OUTPUT_TOKENS`.

3. **응답 변환 (`Anthropic Messages API` → `LLMResponse`)**:
   - `content` 배열이 없거나 리스트가 아니면 `LLMError("bad_response", ...)` 발생.
   - `type == "text"` 블록들의 `text`를 결합해 `LLMResponse.content`로 반환 (텍스트 블록이 없으면 `None`).
   - `type == "tool_use"` 블록들은 OpenAI 규격 `{"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(input, ensure_ascii=False)}}`로 변환해 `LLMResponse.tool_calls`로 반환 (없으면 `None`).
   - `stop_reason` 매핑: `"end_turn"`/`"stop_sequence"` → `"stop"`, `"tool_use"` → `"tool_calls"`, `"max_tokens"` → `"length"`, 그 외 → 원문.
   - `usage` 파싱: `input_tokens`, `output_tokens`를 `{"input_tokens": ..., "output_tokens": ...}`로 반환.

4. **오류 처리 · 보안 · 호출 기록**:
   - `429` → `LLMError("rate_limited", ...)`
   - 그 외 4xx/5xx → `LLMError("http_error", ...)` (본문 `LLM_ERROR_BODY_MAX_CHARS` 절단 + 키 마스킹)
   - JSON 파싱 실패 / dict 아님 / `content` 누락 → `LLMError("bad_response", ...)`
   - `httpx.TimeoutException` → `LLMError("timeout", ...)`
   - `httpx.TransportError` → `LLMError("network", ...)`
   - `finally` 블록에서 `request`/`response`/`error_message`에 API 키 마스킹 후 `on_call` 호출.

---

## 3. 선기술 인수 테스트 시나리오 (입력 → 기대 결과)

> 구현 전 확정된 시나리오입니다. 4단계 구현 시 아래 시나리오를 그대로 `tests/test_hermes_core/test_anthropic_client.py`로 옮기며, 임의로 완화하거나 삭제하지 않습니다.

1. **시나리오 1 (기본 텍스트 응답 및 헤더 규격)**:
   - 입력: `messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]`, 서버 `200` 응답(`content=[{"type":"text","text":"hello"}]`, `stop_reason="end_turn"`, `usage={"input_tokens":12,"output_tokens":4}`).
   - 기대 결과: 요청 URL은 `ANTHROPIC_MESSAGES_URL`, 헤더에 `x-api-key`·`anthropic-version` 포함(`Authorization` 미포함), payload에 `"system": "sys"` 및 `user` 메시지만 포함, 반환 `LLMResponse(content="hello", tool_calls=None, finish_reason="stop", usage={"input_tokens":12,"output_tokens":4})`.
2. **시나리오 2 (도구 스키마 변환 및 `tools=None` 생략)**:
   - 입력: OpenAI 형식 도구 `[{"type":"function","function":{"name":"read_file","description":"read","parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}]` 전달 vs `tools=None` 전달.
   - 기대 결과: 첫 요청 `tools[0]`에 `"name"`, `"description"`, `"input_schema"`가 있고 `"type": "function"` 제거됨; 두 번째 요청(`tools=None`) payload에는 `"tools"` 키 미포함.
3. **시나리오 3 (`tool_use` 응답 역변환 및 다중 `tool` 결과 병합)**:
   - 입력: 1턴 응답에 `tool_use` 2건(`id="t1"`, `id="t2"`, `stop_reason="tool_use"`), 이어서 2턴 요청 `messages`에 `assistant(tool_calls 2건)` + 연속 `role="tool"` 2건(`tool_call_id="t1"`, `"t2"`) 전달.
   - 기대 결과: 1턴 `LLMResponse.finish_reason == "tool_calls"` 및 `tool_calls` 2건이 OpenAI 규격(`function.arguments` JSON 문자열)으로 반환; 2턴 요청 payload에서 두 `tool_result` 블록이 **단일** `role: "user"` 메시지로 병합되어 전송됨.
4. **시나리오 4 (깨진 `tool_calls` arguments 안전 폴백)**:
   - 입력: `messages`의 `assistant.tool_calls[0].function.arguments`에 깨진 JSON 문자열 `"{bad json"` 포함.
   - 기대 결과: 예외 없이 dict 폴백(`{"raw": "{bad json"}`)으로 `tool_use.input`이 구성되어 요청 전송됨.
5. **시나리오 5 (HTTP 오류 · 429 · 잘못된 응답 · 타임아웃 · 네트워크 오류)**:
   - 입력: 각각 `429`, `500` (본문에 API 키 포함), 비-JSON/누락 `content` 응답, `httpx.ReadTimeout`, `httpx.ConnectError`.
   - 기대 결과: 각각 `LLMError.kind`가 `"rate_limited"`, `"http_error"`, `"bad_response"`, `"timeout"`, `"network"`로 발생하며 오류 메시지에 API 키 원문 미포함(마스킹됨).
6. **시나리오 6 (Zero Leakage 및 `on_call` 기록)**:
   - 입력: `repr(client)`, `str(client)` 조회 및 `set_call_recorder` 등록 후 `chat_completion` 1회 실행.
   - 기대 결과: `repr`/`str`에 API 키 미포함, `on_call` 레코드에 `model`, `status_code`, `input_tokens`, `output_tokens`, `latency_ms`가 기록되고 `request`/`response` 내 키 문자열이 `"***"`로 마스킹됨.
7. **시나리오 7 (빈 API 키 생성 거부 및 프로토콜 적합성)**:
   - 입력: `AnthropicClient("   ", "claude-sonnet-4-5")` 생성, 정상 인스턴스에 대해 `isinstance(client, LLMPort)` 및 `isinstance(client, CallRecordable)` 확인.
   - 기대 결과: 빈 키는 `ValueError` 발생, 정상 인스턴스는 두 프로토콜 모두 `True`.

---

## 4. 완료 기준

- 위 7개 인수 시나리오 테스트 통과
- `.venv/bin/python -m pytest -n 4` 전체 통과 (실제 실행 후 결과 보고)
- `src/hermes_core/kernel/` diff 0줄, 전 파일 800줄 이하 유지
