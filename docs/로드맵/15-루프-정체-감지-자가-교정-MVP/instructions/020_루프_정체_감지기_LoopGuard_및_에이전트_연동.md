# [구현지시서 020] 루프 정체 감지기 (`LoopGuard`) 및 에이전트·CLI 연동

> **상위 대시보드**: [15-README.md](../README.md)  
> **선행 조건**: 010(안전한 코드 탐색 도구 `search_files`) 완료  
> **근거**: [ADR-0015](../../../adr/0015-코드_탐색_및_루프_정체_감지_MVP_범위와_결정.md) §2-6·§2-7·§2-8, [ROADMAP.md](../ROADMAP.md) §2·§4(위험 1·5·6)·§6-2, [WHY.md](../WHY.md) 가정 D·E·F

---

## 1. 구현 내용 명세

이 단계는 한 턴 안에서 동일한 도구·인자 호출이 연속 반복될 때 1차($K=3$)로 자가 교정 경고(`_loop_warning`)를 주입하고, 2차($M=5$)로 핸들러 실행을 생략한 채 도구 쌍(Tool Pair) 불변식을 맞춰 기록한 뒤 루프를 즉시 종료(`stopped_reason = "stuck_loop"`)하는 `LoopGuard`를 구현하고 `HermesAgent`와 `cli.py`에 연결한다.

### 1-1. 루프 정체 감지 상수 추가 ([`constants.py`](../../../../src/hermes_core/constants.py))

파일 하단에 루프 정체 감지 상수 6종을 추가한다:

```python
# =============================================================================
# 루프 정체 감지 및 자가 교정 상수 (15 로드맵)
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
LOOP_GUARD_FINAL_RESPONSE = (
    "Stopped because the same tool call was repeated consecutively without progress (stuck_loop)."
)
CLI_STUCK_LOOP_NOTICE = (
    "⚠️  Agent stopped early: repeated identical tool calls detected (stuck_loop)."
)
```

### 1-2. 순수 루프 감지기 모듈 신설 (`src/hermes_core/agent/loop_guard.py`)

외부 I/O나 전역 상태가 전혀 없는 순수 모듈 `src/hermes_core/agent/loop_guard.py`를 만든다.

1. **`make_fingerprint(func_name: str, parsed_args: Optional[dict[str, Any]], raw_args: Any = "") -> tuple[str, str]`**:
   - `name = str(func_name or "unknown")`
   - `if isinstance(parsed_args, dict):`
     - `canonical = json.dumps(parsed_args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
   - `else:`
     - `canonical = f"<invalid_json:{str(raw_args or '').strip()}>"`
   - `return (name, canonical)`
2. **`class LoopGuard`**:
   - `__init__(self, warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD, block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD) -> None`:
     - `if isinstance(warn_threshold, bool) or not isinstance(warn_threshold, int) or warn_threshold < 2:` `raise ValueError("warn_threshold must be an integer >= 2")`
     - `if isinstance(block_threshold, bool) or not isinstance(block_threshold, int) or block_threshold <= warn_threshold:` `raise ValueError("block_threshold must be an integer > warn_threshold")`
     - `self.warn_threshold = warn_threshold`
     - `self.block_threshold = block_threshold`
     - `self.last_fingerprint: Optional[tuple[str, str]] = None`
     - `self.consecutive_count: int = 0`
   - `record(self, func_name: str, parsed_args: Optional[dict[str, Any]], raw_args: Any = "") -> tuple[str, int]`:
     - `fp = make_fingerprint(func_name, parsed_args, raw_args)`
     - `if fp == self.last_fingerprint: self.consecutive_count += 1`
     - `else: self.last_fingerprint = fp; self.consecutive_count = 1`
     - `if self.consecutive_count >= self.block_threshold: return ("block", self.consecutive_count)`
     - `if self.consecutive_count >= self.warn_threshold: return ("warn", self.consecutive_count)`
     - `return ("ok", self.consecutive_count)`
3. **`attach_loop_warning(result_str: str, warning_message: str) -> str`**:
   - `result_str`을 `json.loads`로 파싱해 `isinstance(parsed, dict)`이면 `parsed["_loop_warning"] = warning_message`를 넣고 `json.dumps(parsed, ensure_ascii=False)`를 반환한다.
   - JSON 파싱에 실패하거나 `dict`가 아니면 `f"{result_str}\n{warning_message}"`를 반환한다.

### 1-3. 에이전트 루프 연동 ([`agent/agent.py`](../../../../src/hermes_core/agent/agent.py))

1. `constants.py`에서 `LOOP_GUARD_BLOCKED_MESSAGE`, `LOOP_GUARD_FINAL_RESPONSE`, `LOOP_GUARD_WARNING_MESSAGE`를, `hermes_core.agent.loop_guard`에서 `LoopGuard`, `attach_loop_warning`을 import한다.
2. `_execute_tool_calls` 시그니처와 동작:
   ```python
   def _execute_tool_calls(
       self,
       tool_calls: list[dict[str, Any]],
       messages: list[dict[str, Any]],
       loop_guard: Optional[LoopGuard] = None,
   ) -> bool:
   ```
   - `blocked = False`
   - 각 `tc`에 대해 `call_id`, `func_name`, `args_str`, `args, parse_error = _parse_tool_arguments(args_str)`를 구한다.
   - `if blocked:` (같은 응답 배치의 앞선 호출에서 이미 `block`이 걸린 경우):
     - **핸들러를 실행하지 않는다.**
     - `result_str = json.dumps({"success": False, "error": LOOP_GUARD_BLOCKED_MESSAGE.format(tool_name=func_name, count=loop_guard.consecutive_count if loop_guard else 0)}, ensure_ascii=False)`
   - `else:`
     - `status, count = loop_guard.record(func_name, args if parse_error is None else None, args_str) if loop_guard is not None else ("ok", 1)`
     - `if status == "block":`
       - `blocked = True`
       - **핸들러(`self.dispatcher.handle_function_call`)를 실행하지 않는다.**
       - `logger.warning("LoopGuard blocked repeated tool call '%s' (count=%d).", func_name, count)`
       - `result_str = json.dumps({"success": False, "error": LOOP_GUARD_BLOCKED_MESSAGE.format(tool_name=func_name, count=count)}, ensure_ascii=False)`
     - `else:` (`"ok"` 또는 `"warn"`):
       - 기존처럼 `parse_error is None`이면 `self.dispatcher.handle_function_call(func_name, args)`, 아니면 JSON 파싱 실패 문자열을 만든다.
       - `if status == "warn":`
         - `warning_msg = LOOP_GUARD_WARNING_MESSAGE.format(tool_name=func_name, count=count)`
         - `result_str = attach_loop_warning(result_str, warning_msg)`
   - **도구 쌍(Tool Pair) 불변식 보존 (위험 1)**:
     - `"ok"`, `"warn"`, `"block"` 어느 경우든 반드시 `messages.append({"role": "tool", "tool_call_id": call_id, "content": result_str})`와 `self.session_db.append_message(session_id=self.session_id, role="tool", content=result_str, tool_call_id=call_id, tool_name=func_name)`를 실행한다.
   - 루프가 끝나면 `return blocked`.
3. `run_conversation`:
   - `while iterations < self.max_iterations:` 진입 직전에 `loop_guard = LoopGuard()`를 생성한다(턴마다 새 인스턴스).
   - `if resp.tool_calls:` 분기에서:
     ```python
     if self._execute_tool_calls(resp.tool_calls, messages, loop_guard):
         final_response = LOOP_GUARD_FINAL_RESPONSE
         stopped_reason = "stuck_loop"
         break
     ```

### 1-4. CLI 정체 안내 출력 ([`cli.py`](../../../../src/hermes_core/cli.py))

1. `constants.py`에서 `CLI_STUCK_LOOP_NOTICE`를 import한다.
2. `run_conversation` 결과 출력 분기(`cli.py:479-485`)에 `elif result.get("stopped_reason") == "stuck_loop":`를 추가한다:
   ```python
   if result.get("stopped_reason") == "llm_error":
       err = result.get("error", {})
       kind = err.get("kind", "unknown")
       msg = err.get("message", "Unknown error")
       print(f"\nModel call failed ({kind}): {msg}")
   elif result.get("stopped_reason") == "stuck_loop":
       print(f"\n{CLI_STUCK_LOOP_NOTICE}")
   else:
       print(f"\nAssistant: {result['final_response']}")
   ```

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py`
- `src/hermes_core/agent/loop_guard.py` (신규)
- `src/hermes_core/agent/agent.py`
- `src/hermes_core/cli.py`
- `tests/test_hermes_core/test_loop_guard.py` (신규): 아래 시나리오 1~12
- `docs/로드맵/15-루프-정체-감지-자가-교정-MVP/README.md`: 020 상태와 실행 기록

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험 파일: `tests/test_hermes_core/test_loop_guard.py`(신규).

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `make_fingerprint("patch", {"path": "a.py", "old_string": "x"}, ...)`와 키 순서만 다른 `{"old_string": "x", "path": "a.py"}` | 두 지문이 100% 동일하다 (가정 D) |
| 2 | `make_fingerprint("patch", None, "{bad json")`와 `make_fingerprint("patch", None, "  {bad json  ")` | 두 지문이 `("patch", "<invalid_json:{bad json>")`로 동일하다 (가정 D) |
| 3 | `LoopGuard()`에 동일한 `(func_name, args)`를 5회 연속 `record` 호출 | 1·2회는 `("ok", 1)`, `("ok", 2)`, 3·4회는 `("warn", 3)`, `("warn", 4)`, 5회는 `("block", 5)`를 반환한다 |
| 4 | 동일 호출 2회 후 인자(또는 도구 이름)가 다른 호출 1회, 다시 원래 호출 2회 `record` | 중간의 다른 호출에서 `("ok", 1)`로 카운터가 리셋되어 이후 2회도 `("ok", 1)`, `("ok", 2)`이다 |
| 5 | `LoopGuard(warn_threshold=1, block_threshold=3)` 또는 `LoopGuard(warn_threshold=4, block_threshold=4)` 생성 | `ValueError`가 발생한다 |
| 6 | `attach_loop_warning('{"success": false, "error": "not found"}', "warn!")` 호출 | 반환 문자열이 유효한 JSON이고 `json.loads` 결과에 `"success": False`, `"error": "not found"`, `"_loop_warning": "warn!"`이 모두 들어 있다 (위험 5) |
| 7 | **3회 경고 후 자가 교정 완주 (가정 D)**: `MockLLMClient`가 똑같은 실패 `patch`를 3번 부른 뒤(3번째 도구 결과에 `_loop_warning` 포함 확인), 4번째에 다른 인자로 호출하고 5번째에 최종 응답을 반환 | `result["stopped_reason"] == "completed"`, `result["iterations"] == 5`이고, 4번째 LLM 호출에 전달된 3번째 `role="tool"` 메시지 JSON 안에 `"_loop_warning"`이 들어 있다 |
| 8 | **5회 연속 동일 호출 조기 차단 (가정 E)**: `max_iterations = 50`인 `HermesAgent`에서 `MockLLMClient`가 똑같은 도구·인자를 10번 큐에 넣어 둔 상태로 `run_conversation` 실행 (도구 핸들러 호출 횟수 카운터 부착) | 정확히 5번째 반복에서 멈춰 `result["stopped_reason"] == "stuck_loop"`, `result["iterations"] == 5`, `result["final_response"] == LOOP_GUARD_FINAL_RESPONSE`이고, **실제 도구 핸들러 실행 횟수는 4회**(5번째는 실행 생략)이다 |
| 9 | **도구 쌍(Tool Pair) 불변식 및 DB 정합성 (가정 F, 위험 1)**: 시나리오 8 직후 `result["messages"]`와 `session_db.get_messages_as_conversation(agent.session_id)` 검사 | 모든 `assistant(tool_calls)` 바로 뒤에 같은 `tool_call_id`를 가진 `role="tool"` 메시지가 1:1로 존재하며(5번째 `tool` 메시지 내용은 `LOOP_GUARD_BLOCKED_MESSAGE` 포함), 메모리 `messages[1:]`과 DB 활성 메시지가 일치한다 |
| 10 | **다중 `tool_calls` 배치 내 차단 시 짝 보존 (위험 1)**: 한 번의 응답에 `[tc1, tc2]` 2개의 `tool_calls`가 들어 있고 `tc1`에서 5회째 `"block"`이 발동한 경우 | `tc1`과 `tc2` 모두 핸들러는 실행되지 않지만, `tc1`과 `tc2` 각각의 `tool_call_id`에 대응하는 `role="tool"` 메시지 2개가 모두 `messages`와 DB에 기록된 뒤 `stuck_loop`으로 종료된다 |
| 11 | **턴 간 카운터 독립성**: 턴 1에서 동일 호출을 2회 하고 정상 종료한 뒤, 이어지는 턴 2에서 같은 호출을 2회 수행 | 턴 2는 새 `LoopGuard`로 시작하므로 경고나 차단 없이 `"ok"`로 실행된다 |
| 12 | **CLI `stuck_loop` 안내 출력**: CLI 루프에서 한 턴이 `stopped_reason == "stuck_loop"`으로 끝난 뒤 다음 프롬프트에서 `exit` 입력 | 표준 출력에 `CLI_STUCK_LOOP_NOTICE`가 출력되고 세션이 정상 종료(`user_exit`)된다 |

---

## 4. 검증 기준 (Done 조건)

- 시나리오 1~12 전부 통과.
- 전체 회귀 테스트 `-n 4` 및 `-m pty` 직렬 통과.
- 대시보드 020 `Done`, 실행 기록 갱신.
