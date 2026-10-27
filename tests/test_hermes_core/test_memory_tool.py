"""바운디드 메모리 및 USER 저장소 단위 테스트."""

import json

from hermes_core.tools.memory_tool import MemoryStore, memory_tool


def test_memory_add_and_persistence(tmp_path):
    """메모리 항목 추가 시 디스크에 정상 저장되고 엔트리가 분할되는지 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    res = store.add("memory", "Python 3.14 is installed in .venv")
    assert res["success"] is True
    assert res["entry_count"] == 1

    # 디스크 파일 확인
    memory_file = tmp_path / "MEMORY.md"
    assert memory_file.exists()
    content = memory_file.read_text(encoding="utf-8")
    assert "Python 3.14 is installed in .venv" in content

    # 두 번째 항목 추가
    store.add("memory", "SQLite FTS5 is used for session search")
    assert len(store.memory_entries) == 2

    # 새 store 인스턴스로 디스크 재로드 확인
    new_store = MemoryStore(memory_dir=tmp_path)
    new_store.load_from_disk()
    assert len(new_store.memory_entries) == 2
    assert new_store.memory_entries[0] == "Python 3.14 is installed in .venv"


def test_memory_char_limit_exceeded(tmp_path):
    """2,200자 제한인 MEMORY.md에 2,300자 항목 추가 시 거부 및 안전 에러 반환 검증."""
    store = MemoryStore(memory_dir=tmp_path, memory_char_limit=2200)
    store.load_from_disk()

    huge_fact = "X" * 2300
    res = store.add("memory", huge_fact)

    assert res["success"] is False
    assert "would exceed the limit" in res["error"]
    assert len(store.memory_entries) == 0

    # memory_tool() 함수 호출을 통한 JSON 응답 확인
    json_str = memory_tool(action="add", target="memory", content=huge_fact, store=store)
    data = json.loads(json_str)
    assert data["success"] is False
    assert "would exceed the limit" in data["error"]


def test_memory_replace(tmp_path):
    """부분 일치(substring)를 통한 replace가 정확한 위치의 항목만 치환하는지 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    store.add("user", "User prefers Korean language")
    store.add("user", "User works on macOS Darwin")
    store.add("user", "User likes concise responses")

    # 두 번째 항목("macOS Darwin")을 "macOS Apple Silicon M-series"로 치환
    res = store.replace("user", old_text="macOS", new_content="User works on macOS Apple Silicon M-series")
    assert res["success"] is True

    assert len(store.user_entries) == 3
    assert store.user_entries[0] == "User prefers Korean language"
    assert store.user_entries[1] == "User works on macOS Apple Silicon M-series"
    assert store.user_entries[2] == "User likes concise responses"


def test_memory_remove(tmp_path):
    """remove 액션으로 일치 항목 삭제 및 파일 갱신 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    store.add("memory", "Temporary note 1")
    store.add("memory", "Durable note 2")

    res = store.remove("memory", old_text="Temporary")
    assert res["success"] is True
    assert len(store.memory_entries) == 1
    assert store.memory_entries[0] == "Durable note 2"


def test_frozen_snapshot_preserves_prefix_cache(tmp_path):
    """세션 진행 중 add를 호출해도 load_from_disk() 당시의 시스템 프롬프트 스냅샷은 불변임을 검증."""
    # 1. 초기 상태에서 디스크에 미리 1개 기록
    initial_file = tmp_path / "MEMORY.md"
    initial_file.write_text("Initial fact §", encoding="utf-8")

    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    # 초기 스냅샷 캡처 확인
    snapshot_before = store.format_for_system_prompt("memory")
    assert "Initial fact" in snapshot_before

    # 2. 세션 도중 새로운 기억 추가 (라이브 상태 및 디스크 변경)
    store.add("memory", "Newly learned fact during turn 3")
    assert len(store.memory_entries) == 2

    # 3. 하지만 시스템 프롬프트 스냅샷은 절대 변하지 않음 (Prefix Cache 보호)
    snapshot_after = store.format_for_system_prompt("memory")
    assert snapshot_after == snapshot_before
    assert "Newly learned fact" not in snapshot_after
