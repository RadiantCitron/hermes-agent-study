"""상태 스냅샷(읽기 모델)과 비밀값 마스킹 인수 테스트 (시나리오 1~6).

근거:
- ADR-0019 D-02, D-05
- 로드맵 19 구현지시서 020 §3-1
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import types
from typing import Any

import pytest

from hermes_core.assembly import create_agent
from hermes_core.control import (
    SETTING_SPECS,
    ControlSnapshot,
    ExtensionView,
    SettingView,
    build_snapshot,
    load_settings,
    mask_secrets,
    render_snapshot,
)
from hermes_core.kernel.contract import MiddlewarePhase
from hermes_core.session_db import SessionDB


def test_scenario_01_read_only_side_effect_free_and_token_counts(tmp_path: Path) -> None:
    """시나리오 1: 읽기 전용 무부작용 검증 (WHY 가정 H).

    스냅샷 3회 연속 호출 시 동일한 결과를 내고, 호출 전후의 에이전트 속성,
    세션 행, 메시지 수가 완전히 동일함을 보장한다.
    """
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)

    config_path = tmp_path / "config.toml"
    config_path.write_text("max_iterations = 12\n", encoding="utf-8")

    loaded = load_settings(config_path=config_path)
    agent = create_agent(
        loaded.settings,
        session_db=db,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    db.update_token_counts(agent.session_id, input_tokens=120, output_tokens=45)

    # 호출 전 상태 기록
    before_max_iterations = agent.max_iterations
    before_context_window = agent.context_window_tokens
    before_compression = agent.compression_enabled
    before_extensions = agent.extensions
    before_session = db.get_session(agent.session_id)
    before_messages = db.get_messages(agent.session_id)

    # 3회 연속 호출
    snap1 = build_snapshot(agent, loaded, pending=2)
    snap2 = build_snapshot(agent, loaded, pending=2)
    snap3 = build_snapshot(agent, loaded, pending=2)

    assert snap1 == snap2 == snap3

    # 호출 후 상태가 호출 전과 완전히 동일한지 확인
    assert agent.max_iterations == before_max_iterations
    assert agent.context_window_tokens == before_context_window
    assert agent.compression_enabled == before_compression
    assert agent.extensions == before_extensions
    assert db.get_session(agent.session_id) == before_session
    assert db.get_messages(agent.session_id) == before_messages

    # 스냅샷 내용 확인
    assert snap1.session_id == agent.session_id
    assert snap1.input_tokens == 120
    assert snap1.output_tokens == 45
    assert snap1.pending_commands == 2


def test_scenario_02_setting_views_mapping_and_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시나리오 2: 설정 12개 항목의 값·출처·변경 가능 시점 매핑.

    파일, 환경변수, CLI 오버라이드로 만든 LoadedSettings의 각 항목이 올바르게 뷰에 매핑된다.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text("max_iterations = 10\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_CONTEXT_WINDOW_TOKENS", "4096")

    loaded = load_settings(
        config_path=config_path,
        cli_overrides={"compression_enabled": False},
    )

    dummy_agent = types.SimpleNamespace(session_id="dummy-sess", session_db=None, extensions=())
    snap = build_snapshot(dummy_agent, loaded)

    assert len(snap.settings) == 12
    assert tuple(s.key for s in snap.settings) == tuple(SETTING_SPECS.keys())

    views = {s.key: s for s in snap.settings}
    assert views["max_iterations"] == SettingView(
        key="max_iterations",
        value=10,
        source="file",
        mutability="runtime",
    )
    assert views["context_window_tokens"] == SettingView(
        key="context_window_tokens",
        value=4096,
        source="env",
        mutability="runtime",
    )
    assert views["compression_enabled"] == SettingView(
        key="compression_enabled",
        value=False,
        source="cli",
        mutability="runtime",
    )
    assert views["approval_enabled"] == SettingView(
        key="approval_enabled",
        value=True,
        source="default",
        mutability="locked",
    )
    assert views["model"] == SettingView(
        key="model",
        value=None,
        source="default",
        mutability="startup",
    )


def test_scenario_03_extension_views_grouping_and_failures(tmp_path: Path) -> None:
    """시나리오 3: 기본 확장 2개와 다중 슬롯 확장 묶기 및 실패 수 반영."""
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    loaded = load_settings()
    agent = create_agent(
        loaded.settings,
        session_db=db,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    # (a) 기본 에이전트 확장 검증
    snap_a = build_snapshot(agent, loaded)
    expected_extensions_a = (
        ExtensionView(
            name="compression",
            kinds=("turn_hook",),
            slots=("before_llm",),
            total_failures=0,
        ),
        ExtensionView(
            name="loop_guard",
            kinds=("tool_middleware",),
            slots=("GUARD",),
            total_failures=0,
        ),
    )
    assert snap_a.extensions == expected_extensions_a

    # (b) 다중 슬롯 확장 추가 설치 및 에러 발생
    class MultiExtension:
        name = "multi"

        def install(self, host: Any) -> None:
            host.add_turn_hook("before_llm", lambda *a, **k: None)
            host.add_turn_hook("after_tool_calls", lambda *a, **k: None)
            host.add_tool_middleware(MiddlewarePhase.GUARD, lambda *a, **k: None)

    agent.install_extension(MultiExtension())
    agent._on_extension_error("multi", "before_llm", RuntimeError("boom 1"))
    agent._on_extension_error("multi", "before_llm", RuntimeError("boom 2"))

    snap_b = build_snapshot(agent, loaded)
    assert len(snap_b.extensions) == 3
    multi_view = snap_b.extensions[2]
    assert multi_view == ExtensionView(
        name="multi",
        kinds=("turn_hook", "tool_middleware"),
        slots=("before_llm", "after_tool_calls", "GUARD"),
        total_failures=2,
        enabled=True,
        consecutive_failures=2,
        tripped=False,
    )

    # 차단된 확장의 스냅샷 반영 확인
    agent.set_extension_breaker(3)
    agent._on_extension_error("multi", "before_llm", RuntimeError("boom 3"))
    snap_c = build_snapshot(agent, loaded)
    assert snap_c.extensions[2] == ExtensionView(
        name="multi",
        kinds=("turn_hook", "tool_middleware"),
        slots=("before_llm", "after_tool_calls", "GUARD"),
        total_failures=3,
        enabled=False,
        consecutive_failures=3,
        tripped=True,
    )


def test_scenario_04_mask_secrets_and_render_snapshot_defense_in_depth(tmp_path: Path) -> None:
    """시나리오 4: 비밀값 마스킹과 렌더링 다층 방어 (WHY 가정 H).

    런타임 결합으로 가짜 키를 만들어 gitleaks 검출을 방지한다.
    """
    fake_or_key = "sk-or-" + "v1-" + ("a" * 36)
    fake_bearer = "Bearer " + ("xyz987_" * 4)

    # (a) mask_secrets 단독 호출
    raw_text = f"key={fake_or_key} auth={fake_bearer} normal=hello"
    masked_text = mask_secrets(raw_text)
    assert fake_or_key not in masked_text
    assert "xyz987_" not in masked_text
    assert "key=***" in masked_text
    assert "Bearer ***" in masked_text
    assert "normal=hello" in masked_text

    # (b) CLI 오버라이드에 비밀값이 섞인 경우의 스냅샷 및 렌더링
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    loaded = load_settings(cli_overrides={"model": f"custom/{fake_or_key}"})
    agent = create_agent(
        loaded.settings,
        session_db=db,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    db.update_token_counts(agent.session_id, input_tokens=120, output_tokens=45)

    snap = build_snapshot(agent, loaded)
    text = render_snapshot(snap)

    # snap.settings 확인
    model_view = next(s for s in snap.settings if s.key == "model")
    assert fake_or_key not in str(model_view.value)
    assert model_view.value == "custom/***"

    # text 확인
    assert fake_or_key not in text
    assert "custom/***" in text
    assert agent.session_id in text
    assert "120" in text
    assert "45" in text
    for key in SETTING_SPECS:
        assert key in text
    assert "compression" in text
    assert "loop_guard" in text


def test_scenario_05_immutability_and_frozen_structures() -> None:
    """시나리오 5: 동결(불변) 보장."""
    loaded = load_settings()
    dummy_agent = types.SimpleNamespace(
        session_id="dummy",
        session_db=None,
        extensions=(
            types.SimpleNamespace(name="dummy_ext", kind="hook", slot="before", failures=0),
        ),
    )
    snap = build_snapshot(dummy_agent, loaded)

    # (a) snap.pending_commands = 9 -> FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        snap.pending_commands = 9  # type: ignore[misc]

    # (b) snap.settings[0].value = 999 -> FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        snap.settings[0].value = 999  # type: ignore[misc]

    # (c) snap.extensions[0].total_failures = 5 -> FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        snap.extensions[0].total_failures = 5  # type: ignore[misc]

    assert isinstance(snap.settings, tuple)
    assert isinstance(snap.extensions, tuple)
    assert isinstance(snap.extensions[0].kinds, tuple)
    assert isinstance(snap.extensions[0].slots, tuple)


def test_scenario_06_edge_cases_and_pending_validation(tmp_path: Path) -> None:
    """시나리오 6: 경계 조건 (빈 확장, DB에 없는 세션, 음수 pending 거부)."""
    loaded = load_settings()

    # (a) extensions=[], DB에 없는 세션
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    dummy_agent = types.SimpleNamespace(
        session_id="nonexistent-session",
        session_db=db,
        extensions=[],
    )
    snap = build_snapshot(dummy_agent, loaded)
    assert snap.extensions == ()
    assert snap.input_tokens == 0
    assert snap.output_tokens == 0
    rendered = render_snapshot(snap)
    assert "(none)" in rendered

    # session_db가 None인 경우도 검증
    dummy_agent_none_db = types.SimpleNamespace(
        session_id="",
        session_db=None,
        extensions=(),
    )
    snap_none_db = build_snapshot(dummy_agent_none_db, loaded)
    assert snap_none_db.input_tokens == 0
    assert snap_none_db.output_tokens == 0

    # (b) pending < 0 거부
    with pytest.raises(ValueError, match="pending must be >= 0"):
        build_snapshot(dummy_agent, loaded, pending=-1)
