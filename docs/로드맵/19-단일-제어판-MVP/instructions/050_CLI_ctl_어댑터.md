# [구현지시서 050] CLI /ctl 어댑터

> **상위 대시보드**: [19-README.md](../README.md)  
> **선행 조건**: 040(쓰기 명령, 턴 경계 반영, 감사 기록) `Done` (`bea153a`)  
> **근거**: [ADR-0019](../../../adr/0019-단일_제어판_MVP_범위와_결정.md) §2-2·§2-8·§2-9(D-02·D-08·D-09), [ROADMAP.md](../ROADMAP.md) §2-1·§2-3·§2-5·§3(050행)·§4(위험 6·7·8·9)·§5·§6-5, [WHY.md](../WHY.md) 가정 B·C·D·F·G·H·I, [040 지시서](040_쓰기_명령_턴_경계_반영_감사_기록.md) §5

---

## 0. 착수 전 점검 (코드 변경 전에 수행하고 결과를 대시보드 실행 기록에 남긴다)

1. **기준 코드 및 탐침 결과 대조** (기준: `main` `c75b28f`, `src`·`tests`는 `bea153a`와 같음): 아래 사실이 지금 코드와 같은지 연다. 다르면 멈추고 보고한다.
   - **탐침 1 (REPL 루프의 `/rollback` 분기 및 제어판 주입 상태)**:
     - [`src/hermes_core/cli.py`](../../../../src/hermes_core/cli.py) 390~395행에서 `panel = ControlPanel(agent, loaded, session_db, apply_settings_fn=apply_runtime_settings)`이 이미 생성되어 있다(040 구현).
     - REPL 루프(458~536행)의 479~484행:
       ```python
       tokens = user_input.split()
       if tokens and tokens[0].lower() == "/rollback":
           rollback_res = _handle_rollback(tokens, checkpointer)
           if rollback_res is not None:
               pending_notices.append(rollback_res)
           continue
       ```
       그리고 486행에 `panel.apply_pending()`이 위치해 있다.
     - 따라서 `/rollback` 분기 바로 다음(484행 뒤)에 `if tokens and tokens[0].lower() == "/ctl": _handle_ctl(tokens, panel); continue`를 추가하면, `/ctl` 명령 처리 후 `continue`로 루프를 다시 돌면서 모델 호출 없이 다음 사용자 입력을 기다린다. 사용자가 다음 일반 입력을 넣을 때 486행의 `panel.apply_pending()`이 불리면서 대기 중인 설정·확장 변경이 실제 에이전트에 반영된다(턴 경계 단일 반영 지점 준수, ROADMAP §4 위험 6).
   - **탐침 2 (`constants.py`의 `CLI_*` 상수 네이밍 및 안내 문구 규약)**:
     - [`src/hermes_core/constants.py`](../../../../src/hermes_core/constants.py) 245~275행에는 `CLI_ROLLBACK_*` 상수들이 정의되어 있다 (`CLI_ROLLBACK_USAGE_NOTICE`, `CLI_ROLLBACK_HEADER` 등).
     - 이와 일관되게 `CLI_CTL_*` 상수들(`CLI_CTL_USAGE_NOTICE`, `CLI_CTL_UNKNOWN_SUBCOMMAND`, `CLI_CTL_SET_ACCEPTED`, `CLI_CTL_ENABLE_ACCEPTED`, `CLI_CTL_DISABLE_ACCEPTED`, `CLI_CTL_REJECTED`)을 정의한다(ROADMAP §6-5).
   - **탐침 3 (문자열 설정값 파싱과 검증 거부 감사 연동)**:
     - [`src/hermes_core/control/settings.py`](../../../../src/hermes_core/control/settings.py) 224행의 `parse_setting_string(key, raw)`는 `SETTING_SPECS`의 타입(`bool`, `int`, `float`, `str`)에 맞추어 변환 및 유효성 검증을 수행한다.
     - 040 지시서 §5에서 정하기로 한 방식에 따라: 사용자가 `/ctl set <key> <value>`를 입력했을 때, `try: typed_value = parse_setting_string(key, raw_value)` 시도 후 `except ValueError:`이면 원본 문자열 `typed_value = raw_value`를 그대로 둔다. 그 후 `cmd = SetSetting(key, typed_value)`로 `panel.submit(cmd, source="cli")`를 호출한다.
       - 정상 런타임 설정이면 `accepted=True`가 되고 대기열에 쌓이며 `CLI_CTL_SET_ACCEPTED`가 출력된다.
       - `locked` 설정(`approval_enabled`, `checkpoints_enabled`)이거나 `startup` 전용(`model`, `loop_guard_warn_threshold` 등), 존재하지 않는 키(`unknown_key`), 또는 잘못된 값(`bad_value`)이면 `panel.submit` 내부의 `validate_command`가 거부(`accepted=False`)하고 `session_db.record_control_audit(result="rejected")`에 거부 사유를 선기록한 뒤 `SubmitResult(accepted=False, rejection=...)`를 반환한다.
       - `_handle_ctl`은 거부 사유 메시지를 `CLI_CTL_REJECTED` 형식으로 출력한다. 이로써 모든 비인가·부적합 시도에 대해 거부 감사 기록이 확실하게 남는다(WHY 가정 G, ADR D-09).
   - **탐침 4 (PTY 테스트 환경 및 모의 실행 모드 `--mock`)**:
     - [`src/hermes_core/cli.py`](../../../../src/hermes_core/cli.py) 444행은 `if args.mock and not sys.stdin.isatty():` 조건일 때만 비대화형 1턴 자동 구동 후 종료한다.
     - 가상 터미널(`pty.openpty()`) 환경에서는 `sys.stdin.isatty()`가 `True`이므로, `--mock` 플래그로 CLI를 실행하면 비대화형 조기 종료를 타지 않고 대화형 REPL 루프로 진입한다.
     - 따라서 실제 모델 호출(API 키 필요) 없이도 `--mock` 환경의 pty 세션에서 `/ctl status`, `/ctl set ...`, `exit` 흐름을 종단간으로 검증할 수 있다(4층위 검증 4단계 충족, ROADMAP §5·§6-5).
     - 또한 pty가 없는 환경 및 빠른 단위 테스트를 위해 `ScriptableInput`을 사용한 인메모리 CLI 테스트(`test_cli_control.py`)를 함께 구성한다.
2. **기준 시험 수 (2026-10-31 확인)**: `.venv/bin/pytest --co -q -m "not pty"` 1,131개, `-m pty` 10개, `tests/test_architecture` 34개, `tests/test_kernel` 34개, `tests/test_control` 36개.

---

## 1. 구현 내용 명세

사용자가 대화형 CLI(REPL) 환경에서 단일 제어판을 직관적으로 조작할 수 있도록 `/ctl` 슬래시 명령 어댑터를 추가한다. `/ctl status`를 통한 읽기 모델 조회, `/ctl set <key> <val>`을 통한 런타임 설정 변경 접수, `/ctl enable|disable <ext>`를 통한 확장 제어를 지원하며, 잘못된 명령이나 사용법 오류 시 친절한 안내를 출력하고 대화 턴이나 토큰을 소모하지 않는다. 모든 쓰기 명령은 040에서 구현된 `ControlPanel`의 선감사 및 턴 경계 반영 규약에 따라 다음 턴 시작 직전에 안전하게 적용된다.

### 1-1. `src/hermes_core/constants.py` (변경) — `CLI_CTL_*` 안내 상수 추가

기존 `CLI_ROLLBACK_*` 상수(245~275행) 뒤에 아래 상수들을 추가한다:

```python
# -----------------------------------------------------------------------------
# CLI /ctl (Control Panel) 메시지 상수 (19번 로드맵)
# -----------------------------------------------------------------------------
CLI_CTL_USAGE_NOTICE = (
    "Usage: /ctl <status|set <key> <value>|enable <extension>|disable <extension>>"
)
CLI_CTL_UNKNOWN_SUBCOMMAND = (
    "Unknown /ctl command '{subcommand}'. Usage: /ctl <status|set <key> <value>|enable <extension>|disable <extension>>"
)
CLI_CTL_SET_ACCEPTED = (
    "Setting '{key}' queued to change to {value} (will apply on next turn)."
)
CLI_CTL_ENABLE_ACCEPTED = (
    "Extension '{name}' queued to enable (will apply on next turn)."
)
CLI_CTL_DISABLE_ACCEPTED = (
    "Extension '{name}' queued to disable (will apply on next turn)."
)
CLI_CTL_REJECTED = "Command '{command}' rejected ({reason})."
```

### 1-2. `src/hermes_core/cli.py` (변경) — `_handle_ctl` 핸들러 및 REPL 분기 연결

- **import 추가**:
  - `from hermes_core.control import ControlPanel, DisableExtension, EnableExtension, SetSetting, parse_setting_string, render_snapshot`
  - `from hermes_core.constants import` 절에 `CLI_CTL_DISABLE_ACCEPTED`, `CLI_CTL_ENABLE_ACCEPTED`, `CLI_CTL_REJECTED`, `CLI_CTL_SET_ACCEPTED`, `CLI_CTL_UNKNOWN_SUBCOMMAND`, `CLI_CTL_USAGE_NOTICE` 추가.
- **`_handle_ctl` 함수 신설** (`_handle_rollback` 바로 뒤):
  ```python
  def _handle_ctl(tokens: list[str], panel: ControlPanel) -> None:
      """REPL의 /ctl 제어판 명령을 처리한다.
      
      하위 명령:
        - /ctl status: 현재 상태 스냅샷 출력
        - /ctl set <key> <value>: 런타임 설정 변경 대기열 적재
        - /ctl enable <ext>: 확장 활성화 대기열 적재
        - /ctl disable <ext>: 확장 비활성화 대기열 적재
      """
      if len(tokens) < 2:
          print(CLI_CTL_USAGE_NOTICE)
          return

      subcmd = tokens[1].lower()

      if subcmd == "status":
          if len(tokens) != 2:
              print(CLI_CTL_USAGE_NOTICE)
              return
          snapshot = panel.snapshot()
          print(render_snapshot(snapshot))
          return

      if subcmd == "set":
          if len(tokens) != 4:
              print(CLI_CTL_USAGE_NOTICE)
              return
          key = tokens[2]
          raw_val = tokens[3]
          try:
              typed_val = parse_setting_string(key, raw_val)
          except ValueError:
              typed_val = raw_val
          cmd = SetSetting(key, typed_val)
          result = panel.submit(cmd, source="cli")
          if result.accepted:
              print(CLI_CTL_SET_ACCEPTED.format(key=key, value=typed_val))
          else:
              reason = result.rejection.message if result.rejection else "Rejected"
              print(CLI_CTL_REJECTED.format(command=f"set {key}", reason=reason))
          return

      if subcmd == "enable":
          if len(tokens) != 3:
              print(CLI_CTL_USAGE_NOTICE)
              return
          ext_name = tokens[2]
          cmd = EnableExtension(ext_name)
          result = panel.submit(cmd, source="cli")
          if result.accepted:
              print(CLI_CTL_ENABLE_ACCEPTED.format(name=ext_name))
          else:
              reason = result.rejection.message if result.rejection else "Rejected"
              print(CLI_CTL_REJECTED.format(command=f"enable {ext_name}", reason=reason))
          return

      if subcmd == "disable":
          if len(tokens) != 3:
              print(CLI_CTL_USAGE_NOTICE)
              return
          ext_name = tokens[2]
          cmd = DisableExtension(ext_name)
          result = panel.submit(cmd, source="cli")
          if result.accepted:
              print(CLI_CTL_DISABLE_ACCEPTED.format(name=ext_name))
          else:
              reason = result.rejection.message if result.rejection else "Rejected"
              print(CLI_CTL_REJECTED.format(command=f"disable {ext_name}", reason=reason))
          return

      print(CLI_CTL_UNKNOWN_SUBCOMMAND.format(subcommand=tokens[1]))
  ```
- **REPL 루프 연결** (`cli.py` 480~485행 사이):
  ```python
              tokens = user_input.split()
              if tokens and tokens[0].lower() == "/rollback":
                  rollback_res = _handle_rollback(tokens, checkpointer)
                  if rollback_res is not None:
                      pending_notices.append(rollback_res)
                  continue

              if tokens and tokens[0].lower() == "/ctl":
                  _handle_ctl(tokens, panel)
                  continue

              panel.apply_pending()
  ```

### 1-3. 시험 구현

- **`tests/test_control/test_cli_control.py` (신규)**: 아래 §3-1의 시나리오 1~6.
  - `ScriptableInput`과 `--mock` 플래그를 사용하여 대화형 루프 전체 흐름, 터미널 출력(`capsys`), 모델 호출 0회 확인, 턴 경계 반영 시점 및 `session_db.list_control_audit` 기록을 검증한다.
- **`tests/test_control/test_cli_control_pty.py` (신규)**: 아래 §3-2의 시나리오 7.
  - `pty.openpty()` 가상 터미널 환경에서 `subprocess.Popen([sys.executable, "-m", "hermes_core.cli", "--mock"], ...)`로 실제 CLI 프로세스를 띄워 `/ctl status`, `/ctl set max_iterations 3`, `exit` 흐름을 검증한다(파일명 `_pty.py` 규칙에 따라 자동으로 `pty` 마커 부여).
- 그 밖의 기존 시험 파일(`tests/test_control/test_commands_and_panel.py`, `tests/test_control/test_extension_breaker.py`, `tests/test_control/test_settings.py`, `tests/test_control/test_snapshot.py`, `tests/test_hermes_core/*`, `tests/test_kernel/*`, `tests/test_architecture/*`)은 **한 줄도 수정하지 않는다**(WHY 가정 B).

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py` (§1-1, `CLI_CTL_*` 상수 6개 추가)
- `src/hermes_core/cli.py` (§1-2, `_handle_ctl` 함수 신설 및 REPL 루프 `/ctl` 분기 추가)
- `tests/test_control/test_cli_control.py` (신규, §1-3)
- `tests/test_control/test_cli_control_pty.py` (신규, §1-3)
- `docs/로드맵/19-단일-제어판-MVP/README.md`: 050 상태 `Draft` 및 실행 기록 갱신

위 목록 밖의 파일(특히 `src/hermes_core/kernel/`, `src/hermes_core/tools/`, `src/hermes_core/agent/`)은 바꾸지 않는다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

> **주의 (Zero Leakage 및 gitleaks 방어)**: 시험 코드 안에서 가짜 키 문자열을 만들 때는 파일에 키 모양 리터럴이 직접 적히지 않도록 `"sk-or-" + "v1-" + ("a" * 36)`처럼 런타임 문자열 결합으로만 만든다.

### 3-1. CLI `/ctl` 단위 및 대화 루프 검증 (`tests/test_control/test_cli_control.py`)

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | **`/ctl status` 실행 시 스냅샷 출력 및 모델 호출 0회 (ROADMAP §6-5 시나리오 1)**: `--mock` 모드 대화형 CLI에서 사용자 입력 `["/ctl status", "exit"]` 실행 | 터미널 출력에 `[Settings]`, `[Extensions]`, `max_iterations`, `context_window_tokens`, `compression`, `loop_guard`가 포함되어 출력됨. 모델 호출(LLM call)은 정확히 0회이며, 세션이 `exit`로 정상 종료됨 |
| 2 | **`/ctl set max_iterations 3` 접수 안내 및 다음 턴 자동 반영 (ROADMAP §6-5 시나리오 2)**: 사용자 입력 `["/ctl set max_iterations 3", "hello", "exit"]` 실행 | `/ctl set` 직후 터미널에 `CLI_CTL_SET_ACCEPTED` 안내가 출력됨. `/ctl set` 직후에는 에이전트의 `agent.max_iterations`가 여전히 50이었다가, `"hello"` 턴이 시작되는 순간 3으로 반영됨. 세션 종료 후 `session_db.list_control_audit`에 `set_setting`에 대해 `accepted`와 `applied` 2행이 남음 |
| 3 | **보안 locked 설정, startup 전용, 잘못된 값 거부 및 감사 기록 (ROADMAP §6-5 시나리오 3)**: 사용자 입력 `["/ctl set approval_enabled false", "/ctl set model gpt-4", "/ctl set max_iterations -1", "exit"]` 실행 | 세 입력 모두 `CLI_CTL_REJECTED` 형식의 거부 안내가 출력됨(`locked`, `startup_only`, `bad_value`). 에이전트 설정 값은 바뀌지 않으며, `session_db.list_control_audit`에 각각 `result="rejected"` 행 3개가 기록됨 |
| 4 | **`/ctl disable loop_guard` 및 `/ctl enable loop_guard` 턴 경계 반영 (ROADMAP §6-5 시나리오 4)**: 사용자 입력으로 (a) `/ctl disable loop_guard` 후 1턴 실행, (b) `/ctl enable loop_guard` 후 1턴 실행 | (a) 비활성화 접수 안내 출력 후, 다음 턴에서 `loop_guard`가 꺼져 동일 도구 반복 호출이 차단되지 않음. 감사 장부에 `disable_extension`의 `accepted`·`applied` 기록됨. (b) 활성화 접수 안내 출력 후, 다음 턴에서 `loop_guard`가 정상 재활성화됨 |
| 5 | **`/ctl` 단독 및 인자 부족·모르는 하위 명령 사용법 안내 (ROADMAP §6-5 시나리오 5)**: 사용자 입력 `["/ctl", "/ctl unknown_cmd", "/ctl set", "/ctl set key", "/ctl enable", "/ctl disable", "exit"]` 실행 | 모든 명령에 대해 예외나 크래시 없이 적절한 사용법 안내(`CLI_CTL_USAGE_NOTICE` 또는 `CLI_CTL_UNKNOWN_SUBCOMMAND`)가 출력되며 모델 호출은 0회 발생하고 REPL 프롬프트가 유지됨 |
| 6 | **`/rollback`과 `/ctl` 혼합 실행 및 기존 동작 무변경 (ROADMAP §6-5 시나리오 6)**: 도구를 실행하여 파일을 변경한 후, `/ctl status` 조회 → `/rollback` 실행 → 다음 턴 메시지 실행 | `/rollback` 대화형 확인(y/N) 및 파일 복원 기능이 기존과 완전히 동일하게 작동하며, 중간에 실행된 `/ctl` 명령이 rollback의 `pending_notices`나 장부 기록을 오염시키지 않음 |

### 3-2. 가상 터미널(PTY) 실제 CLI 프로세스 검증 (`tests/test_control/test_cli_control_pty.py`)

| # | 입력 | 기대 결과 |
|---|---|---|
| 7 | **가상 터미널(PTY) 환경에서 실제 CLI 프로세스 종단간 `/ctl` 동작 (ROADMAP §6-5 시나리오 7)**: `pty.openpty()` 가상 터미널 환경에서 subprocess로 `python -m hermes_core.cli --mock`를 실행하고, 터미널 프롬프트 대기 시 순차적으로 `/ctl status\n`, `/ctl set max_iterations 3\n`, `exit\n`을 전송 | 가상 터미널 stdout에 CLI 배너, 스냅샷 표(`[Settings]`, `[Extensions]`), `Setting 'max_iterations' queued to change to 3`, 그리고 `Session saved and closed. Goodbye!`가 수신되고 프로세스가 정상 종료(exit code 0)됨 |

### 3-3. 아키텍처 경계 및 전체 회귀

| # | 입력 | 기대 결과 |
|---|---|---|
| 8 | **R1~R6 의존 방향 규칙, 주석 해석, 커널·디스패처 diff 0줄 및 전체 회귀**: `git diff src/hermes_core/kernel/ src/hermes_core/tools/`, `.venv/bin/pytest -n 4 -q -m "not pty"`, `.venv/bin/pytest -m pty -q`, 그리고 `uv run --no-project --python 3.12`로 CLI 및 control 모듈 import 확인 | `kernel/`·`tools/` diff 0줄(WHY 가정 C), `test_dependency_rules.py` R1~R6 및 `test_annotations_resolve.py` 포함 전체 시험 통과, 3.12 import `ok`(WHY 가정 I) |

---

## 4. 검증 기준 (Done 조건)

- §0 점검 결과가 대시보드 실행 기록에 있다.
- 시나리오 1~8 통과.
- `git diff` 기준 `src/hermes_core/kernel/`·`src/hermes_core/tools/`·`src/hermes_core/agent/` 변경 0줄(WHY 가정 C).
- `git diff` 기준 모든 기존 시험 파일은 변경 0줄(WHY 가정 B).
- Python 3.12 import 확인 `ok`.
- 대시보드 050 상태 `Done`, 실행 기록 갱신. 커밋 제목 `feat(core): R19-050 - ...`.

---

## 5. 다음 지시서로 넘기는 관찰 (이 단계에서 처리하지 않음)

- **060 작동원리 문서 동기화**: 010~050을 통해 단일 제어판(`control/`), 설정 불변 스냅샷과 계층 로더, 읽기 모델 스냅샷, 확장 게이트와 회로 차단기, 감사 원장(DB v7), 턴 경계 지연 반영, CLI `/ctl` 어댑터가 모두 완성되었으므로, [03-코어구조-메인루프-확장방식.md](../../작동원리/03-코어구조-메인루프-확장방식.md)에 제어판 구조와 메인루프 턴 경계 반영 흐름을 동기화하고 새 편(05편) 분리 여부를 검토한다.
- **070 마감 종합보고서**: 18번 070 지시서를 형식 견본으로 삼아, 단일 제어판 MVP의 설계 의도와 검증 결과, 미흡한 부분과 후속 과제를 정리한다.

---

## 6. ROADMAP과 다른 점, 그리고 정한 것

| 항목 | 이전 서술 (ROADMAP §6-5, 040 지시서 §5) | 이 지시서의 결정 | 이유 |
|---|---|---|---|
| 문자열 설정값 파싱 및 검증 거부 감사 연동 (탐침 3) | 040 지시서 §5: 「`parse_setting_string`을 거쳐서 넘길지 그대로 넘길지는 050에서 결정」 | `parse_setting_string` 시도 후 `ValueError` 시 원본 값을 그대로 `SetSetting`에 담아 `panel.submit`에 전달 | `panel.submit` 내부의 `validate_command`가 `bad_value` 거부를 감지하여 `session_db.record_control_audit(result="rejected")`에 안전하게 선기록하므로, 잘못된 입력 시도도 감사 원장에 남길 수 있다 (WHY 가정 G, ADR D-09) |
| CLI /ctl 하위 명령 안내 상수 (탐침 2) | ROADMAP §6-5: 「새 상수 `CLI_CTL_*`」 | `CLI_CTL_USAGE_NOTICE`, `CLI_CTL_UNKNOWN_SUBCOMMAND`, `CLI_CTL_SET_ACCEPTED`, `CLI_CTL_ENABLE_ACCEPTED`, `CLI_CTL_DISABLE_ACCEPTED`, `CLI_CTL_REJECTED` 6개 정의 | 기존 `CLI_ROLLBACK_*`의 일관된 네이밍 규칙을 준수하고 사용자에게 명확한 상태(접수 및 다음 턴 반영 예고)를 안내하기 위함 |
| PTY 테스트 구성 방식 (탐침 4) | ROADMAP §6-5: 「어느 방식이 맞는지는 기존 `/rollback` 시험을 탐침해 정한다」 | 빠른 단위 검증은 `ScriptableInput` 기반의 `test_cli_control.py`로, 실제 터미널 I/O 검증은 `pty.openpty()` 기반의 `test_cli_control_pty.py`로 분리하여 2개 시험 파일로 작성 | 빠른 회귀(`-m "not pty"`)의 실행 속도(6~7초 유지)를 보장하면서도, 가상 터미널 환경(`-m pty`)에서의 실제 CLI subprocess 동작을 완벽히 검증하기 위함 |

---

## 7. 자체 검토 (ROADMAP.md §9 지침 준수)

1. **시험이 동어반복이 아닌가**:
   - `test_cli_control.py`와 `test_cli_control_pty.py`는 구현체의 내부 메서드를 직접 호출하는 것이 아니라, 실제 REPL 환경에 사용자 입력 문자열(`/ctl status`, `/ctl set ...`)을 주입하고 화면 출력 문자열(`capsys`, pty stdout), 에이전트 속성 변화, 감사 원장(DB) 기록, 모델 호출 횟수(0회)를 외부에서 독립적으로 관찰하여 검증한다.
2. **경계값이 작은 값에서도 성립하는가**:
   - 입력 토큰 수 경계(인자 없음, 1개, 2개, 3개, 4개 이상), 설정값 경계(`max_iterations=1`, `breaker_threshold=0`), 대기열 명령 수 경계(0개, 1개, 복수 개) 모두에서 정상 작동 및 예외 처리를 검증한다.
3. **실패·거부 뒤 반복 비용이 폭주하지 않는가**:
   - 잘못된 명령어나 거부되는 설정 변경을 수없이 반복 입력해도 에이전트 대화 턴이나 LLM 호출을 전혀 소모하지 않고 오직 터미널 안내 1줄 출력 후 `continue`하므로 비용이 폭주하지 않는다. 거부 감사 기록도 1회만 남는다.
4. **현재 코드 모양과 맞는가**:
   - `cli.py` 480행의 `tokens and tokens[0].lower() == "/rollback"` 바로 뒤에 `tokens and tokens[0].lower() == "/ctl"` 분기가 배치되고, 040에서 이미 생성해 둔 `panel = ControlPanel(...)` 및 486행의 `panel.apply_pending()`과 정확하게 맞물려 작동한다.
