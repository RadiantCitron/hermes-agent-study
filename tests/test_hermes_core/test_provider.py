"""프로바이더 결정 사다리 · 팩토리 · Settings.provider 통합 인수 테스트 (시나리오 1~7).

근거:
- ADR-0023 §2-3, §2-4, §2-5
- 로드맵 23 구현지시서 030 §3
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_core.anthropic_client import AnthropicClient
from hermes_core.constants import (
    ANTHROPIC_MESSAGES_URL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_REAL_MODEL,
    GEMINI_OPENAI_CHAT_URL,
    OPENROUTER_CHAT_URL,
)
from hermes_core.control import SettingsError, load_settings
from hermes_core.llm_client import OpenRouterClient
from hermes_core.provider import (
    ProviderResolutionError,
    ResolvedProvider,
    create_llm_client,
    infer_provider_from_model,
    resolve_provider_config,
)


def test_scenario_01_explicit_provider_takes_precedence() -> None:
    """시나리오 1: 명시 지정 최우선 + 타 키 무시."""
    env = {
        "OPENROUTER_API_KEY": "sk-or-test-1",
        "GEMINI_API_KEY": "gemini-test-1",
        "ANTHROPIC_API_KEY": "sk-ant-test-1",
    }

    resolved = resolve_provider_config(provider="anthropic", model=None, environ=env)

    assert resolved == ResolvedProvider(
        provider="anthropic",
        model=DEFAULT_ANTHROPIC_MODEL,
        api_key="sk-ant-test-1",
        base_url=ANTHROPIC_MESSAGES_URL,
    )


def test_scenario_02_fail_closed_when_selected_provider_key_missing() -> None:
    """시나리오 2: 지정/추론된 키 누락 시 타 키 우회 없이 Fail-Closed."""
    env = {"OPENROUTER_API_KEY": "sk-or-test-1"}

    with pytest.raises(ProviderResolutionError) as exc_explicit:
        resolve_provider_config(provider="gemini", model=None, environ=env)
    assert "GEMINI_API_KEY" in str(exc_explicit.value)

    with pytest.raises(ProviderResolutionError) as exc_inferred:
        resolve_provider_config(provider="auto", model="claude-sonnet-4-5", environ=env)
    assert "ANTHROPIC_API_KEY" in str(exc_inferred.value)


def test_scenario_03_model_inference_slash_and_prefixes() -> None:
    """시나리오 3: 모델명 기반 추론 우선순위 (슬래시 vs 접두사)."""
    env = {
        "OPENROUTER_API_KEY": "sk-or-test-1",
        "GEMINI_API_KEY": "gemini-test-1",
        "ANTHROPIC_API_KEY": "sk-ant-test-1",
    }

    r_or = resolve_provider_config(
        provider="auto", model="anthropic/claude-3.5-sonnet", environ=env
    )
    assert r_or.provider == "openrouter"
    assert r_or.base_url == OPENROUTER_CHAT_URL
    assert r_or.model == "anthropic/claude-3.5-sonnet"

    r_gemini = resolve_provider_config(
        provider="auto", model="gemini-2.5-flash", environ=env
    )
    assert r_gemini.provider == "gemini"
    assert r_gemini.base_url == GEMINI_OPENAI_CHAT_URL

    r_claude = resolve_provider_config(
        provider="auto", model="Claude-Sonnet-4-5", environ=env
    )
    assert r_claude.provider == "anthropic"
    assert r_claude.base_url == ANTHROPIC_MESSAGES_URL
    assert infer_provider_from_model("   ") is None


def test_scenario_04_ambiguous_model_handling() -> None:
    """시나리오 4: 모호한 모델명 처리 (OpenRouter 키 유무)."""
    resolved = resolve_provider_config(
        provider="auto",
        model="gpt-4o-mini",
        environ={"OPENROUTER_API_KEY": "sk-or-test-1"},
    )
    assert resolved.provider == "openrouter"
    assert resolved.model == "gpt-4o-mini"

    with pytest.raises(ProviderResolutionError) as exc_info:
        resolve_provider_config(
            provider="auto",
            model="gpt-4o-mini",
            environ={"GEMINI_API_KEY": "gemini-test-1"},
        )
    assert "--provider" in str(exc_info.value)


def test_scenario_05_key_priority_auto_detect_and_no_keys_error() -> None:
    """시나리오 5: 키 우선순위 자동 감지(openrouter → gemini → anthropic) 및 키 전무 오류."""
    r_gemini = resolve_provider_config(
        provider=None,
        model=None,
        environ={
            "GEMINI_API_KEY": "gemini-test-1",
            "ANTHROPIC_API_KEY": "sk-ant-test-1",
        },
    )
    assert r_gemini.provider == "gemini"
    assert r_gemini.model == DEFAULT_GEMINI_MODEL

    r_anthropic = resolve_provider_config(
        provider=None,
        model=None,
        environ={"ANTHROPIC_API_KEY": "sk-ant-test-1"},
    )
    assert r_anthropic.provider == "anthropic"
    assert r_anthropic.model == DEFAULT_ANTHROPIC_MODEL

    r_openrouter = resolve_provider_config(
        provider=None,
        model=None,
        environ={
            "OPENROUTER_API_KEY": "sk-or-test-1",
            "GEMINI_API_KEY": "gemini-test-1",
        },
    )
    assert r_openrouter.provider == "openrouter"
    assert r_openrouter.model == DEFAULT_REAL_MODEL

    with pytest.raises(ProviderResolutionError) as exc_none:
        resolve_provider_config(provider=None, model=None, environ={})
    assert "--mock" in str(exc_none.value)


def test_scenario_06_factory_dispatch_and_zero_leakage() -> None:
    """시나리오 6: 팩토리 분기 및 ResolvedProvider repr/str Zero Leakage."""
    r_or = ResolvedProvider(
        provider="openrouter",
        model=DEFAULT_REAL_MODEL,
        api_key="secret-or-key-value",
        base_url=OPENROUTER_CHAT_URL,
    )
    r_gem = ResolvedProvider(
        provider="gemini",
        model=DEFAULT_GEMINI_MODEL,
        api_key="secret-gem-key-value",
        base_url=GEMINI_OPENAI_CHAT_URL,
    )
    r_ant = ResolvedProvider(
        provider="anthropic",
        model=DEFAULT_ANTHROPIC_MODEL,
        api_key="secret-ant-key-value",
        base_url=ANTHROPIC_MESSAGES_URL,
    )

    c_or = create_llm_client(r_or)
    c_gem = create_llm_client(r_gem)
    c_ant = create_llm_client(r_ant)

    assert isinstance(c_or, OpenRouterClient)
    assert c_or.base_url == OPENROUTER_CHAT_URL
    assert isinstance(c_gem, OpenRouterClient)
    assert c_gem.base_url == GEMINI_OPENAI_CHAT_URL
    assert isinstance(c_ant, AnthropicClient)
    assert c_ant.base_url == ANTHROPIC_MESSAGES_URL

    for resolved in (r_or, r_gem, r_ant):
        assert resolved.api_key not in repr(resolved)
        assert resolved.api_key not in str(resolved)


def test_scenario_07_settings_provider_layered_loading_and_validation(
    tmp_path: Path,
) -> None:
    """시나리오 7: Settings.provider 4계층 로드 및 검증."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('provider = "gemini"\n', encoding="utf-8")

    # default -> None
    loaded_default = load_settings(config_path=tmp_path / "missing.toml", environ={})
    assert loaded_default.settings.provider is None
    assert loaded_default.sources["provider"] == "default"

    # file -> gemini
    loaded_file = load_settings(config_path=config_file, environ={})
    assert loaded_file.settings.provider == "gemini"
    assert loaded_file.sources["provider"] == "file"

    # env -> anthropic (overrides file)
    loaded_env = load_settings(
        config_path=config_file,
        environ={"HERMES_PROVIDER": "ANTHROPIC"},
    )
    assert loaded_env.settings.provider == "anthropic"
    assert loaded_env.sources["provider"] == "env"

    # cli -> openrouter (overrides env)
    loaded_cli = load_settings(
        config_path=config_file,
        environ={"HERMES_PROVIDER": "anthropic"},
        cli_overrides={"provider": "openrouter"},
    )
    assert loaded_cli.settings.provider == "openrouter"
    assert loaded_cli.sources["provider"] == "cli"

    # invalid values
    with pytest.raises(SettingsError):
        load_settings(
            config_path=tmp_path / "missing.toml",
            environ={"HERMES_PROVIDER": "openai"},
        )
    with pytest.raises(SettingsError):
        load_settings(
            config_path=tmp_path / "missing.toml",
            environ={"HERMES_PROVIDER": ""},
        )
