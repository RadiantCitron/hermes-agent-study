"""호환 층: hermes_core.kernel.contract의 이름을 다시 내보낸다. ADR-0018 D-05."""

from hermes_core.kernel.contract import (
    Extension,
    ExtensionHost,
    ExtensionRecord,
    FailurePolicy,
    MiddlewarePhase,
    PHASE_FAILURE_POLICY,
    RESERVED_RESULT_KEYS,
    TurnScope,
)

__all__ = [
    "Extension",
    "ExtensionHost",
    "ExtensionRecord",
    "FailurePolicy",
    "MiddlewarePhase",
    "PHASE_FAILURE_POLICY",
    "RESERVED_RESULT_KEYS",
    "TurnScope",
]
