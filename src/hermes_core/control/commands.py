"""Hermes Core Control Commands and Validator.

불변 명령 객체(SetSetting, EnableExtension, DisableExtension)와
명령 검증기(validate_command)를 제공한다.
보안 설정(locked)과 시작 전용 설정(startup) 변경 시도는 거부된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Literal, Optional, Union

from hermes_core.control.settings import SETTING_SPECS, LoadedSettings


@dataclass(frozen=True)
class SetSetting:
    """런타임 설정 변경 명령."""

    key: str
    value: Any


@dataclass(frozen=True)
class EnableExtension:
    """확장 활성화 명령."""

    name: str


@dataclass(frozen=True)
class DisableExtension:
    """확장 비활성화 명령."""

    name: str


ControlCommand = Union[SetSetting, EnableExtension, DisableExtension]


RejectionCode = Literal[
    "unknown_key",
    "bad_value",
    "locked",
    "startup_only",
    "unknown_extension",
]


@dataclass(frozen=True)
class Rejection:
    """명령 검증 거부 결과."""

    code: RejectionCode
    message: str


def validate_command(
    cmd: ControlCommand,
    loaded: LoadedSettings,
    extension_names: Collection[str],
) -> Optional[Rejection]:
    """제어 명령을 검증하여 통과 시 None, 실패 시 Rejection 객체를 반환한다."""
    if isinstance(cmd, SetSetting):
        if cmd.key not in SETTING_SPECS:
            return Rejection("unknown_key", f"Unknown setting key: '{cmd.key}'")
        spec = SETTING_SPECS[cmd.key]
        if spec.mutability == "locked":
            return Rejection("locked", f"Setting '{cmd.key}' is locked and cannot be changed")
        if spec.mutability == "startup":
            return Rejection("startup_only", f"Setting '{cmd.key}' can only be set at startup")
        try:
            spec.validator(cmd.value)
        except Exception as exc:
            return Rejection("bad_value", str(exc))
        return None

    if isinstance(cmd, (EnableExtension, DisableExtension)):
        if not isinstance(cmd.name, str) or cmd.name not in extension_names:
            return Rejection("unknown_extension", f"Unknown extension: '{cmd.name}'")
        return None

    raise TypeError(f"Unsupported control command: {type(cmd)}")
