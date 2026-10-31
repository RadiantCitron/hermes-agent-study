"""Hermes Core 조립 지점 (Assembly).

기본으로 끼울 기능의 목록과 순서는 이 파일 한 곳이 정한다.
기능을 더하거나 빼려면 default_extensions의 목록을 고친다.
"""

from __future__ import annotations

from typing import Any, Callable

from hermes_core.agent.compression_hook import CompressionConfig, CompressionExtension
from hermes_core.agent.loop_guard import LoopGuardExtension
from hermes_core.kernel.contract import Extension


def compression_config_of(agent: Any) -> CompressionConfig:
    """에이전트 인스턴스로부터 호출 시점의 압축 설정을 생성한다."""
    return CompressionConfig(
        enabled=agent.compression_enabled,
        context_window_tokens=agent.context_window_tokens,
        threshold_ratio=agent.compression_threshold_ratio,
        llm_client=agent.llm_client,
    )


def default_extensions(
    compression_config: Callable[[], CompressionConfig],
) -> list[Extension]:
    """기본 확장 목록을 새로 인스턴스화하여 반환한다.

    확장은 설치 뒤 호스트 등 상태를 가지므로 호출할 때마다 새 목록과 새 인스턴스를 반환한다.
    """
    return [
        CompressionExtension(compression_config),
        LoopGuardExtension(),
    ]
