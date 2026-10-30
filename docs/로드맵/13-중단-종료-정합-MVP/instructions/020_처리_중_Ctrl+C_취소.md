# [구현지시서 020] 처리 중 Ctrl+C 취소 (그 턴만 취소하고 프롬프트로 복귀, `history`·원장 유지, 취소된 턴에 실린 롤백 알림 되돌려 놓기)

> **상위 대시보드**: [13-README.md](../README.md)  
> **선행 조건**: 010(세션 종료 단일 지점) 완료.  
> **근거**: [ADR-0013](../../../adr/0013-중단_종료_정합_MVP_범위와_결정.md) §2-3·§2-4, [ROADMAP.md](../ROADMAP.md) §2-3·§2-5·§4(위험 1·2·3)·§6-2·§6-5, [WHY.md](../WHY.md) 가정 B

---

## 1. 구현 내용 명세

처리 중(`agent.run_conversation` 호출 구간) `Ctrl+C`를 누르면 트레이스백으로 끝나는 대신 **그 턴만 취소**한다. 세션은 이어지고 프롬프트로 돌아온다. 취소 알림을 모델에게 보내는 일은 030이 한다. 이 단계는 「안전하게 취소하고 돌아오기」만 만든다.

### 1-1. 취소 처리 ([`cli.py`](../../../../src/hermes_core/cli.py) `main`)

- 현재 코드의 `with hold_typeahead(): result = agent.run_conversation(...)` 블록 **전체를** `try: … except KeyboardInterrupt:`로 감싼다. `try`가 `with` 바깥에 있으므로 `hold_typeahead`가 먼저 끝나 터미널 상태가 복원된 뒤 안내가 출력된다.
- `except KeyboardInterrupt:` 안에서는 아래만 한다.
  1. 안내 한 줄 출력: `print(CLI_TURN_CANCELLED_NOTICE)` (앞에 빈 줄이 필요하면 `"\n" + …`)
  2. **취소된 턴에 붙였던 롤백 알림을 보류 목록에 되돌려 놓는다** (아래 1-2).
  3. `continue` (프롬프트로 복귀).
- **`history`를 건드리지 않는다.** `history`는 `run_conversation`이 정상으로 끝난 뒤에만 교체되므로 취소된 턴의 메시지는 자연히 들어가지 않는다(`agent.py:330`에서 `conversation_history`를 복사한 새 목록에만 이어 붙인다). 도구 호출과 결과의 짝이 깨지지 않는다(위험 1, 가정 B).
- **원장은 지우지 않는다.** 이미 기록된 user·assistant·tool 메시지와 `llm_calls`, `file_checkpoints`는 그대로 둔다(일어난 일의 증거).
- 보호 범위는 `run_conversation` 호출 구간뿐이다. 취소 직후 프롬프트에서 다시 `Ctrl+C`를 누르면 010대로 세션이 끝난다(`user_interrupt`). 안내를 출력하는 한 줄 안에서 두 번째 `Ctrl+C`가 들어오면 그 예외는 잡히지 않고 밖으로 나가 `error`로 기록된다. 이 틈은 막지 않는다(위험 3, 1-4 참조).
- `except Exception`으로 바꾸지 않는다. `KeyboardInterrupt`만 잡는다. 다른 예외는 010대로 밖으로 전파되어 `error`로 기록된다.
- 취소 뒤에는 `checkpoint_refusal_notified` 검사 블록(`result`를 쓰는 부분)을 건너뛴다(`continue`가 막아 준다). `result`가 아직 없기 때문이다.

### 1-2. 취소된 턴에 실린 롤백 알림을 되돌려 놓는다 ([`cli.py`](../../../../src/hermes_core/cli.py))

12번 규칙은 「호출 **전에** `pending_rollbacks`를 비운다」다(실패해도 두 번 보내지 않으려고). 그런데 취소된 턴의 메시지는 `history`에서 빠지므로, 그 메시지 앞에 붙었던 롤백 알림도 모델의 문맥에서 사라진다. 그대로 두면 모델이 되돌림을 영영 모른다.

- 비우기 직전에 `delivered = list(pending_rollbacks)`로 사본을 둔다(`pending_rollbacks.clear()` 앞).
- 취소 시 `pending_rollbacks[:0] = delivered`로 **앞에** 되돌려 놓는다. 그 사이 새로 쌓인 것이 있을 수 없으므로(처리 중에는 입력이 없다) 순서는 그대로다.
- 정상 종료·`LLMError` 경로는 바꾸지 않는다. 그 경로는 `history`가 `result["messages"]`로 교체되어 알림이 문맥에 남는다.

### 1-3. 안내 문구 ([`constants.py`](../../../../src/hermes_core/constants.py))

- `CLI_TURN_CANCELLED_NOTICE = "Request cancelled. It was dropped from the conversation; the session continues."` 상수를 `CLI_ROLLBACK_*` 근처에 추가한다.
- 030이 모델 알림을 붙이게 되면 이 문구를 확장한다(그때 기존 시험의 변경은 예고된 변경으로 지시서에 적는다).

### 1-4. 알려진 한계 (이 단계에서 고치지 않음)

- **연타**: 안내 출력 중에 두 번째 `Ctrl+C`가 들어오는 짧은 틈은 막지 않는다. 파이썬에서 완전히 막으려면 시그널 처리를 바꿔야 해서 MVP에 비해 크다. 그 경우 트레이스백과 함께 `error`로 기록된다.
- **취소된 턴 도중 압축이 일어난 경우**(코드로 확인한 사실, 시험 없음): 압축은 `run_conversation` 안의 **지역 `messages`** 목록만 교체하고, 원장은 한 트랜잭션으로 활성 행을 새 압축본으로 바꾼다(`agent.py:396-440`, `session_db.py:408`). 취소하면 CLI의 `history`는 압축 전 목록 그대로라, 다음 턴에 다시 압축되어 원장에 압축본이 한 번 더 쌓일 수 있다. 원장은 일관되고 비용만 중복된다. 060 스모크에서 관찰하고 마감 보고의 「미흡한 부분」에 적는다.
- **진행 중이던 모델 호출**: `llm_calls`는 호출이 끝난 뒤 기록되는 구조로 보여 취소된 호출은 남지 않을 수 있다(미확인, 060에서 원장으로 확인).
- **터미널 자식 프로세스**: `subprocess.run`이 `Ctrl+C` 때 자식을 정리하는지는 가정 F로 060에서 확인한다.
- 도구 실행 **중** `Ctrl+C`의 체크포인트 확정은 030이 시험으로 고정한다.

---

## 2. 변경할 파일 목록

- `src/hermes_core/cli.py`: 1-1, 1-2
- `src/hermes_core/constants.py`: 1-3
- `tests/test_hermes_core/test_cli_interrupt.py`: 010의 파일에 아래 시나리오 추가
- `docs/로드맵/13-중단-종료-정합-MVP/ROADMAP.md`: §4·§6 정정(이 지시서 작성과 같은 커밋에서 반영 완료)
- `docs/로드맵/13-중단-종료-정합-MVP/README.md`: 020 상태와 실행 기록

> **사전 점검**: `agent/`, `session_db.py`, `tools/`, `cli_input.py`는 수정하지 않는다. 기존 시험 변경은 0건이어야 한다. 착수 전 010이 `Done`이고 `_close_session`이 있는지 확인한다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험 파일: `tests/test_hermes_core/test_cli_interrupt.py`. 이 파일에 **자체 픽스처와 가짜 클라이언트**를 둔다.
- 환경 픽스처: `test_cli_rollback.py`의 `cli_test_env`와 같은 방식(`HERMES_HOME`, 가짜 API 키, `cli.OpenRouterClient` 교체, `monkeypatch.chdir(tmp_path)`). 010의 시나리오와 충돌하지 않는 별도 이름으로 둔다.
- `InterruptingClient`: `ScriptableClient`와 같은 모양(`chat_completion`, `call_history`, `set_call_recorder`)이되, 대본 항목이 **`BaseException` 인스턴스면 raise**한다(`ScriptableClient`는 `Exception`만 raise해서 `KeyboardInterrupt`를 못 낸다).
- 응답 대본은 `test_cli_rollback.py`의 `make_write_file_response`와 같은 모양으로 직접 만든다(`MockLLMResponse` 사용).
- 입력은 `ScriptableInput` 방식(항목이 예외 클래스·인스턴스면 raise)으로 가로챈다.
- 기대값은 문자열 리터럴로 단언한다.

| # | 입력 (사람 입력 순서 / 모델 대본) | 기대 결과 |
|---|---|---|
| 1 | 입력 `go` → `exit` / 모델: 첫 호출에서 `KeyboardInterrupt` | `cli.main()`이 예외 없이 끝난다. 출력에 `Request cancelled. It was dropped from the conversation; the session continues.`가 정확히 1번, `Traceback`은 없다. 모델 호출은 1회. 종료 사유 `end_reason == "user_exit"` |
| 2 | 입력 `hello` → `go` → `again` → `exit` / 모델: `hi` → `KeyboardInterrupt` → `ok` | 3번째 모델 호출이 받은 메시지의 (역할, 내용) 목록이 정확히 `[(system, <시스템 프롬프트>), (user, "hello"), (assistant, "hi"), (user, "again")]`이다. `go`는 어디에도 없다. (시스템 프롬프트 내용은 단언하지 않고 역할만 확인) |
| 3 | 2와 같음 | 원장 `messages`에 `go` user 행이 **남아 있다**(user 행 3개: `hello`·`go`·`again`). 취소는 원장을 지우지 않는다 |
| 4 | 입력 `go` → `exit` / 모델: `write_file`(`a.txt`, `x`) 호출 → 다음 호출에서 `KeyboardInterrupt` | `a.txt`가 존재한다. `file_checkpoints`의 `a.txt` 행 상태가 `applied`다(`pending` 아님). 세션은 정상 종료(`user_exit`) |
| 5 | 입력 `go` → `/rollback`(y) → `exit` / 모델: 4와 같음 | 취소된 턴의 변경도 `/rollback`으로 되돌아간다: `a.txt`가 삭제되고 행 상태가 `rolled_back`이다 |
| 6 | 입력 `go`(`new.txt` 만들기, 정상 종료) → `/rollback`(y) → `read it` → `again` → `exit` / 모델: `write_file` → `done` → `KeyboardInterrupt` → `ok` | `again`을 처리한 모델 호출의 마지막 user 메시지가 `[Rollback notice - added by the CLI, not typed by the user]`로 시작하고 `\n\nagain`으로 끝난다. 그 호출이 받은 메시지 어디에도 `read it`은 없다. 롤백 알림은 취소된 턴에서 소비되지 않고 되돌려진 것이다 |
| 7 | 입력 `go` → (프롬프트에서 `KeyboardInterrupt`) / 모델: 첫 호출에서 `KeyboardInterrupt` | 취소 안내 뒤 프롬프트로 돌아와, 거기서의 `Ctrl+C`가 `Exiting session...`과 함께 세션을 끝낸다. `end_reason == "user_interrupt"`. 예외가 밖으로 나오지 않는다 |
| 8 | `--no-checkpoints`로 입력 `go` → `exit` / 모델: 첫 호출에서 `KeyboardInterrupt` | 1과 같은 안내가 나오고 정상 종료한다(체크포인트 꺼짐 구성에서도 취소 가능) |
| 9 | 입력 `go` → `exit` / 모델: 첫 호출에서 `RuntimeError("boom")` | 취소가 아니라 예외다: `RuntimeError`가 `cli.main()` 밖으로 전파되고 `end_reason == "error"`다(`except KeyboardInterrupt`가 다른 예외를 삼키지 않는다). 010 시나리오 4와 다른 점은 `HermesAgent`가 아니라 모델 클라이언트에서 난다는 것이다 |

시나리오 6의 「받은 메시지」는 `InterruptingClient.call_history`에서 읽는다. 시나리오 4~6의 `file_checkpoints`는 `state.db`를 별도로 열어 `SELECT`로 조회한다(`test_cli_rollback.py`의 방식).

---

## 4. 검증 기준 (Done 조건)

- 시나리오 1~9 전부 통과.
- 기존 시험은 한 줄도 바꾸지 않는다(`git diff -- tests/`는 `test_cli_interrupt.py`만).
- 전체 회귀 통과: 착수 시점 기준선 + 신규 9건. `-n 4` 5회 연속, `-m pty` 직렬.
- `git diff -- src/`에 `cli.py`, `constants.py`만 포함. `agent.py`·`session_db.py` 변경 0.
- 대시보드 020 `Done`, 실행 기록.
