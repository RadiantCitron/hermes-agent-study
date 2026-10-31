"""단일 제어판 쓰기 명령, 턴 경계 반영, 감사 기록(DB v7) 인수 테스트 (시나리오 1~10).

근거:
- ADR-0019 D-02, D-08, D-09
- 로드맵 19 구현지시서 040 §3-1
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import pytest

from hermes_core import cli
from hermes_core.agent.agent import (
    EVENT_EXTENSION_TRIPPED,
    HermesAgent,
    MockLLMClient,
    MockLLMResponse,
)
from hermes_core.assembly import apply_runtime_settings, create_agent
from hermes_core.control import (
    ControlPanel,
    DisableExtension,
    EnableExtension,
    SetSetting,
    load_settings,
)
from hermes_core.kernel.contract import MiddlewarePhase
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher

ECHO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echo input value.",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        },
    },
}


def _create_test_dispatcher() -> ToolDispatcher:
    dispatcher = ToolDispatcher()
    dispatcher.register_tool(
        "echo",
        lambda x=0: json.dumps({"success": True, "x": x}),
        ECHO_SCHEMA,
    )
    return dispatcher


def test_scenario_01_schema_v7_and_column_validation(tmp_path: Path) -> None:
    """시나리오 1: 새 DB 스키마 v7 및 control_audit 열·인자 검증."""
    db_path = tmp_path / "new.db"
    db = SessionDB(db_path)
    assert db.schema_version == 7

    cursor = db._conn.execute("PRAGMA table_info(control_audit)")
    cols = {row["name"] for row in cursor.fetchall()}
    expected_cols = {
        "id",
        "session_id",
        "created_at",
        "source",
        "command",
        "target",
        "old_value",
        "new_value",
        "result",
        "reason",
    }
    assert cols == expected_cols

    # 세션 하나 생성
    db.create_session("s_valid")

    # (b) 잘못된 인자 검증
    # 잘못된 command
    with pytest.raises(ValueError, match="Invalid audit command"):
        db.record_control_audit(
            "s_valid",
            source="cli",
            command="drop_table",
            target="max_iterations",
            result="accepted",
        )

    # 잘못된 result
    with pytest.raises(ValueError, match="Invalid audit result"):
        db.record_control_audit(
            "s_valid",
            source="cli",
            command="set_setting",
            target="max_iterations",
            result="unknown",
        )

    # 빈 target
    with pytest.raises(ValueError, match="target must be a non-empty string"):
        db.record_control_audit(
            "s_valid",
            source="cli",
            command="set_setting",
            target="",
            result="accepted",
        )

    # 없는 session_id (외래키 제약 실패)
    with pytest.raises(sqlite3.IntegrityError):
        db.record_control_audit(
            "s_non_existent",
            source="cli",
            command="set_setting",
            target="max_iterations",
            result="accepted",
        )

    # 표에 행이 늘지 않았는지 확인
    audit_rows = db.list_control_audit("s_valid")
    assert len(audit_rows) == 0


def test_scenario_02_v6_migration_preserves_data(tmp_path: Path) -> None:
    """시나리오 2: v6 옛 DB -> v7 자동 마이그레이션 및 기존 6개 표 데이터 보존."""
    db_path = tmp_path / "legacy_v6.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (6);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            mode TEXT
        );
        INSERT INTO sessions (id, source, started_at) VALUES ('s_v6', 'test', 1000.0);

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            finish_reason TEXT,
            active INTEGER DEFAULT 1,
            compacted INTEGER DEFAULT 0,
            is_copy INTEGER DEFAULT 0
        );
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_v6', 'user', 'hello', 1001.0);

        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            model TEXT NOT NULL
        );
        INSERT INTO llm_calls (session_id, created_at, model) VALUES ('s_v6', 1002.0, 'test-model');

        CREATE TABLE approval_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            argv_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            decision TEXT NOT NULL
        );
        INSERT INTO approval_requests (session_id, tool_name, argv_json, created_at, decision)
            VALUES ('s_v6', 'terminal', '["ls"]', 1003.0, 'approved');

        CREATE TABLE file_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            workspace TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            before_exists INTEGER NOT NULL,
            before_content TEXT,
            after_sha256 TEXT,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL,
            reason TEXT
        );
        INSERT INTO file_checkpoints (session_id, turn_index, tool_name, workspace, rel_path, before_exists, status, created_at)
            VALUES ('s_v6', 1, 'write_file', '/ws', 'a.txt', 0, 'applied', 1004.0);
        """
    )
    conn.commit()
    conn.close()

    db = SessionDB(db_path)
    assert db.schema_version == 7

    # control_audit 표 생성 확인
    cursor = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='control_audit'"
    )
    assert cursor.fetchone() is not None

    # 기존 데이터 보존 확인
    s = db.get_session("s_v6")
    assert s is not None and s["id"] == "s_v6"
    msgs = db.get_messages_as_conversation("s_v6")
    assert len(msgs) == 1 and msgs[0]["content"] == "hello"
    ckpts = db.get_file_checkpoints("s_v6")
    assert len(ckpts) == 1 and ckpts[0]["rel_path"] == "a.txt"

    # 새 감사 기록 동작 확인
    aid = db.record_control_audit(
        "s_v6",
        source="cli",
        command="set_setting",
        target="max_iterations",
        old_value="50",
        new_value="10",
        result="accepted",
    )
    assert aid > 0
    records = db.list_control_audit("s_v6")
    assert len(records) == 1
    assert records[0]["target"] == "max_iterations"


def test_scenario_03_runtime_settings_queue_and_turn_boundary(tmp_path: Path) -> None:
    """시나리오 3: 런타임 설정 변경의 대기열 적재와 턴 경계 반영 (WHY 가정 G)."""
    db = SessionDB(tmp_path / "s3.db")
    dispatcher = _create_test_dispatcher()

    loaded = load_settings()
    mock_llm = MockLLMClient([MockLLMResponse(content="1턴 응답")])
    agent = create_agent(
        loaded.settings,
        model="mock",
        llm_client=mock_llm,
        session_id="s_test3",
        session_db=db,
        dispatcher=dispatcher,
    )

    panel = ControlPanel(
        agent,
        loaded,
        db,
        apply_settings_fn=apply_runtime_settings,
    )

    # 1. submit 호출
    res1 = panel.submit(SetSetting("max_iterations", 7))
    res2 = panel.submit(SetSetting("breaker_threshold", 1))

    assert res1.accepted is True
    assert res2.accepted is True
    assert panel.pending_count == 2
    snap_before = panel.snapshot()
    assert snap_before.pending_commands == 2

    # 반영 전: 에이전트 속성 무변경
    assert agent.max_iterations == 50
    assert agent._breaker_threshold == 3

    audits_before = db.list_control_audit("s_test3")
    assert len(audits_before) == 2
    assert audits_before[0]["result"] == "accepted"
    assert audits_before[0]["old_value"] == "50" and audits_before[0]["new_value"] == "7"
    assert audits_before[1]["result"] == "accepted"
    assert audits_before[1]["old_value"] == "3" and audits_before[1]["new_value"] == "1"

    # 2. apply_pending 호출
    applied = panel.apply_pending()
    assert len(applied) == 2
    assert panel.pending_count == 0
    assert agent.max_iterations == 7
    assert agent._breaker_threshold == 1

    snap_after = panel.snapshot()
    assert snap_after.pending_commands == 0
    max_iter_view = next(s for s in snap_after.settings if s.key == "max_iterations")
    assert max_iter_view.value == 7
    assert max_iter_view.source == "cli"

    audits_after = db.list_control_audit("s_test3")
    assert len(audits_after) == 4
    results = [a["result"] for a in audits_after]
    assert results == ["accepted", "accepted", "applied", "applied"]

    # 실제 1턴 실행 시 정상 동작 확인
    conv_res = agent.run_conversation("테스트 메시지")
    assert conv_res["final_response"] == "1턴 응답"


def test_scenario_04_locked_settings_rejection_and_defense_in_depth(tmp_path: Path) -> None:
    """시나리오 4: 보안 설정(locked) 하향 거부 및 다층 방어 (WHY 가정 F, 위험 8)."""
    db = SessionDB(tmp_path / "s4.db")
    dispatcher = _create_test_dispatcher()

    loaded = load_settings()
    agent = create_agent(
        loaded.settings,
        model="mock",
        session_id="s_test4",
        session_db=db,
        dispatcher=dispatcher,
    )
    panel = ControlPanel(agent, loaded, db, apply_settings_fn=apply_runtime_settings)

    # (a) submit으로 locked 설정 변경 시도
    res1 = panel.submit(SetSetting("approval_enabled", False))
    res2 = panel.submit(SetSetting("checkpoints_enabled", False))

    assert res1.accepted is False
    assert res1.rejection is not None and res1.rejection.code == "locked"
    assert res2.accepted is False
    assert res2.rejection is not None and res2.rejection.code == "locked"

    assert panel.pending_count == 0
    assert panel.apply_pending() == []

    audits = db.list_control_audit("s_test4")
    assert len(audits) == 2
    assert audits[0]["result"] == "rejected" and "locked" in (audits[0]["reason"] or "")
    assert audits[1]["result"] == "rejected" and "locked" in (audits[1]["reason"] or "")

    # (b) apply_runtime_settings를 직접 불러도 locked 변경 거부 (다층 방어)
    with pytest.raises(ValueError, match="not runtime-mutable"):
        apply_runtime_settings(
            agent,
            dataclasses.replace(loaded.settings, approval_enabled=False),
            changed_keys=["approval_enabled"],
        )
    assert loaded.settings.approval_enabled is True


def test_scenario_05_rejections_and_invalid_commands(tmp_path: Path) -> None:
    """시나리오 5: 시작 전용·모르는 키·범위 밖 값·모르는 확장 거부 및 폭주 방지."""
    db = SessionDB(tmp_path / "s5.db")
    dispatcher = _create_test_dispatcher()

    loaded = load_settings()
    agent = create_agent(
        loaded.settings,
        model="mock",
        session_id="s_test5",
        session_db=db,
        dispatcher=dispatcher,
    )
    panel = ControlPanel(agent, loaded, db, apply_settings_fn=apply_runtime_settings)

    commands = [
        SetSetting("model", "gpt-5"),
        SetSetting("skills_index", False),
        SetSetting("loop_guard_warn_threshold", 4),
        SetSetting("no_such_key", 1),
        SetSetting("max_iterations", 0),
        SetSetting("compression_threshold_ratio", 1.5),
        EnableExtension("no_such_ext"),
        DisableExtension("no_such_ext"),
    ]

    expected_codes = [
        "startup_only",
        "startup_only",
        "startup_only",
        "unknown_key",
        "bad_value",
        "bad_value",
        "unknown_extension",
        "unknown_extension",
    ]

    for cmd, exp_code in zip(commands, expected_codes):
        res = panel.submit(cmd)
        assert res.accepted is False
        assert res.rejection is not None
        assert res.rejection.code == exp_code

    assert panel.pending_count == 0
    assert agent.max_iterations == 50

    audits = db.list_control_audit("s_test5")
    assert len(audits) == 8
    for row in audits:
        assert row["result"] == "rejected"


def test_scenario_06_audit_before_write_principle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 6: 감사 선기록 원칙 — 기록 실패 시 미적재·미반영 (WHY 가정 G, 위험 7)."""
    db = SessionDB(tmp_path / "s6.db")
    dispatcher = _create_test_dispatcher()

    loaded = load_settings()
    agent = create_agent(
        loaded.settings,
        model="mock",
        session_id="s_test6",
        session_db=db,
        dispatcher=dispatcher,
    )
    panel = ControlPanel(agent, loaded, db, apply_settings_fn=apply_runtime_settings)

    # (a) submit 시 감사 기록 실패 -> 미적재 및 에이전트 무변경
    orig_record = db.record_control_audit

    def failing_record(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "record_control_audit", failing_record)
    with pytest.raises(RuntimeError, match="disk full"):
        panel.submit(SetSetting("max_iterations", 5))

    assert panel.pending_count == 0
    assert agent.max_iterations == 50

    # (b) 정상 적재 후 apply_pending 시 감사 기록 실패 -> 미반영 및 대기열 보존
    monkeypatch.setattr(db, "record_control_audit", orig_record)
    panel.submit(SetSetting("max_iterations", 5))
    panel.submit(DisableExtension("loop_guard"))
    assert panel.pending_count == 2

    monkeypatch.setattr(db, "record_control_audit", failing_record)
    with pytest.raises(RuntimeError, match="disk full"):
        panel.apply_pending()

    # 에이전트 상태는 여전히 무변경이고 대기열에 2개 모두 남아 있음
    assert agent.max_iterations == 50
    st_dict = {st.name: st.enabled for st in agent.extension_status()}
    assert st_dict["loop_guard"] is True
    assert panel.pending_count == 2

    # 패치 해제 후 다시 apply_pending 호출 시 정상 반영
    monkeypatch.setattr(db, "record_control_audit", orig_record)
    applied = panel.apply_pending()
    assert len(applied) == 2
    assert panel.pending_count == 0
    assert agent.max_iterations == 5
    st_dict_after = {st.name: st.enabled for st in agent.extension_status()}
    assert st_dict_after["loop_guard"] is False


def test_scenario_07_enable_disable_extension_turn_boundary(tmp_path: Path) -> None:
    """시나리오 7: 확장 끄기·켜기(DisableExtension / EnableExtension)의 턴 경계 반영."""
    db = SessionDB(tmp_path / "s7.db")
    dispatcher = _create_test_dispatcher()

    loaded = load_settings()
    # 5회 동일 도구 호출 반복 응답 (반복 도구 호출 패턴)
    repeat_responses = []
    for i in range(5):
        repeat_responses.append(
            MockLLMResponse(
                content=None,
                tool_calls=[{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"x": 100})},
                }],
            )
        )
    repeat_responses.append(MockLLMResponse(content="5회 반복 완료"))

    mock_llm = MockLLMClient(list(repeat_responses))
    agent = create_agent(
        loaded.settings,
        model="mock",
        llm_client=mock_llm,
        session_id="s_test7",
        session_db=db,
        dispatcher=dispatcher,
    )
    panel = ControlPanel(agent, loaded, db, apply_settings_fn=apply_runtime_settings)

    # (a) submit 직후(반영 전): loop_guard는 아직 켜져 있음
    panel.submit(DisableExtension("loop_guard"))
    st_before = {st.name: st.enabled for st in agent.extension_status()}
    assert st_before["loop_guard"] is True

    # (b) apply_pending 후: loop_guard 꺼짐 -> 5회 동일 도구 호출해도 stuck_loop 없이 completed
    panel.apply_pending()
    st_applied = {st.name: st.enabled for st in agent.extension_status()}
    assert st_applied["loop_guard"] is False

    conv_res = agent.run_conversation("반복 도구 호출 시작")
    assert conv_res.get("stopped_reason") != "stuck_loop"
    assert conv_res["final_response"] == "5회 반복 완료"

    audits = db.list_control_audit("s_test7")
    disable_audits = [a for a in audits if a["command"] == "disable_extension"]
    assert len(disable_audits) == 2
    assert disable_audits[0]["result"] == "accepted" and disable_audits[0]["new_value"] == "false"
    assert disable_audits[1]["result"] == "applied" and disable_audits[1]["new_value"] == "false"

    # (c) EnableExtension 제출 후 반영: loop_guard 재활성화
    panel.submit(EnableExtension("loop_guard"))
    panel.apply_pending()
    st_re_enabled = {st.name: st.enabled for st in agent.extension_status()}
    assert st_re_enabled["loop_guard"] is True

    audits_all = db.list_control_audit("s_test7")
    enable_audits = [a for a in audits_all if a["command"] == "enable_extension"]
    assert len(enable_audits) == 2
    assert enable_audits[0]["result"] == "accepted" and enable_audits[0]["new_value"] == "true"
    assert enable_audits[1]["result"] == "applied" and enable_audits[1]["new_value"] == "true"


def test_scenario_08_breaker_audit_logging_and_resilience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 8: 차단기 작동 시 감사 원장 자동 기록 및 기록 실패 시에도 차단 보장 (ROADMAP §6-4, 030 §5)."""
    # (a) breaker_threshold = 2인 에이전트에서 확장 'flaky'가 2회 연속 실패 시 감사 기록 확인
    db = SessionDB(tmp_path / "s8.db")
    dispatcher = _create_test_dispatcher()

    class FlakyMw:
        name = "flaky"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                raise RuntimeError("flaky failure")

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    flaky_ext = FlakyMw()
    events: list[Any] = []
    agent = HermesAgent(
        model="mock",
        llm_client=MockLLMClient([
            MockLLMResponse(
                content=None,
                tool_calls=[{"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            ),
            MockLLMResponse(content="1턴 끝"),
            MockLLMResponse(
                content=None,
                tool_calls=[{"id": "2", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            ),
            MockLLMResponse(content="2턴 끝"),
        ]),
        session_id="s_test8",
        session_db=db,
        dispatcher=dispatcher,
        extensions=[flaky_ext],
        event_sink=lambda ev: events.append(ev),
    )
    agent.set_extension_breaker(2)

    # 1턴: 실패 1회 (차단 안 됨)
    agent.run_conversation("턴 1")
    st1 = next(s for s in agent.extension_status() if s.name == "flaky")
    assert st1.enabled is True
    assert st1.consecutive_failures == 1

    # 2턴: 실패 2회째 -> 차단 발생
    agent.run_conversation("턴 2")
    st2 = next(s for s in agent.extension_status() if s.name == "flaky")
    assert st2.enabled is False
    assert st2.tripped is True

    audits = db.list_control_audit("s_test8")
    breaker_audits = [a for a in audits if a["source"] == "breaker"]
    assert len(breaker_audits) == 1
    rec = breaker_audits[0]
    assert rec["command"] == "disable_extension"
    assert rec["target"] == "flaky"
    assert rec["old_value"] == "true"
    assert rec["new_value"] == "false"
    assert rec["result"] == "applied"
    assert rec["reason"] == "consecutive_failures=2"

    # (b) 감사 기록이 예외를 던져도 차단은 정상 완료됨
    db_b = SessionDB(tmp_path / "s8b.db")

    def failing_audit(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("db error")

    monkeypatch.setattr(db_b, "record_control_audit", failing_audit)

    class FlakyMw2:
        name = "flaky2"

        def install(self, host: Any) -> None:
            def mw(call: ToolCall, next_fn: Any) -> Any:
                raise RuntimeError("flaky2 failure")

            host.add_tool_middleware(MiddlewarePhase.GUARD, mw)

    events_b: list[Any] = []
    agent_b = HermesAgent(
        model="mock",
        llm_client=MockLLMClient([
            MockLLMResponse(
                content=None,
                tool_calls=[{"id": "1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            ),
            MockLLMResponse(content="1턴"),
            MockLLMResponse(
                content=None,
                tool_calls=[{"id": "2", "type": "function", "function": {"name": "echo", "arguments": "{}"}}],
            ),
            MockLLMResponse(content="2턴"),
        ]),
        session_id="s_test8b",
        session_db=db_b,
        dispatcher=dispatcher,
        extensions=[FlakyMw2()],
        event_sink=lambda ev: events_b.append(ev),
    )
    agent_b.set_extension_breaker(2)

    agent_b.run_conversation("턴 1")
    agent_b.run_conversation("턴 2")

    st_b = next(s for s in agent_b.extension_status() if s.name == "flaky2")
    assert st_b.enabled is False
    assert st_b.tripped is True
    assert any(getattr(e, "kind", getattr(e, "event_type", None)) == EVENT_EXTENSION_TRIPPED for e in events_b)


def test_scenario_09_masking_secrets_in_audit_records(tmp_path: Path) -> None:
    """시나리오 9: 감사 기록의 비밀값 마스킹 (WHY 가정 H, ROADMAP §6-4)."""
    db = SessionDB(tmp_path / "s9.db")
    dispatcher = _create_test_dispatcher()

    loaded = load_settings()
    agent = create_agent(
        loaded.settings,
        model="mock",
        session_id="s_test9",
        session_db=db,
        dispatcher=dispatcher,
    )
    panel = ControlPanel(agent, loaded, db, apply_settings_fn=apply_runtime_settings)

    # gitleaks 방어를 위한 런타임 문자열 결합
    fake_or_key = "sk-or-" + "v1-" + ("a" * 36)
    fake_bearer = "Bearer " + ("xyz987_" * 4)

    panel.submit(SetSetting("model", fake_or_key))
    panel.submit(SetSetting("max_iterations", f"bad_{fake_bearer}"))

    audits = db.list_control_audit("s_test9")
    assert len(audits) == 2

    for row in audits:
        text_bundle = f"{row.get('old_value')} {row.get('new_value')} {row.get('reason')}"
        # 원문이 절대 남지 않아야 함
        assert fake_or_key not in text_bundle
        assert ("xyz987_" * 4) not in text_bundle
        # 마스킹 패턴이 적용되었는지 확인
        assert "***" in text_bundle


def test_scenario_10_cli_turn_boundary_automatic_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """시나리오 10: CLI 대화 루프의 턴 경계 자동 반영."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(tmp_path)

    # ControlPanel을 가로채서 1번째 턴 전에 명령을 적재할 수 있도록 캡처
    orig_control_panel = cli.ControlPanel
    captured_panels: list[ControlPanel] = []

    def panel_wrapper(*args: Any, **kwargs: Any) -> ControlPanel:
        p = orig_control_panel(*args, **kwargs)
        captured_panels.append(p)
        return p

    monkeypatch.setattr(cli, "ControlPanel", panel_wrapper)

    # run_conversation 시점에 max_iterations가 반영되어 있는지 기록
    seen_max_iterations: list[int] = []
    orig_run_conversation = HermesAgent.run_conversation

    def wrapped_run_conversation(self: HermesAgent, *args: Any, **kwargs: Any) -> dict[str, Any]:
        seen_max_iterations.append(self.max_iterations)
        return orig_run_conversation(self, *args, **kwargs)

    monkeypatch.setattr(HermesAgent, "run_conversation", wrapped_run_conversation)

    # 대화형 입력 대본: 1번째 입력 전에 명령 적재, 1번째 대화 'hello', 2번째 대화 'exit'
    turn = 0

    def fake_read_user_input(prompt: str = "", **kwargs: Any) -> str:
        nonlocal turn
        turn += 1
        if turn == 1:
            assert len(captured_panels) == 1
            captured_panels[0].submit(SetSetting("max_iterations", 4))
            return "hello"
        return "exit"

    monkeypatch.setattr(cli, "read_user_input", fake_read_user_input)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["cli.py", "--mock"])

    # CLI 실행
    cli.main()

    # run_conversation 호출 시 이미 max_iterations가 4로 반영되어 있었는지 확인
    assert len(seen_max_iterations) == 1
    assert seen_max_iterations[0] == 4

    # DB에 accepted 및 applied 기록이 모두 남아 있는지 확인
    db = SessionDB(hermes_home / "state.db")
    s_row = db._conn.execute("SELECT id FROM sessions").fetchone()
    assert s_row is not None
    sid = s_row["id"]
    audits = db.list_control_audit(sid)
    assert len(audits) >= 2
    results = [a["result"] for a in audits]
    assert "accepted" in results
    assert "applied" in results
