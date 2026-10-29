# [구현지시서 020] `prompt_toolkit` 경로 (터미널 판정, Enter 전송, Ctrl+J 줄바꿈, 붙여넣기, 의존성)

> **상위 대시보드**: [09-README.md](../README.md)  
> **선행 조건**: 010 `Done` ([010 지시서](010_입력_함수_분리와_디코드_예외_방어.md))  
> **근거**: [ADR-0009](../../../adr/0009-CLI_입력_안정화_범위와_prompt_toolkit_도입.md) §2-3·§2-4·§2-5·§2-6, [ROADMAP.md](../ROADMAP.md) §2-4·§4(위험 1·2·5·6)·§6-2, [WHY.md](../WHY.md) 가정 A·C·D·E·G

---

## 1. 구현 내용 명세

`read_user_input`이 **stdin과 stdout이 모두 터미널일 때만** `prompt_toolkit`으로 읽게 한다. 그 밖의 입력(파이프, `io.StringIO`, 출력 리다이렉션)은 010의 `input()` 경로 그대로다. 호출하는 쪽([`cli.py`](../../../../src/hermes_core/cli.py))은 이미 `read_user_input()`을 부르므로 **이 단계에서 `cli.py`는 바꾸지 않는다**(배너 안내는 030).

1. **터미널 판정 함수** `is_interactive_terminal() -> bool` ([`cli_input.py`](../../../../src/hermes_core/cli_input.py)): `sys.stdin.isatty() and sys.stdout.isatty()`. 호출 시점에 `sys.stdin`·`sys.stdout`을 읽는다(시험이 바꿔 끼운 스트림을 따라간다). `isatty`가 없거나 `ValueError`(닫힌 스트림)·`AttributeError`를 내면 `False`다.
2. **터미널 경로** `read_with_prompt_toolkit(prompt, *, input=None, output=None) -> str`:
   - `prompt_toolkit`을 **이 함수 안에서** import한다(모듈 맨 위 import 금지: 라이브러리가 없어도 `hermes_core.cli_input` import가 실패하지 않게 한다).
   - `PromptSession(prompt, multiline=True, key_bindings=…, input=input, output=output).prompt()`의 결과를 가공 없이 돌려준다. `input`·`output`은 시험이 가짜 터미널(파이프 입력, 더미 출력)을 넣는 자리이며 기본 `None`은 실제 표준 입출력이다.
   - 키 설정: **Enter → 입력 전송**(`buffer.validate_and_handle()`), **Ctrl+J → 줄바꿈 삽입**(`insert_text("\n")`). 두 바인딩을 모두 명시한다. 이유: 한 줄 모드의 기본 Ctrl+J는 Enter와 같아 전송된다(임시 폴더 실험에서 `"a\nb\r"` 입력이 `"a"`로 끝남). `multiline=True` 기본 설정의 Enter는 전송이 아니라 줄바꿈으로 추정한다(같은 실험 중 스크립트가 입력 대기로 멈춤, 원인 분리는 안 함: **미확인**). 그래서 기본값에 기대지 않는다.
   - 붙여넣기: 라이브러리의 붙여넣기 감싸기(bracketed paste) 기본 처리를 그대로 쓴다. 붙여넣은 여러 줄은 전송 없이 한 입력에 들어간다. 별도 코드는 두지 않는다.
   - Ctrl+C·Ctrl+D는 기본 동작 그대로 `KeyboardInterrupt`·`EOFError`로 올라온다(재정의하지 않는다).
3. **`read_user_input` 구조 정리**: 010의 재시도 루프(`UnicodeDecodeError` 안내 후 재시도, 연속 5회 시 `EOFError`)는 **그대로 한 곳에 둔다.** 루프 안에서 한 번 읽는 일을 내부 함수 `_read_once(prompt)`로 빼고 다음처럼 고른다.
   - `is_interactive_terminal()`이 거짓 → `input(prompt)` (010과 같다).
   - 참 → `read_with_prompt_toolkit(prompt)`. **`prompt_toolkit` import가 `ImportError`면** `CLI_PROMPT_TOOLKIT_MISSING_NOTICE`를 **프로세스당 한 번만** 출력하고 `input(prompt)`로 읽는다(ADR-0009 §2-5의 이중 방어: 터미널 경로에서 올라온 `UnicodeDecodeError`도 같은 루프가 받는다). import 이후 실행 중 생긴 다른 예외는 폴백으로 삼키지 않고 그대로 올린다.
4. **상수** ([`constants.py`](../../../../src/hermes_core/constants.py), CLI 입력 절): `CLI_PROMPT_TOOLKIT_MISSING_NOTICE = "prompt_toolkit is not installed; falling back to basic input (no multi-line editing)."`
5. **의존성** ([`pyproject.toml`](../../../../pyproject.toml)): `dependencies`에 `"prompt_toolkit>=3.0.52,<4"` 추가(알파벳 순으로 `httpx` 뒤, `pydantic` 앞). 실행 환경 설치는 착수 때 `.venv/bin/python -m pip install "prompt_toolkit>=3.0.52,<4"`로 한다(운영·환경 작업이며 코드 변경이 아님을 보고에 구분해 적는다).
6. **미설치 알림의 프로세스당 1회**는 모듈 수준 플래그 하나로 구현한다. 시험은 이 플래그를 직접 되돌려 독립성을 확보한다.
7. **알려진 한계**:
   - 실제 터미널 앱에서의 화면 모양(줄바꿈 표시, 한글 폭, 붙여넣기 감싸기를 지원하지 않는 터미널)은 **미확인**이다. 이 단계는 가짜 터미널(파이프 입력·더미 출력)로 키 동작과 반환값만 검증하고, 화면은 050에서 사용자가 본다.
   - 이 단계가 끝나면 `cli.py`를 건드리지 않았어도 **실제 터미널에서 REPL이 곧바로 `prompt_toolkit`을 쓴다**(010에서 이미 `read_user_input`에 연결됨). 사용자 눈에 보이는 변화는 이 커밋부터다.
8. **작동원리·ROADMAP 갱신**: 작동원리 갱신은 없음(040에서 일괄). ROADMAP §2-1이 `test_cli_input.py`에 키 동작 시험을 둔다고 한 대로 같은 파일에 **덧붙인다**(기존 줄 삭제 0줄).

---

## 2. 변경할 파일 목록

- `src/hermes_core/cli_input.py` (수정: `is_interactive_terminal`, `read_with_prompt_toolkit`, `_read_once`, 알림 1회 플래그)
- `src/hermes_core/constants.py` (수정: 상수 1개)
- `pyproject.toml` (수정: 의존성 1줄)
- `tests/test_hermes_core/test_cli_input.py` (수정: 시나리오 덧붙임, 삭제 0줄)
- `docs/로드맵/09-CLI-입력-안정화/README.md`, `docs/인계.md` (갱신)

> **사전 점검 (착수 시)**: ① 전체 테스트를 먼저 실행해 기준선을 기록한다(010 마감 시 578건). ② `.venv`에 `prompt_toolkit`이 없음을 확인하고(`import prompt_toolkit` 실패, 지시서 작성 시점 기준) 위 명령으로 설치한 뒤 버전을 기록한다(지시서 작성 때 임시 폴더에서 3.0.53으로 실험함). ③ `cli_input.py`에 `prompt_toolkit` import가 없는지, `cli.py`에 `input(` 직접 호출이 없는지 확인한다. ④ `constants.py`에 같은 이름의 상수가 없는지 확인한다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

터미널 경로 시험(7~13)은 `prompt_toolkit.input.create_pipe_input()`과 `prompt_toolkit.output.DummyOutput()`을 `read_with_prompt_toolkit`의 `input`·`output`에 넣어 **실제 라이브러리**로 실행한다(모의 객체로 라이브러리를 대체하지 않는다). 키는 바이트로 보낸다: Enter `\r`, Ctrl+J `\n`, 백스페이스 `\x7f`, Ctrl+C `\x03`, Ctrl+D `\x04`, 붙여넣기 `\x1b[200~…\x1b[201~`. 판정·분기 시험(1~6, 14~15)은 `sys.stdin`·`sys.stdout`을 가짜로 끼우고 `read_with_prompt_toolkit`만 대체한다. 기대값은 요구(ADR-0009 §2-4)에서 나온 것이며 구현 결과를 보고 쓰지 않았다.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | stdin·stdout 모두 `isatty() == True`인 가짜 | `is_interactive_terminal()` == `True` |
| 2 | stdin만 tty, stdout은 아님 | `False` |
| 3 | stdout만 tty, stdin은 아님 | `False` |
| 4 | `isatty` 메서드가 없는 stdin / `isatty()`가 `ValueError`를 내는 stdin | 각각 `False` (예외가 밖으로 나오지 않음) |
| 5 | tty 아님(판정 거짓), `read_with_prompt_toolkit`을 호출되면 실패하는 함수로 대체, 입력원 `"hello\n"` | `read_user_input()` == `"hello"`, 대체 함수는 호출되지 않음 (비터미널은 `input()` 경로) |
| 6 | 판정 참으로 고정, `read_with_prompt_toolkit`을 `"  뜻  "`을 돌려주는 기록용 함수로 대체, `builtins.input`은 호출되면 실패하게 대체 | 반환값 `"  뜻  "` 그대로(가공 없음), 기록된 프롬프트 인자 `"User > "`, `input`은 호출되지 않음 |
| 7 | 터미널 경로, 키 `"hello\r"` | `"hello"` |
| 8 | 터미널 경로, 키 `"a\nb\r"` (Ctrl+J로 줄바꿈 후 Enter) | `"a\nb"` (Ctrl+J는 전송이 아니라 줄바꿈) |
| 9 | 터미널 경로, `"\x1b[200~l1\nl2\r\nl3\x1b[201~\r"` (여러 줄 붙여넣기 후 Enter) | `"l1\nl2\nl3"` (붙여넣기 안의 줄바꿈은 전송이 아니며 한 입력으로 들어옴, `\r\n`은 `\n`) |
| 10 | 터미널 경로, `"한글ab\x7f\r"` (한글과 영문을 치고 백스페이스) | `"한글a"` |
| 11 | 터미널 경로, `"\r"` (빈 입력 Enter) | `""` |
| 12 | 터미널 경로, `"x\x03"` (Ctrl+C) | `KeyboardInterrupt`가 그대로 올라옴 |
| 13 | 터미널 경로, `"\x04"` (빈 줄에서 Ctrl+D) | `EOFError`가 그대로 올라옴 |
| 14 | 판정 참으로 고정, `read_with_prompt_toolkit`이 `[UnicodeDecodeError, "ok"]`를 차례로 내도록 대체 | `read_user_input()` == `"ok"`, 안내 `CLI_INPUT_DECODE_NOTICE`가 정확히 1번 출력 (터미널 경로에서도 010의 루프가 적용). 또 항상 `UnicodeDecodeError`면 안내가 `CLI_INPUT_DECODE_MAX_RETRIES`번 출력된 뒤 `EOFError` |
| 15 | 판정 참으로 고정, `sys.modules["prompt_toolkit"] = None`(import 실패 모사), 입력원 `StringIO("a\nb\n")`로 `read_user_input()` 2번 호출, 미설치 알림 플래그는 시험 시작 때 초기값으로 되돌림 | 두 반환값 `"a"`, `"b"` (`input()`으로 폴백), `CLI_PROMPT_TOOLKIT_MISSING_NOTICE`가 출력에 정확히 1번 |
| 16 | 프로세스를 새로 띄워(`subprocess`, `PYTHONPATH`에 `src`를 줌) `import hermes_core.cli_input` 후 `"prompt_toolkit" in sys.modules` 출력 | `False` (import만으로 라이브러리가 로드되지 않음, 가정 G) |
| 17 | `pyproject.toml`을 `tomllib`로 읽음 | `project.dependencies`에 `"prompt_toolkit>=3.0.52,<4"`가 있음 |
| 18 | 기존 시험 전체(010의 시나리오 1~11 포함) | 한 줄도 바꾸지 않고 통과 |

---

## 4. 검증 기준 (Done 조건)

- 위 시나리오 1~18 통과, 전체 회귀 테스트 통과(기준선 대비 신규만 증가).
- `git diff -- src/`가 `cli_input.py`, `constants.py`만 포함한다. **`cli.py`·`agent/`·`tools/`·`session_db.py`·`llm_client.py`는 변경 없음**(가정 G).
- 기존 시험 파일에 삭제 0줄, `test_cli.py`·`test_cli_delegate.py`·`test_cli_skills_index.py` 무수정.
- `cli_input.py` 맨 위 import에 `prompt_toolkit`이 없음(`grep`).
- 대시보드 020 상태 `Done`, 실행 기록 행 추가(커밋 칸은 `feat(core): R09-020` 접두부, 010 행의 커밋 칸에 실제 해시를 채움), [인계](../../../인계.md) 갱신, 커밋 1개. `pyproject.toml`과 `.venv` 설치는 같은 커밋 보고에 「운영·환경 작업」으로 구분해 적는다.

---

## 5. 위험·주의

- **시험이 라이브러리를 거울처럼 되비추지 않게**: 7~13의 기대는 키의 의미(Enter=전송, Ctrl+J=줄바꿈, 붙여넣기=한 입력, 백스페이스=한 글자 삭제)에서 나왔다. 실제 라이브러리 결과가 다르면 시나리오를 바꾸지 말고 멈춰서 보고한다. 지시서 작성 때 같은 키 입력을 임시 폴더에서 실행해 7~13이 성립함을 확인했다(참고용이며 시나리오의 근거가 아님).
- **`multiline=True`에는 두 바인딩이 모두 필요하다**: Enter 바인딩이 빠지면 입력이 끝나지 않아 시험이 멈출 수 있다. 키 시험은 입력 끝에 반드시 Enter(`\r`)를 보내고, 멈추면 시나리오를 바꾸지 말고 중단 후 보고한다.
- **가짜 터미널 ≠ 실제 터미널**: `DummyOutput`은 화면을 그리지 않는다. 한글 폭 계산, 줄바꿈 표시, `User >` 사라짐(가정 F), 붙여넣기 감싸기 미지원 터미널의 동작은 이 단계에서 **미확인**이며 040(pty)·050(사람)에서 본다.
- **환경 의존**: 시험 중 stdin·stdout이 우연히 tty여도 시나리오 5·15는 `sys.stdin`을 바꿔 끼워 영향받지 않는다. 터미널 경로 시험은 판정 함수를 거치지 않고 `read_with_prompt_toolkit`을 직접 호출한다.
- **의존성 지연 import의 대가**: import 오류가 첫 입력 때 드러난다. 그 대신 설치되지 않은 환경에서도 CLI가 뜨고 폴백한다. 알림은 프로세스당 한 번이라 반복 출력으로 화면을 어지럽히지 않는다.
- **`ImportError` 폴백 범위**: import 실패만 폴백한다. 실행 중 예외(예: 터미널 초기화 오류)는 삼키지 않고 올린다. 그런 오류가 실제 터미널에서 나는지는 **미확인**이며 050에서 사용자가 확인한다.
- **재시도 상한 5·알림 문구**는 010·이 지시서에서 에이전트가 정한 안이다. 해당 단계 착수(「진행해라」) 시 승인으로 갈음한다.
- **030과의 경계**: 이 단계 후 `cli.py`는 이미 새 경로를 쓴다. 030의 남은 일은 배너 안내 한 줄과 기존 CLI 시험 회귀 확인이다. 030 지시서 작성 때 이 범위를 코드와 다시 대조한다.
