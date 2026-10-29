"""자식 전용 도구 통로 테스트 (로드맵 07 / 구현지시서 010 인수 시나리오)."""

import json
from pathlib import Path

import pytest

from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import (
    create_child_dispatcher,
    create_default_dispatcher,
)
from hermes_core.tools.memory_tool import MemoryStore


def test_scenario_01_child_dispatcher_tool_names():
    """시나리오 1: create_child_dispatcher().get_tools_schema() 도구 이름 집합 6종 검증."""
    child = create_child_dispatcher()
    schemas = child.get_tools_schema()
    names = {s["function"]["name"] for s in schemas}
    assert names == {"terminal", "read_file", "write_file", "patch", "skills_list", "skill_view"}
    assert len(names) == 6


def test_scenario_02_child_dispatcher_excluded_tools():
    """시나리오 2: delegate_task, memory, session_search 호출 시 Unknown tool 실패 검증."""
    child = create_child_dispatcher()
    for tool_name in ("delegate_task", "memory", "session_search"):
        raw = child.handle_function_call(tool_name, {})
        res = json.loads(raw)
        assert res["success"] is False
        assert f"Unknown tool '{tool_name}'." in res["error"]


def test_scenario_03_default_dispatcher_tool_names_and_order():
    """시나리오 3: create_default_dispatcher() 8종 도구 이름 및 등록 순서 보존 검증."""
    dispatcher = create_default_dispatcher()
    schemas = dispatcher.get_tools_schema()
    names = [s["function"]["name"] for s in schemas]
    expected_order = [
        "memory",
        "session_search",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "skills_list",
        "skill_view",
    ]
    assert names == expected_order
    assert set(names) == {
        "memory",
        "session_search",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "skills_list",
        "skill_view",
    }


def test_scenario_05_schemas_identical():
    """시나리오 5: 기본 통로와 자식 통로의 6종 공통 도구 스키마 동일성 검증."""
    default_schemas = {s["function"]["name"]: s for s in create_default_dispatcher().get_tools_schema()}
    child_schemas = {s["function"]["name"]: s for s in create_child_dispatcher().get_tools_schema()}
    for name in ("terminal", "read_file", "write_file", "patch", "skills_list", "skill_view"):
        assert child_schemas[name] == default_schemas[name]


def test_scenario_09_include_filter_terminal_and_empty():
    """시나리오 9: include 필터로 지정한 도구만 등록되거나 빈 목록 등록 검증."""
    d_terminal = create_default_dispatcher(include=["terminal"])
    names_terminal = [s["function"]["name"] for s in d_terminal.get_tools_schema()]
    assert names_terminal == ["terminal"]

    d_empty = create_default_dispatcher(include=[])
    assert d_empty.get_tools_schema() == []


def test_scenario_10_include_filter_unknown_tool():
    """시나리오 10: include에 유효하지 않은 도구명이 포함되면 ValueError 발생 검증."""
    with pytest.raises(ValueError) as excinfo:
        create_default_dispatcher(include=["terminal", "nope"])
    assert "nope" in str(excinfo.value)


def test_scenario_11_instance_independence():
    """시나리오 11: create_child_dispatcher() 호출 간 인스턴스 격리 및 독립성 검증."""
    child1 = create_child_dispatcher()
    child2 = create_child_dispatcher()

    extra_schema = {
        "type": "function",
        "function": {"name": "extra", "description": "extra tool", "parameters": {}},
    }
    child1.register_tool("extra", lambda **k: "ok", extra_schema)

    names1 = {s["function"]["name"] for s in child1.get_tools_schema()}
    names2 = {s["function"]["name"] for s in child2.get_tools_schema()}
    d_new = create_default_dispatcher()
    names_default = {s["function"]["name"] for s in d_new.get_tools_schema()}

    assert "extra" in names1
    assert "extra" not in names2
    assert "extra" not in names_default


def test_scenario_12_default_dispatcher_backward_compatibility_with_injected_stores(tmp_path: Path):
    """시나리오 12: create_default_dispatcher 기존 주입 형태 호환 및 실제 주입 객체 동작 검증."""
    tmp_store = MemoryStore(memory_dir=tmp_path / "memories")
    tmp_store.load_from_disk()
    tmp_db = SessionDB(tmp_path / "state.db")

    dispatcher = create_default_dispatcher(memory_store=tmp_store, session_db=tmp_db)

    expected_order = [
        "memory",
        "session_search",
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "skills_list",
        "skill_view",
    ]
    names = [s["function"]["name"] for s in dispatcher.get_tools_schema()]
    assert names == expected_order

    # memory 주입 동작 확인
    raw_mem = dispatcher.handle_function_call(
        "memory",
        {"action": "add", "target": "memory", "content": "custom memory entry"},
    )
    res_mem = json.loads(raw_mem)
    assert res_mem["success"] is True
    assert "custom memory entry" in tmp_store.memory_entries

    # session_db 주입 동작 확인
    tmp_db.create_session("sess_scenario_12", model="mock")
    tmp_db.append_message("sess_scenario_12", "user", "find_me_needle_123")

    raw_search = dispatcher.handle_function_call("session_search", {"query": "find_me_needle_123"})
    res_search = json.loads(raw_search)
    assert res_search["success"] is True
    assert len(res_search["results"]) > 0
    assert "find_me_needle_123" in res_search["results"][0]["snippet"]
