"""22번 로드맵 020 메모리 조회 포트 및 FTS5 쿼리 안전화 인수 테스트.

선기술 인수 테스트 시나리오 6종:
1. test_scenario_r22_020_memory_list_action_returns_entries_and_usage
2. test_scenario_r22_020_memory_list_reflects_external_disk_edits
3. test_scenario_r22_020_memory_store_conforms_to_memorystore_port
4. test_scenario_r22_020_sanitize_fts_query_preserves_plain_text_contract
5. test_scenario_r22_020_sanitize_fts_query_escapes_special_characters_and_reserved_keywords
6. test_scenario_r22_020_fts5_crash_prevention_end_to_end_with_session_db
"""

import json
from pathlib import Path

from hermes_core.ledger_domain import build_fts_search_query, sanitize_fts_query
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore, memory_tool
from hermes_core.tools.ports import MemoryStorePort


def test_scenario_r22_020_memory_list_action_returns_entries_and_usage(tmp_path: Path) -> None:
    """시나리오 1: list 액션 호출 시 기억 항목 목록과 사용량 반환 및 무수정 멱등성 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    # 등록
    add1 = store.add("memory", "Fact 1")
    assert add1["success"] is True
    add2 = store.add("memory", "Fact 2")
    assert add2["success"] is True

    # list 액션 호출 (memory_tool 진입로 함수 사용)
    res_raw = memory_tool(action="list", target="memory", store=store)
    res = json.loads(res_raw)

    assert res["success"] is True
    assert res["target"] == "memory"
    assert res["entries"] == ["Fact 1", "Fact 2"]
    assert res["entry_count"] == 2
    assert "usage" in res
    assert "chars" in res["usage"]

    # 불변 동결 스냅샷은 여전히 load_from_disk 시점의 None/빈 상태를 유지해야 함
    assert store.format_for_system_prompt("memory") is None


def test_scenario_r22_020_memory_list_reflects_external_disk_edits(tmp_path: Path) -> None:
    """시나리오 2: 외부 디스크 파일 변경 시 _sync_from_disk_if_modified를 통해 list에 반영."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    # 초기에 항목 하나 추가
    store.add("memory", "Initial fact")

    # 외부에서 MEMORY.md 파일 직접 수정
    mem_file = tmp_path / "MEMORY.md"
    current_content = mem_file.read_text(encoding="utf-8")
    updated_content = current_content + " § External list fact"
    mem_file.write_text(updated_content, encoding="utf-8")

    # list 액션 호출 시 디스크 변경이 동기화되어 반영되어야 함
    res_raw = memory_tool(action="list", target="memory", store=store)
    res = json.loads(res_raw)

    assert res["success"] is True
    assert "External list fact" in res["entries"]
    assert "Initial fact" in res["entries"]


def test_scenario_r22_020_memory_store_conforms_to_memorystore_port(tmp_path: Path) -> None:
    """시나리오 3: MemoryStore의 MemoryStorePort 프로토콜 적합성 및 디스패처 주입 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    # 런타임 프로토콜 적합성 검증
    assert isinstance(store, MemoryStorePort)

    # create_default_dispatcher에 주입
    dispatcher = create_default_dispatcher(memory_store=store)
    schemas = dispatcher.get_tools_schema()
    names = [s["function"]["name"] for s in schemas]
    assert "memory" in names

    # 디스패처를 통한 도구 실행
    out = dispatcher.handle_function_call(
        "memory",
        {"action": "add", "target": "memory", "content": "Port verified fact"},
    )
    res = json.loads(out)
    assert res["success"] is True

    # list 액션도 디스패처를 통해 실행 가능
    out_list = dispatcher.handle_function_call("memory", {"action": "list", "target": "memory"})
    res_list = json.loads(out_list)
    assert res_list["success"] is True
    assert "Port verified fact" in res_list["entries"]


def test_scenario_r22_020_sanitize_fts_query_preserves_plain_text_contract() -> None:
    """시나리오 4: 일반 영숫자/공백/비예약어 및 한글 평문 쿼리의 원형 보존 계약 검증."""
    q1 = "error log"
    assert sanitize_fts_query(q1) == "error log"

    sql1, params1 = build_fts_search_query(q1, source_filter=["cli"], role_filter=["user"])
    assert params1[0] == "error log"

    q2 = "한글 검색 쿼리 123"
    assert sanitize_fts_query(q2) == "한글 검색 쿼리 123"

    sql2, params2 = build_fts_search_query(q2)
    assert params2[0] == "한글 검색 쿼리 123"


def test_scenario_r22_020_sanitize_fts_query_escapes_special_characters_and_reserved_keywords() -> None:
    """시나리오 5: 특수문자 및 예약어가 포함된 토큰의 안전한 큰따옴표 감싸기 및 이스케이프 검증."""
    query = 'config.toml deepseek-v4 AND NOT error:404 "quoted"'
    sanitized = sanitize_fts_query(query)

    expected = '"config.toml" "deepseek-v4" "AND" "NOT" "error:404" """quoted"""'
    assert sanitized == expected


def test_scenario_r22_020_fts5_crash_prevention_end_to_end_with_session_db() -> None:
    """시나리오 6: SessionDB FTS5 검색 시 다양한 특수문자/예약어 입력에도 크래시 없이 안전한 검색 반환."""
    db = SessionDB(":memory:")
    sid = db.create_session("cli")
    db.append_message(sid, "assistant", "deepseek-v4 released with config.toml")

    # 1. 하이픈 포함 검색어
    results_hyphen = db.search_messages(query="deepseek-v4")
    assert len(results_hyphen) == 1
    assert "deepseek-v4" in results_hyphen[0]["content"]

    # 2. 마침표(파일명) 포함 검색어
    results_dot = db.search_messages(query="config.toml")
    assert len(results_dot) == 1
    assert "config.toml" in results_dot[0]["content"]

    # 3. FTS5 단독 예약어
    results_reserved = db.search_messages(query="AND")
    assert isinstance(results_reserved, list)

    # 4. FTS5 구문 파싱을 파괴하는 악성/기형 특수문자
    results_syntax = db.search_messages(query='invalid"syntax(')
    assert isinstance(results_syntax, list)
    assert len(results_syntax) == 0
