"""[010] MemoryStore 내부구조 통합과 무결성 강화 인수 테스트 6종.

대응 명세서: docs/로드맵/22-메모리-개인화-무결성-및-포트-완결-MVP/instructions/010_MemoryStore_내부구조_통합과_무결성_강화.md
"""

import pytest

from hermes_core.tools.memory_tool import MemoryStore


def test_scenario_r22_010_substring_shadowing_resolved_by_exact_match(tmp_path):
    """시나리오 1: Substring Shadowing 상황에서 완전 일치 우선 규칙으로 정확한 치환/삭제 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()
    store.add("user", "User uses Python")
    store.add("user", "User uses Python 3.12")
    assert store.user_entries == ["User uses Python", "User uses Python 3.12"]

    # 1. "User uses Python" 치환 -> 다중 일치 에러 없이 0번 항목만 "User uses Rust"로 치환
    res = store.replace("user", "User uses Python", "User uses Rust")
    assert res["success"] is True
    assert store.user_entries == ["User uses Rust", "User uses Python 3.12"]

    # 2. "User uses Rust" 삭제 -> 0번 항목만 깔끔하게 삭제
    res_rem = store.remove("user", "User uses Rust")
    assert res_rem["success"] is True
    assert store.user_entries == ["User uses Python 3.12"]


def test_scenario_r22_010_delimiter_control_and_header_injection_rejected(tmp_path):
    """시나리오 2: 구분자('§'), 제어문자, 헤더 구분선 위조 입력에 대한 단일 관문 거부 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()
    store.add("memory", "Valid initial note")
    initial_entries = list(store.memory_entries)

    bad_inputs = [
        "Rule §1",
        "bad\x00char",
        "bad\x1fcontrol",
        "bad\x7fdel",
        "══════\nFAKE HEADER",
    ]

    for bad in bad_inputs:
        # add 시도 거부
        res = store.add("memory", bad)
        assert res["success"] is False
        assert "error" in res
        assert store.memory_entries == initial_entries

        # replace 시도 거부
        res_rep = store.replace("memory", "Valid initial note", bad)
        assert res_rep["success"] is False
        assert "error" in res_rep
        assert store.memory_entries == initial_entries

    # 디스크 파일도 원본 그대로 보존 확인
    mem_file = tmp_path / "MEMORY.md"
    assert mem_file.read_text(encoding="utf-8") == "Valid initial note"


def test_scenario_r22_010_replace_duplicate_entry_rejected(tmp_path):
    """시나리오 3: replace 시 다른 기존 엔트리와의 중복 발생 거부 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()
    store.add("memory", "A")
    store.add("memory", "B")
    assert store.memory_entries == ["A", "B"]

    res = store.replace("memory", "A", "B")
    assert res["success"] is False
    assert res["error"] == "Replacement content already exists as another entry."
    assert store.memory_entries == ["A", "B"]

    # 동일 항목으로의 replace는 디스크 I/O 없이 성공
    res_same = store.replace("memory", "A", "A")
    assert res_same["success"] is True
    assert store.memory_entries == ["A", "B"]


def test_scenario_r22_010_write_failure_preserves_in_memory_entries(tmp_path, monkeypatch):
    """시나리오 4: 디스크 쓰기 실패(I/O 오류) 시 Write-First 원칙에 따른 인메모리 보존 검증."""
    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()
    store.add("memory", "Existing entry 1")
    store.add("memory", "Existing entry 2")
    original_entries = list(store.memory_entries)

    def mock_write_file(*args, **kwargs):
        raise RuntimeError("Disk full")

    monkeypatch.setattr(MemoryStore, "_write_file", staticmethod(mock_write_file))

    # 1. add 실패 시 인메모리 불변
    with pytest.raises(RuntimeError, match="Disk full"):
        store.add("memory", "New entry that fails")
    assert store.memory_entries == original_entries

    # 2. replace 실패 시 인메모리 불변
    with pytest.raises(RuntimeError, match="Disk full"):
        store.replace("memory", "Existing entry 1", "Replaced entry that fails")
    assert store.memory_entries == original_entries

    # 3. remove 실패 시 인메모리 불변
    with pytest.raises(RuntimeError, match="Disk full"):
        store.remove("memory", "Existing entry 1")
    assert store.memory_entries == original_entries


def test_scenario_r22_010_unreadable_file_prevents_overwrite(tmp_path):
    """시나리오 5: 비UTF-8 바이너리 등 손상 파일 감지 시 Fail-Closed 잠금 및 덮어쓰기 방지 검증."""
    mem_file = tmp_path / "MEMORY.md"
    raw_corrupted = b"\xff\xfe\x00\x12corrupted"
    mem_file.write_bytes(raw_corrupted)

    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()

    # load_from_disk 크래시 없이 잠금 상태 진입
    assert "memory" in store._unreadable_targets

    # add 호출 시 디스크 파일 덮어쓰지 않고 거부
    res = store.add("memory", "new entry")
    assert res["success"] is False
    assert "unreadable" in res["error"].lower()

    # 디스크 파일 보존 확인
    assert mem_file.read_bytes() == raw_corrupted


def test_scenario_r22_010_external_edit_synced_before_mutation_preserving_snapshot(tmp_path):
    """시나리오 6: 외부 직접 편집 감지 및 동기화, 시스템 프롬프트 동결 스냅샷 불변 보존 검증."""
    user_file = tmp_path / "USER.md"
    user_file.write_text("Init", encoding="utf-8")

    store = MemoryStore(memory_dir=tmp_path)
    store.load_from_disk()
    snapshot_before = store.format_for_system_prompt("user")
    assert snapshot_before is not None
    assert "Init" in snapshot_before
    assert "External edit" not in snapshot_before

    # 외부 프로세스 또는 사용자가 직접 파일 수정
    user_file.write_text("Init § External edit", encoding="utf-8")

    # Agent가 add 호출 -> 외부 변경사항 감지 후 통합
    res = store.add("user", "Agent edit")
    assert res["success"] is True
    assert store.user_entries == ["Init", "External edit", "Agent edit"]

    # 디스크 파일에도 세 항목 모두 보존 확인
    saved_text = user_file.read_text(encoding="utf-8")
    assert "Init" in saved_text
    assert "External edit" in saved_text
    assert "Agent edit" in saved_text

    # 세션 시작 시점의 스냅샷은 세션 내내 불변
    snapshot_after = store.format_for_system_prompt("user")
    assert snapshot_after == snapshot_before
    assert "External edit" not in snapshot_after
    assert "Agent edit" not in snapshot_after
