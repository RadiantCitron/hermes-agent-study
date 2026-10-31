# [구현지시서 030] CLI 취소 턴 DB 정합 및 `--resume` 배선 (`cli.py`, `constants.py`)

> **상위 대시보드**: [14-README.md](../README.md)  
> **선행 조건**: 020(체크포인터 및 에이전트 상태 복원) 완료  
> **근거**: [ADR-0014](../../../adr/0014-세션_재개_원장_정합_MVP_범위와_결정.md) §2-1~§2-6, [ROADMAP.md](../ROADMAP.md) §2-3·§2-5·§6-3, [WHY.md](../WHY.md) 가정 A~F

---

## 1. 구현 내용 명세

010·020에서 만든 원장 정합 및 상태 복원 부품을 [`cli.py`](../../../../src/hermes_core/cli.py)와 [`constants.py`](../../../../src/hermes_core/constants.py)에 배선한다.

### 1-1. 상수 추가 ([`constants.py`](../../../../src/hermes_core/constants.py))

CLI 안내 문구 구역에 아래 상수 4종을 추가한다. 값은 문자열 그대로다.

```python
CLI_RESUME_MOCK_ERROR = (
    "Error: --resume cannot be used with --mock (resume requires persistent state.db in real mode)."
)
CLI_RESUME_NOT_FOUND_ERROR = "Error: No resumable CLI session found{detail}."
CLI_RESUME_AMBIGUOUS_ERROR = (
    "Error: Session prefix '{prefix}' is ambiguous (matches: {matches})."
)
CLI_RESUMED_BANNER_NOTICE = (
    "♻️  Resumed session {session_id}... ({message_count} active messages, last turn: {turn_index})."
)
```

### 1-2. `--resume` 명령줄 인자 및 검증 ([`cli.py`](../../../../src/hermes_core/cli.py))

1. `argparse.ArgumentParser`에 `--resume` 인자를 추가한다(`--no-checkpoints` 바로 아래).
   ```python
   parser.add_argument(
       "--resume",
       nargs="?",
       const="latest",
       default=None,
       metavar="SESSION_ID",
       help="Resume the latest CLI session, or a session matching the given ID/prefix",
   )
   ```
2. `args.delegate and args.mock` 검사 바로 아래에 `--resume`과 `--mock` 혼용 검사를 둔다(D-02).
   ```python
   if args.resume is not None and args.mock:
       print(CLI_RESUME_MOCK_ERROR, file=sys.stderr)
       sys.exit(2)
   ```

### 1-3. 세션 탐색과 에이전트·체크포인터·`history` 복원 ([`cli.py`](../../../../src/hermes_core/cli.py))

1. `session_db = SessionDB()` 생성 직후, `session_id`를 결정할 때 `args.resume`을 확인한다.
   ```python
   is_resuming = args.resume is not None
   if is_resuming:
       target_arg = args.resume.strip()
       matches = session_db.find_resumable_session(target_arg)
       if not matches:
           detail = (
               ""
               if target_arg.lower() == "latest"
               else f" matching '{target_arg}'"
           )
           print(CLI_RESUME_NOT_FOUND_ERROR.format(detail=detail), file=sys.stderr)
           session_db.close()
           sys.exit(2)
       if len(matches) > 1:
           short_ids = ", ".join(m["id"][:8] for m in matches[:5])
           print(
               CLI_RESUME_AMBIGUOUS_ERROR.format(prefix=target_arg, matches=short_ids),
               file=sys.stderr,
           )
           session_db.close()
           sys.exit(2)
       session_id = matches[0]["id"]
   else:
       session_id = str(uuid.uuid4())
   ```
2. `checkpointer` 생성 직후, `is_resuming`이고 `checkpointer is not None`이면 `restored_turn = checkpointer.restore_from_db()`를 부른다(`checkpointer`가 `None`이거나 새 세션이면 `restored_turn = 0`).
3. `HermesAgent(...)`를 생성할 때 `resume=is_resuming`을 넘긴다.
4. 대화 루프 진입 전 초기 `history`를 설정할 때:
   ```python
   initial_history = agent.load_conversation_history() if is_resuming else None
   restored_msg_count = 0 if initial_history is None else max(0, len(initial_history) - 1)
   ```
   - 배너 출력 시 `is_resuming`이면 `print(CLI_RESUMED_BANNER_NOTICE.format(session_id=agent.session_id[:8], message_count=restored_msg_count, turn_index=restored_turn))`를 함께 출력한다.
   - 대화 루프 `try:` 안의 `history = None`을 `history = initial_history`로 바꾼다.

### 1-4. 처리 중 `Ctrl+C` 취소 시 원장 `active=0` 비활성화 ([`cli.py`](../../../../src/hermes_core/cli.py))

`run_conversation`을 부르는 `try:` 블록 바로 앞에서 해당 세션의 마지막 활성 메시지 ID를 기록하고, `except KeyboardInterrupt:` 안에서 `deactivate_messages_after`를 부른다(D-01).

```python
last_active_msg_id = agent.session_db.get_last_active_message_id(agent.session_id)
try:
    with hold_typeahead():
        result = agent.run_conversation(message, conversation_history=history)
        ...
except KeyboardInterrupt:
    print(f"\n{CLI_TURN_CANCELLED_NOTICE}")
    pending_notices[:0] = delivered
    try:
        agent.session_db.deactivate_messages_after(
            agent.session_id, last_active_msg_id
        )
    except Exception as e:
        logger.warning("deactivate_messages_after failed: %s", e)
    if checkpointer is not None:
        ...
```

- `deactivate_messages_after`에서 DB 예외가 나더라도 `logger.warning`으로 남기고 프롬프트 복귀와 취소 알림 수집을 막지 않는다.

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py`: 1-1 (`CLI_RESUME_*` 상수 4종)
- `src/hermes_core/cli.py`: 1-2 ~ 1-4 (`--resume` 인자·탐색·복원 배선, 취소 시 `deactivate_messages_after`)
- `tests/test_hermes_core/test_cli_resume.py` (신규): 아래 시나리오 R1~R12
- `docs/로드맵/14-세션-재개-원장-정합-MVP/README.md`: 030 상태와 실행 기록

> **사전 점검**: `session_db.py`, `agent/`, `tools/`는 수정하지 않는다. 기존 `test_cli_interrupt.py`와 `test_cli_rollback.py`가 한 줄도 수정 없이 그대로 통과해야 한다(기존 시험은 `get_messages`로 원장 전체 보존을 검사하므로 `active=0` 비활성화와 100% 호환됨).

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험 파일: `tests/test_hermes_core/test_cli_resume.py`(신규). `test_cli_interrupt.py`와 동일한 방식으로 임시 `HERMES_HOME`, 대본형 `InterruptingClient`, `ScriptableInput`을 사용한다.

| # | 입력 | 기대 결과 |
|---|---|---|
| R1 | 세션에서 턴 1(`"hello"` → `"hi"`) 정상 완료 후, 턴 2(`"go"`) 처리 도중 `write_file` 실행 뒤 두 번째 모델 호출에서 `KeyboardInterrupt` 취소 → `exit` | `state.db`에서 `get_messages_as_conversation(sid)`는 턴 1의 `[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]` 2건만 반환하고, `get_messages(sid)`는 취소된 턴 2의 `user`·`assistant`·`tool` 행까지 모두 담고 있으되 그 행들의 `active == 0`이다 (가정 A) |
| R2 | 첫 번째 `cli.main()`에서 `"my secret word is apple"` → `"got it"` 대화 후 `exit`. 두 번째 `cli.main()`을 `["--resume"]`으로 실행해 `"what was the word?"` → `"it was apple"` 후 `exit` | 두 번째 실행의 세션 ID가 첫 번째와 같고, 배너에 `Resumed session <id8>... (2 active messages, last turn: 1).`이 출력되며, 두 번째 실행의 모델 호출이 받은 `messages`에 첫 세션의 시스템 프롬프트·`"my secret word is apple"`·`"got it"`이 그대로 들어 있다 (가정 C) |
| R3 | R2의 첫 번째 실행은 `exit`(`user_exit`)로 끝났고, 두 번째 `--resume` 실행은 프롬프트에서 `KeyboardInterrupt`로 끝남 | 두 번째 실행 종료 후 `sessions.end_reason`이 `"user_interrupt"`로 갱신되어 있고 `ended_at`도 첫 번째 종료 시각보다 크다 (가정 F) |
| R4 | **세션 간 `/rollback` 및 모델 알림 (가정 D, 12 한계 1)**: 세션 1에서 `a.txt`(`"hello"`) 생성 후 `exit`. 세션 2를 `["--resume"]`으로 켜서 곧바로 `/rollback`(`y`) 실행 후 `"check file"` 입력 → `exit` | 세션 2에서 `a.txt`가 삭제되고, `"check file"` 모델 호출이 받은 마지막 user 메시지가 `[Rollback notice - added by the CLI, not typed by the user]`로 시작하며 `Turn 1:`과 `deleted: a.txt`, `\n\ncheck file`을 포함한다 |
| R5 | **세션 간 `turn_index` 연속성 (가정 E)**: 세션 1에서 턴 1로 `a.txt` 생성 후 `exit`. 세션 2(`--resume`)에서 새 요청으로 `b.txt` 생성 후 `/rollback`(`y`) 실행 → `exit` | 세션 2의 `b.txt` 체크포인트는 `turn_index == 2`로 기록되고, `/rollback`은 턴 2의 `b.txt`만 삭제하며 턴 1의 `a.txt`는 그대로 남아 있다 |
| R6 | **취소된 세션의 깨끗한 재개 (가정 A + C)**: 세션 1에서 턴 1(`"one"` → `"1"`) 완료, 턴 2(`"two"`, 도구 호출 중 `KeyboardInterrupt` 취소) 후 아무 말 없이 `exit`. 세션 2(`--resume`)에서 `"three"` 입력 → `exit` | 세션 2에서 `"three"` 모델 호출이 받은 대화 이력은 `[system, user("one"), assistant("1"), user("three")]`이며, 취소된 `"two"`나 짝이 깨진 `tool_calls`가 전혀 섞이지 않는다 |
| R7 | 세션 2개(`"1111aaaa-..."`, `"2222bbbb-..."`)가 있을 때 `["--resume", "1111"]`로 실행 | 더 오래된 세션이라도 접두사 `"1111"`과 일치하는 `"1111aaaa-..."` 세션이 정확히 재개된다 |
| R8 | 세션이 하나도 없는 상태에서 `["--resume"]` 실행 / 없는 접두사 `["--resume", "9999"]` 실행 | 둘 다 `SystemExit(2)`로 종료되고 `stderr`에 각각 `Error: No resumable CLI session found.`와 `Error: No resumable CLI session found matching '9999'.`가 출력된다 |
| R9 | 같은 접두사를 가진 두 세션(`"aaaa1111-..."`, `"aaaa2222-..."`)이 있을 때 `["--resume", "aaaa"]` 실행 | `SystemExit(2)`로 종료되고 `stderr`에 `Error: Session prefix 'aaaa' is ambiguous (matches: ...).`가 출력된다 |
| R10 | `["--mock", "--resume"]` 실행 | `SystemExit(2)`로 종료되고 `stderr`에 `Error: --resume cannot be used with --mock`가 출력된다 |
| R11 | `--no-checkpoints`와 `--resume`을 함께 사용하여 이전 세션 재개 후 대화 | 오류 없이 대화가 복원되고 배너에 `last turn: 0`이 표시되며 정상 종료된다 |
| R12 | `deactivate_messages_after`가 `RuntimeError("db locked")`를 던지도록 바꾼 상태에서 처리 중 `KeyboardInterrupt` 발생 | CLI가 죽지 않고 취소 안내 후 프롬프트로 정상 복귀하며 `caplog`에 경고가 남는다 |

---

## 4. 검증 기준 (Done 조건)

- 시나리오 R1~R12 전부 통과.
- 기존 시험(`test_cli_interrupt.py`, `test_cli_rollback.py` 등) 무수정 통과.
- 전체 회귀 테스트 `-n 4` 5회 연속 및 `-m pty` 직렬 통과.
- `git diff -- src/`에 `cli.py`, `constants.py`만 포함.
- 대시보드 030 `Done`, 실행 기록 갱신.
