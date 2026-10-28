"""에이전트 자식용 인자 테스트 (로드맵 07 / 구현지시서 020 인수 시나리오)."""

import json
from pathlib import Path
import sqlite3

import pytest

from hermes_core.agent.agent import HermesAgent, MockLLMClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore


@pytest.fixture
def test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """지시문 파일, 메모리 파일, 세션 DB가 준비된 격리 환경 픽스처."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("CTXMARK", encoding="utf-8")

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("MEMMARK", encoding="utf-8")
    (mem_dir / "USER.md").write_text("USRMARK", encoding="utf-8")

    store = MemoryStore(memory_dir=mem_dir)
    db = SessionDB(tmp_path / "state.db")

    return {
        "tmp_path": tmp_path,
        "mem_dir": mem_dir,
        "store": store,
        "db": db,
    }


def test_scenario_01_default_args_regression(test_env):
    """시나리오 1: 새 인자 없이 HermesAgent 생성 시 source == 'cli', parent_session_id is None, 프롬프트 3대 마커 보존 검증."""
    db = test_env["db"]
    store = test_env["store"]
    client = MockLLMClient()

    agent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=client,
        quiet_mode=True,
    )
    sess = db.get_session(agent.session_id)
    assert sess is not None
    assert sess["source"] == "cli"
    assert sess["parent_session_id"] is None

    agent.run_conversation("hi")
    sys_prompt = client.call_history[0]["messages"][0]["content"]
    assert "MEMMARK" in sys_prompt
    assert "USRMARK" in sys_prompt
    assert "CTXMARK" in sys_prompt


def test_scenario_02_parent_session_id_and_source(test_env):
    """시나리오 2: 부모 세션 ID 및 source='subagent' 전달 시 자식/부모 세션 레코드 검증."""
    db = test_env["db"]
    store = test_env["store"]

    parent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=MockLLMClient(),
        quiet_mode=True,
    )
    child = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=MockLLMClient(),
        parent_session_id=parent.session_id,
        source="subagent",
        quiet_mode=True,
    )

    child_sess = db.get_session(child.session_id)
    assert child_sess is not None
    assert child_sess["parent_session_id"] == parent.session_id
    assert child_sess["source"] == "subagent"

    parent_sess = db.get_session(parent.session_id)
    assert parent_sess is not None
    assert parent_sess["parent_session_id"] is None
    assert parent_sess["source"] == "cli"


def test_scenario_03_invalid_parent_session_id_raises_integrity_error(test_env):
    """시나리오 3: 존재하지 않는 parent_session_id 제공 시 sqlite3.IntegrityError 발생 및 세션 미생성 검증."""
    db = test_env["db"]
    store = test_env["store"]

    with pytest.raises(sqlite3.IntegrityError):
        HermesAgent(
            session_db=db,
            memory_store=store,
            llm_client=MockLLMClient(),
            parent_session_id="nope",
            quiet_mode=True,
        )
    assert db.get_session("nope") is None


def test_scenario_04_skip_memory(test_env):
    """시나리오 4: skip_memory=True 시 프롬프트에서 MEMMARK·USRMARK 제외 및 CTXMARK 보존 검증."""
    db = test_env["db"]
    store = test_env["store"]
    client = MockLLMClient()

    agent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=client,
        skip_memory=True,
        quiet_mode=True,
    )
    agent.run_conversation("hi")
    sys_prompt = client.call_history[0]["messages"][0]["content"]
    assert "MEMMARK" not in sys_prompt
    assert "USRMARK" not in sys_prompt
    assert "CTXMARK" in sys_prompt


def test_scenario_05_skip_memory_does_not_access_disk(tmp_path: Path):
    """시나리오 5: skip_memory=True 시 존재하지 않는 memory_dir 폴더가 생성되지 않고 load_from_disk 생략 검증."""
    db = SessionDB(tmp_path / "state.db")
    non_existent_dir = tmp_path / "never_created_memories"
    store = MemoryStore(memory_dir=non_existent_dir)

    HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=MockLLMClient(),
        skip_memory=True,
        quiet_mode=True,
    )
    assert not non_existent_dir.exists()
    assert store.memory_entries == []


def test_scenario_06_skip_memory_preserves_parent_snapshot(test_env):
    """시나리오 6: 부모의 동결 스냅샷이 skip_memory=True 자식 생성 시 재동결되지 않고 불변 보존됨을 검증."""
    db = test_env["db"]
    store = test_env["store"]
    mem_dir = test_env["mem_dir"]

    # 부모 에이전트 생성 (스냅샷에 MEMMARK 캡처)
    HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=MockLLMClient(),
        quiet_mode=True,
    )
    assert "MEMMARK" in store.format_for_system_prompt("memory")

    # 디스크의 MEMORY.md 파일 내용 변경
    (mem_dir / "MEMORY.md").write_text("MEMMARK2", encoding="utf-8")

    # 같은 store 인스턴스를 넘기며 skip_memory=True 자식 생성
    HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=MockLLMClient(),
        skip_memory=True,
        quiet_mode=True,
    )

    # 부모의 스냅샷은 여전히 MEMMARK를 유지하고 MEMMARK2로 바뀌지 않음
    assert "MEMMARK" in store.format_for_system_prompt("memory")
    assert "MEMMARK2" not in store.format_for_system_prompt("memory")


def test_scenario_07_skip_context_files(test_env):
    """시나리오 7: skip_context_files=True 시 프롬프트에서 CTXMARK 제외 및 MEMMARK·USRMARK 보존 검증."""
    db = test_env["db"]
    store = test_env["store"]
    client = MockLLMClient()

    agent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=client,
        skip_context_files=True,
        quiet_mode=True,
    )
    agent.run_conversation("hi")
    sys_prompt = client.call_history[0]["messages"][0]["content"]
    assert "CTXMARK" not in sys_prompt
    assert "MEMMARK" in sys_prompt
    assert "USRMARK" in sys_prompt


def test_scenario_08_skip_memory_and_skip_context_files(test_env):
    """시나리오 8: skip_memory=True 및 skip_context_files=True 동시 적용 시 세 마커 모두 제외 및 기본 정체성 보존 검증."""
    db = test_env["db"]
    store = test_env["store"]
    client = MockLLMClient()

    agent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=client,
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
    )
    agent.run_conversation("hi")
    sys_prompt = client.call_history[0]["messages"][0]["content"]
    assert "MEMMARK" not in sys_prompt
    assert "USRMARK" not in sys_prompt
    assert "CTXMARK" not in sys_prompt
    assert len(sys_prompt) > 0
    assert "Hermes" in sys_prompt


def test_scenario_09_subagent_messages_excluded_from_parent_session_search(test_env):
    """시나리오 9: source='subagent' 자식 대화는 기본 source_filter에 포함되지 않아 부모 session_search에서 격리됨을 검증."""
    db = test_env["db"]
    store = test_env["store"]

    # 자식 에이전트 대화 실행
    child_agent = HermesAgent(
        session_db=db,
        memory_store=store,
        llm_client=MockLLMClient(),
        source="subagent",
        quiet_mode=True,
    )
    child_agent.run_conversation("ZEBRA secret keyword 999")

    # 부모 디스패처에서 session_search 검색 수행
    parent_dispatcher = create_default_dispatcher(memory_store=store, session_db=db)
    raw = parent_dispatcher.handle_function_call("session_search", {"query": "ZEBRA"})
    res = json.loads(raw)
    assert res["success"] is True
    assert res["results"] == []
