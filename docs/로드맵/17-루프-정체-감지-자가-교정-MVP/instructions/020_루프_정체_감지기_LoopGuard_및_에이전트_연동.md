# [구현지시서 020] 루프 정체 감지기 (`LoopGuard`) — GUARD 내장 확장과 CLI 안내

> **상위 대시보드**: [17-README.md](../README.md)  
> **선행 조건**: 010(안전한 코드 탐색 도구 `search_files`) 완료  
> **근거**: [ADR-0017](../../../adr/0017-코드_탐색_및_루프_정체_감지_MVP_범위와_결정.md) §2-6~§2-10(D-06~D-10), [ROADMAP.md](../ROADMAP.md) §2·§3·§4(위험 1·5·6·7·8·9)·§6-2, [WHY.md](../WHY.md) 가정 D·E·F  
> **재작성**: 2026-10-31. 16번 `Closed`(`092b115`) 뒤 확장 계약 위로 전면 재작성했다(미실행 `Draft` 상태에서 플랜 승인으로 재작성 승인, [PLAN.md](../PLAN.md) §8). 첫 판은 `git show 24b006f:<이 파일 경로>`로 본다.

---

## 0. 착수 전 점검 (코드 변경 전에 수행하고 결과를 대시보드 실행 기록에 남긴다)

1. **기준 코드 대조**: 아래 사실이 지금 코드와 같은지 연다. 다르면 멈추고 보고한다.
   - 모든 도구 호출(파싱 실패 포함)이 `HermesAgent._execute_tool_calls` → `ToolDispatcher.dispatch(ToolCall)`를 지나고, 결과 문자열이 호출마다 `role="tool"`로 `messages`와 `SessionDB`에 기록된다 (`agent.py` `_execute_tool_calls`).
   - `ToolCall`에 `scope`, `raw_arguments`, `parse_error`가 있고 파싱 실패면 `args == {}`이다 (`dispatcher.py` `ToolCall`).
   - 정지 요청은 배치와 `after_tool_calls` 훅 뒤에 확인되고 이때 `final_response = ""`이다 (`agent.py` `run_conversation`).
   - 기본 확장은 `HermesAgent.__init__`에서 `install_extension(CompressionExtension(...))`로 설치된다.
2. **기존 시험 영향 전수 조사**: 한 턴 안에서 같은 도구·같은 인자를 연속 3회 이상 호출하는 기존 시험을 찾는다(`MockLLMClient`/`MockTransport` 큐, `max_iterations` 시험 포함). 결과 문자열 비교나 `stopped_reason`이 바뀌어 깨질 시험이 있으면 **코드를 바꾸지 말고 멈춰 목록과 함께 보고**한다(기존 시험 변경은 사용자 승인 사항).

---

## 1. 구현 내용 명세

한 턴 안에서 같은 도구·같은 정규화 인자 호출이 연속될 때 3회째에 자가 교정 경고(`_loop_warning`)를 결과에 넣고, 5회째에 도구를 실행하지 않은 채 정지를 요청(`stopped_reason = "stuck_loop"`)하는 감지기를 **디스패처 GUARD 단계의 내장 확장**으로 구현한다. `run_conversation`·`_execute_tool_calls` 본문은 바꾸지 않는다([코딩규약](../../../코딩규약.md) §3-7).

### 1-1. 상수 추가 ([`constants.py`](../../../../src/hermes_core/constants.py))

파일 하단에 다음 절을 추가한다(`LOOP_GUARD_FINAL_RESPONSE`는 두지 않는다 — D-09):

```python
# =============================================================================
# 루프 정체 감지 및 자가 교정 상수 (17 로드맵)
# =============================================================================

LOOP_GUARD_WARN_THRESHOLD = 3
LOOP_GUARD_BLOCK_THRESHOLD = 5

LOOP_GUARD_WARNING_MESSAGE = (
    "Loop guard warning: tool '{tool_name}' was called {count} times consecutively "
    "with identical arguments. Do not repeat the same call; inspect with "
    "search_files/read_file, change arguments, or ask the user."
)
LOOP_GUARD_BLOCKED_MESSAGE = (
    "Loop guard blocked: tool '{tool_name}' was called {count} times consecutively "
    "with identical arguments. Execution was skipped and the turn is stopping to "
    "prevent an infinite loop."
)
CLI_STUCK_LOOP_NOTICE = (
    "⚠️  Agent stopped early: repeated identical tool calls detected (stuck_loop)."
)
```

### 1-2. 이벤트 이름 ([`agent/events.py`](../../../../src/hermes_core/agent/events.py))

- `EVENT_LOOP_GUARD = "loop_guard"`를 기존 이벤트 이름들 옆에 추가한다. `ConsoleEventSink`는 바꾸지 않는다(이 이벤트는 화면에 찍지 않는다).

### 1-3. 감지기 모듈 신설 (`src/hermes_core/agent/loop_guard.py`)

모듈 독스트링에 원본 매핑(`hermes-agent/tools/file_tools.py`의 읽기·검색 반복 추적, 전역 딕셔너리 대신 턴 칸)을 적는다. 전역 상태를 두지 않는다.

1. **`make_fingerprint(func_name: str, parsed_args: Optional[dict[str, Any]], raw_args: Any = "") -> tuple[str, str]`**:
   - `name = str(func_name or "unknown")`
   - `parsed_args`가 `dict`이면 `canonical = json.dumps(parsed_args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
   - 아니면 `canonical = f"<invalid_json:{str(raw_args or '').strip()}>"`
   - `return (name, canonical)`
2. **`class LoopGuard`** (순수 카운터):
   - `__init__(self, warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD, block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD)`:
     - `warn_threshold`가 `bool`이거나 `int`가 아니거나 `< 2`이면 `ValueError("warn_threshold must be an integer >= 2")`
     - `block_threshold`가 `bool`이거나 `int`가 아니거나 `<= warn_threshold`이면 `ValueError("block_threshold must be an integer > warn_threshold")`
     - `self.last_fingerprint: Optional[tuple[str, str]] = None`, `self.consecutive_count = 0`
   - `record(self, func_name, parsed_args, raw_args="") -> tuple[str, int]`:
     - 지문이 직전과 같으면 `consecutive_count += 1`, 다르면 지문 교체 후 `1`.
     - `count >= block_threshold` → `("block", count)`, `count >= warn_threshold` → `("warn", count)`, 그 외 `("ok", count)`.
3. **`attach_loop_warning(result_str: str, warning_message: str) -> str`**:
   - `json.loads`가 성공하고 결과가 `dict`이면 `"_loop_warning"` 키를 넣어 `json.dumps(..., ensure_ascii=False)`로 반환.
   - 실패하거나 `dict`가 아니면 `f"{result_str}\n{warning_message}"`.
4. **`class LoopGuardExtension`** (`name = "loop_guard"`):
   - `install(self, host: ExtensionHost) -> None`: `self._host = host`, `host.add_tool_middleware(MiddlewarePhase.GUARD, self._middleware)`.
   - `_middleware(self, call: ToolCall, next_fn) -> str` — 이 순서를 지킨다:
     1. `if call.scope is None: return next_fn(call)` (턴 밖 직접 호출은 세지 않는다).
     2. `slot = call.scope.state("loop_guard")`; `if slot.setdefault("owner", self) is not self: return next_fn(call)` (D-10: 디스패처 공유로 중복 등록된 다른 인스턴스는 통과만 한다).
     3. `guard = slot.setdefault("guard", LoopGuard())`
     4. `if slot.get("blocked"): return _blocked_result(call.name, guard.consecutive_count)` (같은 배치의 나머지 호출, `next` 미호출).
     5. `status, count = guard.record(call.name, call.args if call.parse_error is None else None, call.raw_arguments or "")`
     6. `status == "block"`: `slot["blocked"] = True` → `call.scope.request_stop("stuck_loop")` → `logger.warning("LoopGuard blocked repeated tool call '%s' (count=%d).", call.name, count)` → `self._host.emit(EVENT_LOOP_GUARD, status="block", tool=call.name, count=count)` → `return _blocked_result(call.name, count)`. **`next_fn`을 부르지 않는다.**
     7. 그 밖: `result = next_fn(call)`. `status == "warn"`이면 `self._host.emit(EVENT_LOOP_GUARD, status="warn", tool=call.name, count=count)` 후 `return attach_loop_warning(result, LOOP_GUARD_WARNING_MESSAGE.format(tool_name=call.name, count=count))`. `ok`면 `return result`.
   - `_blocked_result(tool_name, count)`는 `json.dumps({"success": False, "error": LOOP_GUARD_BLOCKED_MESSAGE.format(tool_name=tool_name, count=count)}, ensure_ascii=False)`를 반환하는 모듈 함수다.
   - 알려진 한계(D-10): 디스패처를 공유하면 턴 칸 소유권을 먼저 잡은 인스턴스(먼저 생성된 에이전트의 확장)가 세므로, `EVENT_LOOP_GUARD`가 그 에이전트의 `event_sink`로 나간다. 운영 경로는 공유하지 않으므로 고치지 않고 18번 후보로 둔다. 시나리오 14는 계수만 검증한다.
   - import 방향: `loop_guard.py`는 `hermes_core.extensions.contract`(`ExtensionHost`, `MiddlewarePhase`), `hermes_core.tools.dispatcher`(`ToolCall` — 타입 표기용), `hermes_core.agent.events`, `hermes_core.constants`만 참조한다. `agent.py`를 참조하지 않는다.

### 1-4. 기본 설치 ([`agent/agent.py`](../../../../src/hermes_core/agent/agent.py))

- `from hermes_core.agent.loop_guard import LoopGuardExtension` 1줄.
- `__init__`의 `self.install_extension(CompressionExtension(self._compression_config))` **다음 줄**에 `self.install_extension(LoopGuardExtension())` 1줄. 기본 켜짐이며 위임 자식에도 같은 경로로 설치된다.
- 이 두 줄 외에 `agent.py`는 바꾸지 않는다.

### 1-5. CLI 정체 안내 ([`cli.py`](../../../../src/hermes_core/cli.py))

- `constants.py`에서 `CLI_STUCK_LOOP_NOTICE`를 import한다.
- `run_conversation` 결과 출력 분기(`llm_error` 분기 다음, `else` 앞)에 추가한다:
  ```python
  elif result.get("stopped_reason") == "stuck_loop":
      print(f"\n{CLI_STUCK_LOOP_NOTICE}")
  ```

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py`
- `src/hermes_core/agent/events.py`
- `src/hermes_core/agent/loop_guard.py` (신규)
- `src/hermes_core/agent/agent.py` (import 1줄 + 설치 1줄)
- `src/hermes_core/cli.py`
- `tests/test_hermes_core/test_loop_guard.py` (신규): 아래 시나리오 1~18
- `docs/로드맵/17-루프-정체-감지-자가-교정-MVP/README.md`: 020 상태, 실행 기록(착수 전 점검 결과 포함)

`delegate_tool.py`, `dispatcher.py`, `extensions/contract.py`, `turn_hooks.py`, `session_db.py`는 바꾸지 않는다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험 파일: `tests/test_hermes_core/test_loop_guard.py`(신규). 에이전트 시험은 `MockLLMClient`와 `tmp_path`의 `SessionDB`를 쓰고, 도구는 시험 안에서 등록한 계수 핸들러(호출 횟수를 세는 `echo`류)를 기본으로 한다. 기대값의 횟수는 임계값 상수(3·5)에서 나온다.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `make_fingerprint("patch", {"path": "a.py", "old_string": "x"})`와 키 순서만 다른 `{"old_string": "x", "path": "a.py"}` | 두 지문이 같다 (가정 D) |
| 2 | `make_fingerprint("patch", None, "{bad json")`와 `make_fingerprint("patch", None, "  {bad json  ")` | 둘 다 `("patch", "<invalid_json:{bad json>")`이다 (가정 D) |
| 3 | `LoopGuard()`에 같은 `(이름, 인자)`를 5회 `record` | `("ok", 1)`, `("ok", 2)`, `("warn", 3)`, `("warn", 4)`, `("block", 5)` |
| 4 | 같은 호출 2회 → 인자(또는 이름)가 다른 호출 1회 → 원래 호출 2회 | 다른 호출에서 `("ok", 1)`, 이후 `("ok", 1)`, `("ok", 2)` |
| 5 | `LoopGuard(warn_threshold=1, block_threshold=3)`, `LoopGuard(warn_threshold=4, block_threshold=4)`, `LoopGuard(warn_threshold=True, block_threshold=5)` | 셋 다 `ValueError` |
| 6 | `attach_loop_warning('{"success": false, "error": "not found"}', "warn!")`, `attach_loop_warning("plain text", "warn!")` | 첫째는 유효 JSON이며 `success`·`error`·`"_loop_warning": "warn!"`을 모두 가진다. 둘째는 `"plain text\nwarn!"` (위험 5) |
| 7 | **경고 후 자가 교정 (가정 D)**: 같은 인자의 같은 도구 호출 3회(2회째는 인자 키 순서만 다르게) → 4회째 다른 인자 → 5회째 텍스트 응답 | `stopped_reason == "completed"`, `iterations == 5`. 4번째 LLM 호출에 전달된 메시지 중 3번째 `role="tool"`의 JSON에 `"_loop_warning"`이 있고, 1·2번째 `tool` 메시지에는 없다 |
| 8 | **5회째 조기 정지 (가정 E)**: `max_iterations=50`, 같은 도구·인자 호출 응답 10개를 큐에 넣고 실행 | `stopped_reason == "stuck_loop"`, `iterations == 5`, **`final_response == ""`**, 핸들러 실행 4회, 5번째 `tool` 메시지 내용에 `LOOP_GUARD_BLOCKED_MESSAGE`의 앞부분(`"Loop guard blocked: tool '"`)이 들어 있다 |
| 9 | **도구 쌍·DB 정합 (가정 F, 위험 1)**: 시나리오 8 직후 `result["messages"]`와 `session_db.get_messages_as_conversation(agent.session_id)` | 모든 `assistant(tool_calls)`의 각 `id`에 같은 `tool_call_id`의 `role="tool"`이 바로 뒤따라 1:1로 있고, 메모리 `messages[1:]`과 DB 활성 메시지가 역할·내용·`tool_call_id` 기준으로 같다 |
| 10 | **배치 안 차단 (위험 1)**: 같은 호출 4회 뒤, 5번째 응답이 `[tc1(같은 호출), tc2(다른 도구·인자)]` | tc1·tc2 모두 핸들러 미실행(tc2 핸들러 실행 0회), 두 `tool_call_id`의 `tool` 메시지가 `messages`와 DB에 모두 있고 둘 다 차단 JSON, `stopped_reason == "stuck_loop"` |
| 11 | **턴 간 독립**: 턴 1에서 같은 호출 2회 후 텍스트 응답, 같은 에이전트의 턴 2에서 같은 호출 2회 후 텍스트 응답 | 턴 2의 두 `tool` 결과에 `_loop_warning`이 없고 `stopped_reason == "completed"` |
| 12 | **CLI 안내**: CLI 루프에서 한 턴이 `stuck_loop`으로 끝난 뒤 다음 프롬프트에서 `exit` | 표준 출력에 `CLI_STUCK_LOOP_NOTICE`가 있고 그 턴에 대해 `Assistant:` 줄은 없으며, 세션이 `user_exit`로 끝난다 |
| 13 | **깨진 JSON 반복**: 인자 문자열 `"{bad json"`인 같은 도구 호출 응답 10개 | 5번째에서 `stopped_reason == "stuck_loop"`, `iterations == 5`, 1~4번째 `tool` 결과는 `"Invalid JSON arguments for tool"` 실패 JSON(3·4번째는 `_loop_warning` 포함), 5번째는 차단 JSON (별도 카운터 없이 단일 입구로 감지) |
| 14 | **디스패처 공유 (D-10, 위험 7)**: 같은 `ToolDispatcher`로 `HermesAgent` 둘을 만든 뒤 두 번째 에이전트에서 같은 호출 3회 후 텍스트 응답 | 2번째 `tool` 결과에는 `_loop_warning`이 없고 3번째에만 있다(한 호출을 한 번만 센다). `stopped_reason == "completed"` |
| 15 | **위임 자식**: 부모 `run_conversation`에서 모델이 `delegate_task`를 부르고, 자식 클라이언트는 같은 `read_file` 호출 10개(`max_iterations=10`), 부모의 다음 응답은 텍스트 | 부모 `stopped_reason == "completed"`. 부모의 `delegate_task` `tool` 결과 JSON이 `status == "failed"`, `stopped_reason == "stuck_loop"`, `iterations == 5`이고, 자식 세션 `end_reason == "stuck_loop"` |
| 16 | **설치 기록과 단계 순서**: 에이전트에 POLICY 단계 계수 미들웨어 확장을 설치하고 시나리오 8과 같은 입력 | `agent.extensions`에 `name == "loop_guard"`, `kind == "tool_middleware"`, `slot == "GUARD"` 기록이 있고 첫 기록은 여전히 `compression`이다. POLICY 미들웨어 호출 횟수는 4회(차단된 호출은 안쪽 단계에 도달하지 않음) |
| 17 | **턴 밖 직접 호출**: 계수 도구를 등록한 디스패처로 `HermesAgent`를 만든 뒤(LoopGuard 미들웨어가 붙은 상태), `agent.dispatcher.handle_function_call`로 같은 호출 5회 | 다섯 결과 모두 핸들러 결과 그대로(`_loop_warning` 없음, 차단 없음), 핸들러 실행 5회 |
| 18 | **관찰 이벤트**: `event_sink`로 이벤트를 모으며 시나리오 8과 같은 입력 | `kind == "loop_guard"` 이벤트가 정확히 3개이며 순서대로 `status`가 `"warn"`(count 3), `"warn"`(count 4), `"block"`(count 5)이고 `tool`은 호출한 도구 이름이다 |

---

## 4. 검증 기준 (Done 조건)

- §0 착수 전 점검 결과가 대시보드 실행 기록에 있다(영향 시험 0건, 또는 사용자 승인된 처리).
- 시나리오 1~18 전부 통과.
- `git diff` 기준 `agent.py` 변경이 import 1줄 + 설치 1줄뿐이다.
- 전체 회귀 테스트 `-n 4` 및 `-m pty` 직렬 통과.
- 대시보드 020 `Done`, 실행 기록 갱신.
