# [030] 프로바이더 결정 사다리 · 팩토리 · `Settings.provider` 통합

- **단계 번호**: `030`
- **단계명**: 프로바이더 결정 사다리 · 팩토리 · `Settings.provider` 통합
- **상태**: `Done`
- **의존성**: `010` (`constants.py` 프로바이더 상수), `020` (`AnthropicClient`)
- **관련 도해**: [ROADMAP.md §2~§3](../ROADMAP.md#2-폴더-및-변경-파일-구조)
- **변경 파일**:
  - `[신규]` `src/hermes_core/provider.py` (결정 사다리 `resolve_provider_config`, 추론 `infer_provider_from_model`, 팩토리 `create_llm_client`)
  - `[변경]` `src/hermes_core/control/settings.py` (`Settings.provider` 및 `SETTING_SPECS["provider"]` 추가)
  - `[변경]` `tests/test_control/test_settings.py` (`SETTING_SPECS` 수 단언 `12 → 13` 동기화 및 `provider` 설정 검증)
  - `[변경]` `tests/test_control/test_snapshot.py` (`SETTING_SPECS` 수 단언 `12 → 13` 동기화)
  - `[신규]` `tests/test_hermes_core/test_provider.py` (결정 사다리 우선순위 · Fail-Closed · 팩토리 인수 테스트)
- **커밋 메시지**: `feat(core): R23-030 - 프로바이더 결정 사다리(provider.py) 및 Settings.provider 통합`

---

## 1. 배경 및 목적

[ADR-0023 §2-3~§2-5](../../../adr/0023-다중_LLM_프로바이더_MVP_범위와_결정.md)에 따라 3종 프로바이더(`openrouter`, `gemini`, `anthropic`) 중 어떤 프로바이더·모델·API 키·어댑터를 사용할지 일관된 우선순위 사다리로 결정하고, `Settings.provider` 계층(`default < file < env < cli`)과 연결합니다.

---

## 2. 상세 구현 명세

### 2.1 `src/hermes_core/control/settings.py` 확장
- `SETTING_SPECS`에 `"provider"` 항목을 추가한다:
  - `key="provider"`, `type=str`, `default=None`, `mutability="startup"`, `nullable=True`
  - 검증기 `_validate_provider(v)`: `None`은 `None`, 문자열이면 `strip().lower()` 후 `"auto"` 또는 `SUPPORTED_PROVIDERS`(`"openrouter"`, `"gemini"`, `"anthropic"`) 중 하나만 허용하고 그 외(빈 문자열·미지원 이름·비문자열)는 `ValueError`를 발생시킨다.
- `Settings` dataclass에 `provider: Optional[str] = None` 필드를 추가한다.
- 기존 `load_settings()`의 4계층(`default < config.toml < HERMES_PROVIDER < cli_overrides`)이 `provider`에도 동일하게 적용된다.

### 2.2 `src/hermes_core/provider.py` 신규 작성
1. **데이터 모델 및 예외**:
   - `ProviderResolutionError(ValueError)`: 프로바이더 결정 또는 키 검증 실패 시 발생하는 예외.
   - `ResolvedProvider(frozen=True)`: `provider: str`, `model: str`, `api_key: str`, `base_url: str`를 담는 불변 값 객체. `__repr__`와 `__str__`에 `api_key` 원문을 절대 노출하지 않는다.
2. **모델명 추론 (`infer_provider_from_model(model: str) -> Optional[str]`)**:
   - 슬래시(`/`) 포함 (예: `anthropic/claude-3.5-sonnet`, `google/gemini-2.5-flash`, `deepseek/deepseek-v4.1-flash-1017`) → `"openrouter"`
   - `gemini` 접두사 (대소문자 무시) → `"gemini"`
   - `claude` 접두사 (대소문자 무시) → `"anthropic"`
   - 그 외 모호한 모델명 또는 빈 문자열 → `None`
3. **결정 사다리 (`resolve_provider_config(provider=None, model=None, environ=None) -> ResolvedProvider`)**:
   - **1순위 (명시 지정)**: `provider`가 `None`이나 `"auto"`가 아닌 명시값이면 해당 프로바이더로 고정한다(미지원 이름이면 `ProviderResolutionError`).
   - **2순위 (모델명 추론)**: `provider`가 미지정/`"auto"`이고 `model`이 주어지면 `infer_provider_from_model(model)`을 호출한다. 추론 실패(`None`) 시 `OPENROUTER_API_KEY`가 있으면 `"openrouter"`로 해석하고, 없으면 `--provider` 명시를 요구하는 `ProviderResolutionError`를 발생시킨다.
   - **3순위 (키 우선순위 자동 감지)**: `provider`·`model` 모두 없으면 `PROVIDER_KEY_PRIORITY`(`openrouter` → `gemini` → `anthropic`) 순서로 비어 있지 않은 첫 키와 해당 기본 모델(`DEFAULT_REAL_MODEL`, `DEFAULT_GEMINI_MODEL`, `DEFAULT_ANTHROPIC_MODEL`)을 선택한다. 3종 키가 모두 없으면 키 설정 및 `--mock` 안내를 포함한 `ProviderResolutionError`를 발생시킨다.
   - **Fail-Closed 키 검증**: 선택된 프로바이더의 환경변수 키가 비어 있거나 없으면 타 프로바이더 키가 존재하더라도 우회하지 않고 즉시 `ProviderResolutionError`를 발생시킨다.
4. **클라이언트 팩토리 (`create_llm_client(resolved, *, on_call=None, timeout=LLM_TIMEOUT_SECONDS, transport=None) -> LLMPort`)**:
   - `resolved.provider == "anthropic"` → `AnthropicClient` 생성 반환.
   - `resolved.provider in ("openrouter", "gemini")` → `OpenRouterClient(..., base_url=resolved.base_url)` 생성 반환.

### 2.3 기존 제어판 테스트 동기화
- `tests/test_control/test_settings.py`와 `tests/test_control/test_snapshot.py`의 `SETTING_SPECS` 항목 수 단언(`12` → `13`) 및 `Settings` 기본값 단언을 갱신한다.

---

## 3. 선기술 인수 테스트 시나리오 (입력 → 기대 결과)

> 구현 전 확정된 시나리오입니다. 4단계 구현 시 아래 시나리오를 그대로 `tests/test_hermes_core/test_provider.py` (및 `test_settings.py`)로 옮기며, 임의로 완화하거나 삭제하지 않습니다.

1. **시나리오 1 (명시 지정 최우선 + 타 키 무시)**:
   - 입력: `environ={"OPENROUTER_API_KEY": "sk-or-1", "GEMINI_API_KEY": "AIzaSyTestKey1234567890", "ANTHROPIC_API_KEY": "sk-ant-1"}`, `provider="anthropic"`, `model=None`.
   - 기대 결과: `ResolvedProvider(provider="anthropic", model=DEFAULT_ANTHROPIC_MODEL, api_key="sk-ant-1", base_url=ANTHROPIC_MESSAGES_URL)` 반환.
2. **시나리오 2 (Fail-Closed — 지정/추론된 키 누락 시 타 키 우회 금지)**:
   - 입력: `environ={"OPENROUTER_API_KEY": "sk-or-1"}` 상태에서 (a) `provider="gemini"` 호출, (b) `provider="auto", model="claude-sonnet-4-5"` 호출.
   - 기대 결과: 둘 다 `openrouter`로 우회하지 않고 `ProviderResolutionError` 발생(누락된 키 이름 `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` 포함).
3. **시나리오 3 (모델명 기반 추론 우선순위 — 슬래시 vs 접두사)**:
   - 입력: 3종 키 모두 있는 환경에서 `model="anthropic/claude-3.5-sonnet"`, `model="gemini-2.5-flash"`, `model="Claude-Sonnet-4-5"` 각각 `provider="auto"`로 호출.
   - 기대 결과: 각각 `"openrouter"`(`OPENROUTER_CHAT_URL`), `"gemini"`(`GEMINI_OPENAI_CHAT_URL`), `"anthropic"`(`ANTHROPIC_MESSAGES_URL`)로 해석됨.
4. **시나리오 4 (모호한 모델명 처리)**:
   - 입력: `model="gpt-4o-mini"` (슬래시·접두사 없음)에 대해 (a) `OPENROUTER_API_KEY` 존재 시, (b) `GEMINI_API_KEY`만 존재 시 각각 호출.
   - 기대 결과: (a) `"openrouter"`로 해석, (b) `ProviderResolutionError` 발생(`--provider` 명시 요구).
5. **시나리오 5 (키 우선순위 자동 감지 및 키 전무 오류)**:
   - 입력: `provider=None, model=None`으로 (a) Gemini + Anthropic 키만 있을 때, (b) Anthropic 키만 있을 때, (c) 키가 전혀 없을 때 호출.
   - 기대 결과: (a) `"gemini"` + `DEFAULT_GEMINI_MODEL`, (b) `"anthropic"` + `DEFAULT_ANTHROPIC_MODEL`, (c) `ProviderResolutionError` (`--mock` 안내 포함).
6. **시나리오 6 (팩토리 분기 · `repr` Zero Leakage)**:
   - 입력: `openrouter` / `gemini` / `anthropic` 각각에 대한 `ResolvedProvider`로 `create_llm_client()` 호출 및 `repr(resolved)` / `str(resolved)` 확인.
   - 기대 결과: `openrouter`·`gemini`는 `OpenRouterClient`(각각 `OPENROUTER_CHAT_URL`, `GEMINI_OPENAI_CHAT_URL`), `anthropic`은 `AnthropicClient`(`ANTHROPIC_MESSAGES_URL`) 반환; `repr`/`str`에 `api_key` 원문 미포함.
7. **시나리오 7 (`Settings.provider` 4계층 로드 및 검증)**:
   - 입력: `config.toml`에 `provider = "gemini"`, `environ={"HERMES_PROVIDER": "anthropic"}`, `cli_overrides={"provider": "openrouter"}` 계층 로드 및 잘못된 값(`provider="openai"`, `provider=""`) 검증.
   - 기대 결과: 우선순위 `default(None) < file(gemini) < env(anthropic) < cli(openrouter)`와 `sources["provider"]`가 정확히 추적되며, 잘못된 값은 `SettingsError` 발생.

---

## 4. 완료 기준

- 위 7개 인수 시나리오 테스트 통과
- `tests/test_control/test_settings.py` 및 `tests/test_control/test_snapshot.py` 통과
- `tests/test_architecture/` 전체 통과 (`R1~R7`, 타입 주석 해석, 800줄 상한)
- `.venv/bin/python -m pytest -n 4` 전체 통과 (실제 실행 후 결과 보고)
- `src/hermes_core/kernel/` diff 0줄 유지
