"""호환 층: hermes_core.kernel.turn_hooks의 이름을 다시 내보낸다. ADR-0018 D-05."""

from hermes_core.kernel.turn_hooks import (
    AFTER_TOOL_CALLS,
    BEFORE_LLM,
    HOOK_POINTS,
    HookRegistry,
    TurnContext,
    TurnHook,
)

__all__ = [
    "AFTER_TOOL_CALLS",
    "BEFORE_LLM",
    "HOOK_POINTS",
    "HookRegistry",
    "TurnContext",
    "TurnHook",
]
