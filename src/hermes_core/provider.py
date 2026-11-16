"""다중 LLM 프로바이더 결정 사다리 및 클라이언트 팩토리.

지원 프로바이더(openrouter · gemini · anthropic)에 대해
`명시 지정 > 모델명 추론 > 키 우선순위 자동 감지` 순서로 대상을 결정하고,
지정 또는 추론된 프로바이더의 API 키가 없으면 타 키로 우회하지 않고 즉시 실패한다(Fail-Closed).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable, Mapping, Optional

import httpx

from hermes_core.anthropic_client import AnthropicClient
from hermes_core.constants import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MESSAGES_URL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_REAL_MODEL,
    GEMINI_API_KEY_ENV,
    GEMINI_OPENAI_CHAT_URL,
    LLM_TIMEOUT_SECONDS,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_CHAT_URL,
    PROVIDER_KEY_PRIORITY,
    SUPPORTED_PROVIDERS,
)
from hermes_core.kernel.ports import LLMPort
from hermes_core.llm_client import OpenRouterClient

_PROVIDER_ENV_MAP: dict[str, str] = {
    "openrouter": OPENROUTER_API_KEY_ENV,
    "gemini": GEMINI_API_KEY_ENV,
    "anthropic": ANTHROPIC_API_KEY_ENV,
}

_PROVIDER_DEFAULT_MODEL_MAP: dict[str, str] = {
    "openrouter": DEFAULT_REAL_MODEL,
    "gemini": DEFAULT_GEMINI_MODEL,
    "anthropic": DEFAULT_ANTHROPIC_MODEL,
}

_PROVIDER_BASE_URL_MAP: dict[str, str] = {
    "openrouter": OPENROUTER_CHAT_URL,
    "gemini": GEMINI_OPENAI_CHAT_URL,
    "anthropic": ANTHROPIC_MESSAGES_URL,
}


class ProviderResolutionError(ValueError):
    """프로바이더 결정 또는 API 키 검증 실패 시 발생하는 예외."""


@dataclass(frozen=True)
class ResolvedProvider:
    """결정 사다리를 통과해 확정된 프로바이더·모델·키·엔드포인트 정보."""

    provider: str
    model: str
    api_key: str
    base_url: str

    def __repr__(self) -> str:
        return (
            f"ResolvedProvider(provider={self.provider!r}, "
            f"model={self.model!r}, base_url={self.base_url!r})"
        )

    def __str__(self) -> str:
        return f"ResolvedProvider(provider={self.provider}, model={self.model})"


def infer_provider_from_model(model: str) -> Optional[str]:
    """모델 이름 표기만으로 프로바이더를 추론한다. 추론 불가 시 None을 반환한다."""
    stripped = model.strip()
    if not stripped:
        return None
    if "/" in stripped:
        return "openrouter"
    lower = stripped.lower()
    if lower.startswith("gemini"):
        return "gemini"
    if lower.startswith("claude"):
        return "anthropic"
    return None


def _select_provider(
    clean_provider: Optional[str],
    clean_model: Optional[str],
    env: Mapping[str, str],
) -> str:
    """우선순위 사다리(명시 지정 > 모델명 추론 > 키 자동 감지)로 프로바이더를 선택한다."""
    if clean_provider is not None and clean_provider != "auto":
        if clean_provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(SUPPORTED_PROVIDERS)
            raise ProviderResolutionError(
                f"Unsupported provider: {clean_provider!r}. Supported: {supported} (or 'auto')."
            )
        return clean_provider

    if clean_model is not None:
        inferred = infer_provider_from_model(clean_model)
        if inferred is not None:
            return inferred
        if env.get(OPENROUTER_API_KEY_ENV, "").strip():
            return "openrouter"
        raise ProviderResolutionError(
            f"Cannot infer provider for model {clean_model!r} without OPENROUTER_API_KEY. "
            "Specify --provider (openrouter, gemini, or anthropic)."
        )

    for candidate in PROVIDER_KEY_PRIORITY:
        env_name = _PROVIDER_ENV_MAP[candidate]
        if env.get(env_name, "").strip():
            return candidate

    env_list = ", ".join(_PROVIDER_ENV_MAP[p] for p in PROVIDER_KEY_PRIORITY)
    raise ProviderResolutionError(
        f"No LLM API key found. Set one of [{env_list}] in environment or .env, or use --mock."
    )


def resolve_provider_config(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ResolvedProvider:
    """표준 우선순위 사다리와 Fail-Closed 원칙으로 프로바이더 설정을 확정한다."""
    env = os.environ if environ is None else environ
    clean_provider = (
        provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    )
    clean_model = model.strip() if isinstance(model, str) and model.strip() else None

    chosen_provider = _select_provider(clean_provider, clean_model, env)

    env_name = _PROVIDER_ENV_MAP[chosen_provider]
    api_key = env.get(env_name, "").strip()
    if not api_key:
        raise ProviderResolutionError(
            f"Missing API key for provider '{chosen_provider}': set {env_name} (or use --mock)."
        )

    chosen_model = clean_model or _PROVIDER_DEFAULT_MODEL_MAP[chosen_provider]
    base_url = _PROVIDER_BASE_URL_MAP[chosen_provider]

    return ResolvedProvider(
        provider=chosen_provider,
        model=chosen_model,
        api_key=api_key,
        base_url=base_url,
    )


def create_llm_client(
    resolved: ResolvedProvider,
    *,
    on_call: Optional[Callable[[dict[str, Any]], None]] = None,
    timeout: int = LLM_TIMEOUT_SECONDS,
    transport: Optional[httpx.BaseTransport] = None,
) -> LLMPort:
    """확정된 프로바이더 설정에 맞는 LLMPort 클라이언트 어댑터를 생성한다."""
    if resolved.provider == "anthropic":
        return AnthropicClient(
            api_key=resolved.api_key,
            model=resolved.model,
            base_url=resolved.base_url,
            timeout=timeout,
            transport=transport,
            on_call=on_call,
        )
    if resolved.provider in ("openrouter", "gemini"):
        return OpenRouterClient(
            api_key=resolved.api_key,
            model=resolved.model,
            base_url=resolved.base_url,
            timeout=timeout,
            transport=transport,
            on_call=on_call,
        )
    raise ProviderResolutionError(f"Unsupported provider: {resolved.provider!r}")
