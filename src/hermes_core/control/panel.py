"""Hermes Core ControlPanel.

단일 제어판: 명령 검증, 감사 선기록, 대기열 관리, 턴 경계 반영, 스냅샷 조회를 담당한다.
R6 패키지 의존 규칙에 따라 assembly, agent, session_db를 직접 import하지 않는다.
"""

from __future__ import annotations

from collections import deque
import dataclasses
from dataclasses import dataclass
import re
from typing import Any, Callable, Collection, Optional

from hermes_core.control.commands import (
    ControlCommand,
    DisableExtension,
    EnableExtension,
    Rejection,
    SetSetting,
    validate_command,
)
from hermes_core.control.settings import SETTING_SPECS, LoadedSettings, Settings
from hermes_core.control.snapshot import ControlSnapshot, build_snapshot, mask_secrets


@dataclass(frozen=True)
class SubmitResult:
    """submit() 호출 결과."""

    accepted: bool
    command: ControlCommand
    audit_id: int
    rejection: Optional[Rejection] = None


@dataclass(frozen=True)
class AppliedChange:
    """apply_pending()에서 실제 반영된 단일 명령 결과."""

    command: ControlCommand
    old_value: str
    new_value: str
    audit_id: int


_EXTRA_BEARER_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9])(Bearer\s+)[^\s\"',;\)]+")


def _mask_audit_text(text: str) -> str:
    """감사 기록용 문자열 비밀값 마스킹 (Bearer 토큰 접두사 경계 보완)."""
    masked = mask_secrets(text)
    return _EXTRA_BEARER_PATTERN.sub(r"\g<1>***", masked)


def _format_audit_value(val: Any) -> Optional[str]:
    """감사 기록용 값 문자열화 및 비밀값 마스킹."""
    if val is None:
        return None
    if isinstance(val, bool):
        return "true" if val else "false"
    return _mask_audit_text(str(val))


def _default_apply_runtime_settings(
    agent: Any,
    settings: Settings,
    changed_keys: Collection[str],
) -> None:
    """기본 런타임 설정 반영 함수 (apply_settings_fn 미주입 시)."""
    for k in changed_keys:
        if k not in SETTING_SPECS or SETTING_SPECS[k].mutability != "runtime":
            raise ValueError(f"Setting '{k}' is not runtime-mutable")

    target_keys = set(changed_keys)
    if "max_iterations" in target_keys:
        agent.max_iterations = settings.max_iterations
    if "context_window_tokens" in target_keys:
        agent.context_window_tokens = settings.context_window_tokens
    if "compression_enabled" in target_keys:
        agent.compression_enabled = settings.compression_enabled
    if "compression_threshold_ratio" in target_keys:
        agent.compression_threshold_ratio = settings.compression_threshold_ratio
    if "breaker_threshold" in target_keys:
        agent.set_extension_breaker(settings.breaker_threshold)


class ControlPanel:
    """단일 제어판: 명령 검증, 감사 선기록, 대기열 관리, 턴 경계 반영, 스냅샷 조회."""

    def __init__(
        self,
        agent: Any,
        loaded: LoadedSettings,
        session_db: Any,
        *,
        apply_settings_fn: Optional[Callable[[Any, Settings], None]] = None,
    ) -> None:
        self._agent = agent
        self._loaded = loaded
        self._session_db = session_db
        self._apply_settings_fn = apply_settings_fn
        self._queue: deque[tuple[ControlCommand, str]] = deque()

    @property
    def loaded(self) -> LoadedSettings:
        """현재 LoadedSettings 반환."""
        return self._loaded

    @property
    def pending_count(self) -> int:
        """대기열에 쌓인 명령 수 반환."""
        return len(self._queue)

    def _get_extension_names_and_status(self) -> dict[str, bool]:
        """현재 설치된 확장의 이름과 켜짐 여부를 조회한다."""
        status_fn = getattr(self._agent, "extension_status", None)
        if callable(status_fn):
            return {st.name: bool(st.enabled) for st in status_fn()}
        exts = getattr(self._agent, "extensions", ())
        res: dict[str, bool] = {}
        for rec in exts:
            name = getattr(rec, "name", None)
            if isinstance(name, str) and name and name not in res:
                res[name] = True
        return res

    def submit(self, cmd: ControlCommand, *, source: str = "cli") -> SubmitResult:
        """명령을 검증하고, 감사 원장에 기록한 뒤 성공 시 대기열에 적재한다."""
        ext_status = self._get_extension_names_and_status()
        rejection = validate_command(cmd, self._loaded, ext_status.keys())

        if isinstance(cmd, SetSetting):
            command_name = "set_setting"
            target = cmd.key
            old_raw = getattr(self._loaded.settings, cmd.key, None)
            old_val_str = _format_audit_value(old_raw)
            if rejection is None:
                spec = SETTING_SPECS[cmd.key]
                new_val_str = _format_audit_value(spec.validator(cmd.value))
            else:
                new_val_str = _format_audit_value(cmd.value)
        elif isinstance(cmd, (EnableExtension, DisableExtension)):
            command_name = "enable_extension" if isinstance(cmd, EnableExtension) else "disable_extension"
            target = cmd.name
            cur_enabled = ext_status.get(cmd.name)
            old_val_str = "true" if cur_enabled else ("false" if cur_enabled is False else None)
            new_val_str = "true" if isinstance(cmd, EnableExtension) else "false"
        else:
            raise TypeError(f"Unsupported control command: {type(cmd)}")

        if rejection is not None:
            reason_str = _mask_audit_text(f"{rejection.code}: {rejection.message}")
            audit_id = self._session_db.record_control_audit(
                self._agent.session_id,
                source=source,
                command=command_name,
                target=target,
                old_value=old_val_str,
                new_value=new_val_str,
                result="rejected",
                reason=reason_str,
            )
            return SubmitResult(
                accepted=False,
                command=cmd,
                audit_id=audit_id,
                rejection=rejection,
            )

        audit_id = self._session_db.record_control_audit(
            self._agent.session_id,
            source=source,
            command=command_name,
            target=target,
            old_value=old_val_str,
            new_value=new_val_str,
            result="accepted",
            reason=None,
        )
        self._queue.append((cmd, source))
        return SubmitResult(
            accepted=True,
            command=cmd,
            audit_id=audit_id,
            rejection=None,
        )

    def apply_pending(self) -> list[AppliedChange]:
        """대기열의 명령들을 감사 원장에 선기록한 뒤 에이전트와 상태에 반영한다."""
        applied_list: list[AppliedChange] = []

        while self._queue:
            cmd, source = self._queue[0]

            if isinstance(cmd, SetSetting):
                spec = SETTING_SPECS.get(cmd.key)
                if spec is None or spec.mutability != "runtime":
                    raise ValueError(f"Cannot apply non-runtime setting: {cmd.key}")
                valid_val = spec.validator(cmd.value)
                old_raw = getattr(self._loaded.settings, cmd.key)
                old_str = _format_audit_value(old_raw) or ""
                new_str = _format_audit_value(valid_val) or ""

                # 감사 선기록 (실패 시 예외 전파되어 대기열 및 에이전트 상태 보존)
                audit_id = self._session_db.record_control_audit(
                    self._agent.session_id,
                    source=source,
                    command="set_setting",
                    target=cmd.key,
                    old_value=old_str,
                    new_value=new_str,
                    result="applied",
                    reason=None,
                )

                new_settings = dataclasses.replace(self._loaded.settings, **{cmd.key: valid_val})
                new_sources = dict(self._loaded.sources)
                new_sources[cmd.key] = source if source in ("cli", "default", "file", "env") else "cli"

                if self._apply_settings_fn is not None:
                    try:
                        self._apply_settings_fn(self._agent, new_settings, changed_keys=(cmd.key,))
                    except TypeError:
                        self._apply_settings_fn(self._agent, new_settings)
                else:
                    _default_apply_runtime_settings(self._agent, new_settings, (cmd.key,))

                self._loaded = LoadedSettings(
                    settings=new_settings,
                    sources=new_sources,
                    file_path=self._loaded.file_path,
                )
                self._queue.popleft()
                applied_list.append(
                    AppliedChange(
                        command=cmd,
                        old_value=old_str,
                        new_value=new_str,
                        audit_id=audit_id,
                    )
                )

            elif isinstance(cmd, (EnableExtension, DisableExtension)):
                target_enabled = isinstance(cmd, EnableExtension)
                cmd_name = "enable_extension" if target_enabled else "disable_extension"
                ext_status = self._get_extension_names_and_status()
                cur_enabled = ext_status.get(cmd.name, True)
                old_str = "true" if cur_enabled else "false"
                new_str = "true" if target_enabled else "false"

                # 감사 선기록 (실패 시 예외 전파되어 대기열 및 에이전트 상태 보존)
                audit_id = self._session_db.record_control_audit(
                    self._agent.session_id,
                    source=source,
                    command=cmd_name,
                    target=cmd.name,
                    old_value=old_str,
                    new_value=new_str,
                    result="applied",
                    reason=None,
                )

                self._agent.set_extension_enabled(cmd.name, target_enabled)
                self._queue.popleft()
                applied_list.append(
                    AppliedChange(
                        command=cmd,
                        old_value=old_str,
                        new_value=new_str,
                        audit_id=audit_id,
                    )
                )

            else:
                raise TypeError(f"Unsupported command in queue: {type(cmd)}")

        return applied_list

    def snapshot(self) -> ControlSnapshot:
        """현재 에이전트와 설정, 대기열 상태를 담은 ControlSnapshot을 생성한다."""
        return build_snapshot(self._agent, self._loaded, pending=len(self._queue))
