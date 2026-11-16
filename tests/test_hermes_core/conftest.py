"""Hermes Core 테스트 공용 fixture 및 안전장치.

- 환경변수 OPENROUTER_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY, HERMES_PROVIDER 자동 제거
  (사용자 환경의 실제 키 및 프로바이더 설정 누출 차단).
- httpx.HTTPTransport.handle_request 차단 (테스트 도중 실제 외부 네트워크 호출 원천 차단).
"""

import httpx
import pytest


@pytest.fixture(autouse=True)
def clean_llm_env_keys(monkeypatch: pytest.MonkeyPatch):
    """코어 시험 전체에서 다중 LLM 키 및 HERMES_PROVIDER 환경변수를 자동 제거한다."""
    for env_name in (
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HERMES_PROVIDER",
    ):
        monkeypatch.delenv(env_name, raising=False)


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch):
    """테스트 실행 도중 실제 HTTP 네트워크 요청을 원천 차단한다."""
    def _blocked_handle_request(*args, **kwargs):
        raise RuntimeError("real network is blocked in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_handle_request)
