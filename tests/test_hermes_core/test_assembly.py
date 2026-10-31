"""Hermes Core 조립 지점 및 extensions 인자 검증 시험 (시나리오 1~8).

근거:
- ADR-0018 §2-3, §2-7, §2-9 (D-03, D-07, D-09)
- 040_기본_확장_조립_분리.md §3-1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.agent.compression_hook import CompressionConfig, CompressionExtension
from hermes_core.agent.loop_guard import (
    LOOP_GUARD_BLOCK_THRESHOLD,
    LOOP_GUARD_WARN_THRESHOLD,
    LoopGuardExtension,
)
from hermes_core.assembly import compression_config_of, default_extensions
from hermes_core.kernel.contract import Extension, ExtensionHost
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher


def test_scenario_1_default_extensions_shape():
    """시나리오 1: 기본 목록의 모양."""
    p = lambda: None
    list1 = default_extensions(p)
    list2 = default_extensions(p)

    assert len(list1) == 2
    assert len(list2) == 2

    assert [type(e) for e in list1] == [CompressionExtension, LoopGuardExtension]
    assert [type(e) for e in list2] == [CompressionExtension, LoopGuardExtension]

    assert [e.name for e in list1] == ["compression", "loop_guard"]
    assert [e.name for e in list2] == ["compression", "loop_guard"]

    # 첫 원소의 _config is p
    assert list1[0]._config is p
    assert list2[0]._config is p

    # 둘째 원소의 임계값
    assert list1[1].warn_threshold == LOOP_GUARD_WARN_THRESHOLD
    assert list1[1].block_threshold == LOOP_GUARD_BLOCK_THRESHOLD
    assert list2[1].warn_threshold == LOOP_GUARD_WARN_THRESHOLD
    assert list2[1].block_threshold == LOOP_GUARD_BLOCK_THRESHOLD

    # 두 번의 결과는 서로 다른 목록이고 원소도 서로 다른 객체(is not)
    assert list1 is not list2
    assert list1[0] is not list2[0]
    assert list1[1] is not list2[1]


def test_scenario_2_compression_config_dynamic_evaluation(tmp_path: Path):
    """시나리오 2: 압축 설정은 부를 때의 값."""
    c1 = MockLLMClient()
    c2 = MockLLMClient()

    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(
        session_db=db,
        llm_client=c1,
        context_window_tokens=1234,
        compression_threshold_ratio=0.5,
        compression_enabled=False,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    first_cfg = compression_config_of(agent)
    assert first_cfg == CompressionConfig(
        enabled=False,
        context_window_tokens=1234,
        threshold_ratio=0.5,
        llm_client=c1,
    )

    # 속성 변경
    agent.compression_enabled = True
    agent.llm_client = c2

    second_cfg = compression_config_of(agent)
    assert second_cfg == CompressionConfig(
        enabled=True,
        context_window_tokens=1234,
        threshold_ratio=0.5,
        llm_client=c2,
    )

    assert agent._compression_config() == second_cfg


def test_scenario_3_extensions_omitted_and_none_equivalence(tmp_path: Path):
    """시나리오 3: 생략과 None은 지금과 같다."""
    db1 = SessionDB(tmp_path / "test1.db")
    agent_omitted = HermesAgent(
        session_db=db1,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    db2 = SessionDB(tmp_path / "test2.db")
    agent_none = HermesAgent(
        session_db=db2,
        extensions=None,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    expected = [
        ("compression", "turn_hook", "before_llm"),
        ("loop_guard", "tool_middleware", "GUARD"),
    ]
    assert [(r.name, r.kind, r.slot) for r in agent_omitted.extensions] == expected
    assert [(r.name, r.kind, r.slot) for r in agent_none.extensions] == expected


def test_scenario_4_default_extensions_active_vs_empty(tmp_path: Path):
    """시나리오 4: 기본 목록이 실제로 동작한다 / 빈 목록이면 아무것도 끼지 않는다."""
    # (a) extensions 생략
    calls_a = [
        MockLLMResponse(
            content=None,
            tool_calls=[{"id": f"call_{i}", "function": {"name": "echo", "arguments": '{"x": 1}'}}],
        )
        for i in range(LOOP_GUARD_BLOCK_THRESHOLD + 5)
    ]
    client_a = MockLLMClient(responses=calls_a)
    db_a = SessionDB(tmp_path / "test_a.db")
    agent_a = HermesAgent(
        session_db=db_a,
        dispatcher=ToolDispatcher(),
        llm_client=client_a,
        max_iterations=LOOP_GUARD_BLOCK_THRESHOLD + 2,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    result_a = agent_a.run_conversation("test query")
    assert result_a["stopped_reason"] == "stuck_loop"
    assert result_a["iterations"] == LOOP_GUARD_BLOCK_THRESHOLD

    # (b) extensions=[]
    calls_b = [
        MockLLMResponse(
            content=None,
            tool_calls=[{"id": f"call_{i}", "function": {"name": "echo", "arguments": '{"x": 1}'}}],
        )
        for i in range(LOOP_GUARD_BLOCK_THRESHOLD + 5)
    ]
    client_b = MockLLMClient(responses=calls_b)
    db_b = SessionDB(tmp_path / "test_b.db")
    agent_b = HermesAgent(
        session_db=db_b,
        dispatcher=ToolDispatcher(),
        llm_client=client_b,
        max_iterations=LOOP_GUARD_BLOCK_THRESHOLD + 2,
        extensions=[],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    assert agent_b.extensions == ()
    result_b = agent_b.run_conversation("test query")
    assert result_b["stopped_reason"] == "max_iterations"
    assert result_b["iterations"] == LOOP_GUARD_BLOCK_THRESHOLD + 2


def test_scenario_5_user_extensions_ordering_and_isolation(tmp_path: Path):
    """시나리오 5: 사용자 목록은 그 목록만, 그 순서로."""
    hook_calls: list[str] = []

    class DummyExtension:
        def __init__(self, name: str):
            self.name = name

        def install(self, host: ExtensionHost) -> None:
            def _hook(turn_ctx: Any) -> None:
                hook_calls.append(self.name)

            host.add_turn_hook("before_llm", _hook)

    ext_a = DummyExtension("a")
    ext_b = DummyExtension("b")

    client = MockLLMClient(responses=[MockLLMResponse(content="final reply")])
    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(
        session_db=db,
        llm_client=client,
        extensions=[ext_a, ext_b],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    assert [r.name for r in agent.extensions] == ["a", "b"]
    agent.run_conversation("hello")
    assert hook_calls == ["a", "b"]


def test_scenario_6_duplicate_names_rejected_before_side_effects(tmp_path: Path):
    """시나리오 6: 중복 이름은 부수 효과 전에 거절."""
    installed_calls: list[str] = []

    class DummyExtension:
        def __init__(self, name: str):
            self.name = name

        def install(self, host: ExtensionHost) -> None:
            installed_calls.append(self.name)

    ext1 = DummyExtension("dup")
    ext2 = DummyExtension("dup")

    db = SessionDB(tmp_path / "test.db")
    with pytest.raises(ValueError, match="Duplicate extension name 'dup'"):
        HermesAgent(
            session_id="s_dup",
            session_db=db,
            extensions=[ext1, ext2],
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
        )

    # 원장에 세션 행이 생기지 않음
    assert db.get_session("s_dup") is None
    # 두 확장 모두 install이 불리지 않음
    assert installed_calls == []


def test_scenario_7_generator_iterable_consumed_once(tmp_path: Path):
    """시나리오 7: 한 번만 꺼내 쓴다."""
    install_count = 0

    class DummyExtension:
        name = "a"

        def install(self, host: ExtensionHost) -> None:
            nonlocal install_count
            install_count += 1
            host.add_turn_hook("before_llm", lambda ctx: None)

    ext = DummyExtension()
    gen = (e for e in [ext])

    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(
        session_db=db,
        extensions=gen,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    assert [r.name for r in agent.extensions] == ["a"]
    assert install_count == 1


def test_scenario_8_post_init_install_extension_behavior(tmp_path: Path):
    """시나리오 8: 생성 뒤 설치는 그대로."""
    class DummyExtension:
        def __init__(self, name: str):
            self.name = name

        def install(self, host: ExtensionHost) -> None:
            host.add_turn_hook("before_llm", lambda ctx: None)

    ext_a1 = DummyExtension("a")
    ext_a2 = DummyExtension("a")

    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(
        session_db=db,
        extensions=[],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    agent.install_extension(ext_a1)
    assert [r.name for r in agent.extensions] == ["a"]

    with pytest.raises(ValueError, match="Extension 'a' is already installed."):
        agent.install_extension(ext_a2)
