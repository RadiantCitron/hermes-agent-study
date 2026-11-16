"""Settings 모델, 계층 로더, 조립 지점 및 CLI 연결 인수 테스트 (시나리오 1~11).

근거:
- ADR-0019 D-02, D-03, D-04
- 로드맵 19 구현지시서 010 §3-1, §3-2
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import io
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from hermes_core import cli
from hermes_core.agent import MockLLMClient
from hermes_core.assembly import create_agent
from hermes_core.constants import (
    COMPRESSION_THRESHOLD_RATIO,
    CONTEXT_WINDOW_TOKENS,
    LOOP_GUARD_BLOCK_THRESHOLD,
    LOOP_GUARD_WARN_THRESHOLD,
)
from hermes_core.control import (
    SETTING_SPECS,
    LoadedSettings,
    Settings,
    SettingsError,
    load_settings,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """CLI 테스트 실행 도우미."""
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cli.main()


# ==============================================================================
# 3-1. 설정 스냅샷·계층 로더·검증 (시나리오 1~7)
# ==============================================================================


def test_scenario_01_default_layer_and_sources(tmp_path: Path):
    """시나리오 1: 아무 계층도 없음(기본값과 출처)."""
    no_file = tmp_path / "no.toml"
    loaded = load_settings(config_path=no_file, environ={}, cli_overrides={})

    assert loaded.file_path is None
    expected_settings = Settings(
        model=None,
        provider=None,
        max_iterations=50,
        context_window_tokens=CONTEXT_WINDOW_TOKENS,
        compression_enabled=True,
        compression_threshold_ratio=COMPRESSION_THRESHOLD_RATIO,
        loop_guard_warn_threshold=LOOP_GUARD_WARN_THRESHOLD,
        loop_guard_block_threshold=LOOP_GUARD_BLOCK_THRESHOLD,
        skills_index=True,
        delegate=False,
        approval_enabled=True,
        checkpoints_enabled=True,
        breaker_threshold=3,
    )
    assert loaded.settings == expected_settings
    assert len(loaded.sources) == 13
    for k in SETTING_SPECS:
        assert loaded.sources[k] == "default"


def test_scenario_02_four_layer_priority_and_sources(tmp_path: Path):
    """시나리오 2: 4계층 우선순위와 출처 추적."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "max_iterations = 10\n"
        "context_window_tokens = 4000\n"
        'model = "from-file"\n',
        encoding="utf-8",
    )
    environ = {
        "HERMES_CONTEXT_WINDOW_TOKENS": "8000",
        "HERMES_MODEL": "from-env",
    }
    cli_overrides = {
        "model": "from-cli",
        "delegate": None,
    }

    loaded = load_settings(
        config_path=config_file,
        environ=environ,
        cli_overrides=cli_overrides,
    )

    assert loaded.file_path == config_file.resolve()
    assert loaded.settings.max_iterations == 10
    assert loaded.sources["max_iterations"] == "file"

    assert loaded.settings.context_window_tokens == 8000
    assert loaded.sources["context_window_tokens"] == "env"

    assert loaded.settings.model == "from-cli"
    assert loaded.sources["model"] == "cli"

    assert loaded.settings.delegate is False
    assert loaded.sources["delegate"] == "default"


def test_scenario_03_unknown_nested_and_syntax_errors(tmp_path: Path):
    """시나리오 3: 모르는 키·중첩 테이블·문법 오류 거부."""
    # (a) 파일에 unknown_opt = 1
    f1 = tmp_path / "c1.toml"
    f1.write_text("unknown_opt = 1\n", encoding="utf-8")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(config_path=f1, environ={})
    assert exc_info.value.layer == "file"
    assert exc_info.value.key == "unknown_opt"

    # (b) cli_overrides={"no_such": 1}
    with pytest.raises(SettingsError) as exc_info:
        load_settings(
            config_path=tmp_path / "no.toml",
            environ={},
            cli_overrides={"no_such": 1},
        )
    assert exc_info.value.layer == "cli"
    assert exc_info.value.key == "no_such"

    # (c) 파일에 [section]\nmax_iterations = 5
    f2 = tmp_path / "c2.toml"
    f2.write_text("[section]\nmax_iterations = 5\n", encoding="utf-8")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(config_path=f2, environ={})
    assert exc_info.value.layer == "file"
    assert exc_info.value.key == "section"

    # (d) 파일에 깨진 TOML max_iterations =
    f3 = tmp_path / "c3.toml"
    f3.write_text("max_iterations =\n", encoding="utf-8")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(config_path=f3, environ={})
    assert exc_info.value.layer == "file"
    assert exc_info.value.key == "<toml>"


def test_scenario_04_boundary_and_invalid_values(tmp_path: Path):
    """시나리오 4: 경계값(작은 값 포함) 및 범위 밖 값 검증."""
    no_file = tmp_path / "no.toml"

    # (a) 유효한 최소 경계값
    valid_cli = {
        "max_iterations": 1,
        "context_window_tokens": 1,
        "compression_threshold_ratio": 0.01,
        "loop_guard_warn_threshold": 2,
        "loop_guard_block_threshold": 3,
        "breaker_threshold": 0,
    }
    loaded = load_settings(config_path=no_file, environ={}, cli_overrides=valid_cli)
    assert loaded.settings.max_iterations == 1
    assert loaded.settings.context_window_tokens == 1
    assert loaded.settings.compression_threshold_ratio == 0.01
    assert loaded.settings.loop_guard_warn_threshold == 2
    assert loaded.settings.loop_guard_block_threshold == 3
    assert loaded.settings.breaker_threshold == 0

    # compression_threshold_ratio = 1.0 유효
    loaded_max_ratio = load_settings(
        config_path=no_file,
        environ={},
        cli_overrides={"compression_threshold_ratio": 1.0},
    )
    assert loaded_max_ratio.settings.compression_threshold_ratio == 1.0

    # (b) 무효값 검증
    invalid_cases: list[tuple[str, Any]] = [
        ("max_iterations", 0),
        ("max_iterations", True),
        ("context_window_tokens", 0),
        ("compression_threshold_ratio", 0.0),
        ("compression_threshold_ratio", 1.5),
        ("compression_threshold_ratio", float("nan")),
        ("loop_guard_warn_threshold", 1),
        ("breaker_threshold", -1),
        ("model", ""),
        ("model", "   "),
    ]

    for key, val in invalid_cases:
        with pytest.raises(SettingsError) as exc_info:
            load_settings(
                config_path=no_file,
                environ={},
                cli_overrides={key: val},
            )
        assert exc_info.value.layer == "cli"
        assert exc_info.value.key == key

    # loop_guard_block_threshold <= warn_threshold 검증
    with pytest.raises(SettingsError) as exc_info:
        load_settings(
            config_path=no_file,
            environ={},
            cli_overrides={
                "loop_guard_warn_threshold": 3,
                "loop_guard_block_threshold": 3,
            },
        )
    assert exc_info.value.key == "loop_guard_block_threshold"


def test_scenario_05_cross_validation_layer_attribution(tmp_path: Path):
    """시나리오 5: 교차 검증 계층 귀속."""
    # (a) 파일에 warn=4 + 환경변수에 block="4" -> env 실패
    f1 = tmp_path / "c1.toml"
    f1.write_text("loop_guard_warn_threshold = 4\n", encoding="utf-8")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(
            config_path=f1,
            environ={"HERMES_LOOP_GUARD_BLOCK_THRESHOLD": "4"},
        )
    assert exc_info.value.layer == "env"
    assert exc_info.value.key == "loop_guard_block_threshold"

    # (b) 파일에 warn=6 (기본 block=5) -> file 실패
    f2 = tmp_path / "c2.toml"
    f2.write_text("loop_guard_warn_threshold = 6\n", encoding="utf-8")
    with pytest.raises(SettingsError) as exc_info:
        load_settings(config_path=f2, environ={})
    assert exc_info.value.layer == "file"
    assert exc_info.value.key == "loop_guard_block_threshold"


def test_scenario_06_env_parsing_and_irrelevant_env_ignored(tmp_path: Path):
    """시나리오 6: 환경변수 불리언·숫자 해석 및 무관한 HERMES_* 무시."""
    no_file = tmp_path / "no.toml"

    # (a) 불리언 파싱
    for true_str in ("1", "true", "TRUE", "yes"):
        loaded = load_settings(
            config_path=no_file,
            environ={"HERMES_COMPRESSION_ENABLED": true_str},
        )
        assert loaded.settings.compression_enabled is True

    for false_str in ("0", "false", "NO"):
        loaded = load_settings(
            config_path=no_file,
            environ={"HERMES_COMPRESSION_ENABLED": false_str},
        )
        assert loaded.settings.compression_enabled is False

    # (b) 무효한 불리언 문자열
    for bad_bool in ("maybe", ""):
        with pytest.raises(SettingsError) as exc_info:
            load_settings(
                config_path=no_file,
                environ={"HERMES_COMPRESSION_ENABLED": bad_bool},
            )
        assert exc_info.value.layer == "env"
        assert exc_info.value.key == "compression_enabled"

    # (c) 숫자에 문자열
    with pytest.raises(SettingsError) as exc_info:
        load_settings(
            config_path=no_file,
            environ={"HERMES_MAX_ITERATIONS": "abc"},
        )
    assert exc_info.value.layer == "env"
    assert exc_info.value.key == "max_iterations"

    # (d) 무관한 HERMES_* 환경변수 무시
    loaded = load_settings(
        config_path=no_file,
        environ={"HERMES_HOME": "/tmp/x", "HERMES_TEST_KEEP": "keep"},
    )
    assert loaded.settings == Settings()


def test_scenario_07_immutability_guarantees(tmp_path: Path):
    """시나리오 7: 동결(불변) 보장."""
    loaded = load_settings(environ={}, config_path=tmp_path / "no.toml")

    # (a) settings 필드 수정 거부
    with pytest.raises(FrozenInstanceError):
        loaded.settings.max_iterations = 99  # type: ignore[misc]

    # (b) loaded 필드 수정 거부
    with pytest.raises(FrozenInstanceError):
        loaded.file_path = None  # type: ignore[misc]

    # (c) sources 사전 수정 거부
    with pytest.raises(TypeError):
        loaded.sources["model"] = "cli"  # type: ignore[index]

    # (d) SETTING_SPECS 수정 거부
    with pytest.raises(TypeError):
        SETTING_SPECS["model"] = None  # type: ignore[index]


# ==============================================================================
# 3-2. 조립 지점과 CLI 연결 (시나리오 8~11)
# ==============================================================================


def test_scenario_08_create_agent_and_late_binding_compression(tmp_path: Path):
    """시나리오 8: create_agent 기본 설정 및 늦게 묶인 압축 설정."""
    db = SessionDB(tmp_path / "state.db")
    settings = load_settings(environ={}, config_path=tmp_path / "no.toml").settings

    agent = create_agent(
        settings,
        session_db=db,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    assert agent.max_iterations == 50
    assert agent.context_window_tokens == CONTEXT_WINDOW_TOKENS
    assert agent.skills_index is True
    assert [r.name for r in agent.extensions] == ["compression", "loop_guard"]

    # CompressionExtension config 확인
    comp_ext = next(ext for ext in agent._installed_extensions if ext.name == "compression")
    cfg = comp_ext._config()  # type: ignore[attr-defined]
    assert cfg.context_window_tokens == CONTEXT_WINDOW_TOKENS
    assert cfg.enabled is True

    # agent 속성 변경 후 다시 _config() 호출
    agent.context_window_tokens = 2048
    agent.compression_enabled = False

    cfg2 = comp_ext._config()  # type: ignore[attr-defined]
    assert cfg2.context_window_tokens == 2048
    assert cfg2.enabled is False


def test_scenario_09_create_agent_small_boundary_execution(tmp_path: Path):
    """시나리오 9: create_agent의 작은 경계값 동작 (동어반복 방지)."""
    db = SessionDB(tmp_path / "state.db")

    # (a) max_iterations=2 동작 검증
    disp1 = ToolDispatcher()
    disp1.register_tool("echo", lambda x: json.dumps({"echo": x}), {})

    client1 = MockLLMClient()
    for i in range(5):
        client1.queue_response(
            tool_calls=[{
                "id": f"c{i}",
                "function": {"name": "echo", "arguments": json.dumps({"x": i})},
            }],
        )

    agent1 = create_agent(
        Settings(max_iterations=2),
        session_db=db,
        dispatcher=disp1,
        llm_client=client1,
        quiet_mode=True,
    )
    res1 = agent1.run_conversation("start")
    assert res1["stopped_reason"] == "max_iterations"
    assert res1["iterations"] == 2

    # (b) loop_guard_warn_threshold=2, block_threshold=3 검증
    disp2 = ToolDispatcher()
    disp2.register_tool("echo", lambda x: json.dumps({"echo": x}), {})

    client2 = MockLLMClient()
    for i in range(10):
        client2.queue_response(
            tool_calls=[{
                "id": f"c{i}",
                "function": {"name": "echo", "arguments": '{"x": 1}'},
            }],
        )

    agent2 = create_agent(
        Settings(
            loop_guard_warn_threshold=2,
            loop_guard_block_threshold=3,
            max_iterations=10,
        ),
        session_db=db,
        dispatcher=disp2,
        llm_client=client2,
        quiet_mode=True,
    )
    res2 = agent2.run_conversation("start")
    assert res2["stopped_reason"] == "stuck_loop"
    assert res2["iterations"] == 3

    tool_msgs = [m for m in res2["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    t1 = json.loads(tool_msgs[0]["content"])
    t2 = json.loads(tool_msgs[1]["content"])
    t3 = json.loads(tool_msgs[2]["content"])
    assert "_loop_warning" not in t1
    assert "_loop_warning" in t2
    assert t3["success"] is False


def test_scenario_10_cli_mock_execution_and_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """시나리오 10: CLI --mock 기본 실행과 설정 파일·환경변수 반영."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    captured_agents: list[Any] = []
    orig_init = cli.HermesAgent.__init__

    def tracked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        captured_agents.append(self)

    monkeypatch.setattr(cli.HermesAgent, "__init__", tracked_init)

    # (a) 아무 파일·옵션 없이 CLI --mock
    _run_cli(monkeypatch, "--mock")
    assert len(captured_agents) == 1
    a1 = captured_agents[-1]
    assert a1.max_iterations == 50
    assert a1.context_window_tokens == CONTEXT_WINDOW_TOKENS
    assert a1.skills_index is True

    # (b) config.toml 에 max_iterations=7, context_window_tokens=4096 작성 후 실행
    cfg_file = hermes_home / "config.toml"
    cfg_file.write_text(
        "max_iterations = 7\n"
        "context_window_tokens = 4096\n",
        encoding="utf-8",
    )
    _run_cli(monkeypatch, "--mock")
    assert len(captured_agents) == 2
    a2 = captured_agents[-1]
    assert a2.max_iterations == 7
    assert a2.context_window_tokens == 4096

    # (c) CLI 옵션으로 context-window 2048 오버라이드
    _run_cli(monkeypatch, "--mock", "--context-window", "2048")
    assert len(captured_agents) == 3
    a3 = captured_agents[-1]
    assert a3.max_iterations == 7
    assert a3.context_window_tokens == 2048


def test_scenario_11_cli_invalid_settings_exit_code_2_no_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """시나리오 11: CLI 잘못된 설정 시 DB 미생성 및 종료 코드 2."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg_file = hermes_home / "config.toml"
    cfg_file.write_text("max_iterations = 0\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--mock")

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "max_iterations" in err
    assert not (hermes_home / "state.db").exists()
