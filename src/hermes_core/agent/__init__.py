"""Hermes Agent 프롬프트 조립 및 에이전트 서브패키지."""

__all__ = ["HermesAgent", "AIAgent", "MockLLMClient", "MockLLMResponse"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from hermes_core.agent import agent as _agent_module

        return getattr(_agent_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
