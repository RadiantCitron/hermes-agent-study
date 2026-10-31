"""Hermes Core Settings 모델 및 계층 로더 (Settings & Layered Loader).

기본값 < 설정 파일($HERMES_HOME/config.toml) < 환경변수(HERMES_<KEY>) < CLI 옵션 순서로
설정을 병합하여 불변 스냅샷(Settings)과 출처 기록(LoadedSettings)을 생성한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import tomllib
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Optional

from hermes_core.constants import (
    COMPRESSION_THRESHOLD_RATIO,
    CONTEXT_WINDOW_TOKENS,
    LOOP_GUARD_BLOCK_THRESHOLD,
    LOOP_GUARD_WARN_THRESHOLD,
    get_hermes_home,
)

Mutability = Literal["runtime", "startup", "locked"]
SettingSource = Literal["default", "file", "env", "cli"]


class SettingsError(ValueError):
    """설정 로드 또는 검증 실패 시 발생하는 예외."""

    def __init__(self, layer: str, key: str, reason: str) -> None:
        self.layer = layer
        self.key = key
        self.reason = reason
        super().__init__(f"Invalid setting '{key}' in {layer}: {reason}")


@dataclass(frozen=True)
class SettingSpec:
    """단일 설정 항목의 명세."""

    key: str
    type: type
    default: Any
    mutability: Mutability
    validator: Callable[[Any], Any]
    nullable: bool = False


def _validate_model(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() != "":
        return v
    raise ValueError("model must be a non-empty string or None")


def _validate_max_iterations(v: Any) -> int:
    if not isinstance(v, bool) and isinstance(v, int) and v >= 1:
        return v
    raise ValueError("max_iterations must be an integer >= 1")


def _validate_context_window(v: Any) -> int:
    if not isinstance(v, bool) and isinstance(v, int) and v >= 1:
        return v
    raise ValueError("context_window_tokens must be an integer >= 1")


def _validate_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError("value must be a boolean")


def _validate_compression_threshold_ratio(v: Any) -> float:
    if not isinstance(v, bool) and isinstance(v, (int, float)):
        val = float(v)
        if math.isfinite(val) and 0.0 < val <= 1.0:
            return val
    raise ValueError("compression_threshold_ratio must be a float > 0.0 and <= 1.0")


def _validate_loop_guard_warn(v: Any) -> int:
    if not isinstance(v, bool) and isinstance(v, int) and v >= 2:
        return v
    raise ValueError("loop_guard_warn_threshold must be an integer >= 2")


def _validate_loop_guard_block(v: Any) -> int:
    if not isinstance(v, bool) and isinstance(v, int) and v >= 3:
        return v
    raise ValueError("loop_guard_block_threshold must be an integer >= 3")


def _validate_breaker_threshold(v: Any) -> int:
    if not isinstance(v, bool) and isinstance(v, int) and v >= 0:
        return v
    raise ValueError("breaker_threshold must be an integer >= 0")


_SPECS_DICT: dict[str, SettingSpec] = {
    "model": SettingSpec(
        key="model",
        type=str,
        default=None,
        mutability="startup",
        validator=_validate_model,
        nullable=True,
    ),
    "max_iterations": SettingSpec(
        key="max_iterations",
        type=int,
        default=50,
        mutability="runtime",
        validator=_validate_max_iterations,
    ),
    "context_window_tokens": SettingSpec(
        key="context_window_tokens",
        type=int,
        default=CONTEXT_WINDOW_TOKENS,
        mutability="runtime",
        validator=_validate_context_window,
    ),
    "compression_enabled": SettingSpec(
        key="compression_enabled",
        type=bool,
        default=True,
        mutability="runtime",
        validator=_validate_bool,
    ),
    "compression_threshold_ratio": SettingSpec(
        key="compression_threshold_ratio",
        type=float,
        default=COMPRESSION_THRESHOLD_RATIO,
        mutability="runtime",
        validator=_validate_compression_threshold_ratio,
    ),
    "loop_guard_warn_threshold": SettingSpec(
        key="loop_guard_warn_threshold",
        type=int,
        default=LOOP_GUARD_WARN_THRESHOLD,
        mutability="startup",
        validator=_validate_loop_guard_warn,
    ),
    "loop_guard_block_threshold": SettingSpec(
        key="loop_guard_block_threshold",
        type=int,
        default=LOOP_GUARD_BLOCK_THRESHOLD,
        mutability="startup",
        validator=_validate_loop_guard_block,
    ),
    "skills_index": SettingSpec(
        key="skills_index",
        type=bool,
        default=True,
        mutability="startup",
        validator=_validate_bool,
    ),
    "delegate": SettingSpec(
        key="delegate",
        type=bool,
        default=False,
        mutability="startup",
        validator=_validate_bool,
    ),
    "approval_enabled": SettingSpec(
        key="approval_enabled",
        type=bool,
        default=True,
        mutability="locked",
        validator=_validate_bool,
    ),
    "checkpoints_enabled": SettingSpec(
        key="checkpoints_enabled",
        type=bool,
        default=True,
        mutability="locked",
        validator=_validate_bool,
    ),
    "breaker_threshold": SettingSpec(
        key="breaker_threshold",
        type=int,
        default=3,
        mutability="runtime",
        validator=_validate_breaker_threshold,
    ),
}

SETTING_SPECS: Mapping[str, SettingSpec] = MappingProxyType(_SPECS_DICT)


@dataclass(frozen=True)
class Settings:
    """Hermes Core의 모든 설정값을 담는 동결 스냅샷."""

    model: Optional[str] = None
    max_iterations: int = 50
    context_window_tokens: int = CONTEXT_WINDOW_TOKENS
    compression_enabled: bool = True
    compression_threshold_ratio: float = COMPRESSION_THRESHOLD_RATIO
    loop_guard_warn_threshold: int = LOOP_GUARD_WARN_THRESHOLD
    loop_guard_block_threshold: int = LOOP_GUARD_BLOCK_THRESHOLD
    skills_index: bool = True
    delegate: bool = False
    approval_enabled: bool = True
    checkpoints_enabled: bool = True
    breaker_threshold: int = 3


@dataclass(frozen=True)
class LoadedSettings:
    """설정 스냅샷과 각 항목의 출처 기록."""

    settings: Settings
    sources: Mapping[str, SettingSource]
    file_path: Optional[Path] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


def parse_setting_string(key: str, raw: str) -> Any:
    """문자열 형태의 설정값을 타입에 맞게 파싱하고 검증한다."""
    if key not in SETTING_SPECS:
        raise ValueError(f"Unknown setting key: {key}")

    spec = SETTING_SPECS[key]
    if spec.type is bool:
        s = raw.strip().lower()
        if s in ("1", "true", "yes"):
            return True
        if s in ("0", "false", "no"):
            return False
        raise ValueError(f"Invalid boolean string: {raw!r}")

    if spec.type is int:
        s = raw.strip()
        try:
            val = int(s, 10)
        except ValueError as exc:
            raise ValueError(f"Invalid integer string: {raw!r}") from exc
        return spec.validator(val)

    if spec.type is float:
        s = raw.strip()
        try:
            val = float(s)
        except ValueError as exc:
            raise ValueError(f"Invalid float string: {raw!r}") from exc
        return spec.validator(val)

    if spec.type is str:
        return spec.validator(raw)

    return spec.validator(raw)


def load_settings(
    *,
    cli_overrides: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> LoadedSettings:
    """계층 순서(기본값 < 파일 < 환경변수 < CLI)로 설정을 로드하여 LoadedSettings를 반환한다."""
    # 1. 기본값 초기화
    values: dict[str, Any] = {k: spec.default for k, spec in SETTING_SPECS.items()}
    sources: dict[str, SettingSource] = {k: "default" for k in SETTING_SPECS}

    # 2. 파일 로드
    target_path: Optional[Path] = None
    if config_path is not None:
        target_path = Path(config_path)
    else:
        if environ is not None and environ.get("HERMES_HOME"):
            target_path = Path(environ["HERMES_HOME"]).expanduser().resolve() / "config.toml"
        else:
            target_path = get_hermes_home() / "config.toml"

    loaded_file_path: Optional[Path] = None
    if target_path.exists():
        if not target_path.is_file():
            raise SettingsError("file", "<toml>", f"{target_path} is not a regular file")
        try:
            with open(target_path, "rb") as f:
                file_data = tomllib.load(f)
        except Exception as exc:
            raise SettingsError("file", "<toml>", str(exc)) from exc

        for k, v in file_data.items():
            if isinstance(v, dict):
                raise SettingsError("file", k, "nested tables are not allowed")
            if k not in SETTING_SPECS:
                raise SettingsError("file", k, "unknown setting key")
            spec = SETTING_SPECS[k]
            try:
                valid_val = spec.validator(v)
            except Exception as exc:
                raise SettingsError("file", k, str(exc)) from exc
            values[k] = valid_val
            sources[k] = "file"

        loaded_file_path = target_path.resolve()

    # 3. 환경변수 로드
    env_map = os.environ if environ is None else environ
    for k, spec in SETTING_SPECS.items():
        env_key = f"HERMES_{k.upper()}"
        if env_key in env_map:
            raw_val = env_map[env_key]
            try:
                valid_val = parse_setting_string(k, raw_val)
            except Exception as exc:
                raise SettingsError("env", k, str(exc)) from exc
            values[k] = valid_val
            sources[k] = "env"

    # 4. CLI 오버라이드
    if cli_overrides is not None:
        for k, v in cli_overrides.items():
            if k not in SETTING_SPECS:
                raise SettingsError("cli", k, "unknown setting key")
            if v is None:
                continue
            spec = SETTING_SPECS[k]
            try:
                valid_val = spec.validator(v)
            except Exception as exc:
                raise SettingsError("cli", k, str(exc)) from exc
            values[k] = valid_val
            sources[k] = "cli"

    # 5. 교차 검증 (loop_guard_block_threshold > loop_guard_warn_threshold)
    warn_val = values["loop_guard_warn_threshold"]
    block_val = values["loop_guard_block_threshold"]
    if block_val <= warn_val:
        err_layer = (
            sources["loop_guard_block_threshold"]
            if sources["loop_guard_block_threshold"] != "default"
            else sources["loop_guard_warn_threshold"]
        )
        raise SettingsError(
            err_layer,
            "loop_guard_block_threshold",
            "loop_guard_block_threshold must be > loop_guard_warn_threshold",
        )

    return LoadedSettings(
        settings=Settings(**values),
        sources=sources,
        file_path=loaded_file_path,
    )
