# [구현지시서 010] Settings 불변 스냅샷과 계층 로더, 조립 지점 연결, R6 경계 시험

> **상위 대시보드**: [19-README.md](../README.md)  
> **선행 조건**: 로드맵 18 마감(`Closed`, 태그 `roadmap-18-closed`), 로드맵 19 계획 문서(`ADR-0019`·`PLAN.md`·`WHY.md`·`ROADMAP.md`) 완료  
> **근거**: [ADR-0019](../../../adr/0019-단일_제어판_MVP_범위와_결정.md) §2-2~§2-4(D-02·D-03·D-04), [ROADMAP.md](../ROADMAP.md) §2-1·§2-3·§2-4·§3(010행)·§4(위험 4·5·10)·§6-1, [WHY.md](../WHY.md) 가정 A·B·C·D·I

---

## 0. 착수 전 점검 (코드 변경 전에 수행하고 결과를 대시보드 실행 기록에 남긴다)

1. **기준 코드 대조** (기준: `main` `a7ddadc`, `src`·`tests`는 `a972b78`과 같음): 아래 사실이 지금 코드와 같은지 연다. 다르면 멈추고 보고한다.
   - [`src/hermes_core/agent/compression_hook.py`](../../../../src/hermes_core/agent/compression_hook.py)의 `CompressionExtension.__init__(self, config: Callable[[], CompressionConfig])`(56행)는 `self._config = config`만 저장하고, `install(self, host)`(59행)는 `host.add_turn_hook(BEFORE_LLM, self._before_llm)`만 부른다. 즉 설치 시점에는 `self._config()`를 **부르지 않고**, 턴 중 `_before_llm`(63행)에서 비로소 부른다. 따라서 `create_agent`에서 에이전트 생성 전 늦게 묶는 클로저(`lambda: compression_config_of(agent)`)를 넘겨도 설치 중 실패하지 않는다(탐침 1 확인 완료).
   - [`src/hermes_core/agent/loop_guard.py`](../../../../src/hermes_core/agent/loop_guard.py)의 `LoopGuard.__init__`(50~63행)은 `warn_threshold < 2`이면 `ValueError("warn_threshold must be an integer >= 2")`를, `block_threshold <= warn_threshold`이면 `ValueError("block_threshold must be an integer > warn_threshold")`를 낸다. `LoopGuardExtension.__init__`(120행)은 `warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD`(3), `block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD`(5)를 받아 저장만 하고, 첫 도구 호출 때 `LoopGuard`를 만들며 위 검사가 실행된다.
   - [`src/hermes_core/assembly.py`](../../../../src/hermes_core/assembly.py)에는 `compression_config_of`, `default_extensions`, `usage_ledger_middleware`, `default_llm_middlewares`, `connect_call_recorder` 5개 함수가 있고, `hermes_core.agent.agent`는 import하지 않는다(순환 방지).
   - [`src/hermes_core/cli.py`](../../../../src/hermes_core/cli.py)의 `main()`(202행)은 CLI 옵션 8개(`--mock`, `--model`, `--context-window`, `--delegate`, `--no-skills-index`, `--no-approval`, `--no-checkpoints`, `--resume`)를 파싱하고, `--delegate`+`--mock`(247행)·`--resume`+`--mock`(254행)·`--context-window < 1`(258행)을 검사해 실패 시 세션 DB를 열기 전에 종료 코드 2로 끝낸다. 364~374행에서 `HermesAgent(...)`를 직접 생성한다.
   - [`tests/test_architecture/test_dependency_rules.py`](../../../../tests/test_architecture/test_dependency_rules.py)는 `ast.walk`로 모듈 전체(함수 내부·`TYPE_CHECKING` 블록 포함)의 import를 수집해 R1~R5를 검사한다.
   - [`tests/test_architecture/test_annotations_resolve.py`](../../../../tests/test_architecture/test_annotations_resolve.py)는 `hermes_core` 하위 모든 모듈을 import해 모든 함수·메서드에 `typing.get_type_hints()`를 런타임 실행하며, `ALLOWED_UNRESOLVED`는 빈 사전(`{}`)이다.
2. **환경변수 및 소비자 전수 (2026-10-31 확인값과 대조)**: 다르면 멈추고 보고한다.
   - 셸 환경의 `HERMES_*` 환경변수: 없음(탐침 2 확인 완료).
   - 기존 시험(`tests/`)에서 설정하는 `HERMES_*` 환경변수: `HERMES_HOME`(경로 지정용)과 `HERMES_TEST_KEEP`(`test_terminal_approval.py` 621·668행)뿐이며, Settings 항목 키와 겹치는 `HERMES_<KEY>`는 0건이다.
   - `default_extensions(` 호출처: `src/hermes_core/agent/agent.py` 1곳, `tests/test_hermes_core/test_assembly.py` 2곳, `tests/test_architecture/test_assembly_boundary.py` 1곳(모두 위치 인자 1개 `default_extensions(fn)`로 호출).
   - `cli.HermesAgent` 참조처: `tests/test_hermes_core/test_cli.py` 447·453행(`cli.HermesAgent.__init__` 패치), `tests/test_hermes_core/test_cli_interrupt.py` 152행(`cli.HermesAgent.run_conversation` 패치).
3. **기준 시험 수**: `.venv/bin/pytest --co -q -m "not pty"` 1,094개, `-m pty` 10개, `tests/test_architecture` 33개, `tests/test_kernel` 34개(2026-10-31 확인).

---

## 1. 구현 내용 명세

흩어진 설정 값을 계층(기본값 < `$HERMES_HOME/config.toml` < 환경변수 `HERMES_<KEY>` < CLI 옵션)으로 합쳐 동결 스냅샷(`Settings`)과 출처 기록(`LoadedSettings`)으로 만드는 `hermes_core.control.settings`를 신설한다. 조립 지점(`assembly.py`)에 `create_agent(settings, **parts)`를 두고 `cli.py`가 이를 통해 에이전트를 만들게 연결하며, `control` 패키지의 의존 경계 규칙 R6을 아키텍처 시험에 추가한다. 설정 파일·환경변수·CLI 옵션을 주지 않았을 때 에이전트의 모든 속성 값과 동작은 지금과 같아야 한다(WHY 가정 A·B).

### 1-1. `src/hermes_core/control/__init__.py`, `src/hermes_core/control/settings.py` (신규)

- **패키지 의존 규칙**: `hermes_core.control` 하위 모듈은 **표준 라이브러리**, `hermes_core.constants`, `hermes_core.control.*`만 import한다. `hermes_core.agent`·`hermes_core.kernel`·`hermes_core.tools`·`hermes_core.session_db`·`hermes_core.assembly`·`hermes_core.cli`는 런타임이든 `TYPE_CHECKING`이든 import하지 않는다(R6 양방향 및 `test_annotations_resolve.py` 정합).
- **`control/__init__.py`**: 패키지 docstring(「단일 제어판(Control Panel) 패키지. 이 패키지를 import할 수 있는 곳은 assembly와 cli뿐이다(R6)」)을 두고, `settings.py`의 공개 이름(`Settings`, `SettingSpec`, `LoadedSettings`, `SettingsError`, `SETTING_SPECS`, `load_settings`, `Mutability`, `SettingSource`)을 재수출(`__all__`)한다.
- **타입과 예외 (`control/settings.py`)**:
  - `Mutability = Literal["runtime", "startup", "locked"]`
  - `SettingSource = Literal["default", "file", "env", "cli"]`
  - `class SettingsError(ValueError)`:
    - 속성: `layer: str`(`"file" | "env" | "cli"`), `key: str`, `reason: str`.
    - `def __init__(self, layer: str, key: str, reason: str) -> None`: 속성을 저장하고 `super().__init__(f"Invalid setting '{key}' in {layer}: {reason}")`를 부른다.
  - `@dataclass(frozen=True) class SettingSpec`:
    - 필드: `key: str`, `type: type`(`str`, `int`, `float`, `bool`), `default: Any`, `mutability: Mutability`, `validator: Callable[[Any], Any]`, `nullable: bool = False`.
  - `@dataclass(frozen=True) class Settings`: 아래 12개 필드를 순서대로 가진다(기본값은 표와 같음).
  - `@dataclass(frozen=True) class LoadedSettings`:
    - 필드: `settings: Settings`, `sources: Mapping[str, SettingSource]`, `file_path: Optional[Path] = None`.
    - `__post_init__`에서 `object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))`로 감싸 `sources` 사전도 변경할 수 없게 한다.

- **설정 항목 정본 표 (`SETTING_SPECS: Mapping[str, SettingSpec]`, `MappingProxyType`으로 동결)**:

| 키 (`key`) | `type` | `nullable` | 기본값 (`default`) | `mutability` | 검증 규칙 (`validator`) |
|---|---|---|---|---|---|
| `model` | `str` | `True` | `None` | `"startup"` | `None`이거나, `isinstance(v, str)`이고 `v.strip() != ""`여야 함(앞뒤 공백 제거 문자열이 비어 있으면 거부). 반환값은 `v` 그대로 |
| `max_iterations` | `int` | `False` | `50` | `"runtime"` | `not isinstance(v, bool) and isinstance(v, int) and v >= 1` |
| `context_window_tokens` | `int` | `False` | `CONTEXT_WINDOW_TOKENS` (`1_048_576`) | `"runtime"` | `not isinstance(v, bool) and isinstance(v, int) and v >= 1` |
| `compression_enabled` | `bool` | `False` | `True` | `"runtime"` | `isinstance(v, bool)` |
| `compression_threshold_ratio` | `float` | `False` | `COMPRESSION_THRESHOLD_RATIO` (`0.5`) | `"runtime"` | `not isinstance(v, bool) and isinstance(v, (int, float))`이고 `math.isfinite(float(v))`이며 `0.0 < float(v) <= 1.0`. 반환값은 `float(v)` |
| `loop_guard_warn_threshold` | `int` | `False` | `LOOP_GUARD_WARN_THRESHOLD` (`3`) | `"startup"` | `not isinstance(v, bool) and isinstance(v, int) and v >= 2` (`LoopGuard.__init__`의 `>= 2` 계약과 일치, §6) |
| `loop_guard_block_threshold` | `int` | `False` | `LOOP_GUARD_BLOCK_THRESHOLD` (`5`) | `"startup"` | 단일 필드 검사는 `not isinstance(v, bool) and isinstance(v, int) and v >= 3`, 교차 검사는 `block > warn` |
| `skills_index` | `bool` | `False` | `True` | `"startup"` | `isinstance(v, bool)` |
| `delegate` | `bool` | `False` | `False` | `"startup"` | `isinstance(v, bool)` |
| `approval_enabled` | `bool` | `False` | `True` | `"locked"` | `isinstance(v, bool)` |
| `checkpoints_enabled` | `bool` | `False` | `True` | `"locked"` | `isinstance(v, bool)` |
| `breaker_threshold` | `int` | `False` | `3` | `"runtime"` | `not isinstance(v, bool) and isinstance(v, int) and v >= 0` (`0` = 꺼짐) |

- **환경변수·문자열 해석 규칙 (`parse_env_value(spec: SettingSpec, raw: str) -> Any` 또는 내부 도우미)**:
  - `spec.type is bool`: `raw.strip().lower()`가 `("1", "true", "yes")`이면 `True`, `("0", "false", "no")`이면 `False`, 그 밖은 실패.
  - `spec.type is int`: `raw.strip()`을 `int(s, 10)`으로 변환(실패 시 거부)한 뒤 `spec.validator` 적용.
  - `spec.type is float`: `raw.strip()`을 `float(s)`로 변환(실패 시 거부)한 뒤 `spec.validator` 적용.
  - `spec.type is str`: `raw` 문자열에 `spec.validator` 적용(`""`나 공백만 있는 문자열은 거부).

- **계층 로더 (`load_settings`)**:
  ```python
  def load_settings(
      *,
      cli_overrides: Optional[Mapping[str, Any]] = None,
      environ: Optional[Mapping[str, str]] = None,
      config_path: Optional[Path] = None,
  ) -> LoadedSettings:
  ```
  1. **기본값(`"default"`)**: 모든 키를 `spec.default`로, `sources[key] = "default"`로 초기화한다.
  2. **파일(`"file"`)**:
     - `config_path`가 주어지면 `Path(config_path)`를 쓴다. `None`이면: `environ`이 명시적으로 주어졌고 그 안에 비어 있지 않은 `"HERMES_HOME"`이 있으면 `Path(environ["HERMES_HOME"]).expanduser().resolve() / "config.toml"`, 아니면 `get_hermes_home() / "config.toml"`을 쓴다.
     - 해당 경로에 파일이 없으면(`not target_path.exists()`) 파일 계층을 건너뛰고 `loaded_file_path = None`으로 둔다.
     - 경로가 존재하지만 일반 파일이 아니거나(`not target_path.is_file()`), 읽기 실패·`tomllib.TOMLDecodeError`가 나면 `SettingsError("file", "<toml>", str(exc))`를 던진다.
     - 파싱된 사전의 각 `(k, v)`에 대해:
       - `isinstance(v, dict)`이면 중첩 테이블이므로 `SettingsError("file", k, "nested tables are not allowed")`를 던진다(평평한 키만 허용).
       - `k not in SETTING_SPECS`이면 `SettingsError("file", k, "unknown setting key")`를 던진다.
       - `SETTING_SPECS[k].validator(v)`로 검증해 실패하면 `SettingsError("file", k, ...)`를 던지고, 통과하면 `values[k]`와 `sources[k] = "file"`을 갱신한다.
     - 파일이 정상 로드되면 `loaded_file_path = target_path.resolve()`다.
  3. **환경변수(`"env"`)**:
     - `env_map = os.environ if environ is None else environ`.
     - `SETTING_SPECS`의 각 키 `k`에 대해 `env_key = f"HERMES_{k.upper()}"`가 `env_map`에 있는지 본다(`HERMES_HOME`, `HERMES_TEST_KEEP` 등 `SETTING_SPECS`에 없는 `HERMES_*` 환경변수는 무시한다 — §0-2).
     - 있으면 문자열을 해석·검증하여 실패 시 `SettingsError("env", k, ...)`를 던지고, 통과하면 `values[k]`와 `sources[k] = "env"`를 갱신한다.
  4. **CLI(`"cli"`)**:
     - `cli_overrides`가 주어지면 각 `(k, v)`에 대해:
       - `k not in SETTING_SPECS`이면 `SettingsError("cli", k, "unknown setting key")`를 던진다.
       - `v is None`이면 **사용자가 CLI에서 주지 않은 옵션**이므로 건너뛴다(이전 계층의 값과 출처를 유지).
       - `v is not None`이면 `SETTING_SPECS[k].validator(v)`로 검증하여 실패 시 `SettingsError("cli", k, ...)`를 던지고, 통과하면 `values[k]`와 `sources[k] = "cli"`를 갱신한다.
  5. **교차 검증 (`loop_guard_block_threshold > loop_guard_warn_threshold`)**:
     - `values["loop_guard_block_threshold"] <= values["loop_guard_warn_threshold"]`이면:
       - 책임 계층(`err_layer`)은 `sources["loop_guard_block_threshold"] != "default"`이면 `sources["loop_guard_block_threshold"]`, 아니면 `sources["loop_guard_warn_threshold"]`로 정한다.
       - `SettingsError(err_layer, "loop_guard_block_threshold", "loop_guard_block_threshold must be > loop_guard_warn_threshold")`를 던진다.
  6. `LoadedSettings(settings=Settings(**values), sources=sources, file_path=loaded_file_path)`를 반환한다.

### 1-2. `src/hermes_core/assembly.py` (변경) — `default_extensions` 임계 인자와 `create_agent`

- import 추가: `from hermes_core.constants import LOOP_GUARD_BLOCK_THRESHOLD, LOOP_GUARD_WARN_THRESHOLD`, `from hermes_core.control.settings import Settings`. `hermes_core.agent.agent`는 모듈 최상위에서 import하지 않는다(순환 방지).
- `default_extensions` 시그니처 확장:
  ```python
  def default_extensions(
      compression_config: Callable[[], CompressionConfig],
      *,
      loop_guard_warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD,
      loop_guard_block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD,
  ) -> list[Extension]:
  ```
  `LoopGuardExtension(warn_threshold=loop_guard_warn_threshold, block_threshold=loop_guard_block_threshold)`로 생성한다. 키워드 전용 기본값이 지금 상수이므로 기존 호출 4곳(`default_extensions(fn)`)은 한 줄도 바꾸지 않고 그대로 동작한다.
- `create_agent(settings: Settings, **parts: Any) -> Any` 신설:
  - 함수 안에서 `from hermes_core.agent.agent import HermesAgent`를 지연 import한다(모듈 최상위에서는 import하지 않아 `import hermes_core.agent.agent`가 먼저 실행될 때의 순환을 막고, 반환 타입 주석은 `Any`로 두어 `test_annotations_resolve.py`를 만족한다).
  - `kwargs: dict[str, Any]`에 `settings`로부터 에이전트 생성자 인자를 채운다:
    - `"model": settings.model`
    - `"max_iterations": settings.max_iterations`
    - `"context_window_tokens": settings.context_window_tokens`
    - `"compression_threshold_ratio": settings.compression_threshold_ratio`
    - `"compression_enabled": settings.compression_enabled`
    - `"skills_index": settings.skills_index`
  - `kwargs.update(parts)`로 호출자가 넘긴 부품(`llm_client`, `session_id`, `session_db`, `memory_store`, `dispatcher`, `resume`, 또는 실제 모드에서 해석된 `model` 등)을 덮어쓴다.
  - `parts`에 `"extensions"`가 없거나 `parts["extensions"] is None`이면:
    - 늦게 묶는 클로저(`agent: Any = None` → `lambda: compression_config_of(agent)`)와 `settings.loop_guard_warn_threshold`·`settings.loop_guard_block_threshold`로 `default_extensions(...)`를 만들어 `kwargs["extensions"]`에 넣는다.
  - `agent = HermesAgent(**kwargs)`를 생성해 반환한다.

### 1-3. `src/hermes_core/cli.py` (변경) — `load_settings`와 `create_agent` 연결

- import 추가: `from hermes_core.assembly import create_agent`, `from hermes_core.control.settings import SettingsError, load_settings`.
- **유지할 이름**: `from hermes_core.agent import HermesAgent, MockLLMClient`는 그대로 남긴다(`test_cli.py`와 `test_cli_interrupt.py`가 `cli.HermesAgent`를 패치함 — §0-2).
- `argparse` 정의(203~245행):
  - `--context-window`의 `default=CONTEXT_WINDOW_TOKENS`를 `default=None`으로 바꾼다(`help=f"Context window token limit (default: {CONTEXT_WINDOW_TOKENS})"` 문구는 그대로 유지).
  - 나머지 옵션 이름·플래그·도움말은 그대로 둔다.
- 설정 로드 및 검증(247~265행 부근, `SessionDB()` 및 `OpenRouterClient` 생성 **전**):
  - 기존 `if args.delegate and args.mock:`(종료 코드 2)와 `if args.resume is not None and args.mock:`(종료 코드 2) 검사는 그대로 둔다.
  - `cli_overrides: dict[str, Any] = {}`를 구성한다:
    - `if args.model is not None: cli_overrides["model"] = args.model`
    - `if args.context_window is not None: cli_overrides["context_window_tokens"] = args.context_window`
    - `if args.delegate: cli_overrides["delegate"] = True`
    - `if args.no_skills_index: cli_overrides["skills_index"] = False`
    - `if args.no_approval: cli_overrides["approval_enabled"] = False`
    - `if args.no_checkpoints: cli_overrides["checkpoints_enabled"] = False`
  - `loaded = load_settings(cli_overrides=cli_overrides)`를 부른다. `SettingsError`가 나면 표준 오류(`sys.stderr`)에 `Error: ...` 안내를 출력하고 `sys.exit(2)`로 끝낸다(세션 DB가 생성되기 전이므로 `test_cli.py` 시나리오 25가 그대로 성립한다). `settings = loaded.settings`.
  - 만약 `settings.delegate and args.mock`이고 `--delegate`를 CLI로 준 경우(위에서 이미 걸러짐) 외에, 파일·환경변수로 `delegate=True`가 들어왔더라도 아래 `if not args.mock and settings.delegate:`에서 실제 모드일 때만 위임 도구가 등록된다.
- 모델·보안·위임·에이전트 생성 연결:
  - `model = settings.model`로 시작하고, 실제 모드(`else:` 분기)에서 `if model is None: model = DEFAULT_REAL_MODEL`로 정해 `OpenRouterClient(api_key=api_key, model=model)`에 넘긴다(지금 265·304~307행과 동일).
  - 309행: `approval_enabled = settings.approval_enabled and is_interactive_terminal()`
  - 348~351행: `checkpointer = Checkpointer(session_db, session_id, Path.cwd()) if settings.checkpoints_enabled else None`
  - 364~374행: `HermesAgent(...)` 직접 호출을 `agent = create_agent(settings, model=model, llm_client=llm_client, session_id=session_id, session_db=session_db, memory_store=memory_store, dispatcher=dispatcher, resume=is_resuming)`로 바꾼다.
  - 376행·405행: `args.delegate` → `settings.delegate`
  - 407행: `if not settings.skills_index:`
  - 409행: `if not settings.approval_enabled:`
  - 413행: `if not settings.checkpoints_enabled:`

### 1-4. 의존 방향 규칙 R6 (`tests/test_architecture/test_dependency_rules.py` 변경)

- ADR-0019 D-02와 ROADMAP §4 위험 10·§6-1·§9-6에 따라 R6을 양방향으로 정의한다:
  1. **R6 유입(inbound)**: `target == "hermes_core.control"` 또는 `target.startswith("hermes_core.control.")`일 때, `importer`가 `"hermes_core.assembly"`, `"hermes_core.cli"`, `"hermes_core.control"`, 또는 `"hermes_core.control."`로 시작하지 않으면 `(importer, target, "R6")` 위반이다.
  2. **R6 유출(outbound)**: `importer == "hermes_core.control"` 또는 `importer.startswith("hermes_core.control.")`일 때, `target`이 표준 라이브러리(`_is_stdlib(root_pkg)`), `"hermes_core.constants"`(하위 포함), `"hermes_core.control"`(하위 포함) 중 어느 것도 아니면 `(importer, target, "R6")` 위반이다.
- `find_violations`의 docstring과 시나리오 10의 실패 메시지 안 표기를 `R1~R5` → `R1~R6`으로 갱신하고, R6 단위 검증 시험 함수를 추가한다. `KNOWN_DEBT`는 바꾸지 않는다.

### 1-5. 시험

- **`tests/test_control/__init__.py`, `tests/test_control/test_settings.py` (신규)**: 시나리오 1~11.
- **`tests/test_architecture/test_dependency_rules.py` (§1-4 범위만 변경)**: 시나리오 12·13.
- 기존 시험 파일(`tests/test_hermes_core/*`, `tests/test_kernel/*`, `tests/test_architecture/`의 나머지 6개 파일)은 **한 줄도 수정하지 않는다**(WHY 가정 B).

---

## 2. 변경할 파일 목록

- `src/hermes_core/control/__init__.py` (신규)
- `src/hermes_core/control/settings.py` (신규)
- `src/hermes_core/assembly.py` (§1-2)
- `src/hermes_core/cli.py` (§1-3)
- `tests/test_control/__init__.py` (신규)
- `tests/test_control/test_settings.py` (신규)
- `tests/test_architecture/test_dependency_rules.py` (§1-4의 범위만)
- `docs/로드맵/19-단일-제어판-MVP/ROADMAP.md`: §6-1의 확정 내용 반영(`loop_guard_warn_threshold` `int >= 2`, R6 유입·유출 양방향 확정, 탐침 결과 반영)
- `docs/로드맵/19-단일-제어판-MVP/README.md`: 010 상태 `Done`, 실행 기록 갱신, 직전 행 커밋 해시 채움

위 목록 밖의 파일(특히 `kernel/`, `tools/`, `agent/agent.py`, `agent/compression_hook.py`, `agent/loop_guard.py`, `constants.py`, `session_db.py`, 기존 `tests/test_hermes_core/` 파일들)은 바꾸지 않는다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

### 3-1. 설정 스냅샷·계층 로더·검증 (`tests/test_control/test_settings.py`)

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | **아무 계층도 없음(기본값과 출처)**: 존재하지 않는 `config_path=tmp_path / "no.toml"`, `environ={}`, `cli_overrides={}`로 `load_settings` 호출 | `loaded.file_path is None`. `loaded.settings == Settings()`(12개 필드 모두 §1-1 표의 기본값: `model=None`, `max_iterations=50`, `context_window_tokens=1_048_576`, `compression_enabled=True`, `compression_threshold_ratio=0.5`, `loop_guard_warn_threshold=3`, `loop_guard_block_threshold=5`, `skills_index=True`, `delegate=False`, `approval_enabled=True`, `checkpoints_enabled=True`, `breaker_threshold=3`). 12개 키 모두 `loaded.sources[k] == "default"` |
| 2 | **4계층 우선순위와 출처 추적**: `config.toml`에 `max_iterations = 10\ncontext_window_tokens = 4000\nmodel = "from-file"\n`, `environ={"HERMES_CONTEXT_WINDOW_TOKENS": "8000", "HERMES_MODEL": "from-env"}`, `cli_overrides={"model": "from-cli", "delegate": None}` | `settings.max_iterations == 10`(`sources["max_iterations"] == "file"`), `settings.context_window_tokens == 8000`(`sources["context_window_tokens"] == "env"`), `settings.model == "from-cli"`(`sources["model"] == "cli"`), `settings.delegate is False`(`sources["delegate"] == "default"` — `None`인 CLI 값은 덮지 않음). `loaded.file_path == config_path.resolve()` |
| 3 | **모르는 키·중첩 테이블·문법 오류 거부**: (a) 파일에 `unknown_opt = 1`, (b) `cli_overrides={"no_such": 1}`, (c) 파일에 `[section]\nmax_iterations = 5`, (d) 파일에 깨진 TOML `max_iterations =` | 모두 `SettingsError` 발생. (a) `err.layer == "file"`, `err.key == "unknown_opt"`, (b) `err.layer == "cli"`, `err.key == "no_such"`, (c) `err.layer == "file"`, `err.key == "section"`, (d) `err.layer == "file"` |
| 4 | **경계값(작은 값 포함) 및 범위 밖 값 검증**: (a) 유효한 최소 경계값: `max_iterations=1`, `context_window_tokens=1`, `compression_threshold_ratio=0.01` 및 `1.0`, `loop_guard_warn_threshold=2`, `loop_guard_block_threshold=3`, `breaker_threshold=0`, (b) 무효값: `max_iterations=0`, `max_iterations=True`(불리언을 정수로 위장), `context_window_tokens=0`, `compression_threshold_ratio=0.0`, `compression_threshold_ratio=1.5`, `compression_threshold_ratio=float("nan")`, `loop_guard_warn_threshold=1`, `loop_guard_block_threshold=3`(warn=3일 때 `block <= warn`), `breaker_threshold=-1`, `model=""`, `model="   "` | (a) 모두 정상 로드. (b) 각각 해당 `layer`와 `key`를 가진 `SettingsError` 발생 |
| 5 | **교차 검증 계층 귀속**: (a) 파일에 `loop_guard_warn_threshold = 4`(기본 block=5이므로 파일 단독으로는 유효) + 환경변수 `HERMES_LOOP_GUARD_BLOCK_THRESHOLD="4"` → 실패, (b) 파일에 `loop_guard_warn_threshold = 6`(기본 block=5) → 실패 | 둘 다 `SettingsError`이고 `err.key == "loop_guard_block_threshold"`. (a) `err.layer == "env"`, (b) `err.layer == "file"` |
| 6 | **환경변수 불리언·숫자 해석 및 무관한 `HERMES_*` 무시**: (a) `HERMES_COMPRESSION_ENABLED`에 `"1"`, `"true"`, `"TRUE"`, `"yes"` → `True`; `"0"`, `"false"`, `"NO"` → `False`, (b) `"maybe"`, `""` → `SettingsError(layer="env", key="compression_enabled")`, (c) `HERMES_MAX_ITERATIONS="abc"` → `SettingsError(layer="env", key="max_iterations")`, (d) `environ={"HERMES_HOME": "/tmp/x", "HERMES_TEST_KEEP": "keep"}` → 오류 없이 통과 | (a)~(d) 기대 결과와 일치 |
| 7 | **동결(불변) 보장**: `loaded = load_settings(environ={}, config_path=tmp_path / "no.toml")` 후 (a) `loaded.settings.max_iterations = 99`, (b) `loaded.file_path = None`, (c) `loaded.sources["model"] = "cli"`, (d) `SETTING_SPECS["model"] = ...` | (a)(b) `dataclasses.FrozenInstanceError`, (c)(d) `TypeError` |

### 3-2. 조립 지점과 CLI 연결 (`tests/test_control/test_settings.py`)

| # | 입력 | 기대 결과 |
|---|---|---|
| 8 | **`create_agent` 기본 설정 및 늦게 묶인 압축 설정**: `settings = load_settings(environ={}, config_path=tmp_path / "no.toml").settings`, `agent = create_agent(settings, session_db=db, quiet_mode=True, skip_memory=True, skip_context_files=True)` 생성 후, 설치된 `CompressionExtension`의 `_config()`를 호출하고, 이어서 `agent.context_window_tokens = 2048`, `agent.compression_enabled = False`로 바꾼 뒤 다시 `_config()` 호출 | 생성 직후 `agent.max_iterations == 50`, `agent.context_window_tokens == 1_048_576`, `agent.skills_index is True`, `[r.name for r in agent.extensions] == ["compression", "loop_guard"]`. 첫 `_config()`는 `context_window_tokens == 1_048_576, enabled is True`, 속성 변경 뒤 둘째 `_config()`는 `context_window_tokens == 2048, enabled is False`(에이전트 현재 속성을 동적으로 반영) |
| 9 | **`create_agent`의 작은 경계값 동작 (동어반복 방지)**: (a) `Settings(max_iterations=2)`로 만든 에이전트에 매번 도구 호출만 돌려주는 `MockLLMClient`로 1턴 실행, (b) `Settings(loop_guard_warn_threshold=2, loop_guard_block_threshold=3, max_iterations=10)`으로 만든 에이전트에 같은 도구 호출(`echo`, `{"x": 1}`)을 반복하는 `MockLLMClient`로 1턴 실행 | (a) `result["stopped_reason"] == "max_iterations"`, `result["iterations"] == 2`. (b) 2번째 도구 결과 메시지에 `_loop_warning`이 붙고, 3번째에서 차단되어 `result["stopped_reason"] == "stuck_loop"`, `result["iterations"] == 3` |
| 10 | **CLI `--mock` 기본 실행과 설정 파일·환경변수 반영**: `HERMES_HOME`을 임시 폴더로 지정하고 (a) 아무 파일·옵션 없이 `cli.main()`(`--mock`), (b) `$HERMES_HOME/config.toml`에 `max_iterations = 7\ncontext_window_tokens = 4096\n`을 쓰고 `cli.main()`(`--mock`), (c) 같은 파일이 있는 상태에서 `--mock --context-window 2048`로 `cli.main()` | `cli.HermesAgent.__init__`에서 포착한 에이전트가 (a) `max_iterations == 50`, `context_window_tokens == 1_048_576`, `skills_index is True`, (b) `max_iterations == 7`, `context_window_tokens == 4096`, (c) `max_iterations == 7`, `context_window_tokens == 2048`(CLI가 파일을 덮음) |
| 11 | **CLI 잘못된 설정 시 DB 미생성 및 종료 코드 2**: `HERMES_HOME`의 `config.toml`에 `max_iterations = 0`(또는 모르는 키 `bad_key = 1`)을 쓰고 `cli.main()`(`--mock`) 실행 | `SystemExit` 종료 코드 `2`, 표준 오류에 `max_iterations`(또는 `bad_key`) 포함, `not (hermes_home / "state.db").exists()`(폭주·부수 효과 없음) |

### 3-3. 의존 방향 규칙 R6 (`tests/test_architecture/test_dependency_rules.py`)

| # | 입력 | 기대 결과 |
|---|---|---|
| 12 | **R6 단위 검증 (유입·유출 양방향)**: `find_violations`에 (a) `{"hermes_core.agent.agent": {"hermes_core.control.settings"}}` (유입 위반), (b) `{"hermes_core.tools.dispatcher": {"hermes_core.control"}}` (유입 위반), (c) `{"hermes_core.session_db": {"hermes_core.control.settings"}}` (유입 위반), (d) `{"hermes_core.assembly": {"hermes_core.control.settings"}, "hermes_core.cli": {"hermes_core.control.settings"}, "hermes_core.control": {"hermes_core.control.settings"}}` (유입 허용), (e) `{"hermes_core.control.settings": {"hermes_core.agent.agent"}}` (유출 위반), (f) `{"hermes_core.control.settings": {"hermes_core.assembly"}}` (유출 위반), (g) `{"hermes_core.control.settings": {"tomllib", "hermes_core.constants", "hermes_core.control"}}` (유출 허용) | (a) `{("hermes_core.agent.agent", "hermes_core.control.settings", "R6")}`, (b) `{("hermes_core.tools.dispatcher", "hermes_core.control", "R6")}`, (c) `{("hermes_core.session_db", "hermes_core.control.settings", "R6")}`, (d) `set()`, (e) `{("hermes_core.control.settings", "hermes_core.agent.agent", "R6")}`, (f) `{("hermes_core.control.settings", "hermes_core.assembly", "R6")}`, (g) `set()` |
| 13 | **실제 소스 트리 R1~R6 통과**: `test_scenario_10_actual_tree_ratchet_enforcement` 실행 | 통과(새 위반 0, `KNOWN_DEBT` 4건 그대로). `all_imports`에 `hermes_core.control.settings`가 존재하고, 이를 import하는 모듈은 `hermes_core.control`·`hermes_core.assembly`·`hermes_core.cli`뿐임 |

### 3-4. 회귀

| # | 입력 | 기대 결과 |
|---|---|---|
| 14 | **기존 코어 시험 무수정 통과** (가정 A·B): `tests/test_hermes_core/` 전체(특히 `test_cli.py` 시나리오 24·25, `test_cli_delegate.py`, `test_cli_skills_index.py`, `test_cli_approval.py`, `test_cli_rollback.py`, `test_cli_resume.py`, `test_assembly.py`) | 한 줄도 수정하지 않고 전부 통과 |
| 15 | **커널·아키텍처 시험 통과** (가정 C·D): `tests/test_kernel/`, `tests/test_architecture/`(`test_annotations_resolve.py`가 새 `control` 모듈을 포함해 `ALLOWED_UNRESOLVED == {}`로 통과, `test_assembly_boundary.py` 시나리오 13~16 통과) | 전부 통과 |
| 16 | **전체 스위트 및 Python 3.12 확인** (가정 I): `.venv/bin/pytest -n 4 -q -m "not pty"`, `.venv/bin/pytest -m pty -q`, 그리고 `uv run --no-project --python 3.12`로 `hermes_core.control.settings` 포함 import 확인 | 전부 통과, 3.12 import `ok` |

---

## 4. 검증 기준 (Done 조건)

- §0 점검 결과가 대시보드 실행 기록에 있다.
- 시나리오 1~16 통과.
- `git diff` 기준 `src/hermes_core/kernel/`·`src/hermes_core/tools/`·`src/hermes_core/agent/` 변경 0줄(WHY 가정 B·C).
- `git diff` 기준 `tests/test_hermes_core/`·`tests/test_kernel/` 변경 0줄, `tests/test_architecture/`는 `test_dependency_rules.py`의 §1-4 범위만 변경(WHY 가정 B).
- Python 3.12 import 확인 `ok`.
- `ROADMAP.md` §6-1이 이 지시서 §6의 확정 내용과 맞게 갱신되어 있다.
- 대시보드 010 상태 `Done`, 실행 기록 갱신, 직전 행 커밋 해시 채움. 커밋 제목 `feat(core): R19-010 - ...`.

---

## 5. 다음 지시서로 넘기는 관찰 (이 단계에서 처리하지 않음)

- **`breaker_threshold`의 소비자 연결 시점**: `Settings.breaker_threshold`(기본 3)는 이 단계에서 `Settings`와 `SETTING_SPECS`에 정의·검증되지만, `HermesAgent.set_extension_breaker`는 030에서 만들어지므로 `assembly.create_agent`가 `agent.set_extension_breaker(settings.breaker_threshold)`를 부르는 연결은 030에서 한다(ROADMAP §6-1·§6-3).
- **`cli.py`의 `LoadedSettings` 보유**: 010에서는 `cli.py`가 `loaded = load_settings(...)`에서 `loaded.settings`만 쓰지만, `loaded` 지역 변수를 그대로 두면 020·040·050에서 `ControlPanel(agent, loaded, session_db)`에 바로 넘길 수 있다.
- **문자열 → 설정 타입 변환 함수의 재사용**: 050의 `/ctl set <key> <value>`도 CLI에서 문자열로 들어온 값을 `SETTING_SPECS[key]`에 맞춰 변환해야 하므로, 문자열 해석 함수를 `control/settings.py`에 공개 도우미(`parse_setting_string(key: str, raw: str) -> Any` 등)로 두면 050에서 중복 없이 재사용할 수 있다.

---

## 6. ROADMAP과 다른 점, 그리고 정한 것

| 항목 | 이전 서술 (ROADMAP §4·§6-1·§9-6) | 이 지시서의 결정 | 이유 |
|---|---|---|---|
| `loop_guard_warn_threshold` 하한 | ROADMAP §6-1 표: `int ≥ 1` | `int >= 2` (그리고 `loop_guard_block_threshold`는 `int > warn`이므로 최소 3) | [`agent/loop_guard.py`](../../../../src/hermes_core/agent/loop_guard.py) 56행 `LoopGuard.__init__`이 `warn_threshold < 2`이면 `ValueError("warn_threshold must be an integer >= 2")`를 던진다(1회 호출부터 경고가 뜨는 것을 막는 불변식). 로더가 1을 통과시키면 첫 도구 호출 때 런타임 예외가 난다 |
| R6의 `control` 유출 방향 및 `TYPE_CHECKING` (열린 질문 §9-6 #2, 위험 10) | 위험 10: 「`control`은 `HermesAgent`의 타입만 `TYPE_CHECKING` 아래에서 쓴다. R6 시험이 `control → agent`도 볼지는 010 지시서에서 정한다」 | `control/`은 `HermesAgent`를 `TYPE_CHECKING` 아래에서도 import하지 않고(`Any` 사용), R6은 **유입**(`control`을 import할 수 있는 곳은 `assembly`·`cli`·`control.*`뿐)과 **유출**(`control`이 import할 수 있는 곳은 표준 라이브러리·`constants`·`control.*`뿐) 양방향을 모두 검사한다 | ① `test_annotations_resolve.py`가 런타임에 `typing.get_type_hints()`를 실행하므로 `TYPE_CHECKING` 전용 import는 `NameError`로 실패한다. ② `test_dependency_rules.py`의 `collect_imports`는 `ast.walk`라 `TYPE_CHECKING` 안의 import도 수집한다. `assembly.py`가 18-040에서 쓴 것처럼 `Any`를 쓰면 순환과 주석 해석 문제가 동시에 사라진다 |
| 환경변수 계층의 검사 범위 | ROADMAP §6-1: 「환경변수는 `HERMES_` + 키 대문자」 | `SETTING_SPECS`에 등록된 키에 대응하는 `HERMES_<KEY>`만 읽고, 그 밖의 `HERMES_*`(`HERMES_HOME`, 시험용 `HERMES_TEST_KEEP` 등)는 무시한다 | 프로세스 환경변수는 설정 전용 공간이 아니며 `HERMES_HOME`과 기존 시험의 `HERMES_TEST_KEEP`이 공존한다(§0-2) |
| `assembly.create_agent`의 `HermesAgent` 참조 방식 | ROADMAP §6-1: `create_agent(settings: Settings, **parts) -> HermesAgent` | 반환 주석은 `-> Any`로 두고, `HermesAgent` import는 `create_agent` 함수 본문 안에서 수행한다 | `agent/agent.py`가 모듈 최상위에서 `assembly`를 import하므로, `assembly` 최상위에서 `agent`를 import하면 `import hermes_core.agent.agent` 진입 순서(시나리오 13)에서 순환 import가 발생한다 |
