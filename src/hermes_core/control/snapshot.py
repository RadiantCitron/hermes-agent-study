"""단일 제어판 상태 스냅샷(읽기 모델)과 비밀값 마스킹.

이 모듈은 에이전트와 설정 출처로부터 읽기 전용 불변 스냅샷(ControlSnapshot)을 조립한다.
에이전트나 세션 DB를 일체 변경하지 않으며(WHY 가정 H),
문자열에 포함된 비밀값 패턴(Bearer 토큰, sk- 키)을 마스킹한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from hermes_core.control.settings import (
    SETTING_SPECS,
    LoadedSettings,
    Mutability,
    SettingSource,
)

_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)[^\s\"',;\)]+")
_SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_GEMINI_KEY_PATTERN = re.compile(r"\bAIzaSy[A-Za-z0-9_-]{20,}\b")


def mask_secrets(text: str) -> str:
    """문자열 내의 API 키 및 Bearer 토큰 등 비밀값 패턴을 '***'로 마스킹한다."""
    if not text:
        return text
    text = _BEARER_PATTERN.sub(r"\g<1>***", text)
    text = _SK_PATTERN.sub("***", text)
    text = _GEMINI_KEY_PATTERN.sub("***", text)
    return text


@dataclass(frozen=True)
class SettingView:
    """단일 설정 항목의 값, 출처, 변경 가능 시점 뷰."""

    key: str
    value: Any
    source: SettingSource
    mutability: Mutability


@dataclass(frozen=True)
class ExtensionView:
    """등록된 확장의 종합 뷰."""

    name: str
    kinds: tuple[str, ...]
    slots: tuple[str, ...]
    total_failures: int
    enabled: bool = True
    consecutive_failures: int = 0
    tripped: bool = False


@dataclass(frozen=True)
class ControlSnapshot:
    """에이전트 및 설정의 현재 상태를 담는 불변(동결) 스냅샷."""

    settings: tuple[SettingView, ...]
    extensions: tuple[ExtensionView, ...]
    session_id: str
    input_tokens: int
    output_tokens: int
    pending_commands: int


class _ExtensionGroup:
    __slots__ = ("name", "kinds", "slots", "failures")

    def __init__(self, name: str, failures: int) -> None:
        self.name = name
        self.kinds: list[str] = []
        self.slots: list[str] = []
        self.failures = failures


def build_snapshot(
    agent: Any,
    loaded: LoadedSettings,
    *,
    pending: int = 0,
) -> ControlSnapshot:
    """에이전트와 로드된 설정으로부터 ControlSnapshot을 조립한다.

    에이전트나 세션 DB를 변경하지 않는 순수 읽기 전용 함수다.
    """
    if pending < 0:
        raise ValueError("pending must be >= 0")

    # 1. 설정 뷰 조립 (SETTING_SPECS 순서 보장)
    setting_views: list[SettingView] = []
    for key, spec in SETTING_SPECS.items():
        raw_val = getattr(loaded.settings, key)
        val = mask_secrets(raw_val) if isinstance(raw_val, str) else raw_val
        source: SettingSource = loaded.sources.get(key, "default")
        setting_views.append(
            SettingView(
                key=key,
                value=val,
                source=source,
                mutability=spec.mutability,
            )
        )

    # 2. 확장 뷰 조립 (이름별 최초 등장 순서 유지, kinds/slots 중복 제거)
    status_fn = getattr(agent, "extension_status", None)
    status_by_name: dict[str, Any] = {}
    if callable(status_fn):
        for st in status_fn():
            status_by_name[st.name] = st

    groups: dict[str, _ExtensionGroup] = {}
    for rec in getattr(agent, "extensions", ()):
        name = str(getattr(rec, "name", ""))
        failures = int(getattr(rec, "failures", 0))
        if name not in groups:
            groups[name] = _ExtensionGroup(name, failures)
        group = groups[name]
        kind = str(getattr(rec, "kind", ""))
        if kind and kind not in group.kinds:
            group.kinds.append(kind)
        slot = str(getattr(rec, "slot", ""))
        if slot and slot not in group.slots:
            group.slots.append(slot)
        group.failures = failures

    ext_views: list[ExtensionView] = []
    for grp in groups.values():
        st = status_by_name.get(grp.name)
        if st is not None:
            ext_views.append(
                ExtensionView(
                    name=grp.name,
                    kinds=tuple(grp.kinds),
                    slots=tuple(grp.slots),
                    total_failures=grp.failures,
                    enabled=bool(st.enabled),
                    consecutive_failures=int(st.consecutive_failures),
                    tripped=bool(st.tripped),
                )
            )
        else:
            ext_views.append(
                ExtensionView(
                    name=grp.name,
                    kinds=tuple(grp.kinds),
                    slots=tuple(grp.slots),
                    total_failures=grp.failures,
                )
            )
    extension_views = tuple(ext_views)

    # 3. 세션 및 토큰 수 조회
    session_id = str(getattr(agent, "session_id", ""))
    input_tokens = 0
    output_tokens = 0
    session_db = getattr(agent, "session_db", None)
    if session_db is not None and session_id:
        row = session_db.get_session(session_id)
        if isinstance(row, dict):
            input_tokens = int(row.get("input_tokens") or 0)
            output_tokens = int(row.get("output_tokens") or 0)

    return ControlSnapshot(
        settings=tuple(setting_views),
        extensions=extension_views,
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        pending_commands=pending,
    )


def render_snapshot(snapshot: ControlSnapshot) -> str:
    """ControlSnapshot을 사람이 읽기 쉬운 다중 라인 문자열로 렌더링한다."""
    lines: list[str] = [
        f"Session: {snapshot.session_id or '(none)'} | Tokens: in={snapshot.input_tokens}, out={snapshot.output_tokens} | Pending Commands: {snapshot.pending_commands}",
        "",
        "[Settings]",
    ]
    for s in snapshot.settings:
        lines.append(f"  {s.key} = {s.value!r} (source={s.source}, mutability={s.mutability})")

    lines.append("")
    lines.append("[Extensions]")
    if not snapshot.extensions:
        lines.append("  (none)")
    else:
        for ext in snapshot.extensions:
            kinds_str = ", ".join(ext.kinds)
            slots_str = ", ".join(ext.slots)
            lines.append(
                f"  {ext.name} (kinds: {kinds_str} | slots: {slots_str} | enabled={ext.enabled} | failures={ext.total_failures} | consecutive={ext.consecutive_failures} | tripped={ext.tripped})"
            )

    rendered = "\n".join(lines)
    return mask_secrets(rendered)
