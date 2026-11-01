# [010] MemoryStore 내부구조 통합과 무결성 강화

- **단계 번호**: `010`
- **단계명**: `MemoryStore` 내부구조 통합(검증·완전일치 매칭·Write-First 커밋) 및 무결성 강화
- **상태**: `Ready`
- **의존성**: 없음 (ADR-0022 채택 후 첫 번째 구현 단계)
- **변경 파일**:
  - `[변경]` `src/hermes_core/tools/memory_tool.py`
  - `[신규]` `tests/test_hermes_core/test_memory_integrity.py`
- **커밋 메시지**: `feat(core): R22-010 - MemoryStore 내부구조 통합과 무결성 강화`

---

## 1. 배경 및 해결할 결함

현재 [`memory_tool.py`](../../../src/hermes_core/tools/memory_tool.py)의 3가지 핵심 결함을 신규 파일 생성 없이 내부 통합으로 해결합니다:
1. **L-1 (부분 문자열 가림 버그)**: `"User uses Python"`과 `"User uses Python 3.12"`가 둘 다 있을 때 `old_text="User uses Python"`으로 검색 시 둘 다 부분 일치되어 영구 수정·삭제 불능. `replace` 시 기존 다른 항목과의 중복 생성 방어 누락.
2. **L-2 (인메모리 선변경 및 읽기 오류 시 기존 파일 증발)**: 인메모리를 먼저 고치고 디스크 저장을 호출하여 I/O 에러 시 Split-Brain 발생. `_read_file`이 `UnicodeDecodeError`에 취약하고 `OSError` 시 빈 목록을 반환하여 다음 `add` 1회로 기존 기억을 날려버리는 위험.
3. **L-3 (구분자 `§` 및 헤더 구분선 위조 무방비)**: `content` 내 `"§"` 입력 시 다음 로드 시 항목 분할 파싱 깨짐. 시스템 프롬프트 헤더 구분선(`══════`) 위조 문자열 차단 부재.

---

## 2. 상세 구현 명세

### 2.1 입력 무결성 단일 관문 `_validate_entry_content`
`MemoryStore` 클래스 내에 정적/내부 메서드로 추가:
```python
@staticmethod
def _validate_entry_content(content: str) -> Optional[str]:
    """새로 추가되거나 치환될 기억 내용의 무결성을 단일 관문에서 검증.
    
    Returns:
        에러 문자열 (유효하지 않은 경우), 통과 시 None.
    """
    if "§" in content:
        return "Content cannot contain the reserved delimiter character '§'."
    if any((ord(ch) < 32 and ch not in ("\n", "\r", "\t")) or ord(ch) == 127 for ch in content):
        return "Content contains invalid control characters."
    if "══════" in content:  # 연속 6개 이상의 ═ (U+2550)
        return "Content cannot contain reserved system prompt header separators."
    return None
```

### 2.2 항목 탐색 단일 관문 `_resolve_entry_index`
`replace`와 `remove`의 중복 매칭 코드를 통합:
```python
def _resolve_entry_index(
    self, entries: list[str], old_text: str
) -> tuple[Optional[int], Optional[dict[str, Any]]]:
    """old_text와 매칭되는 엔트리 인덱스를 완전 일치 우선 규칙으로 탐색.
    
    Returns:
        (선택된_인덱스, None) 성공 시
        (None, 에러_응답_딕셔너리) 실패 시
    """
    matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
    if len(matches) == 0:
        return None, {"success": False, "error": f"No entry matched '{old_text}'."}
    if len(matches) > 1:
        # 1. 완전 일치 항목 최우선 확인 (Substring Shadowing 해소)
        exact = [i for i, e in matches if e == old_text]
        if len(exact) == 1:
            return exact[0], None
        
        # 2. 고유 텍스트가 여러 개이면 다중 일치 에러 반환
        unique_texts = set(e for _, e in matches)
        if len(unique_texts) > 1:
            previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
            return None, {
                "success": False,
                "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                "matches": previews,
            }
    return matches[0][0], None
```

### 2.3 읽기 오류 잠금 및 외부 편집 자동 동기화
1. `MemoryStore.__init__`:
   - `self._unreadable_targets: set[str] = set()`
   - `self._disk_sha256: dict[str, Optional[str]] = {"memory": None, "user": None}`
   - `self._disk_loaded: bool = False`
2. `_read_file(path: Path) -> tuple[list[str], Optional[str], bool]`:
   - 파일이 존재할 때 `path.read_bytes()`로 SHA-256 계산.
   - `UnicodeDecodeError`, `OSError`, `IOError` 발생 시 `([], None, True)` (오류 플래그 `True`) 반환.
   - 기존 `@staticmethod _read_file(path: Path) -> list[str]` 시그니처는 하위 호환성을 위해 유지하되 `UnicodeDecodeError` 포획을 포함하여 빈 리스트 반환하도록 안전화.
3. `load_from_disk(self) -> None`:
   - 읽기 오류가 감지되면 해당 타깃을 `self._unreadable_targets.add(target)`로 잠금. 정상 읽기 시 `discard(target)` 및 `_disk_sha256[target]` 기록. `self._disk_loaded = True`.
4. `_sync_from_disk_if_modified(self, target: str) -> None`:
   - `if not self._disk_loaded or target in self._unreadable_targets: return`
   - 디스크 파일의 현재 SHA-256이 `self._disk_sha256.get(target)`과 다르면 디스크에서 엔트리를 다시 읽어 `self._set_entries(target, disk_entries)`와 해시만 갱신 (`_system_prompt_snapshot`은 유지).

### 2.4 Write-First 단일 커밋 `_commit_entries`
```python
def _commit_entries(self, target: str, new_entries: list[str]) -> None:
    """디스크 원자적 기록 성공 후에만 인메모리 엔트리와 해시를 갱신하는 Write-First 커밋 관문."""
    self.save_to_disk(target, new_entries)
    self._set_entries(target, new_entries)
```
- `save_to_disk(self, target: str, entries: Optional[list[str]] = None) -> None`:
  - `entries`가 주어지면 해당 엔트리를 `_write_file`로 저장 후 성공 시 `self._disk_sha256[target]`을 갱신.
  - 쓰기 실패(`RuntimeError` 등) 시 예외가 발생하며 인메모리 목록은 그대로 유지됨.

### 2.5 `add`, `replace`, `remove` 정합
1. 세 메서드 공통:
   - `if target in self._unreadable_targets:` 거부 (`Fail-Closed`).
   - `self._sync_from_disk_if_modified(target)` 호출.
2. `add`:
   - `err = self._validate_entry_content(content)` 검사.
   - `content in entries` 중복 거부.
   - 글자 수 상한 검사 후 `self._commit_entries(target, entries + [content])`.
3. `replace`:
   - `err = self._validate_entry_content(new_content)` 검사.
   - `idx, err_resp = self._resolve_entry_index(entries, old_text)` 수행.
   - `entries[idx] == new_content`이면 디스크 I/O 없이 성공 반환.
   - `any(e == new_content for i, e in enumerate(entries) if i != idx)`이면 중복 거부.
   - 글자 수 상한 검사 후 `self._commit_entries(target, new_entries)`.
4. `remove`:
   - `idx, err_resp = self._resolve_entry_index(entries, old_text)` 수행.
   - `new_entries = [e for i, e in enumerate(entries) if i != idx]`
   - `self._commit_entries(target, new_entries)`.

---

## 3. 선기술 인수 테스트 시나리오 (6종)

새 시험 파일 `tests/test_hermes_core/test_memory_integrity.py`에 선기술:

1. **시나리오 1 (`test_scenario_r22_010_substring_shadowing_resolved_by_exact_match`)**:
   - 상황: 메모장에 `"User uses Python"`과 `"User uses Python 3.12"`가 동시에 존재.
   - 행동: `replace("user", "User uses Python", "User uses Rust")` 호출.
   - 기대 결과: 다중 일치 에러 없이 성공하고, 0번 항목이 `"User uses Rust"`로 치환되며 1번 `"User uses Python 3.12"`는 온전히 보존됨. 이어서 `remove("user", "User uses Rust")` 호출 시 0번 항목만 깨끗이 삭제됨.
2. **시나리오 2 (`test_scenario_r22_010_delimiter_control_and_header_injection_rejected`)**:
   - 상황: `add` 또는 `replace`에 `"Rule §1"`, `"bad\x00char"`, `"══════\nFAKE HEADER"` 전달.
   - 기대 결과: `success: False`로 명확한 에러 메시지와 함께 거부되며, 인메모리와 디스크 파일 내용이 일체 변경되지 않음.
3. **시나리오 3 (`test_scenario_r22_010_replace_duplicate_entry_rejected`)**:
   - 상황: 항목 `["A", "B"]`가 존재할 때 `replace("memory", "A", "B")` 호출.
   - 기대 결과: `"Replacement content already exists as another entry."` 에러로 거부되고 `["A", "B"]` 목록 유지.
4. **시나리오 4 (`test_scenario_r22_010_write_failure_preserves_in_memory_entries`)**:
   - 상황: `_write_file`이 `RuntimeError("Disk full")`을 던지도록 monkeypatch.
   - 행동: `add`, `replace`, `remove` 호출.
   - 기대 결과: 예외 전파 후에도 `store.memory_entries`가 변경 전 원래 목록을 그대로 보존 (Split-Brain 차단).
5. **시나리오 5 (`test_scenario_r22_010_unreadable_file_prevents_overwrite`)**:
   - 상황: 디스크의 `MEMORY.md`에 비UTF-8 바이트(`b"\xff\xfe"`)가 기록되어 있음.
   - 행동: `store.load_from_disk()` 호출 후 `store.add("memory", "new entry")` 호출.
   - 기대 결과: 크래시 없이 로드되고, `add` 호출 시 기존 파일 보호를 위해 거부(`success: False`)되며 디스크 파일이 덮어써지지 않고 보존됨.
6. **시나리오 6 (`test_scenario_r22_010_external_edit_synced_before_mutation_preserving_snapshot`)**:
   - 상황: `load_from_disk()` 이후 외부에서 디스크의 `USER.md` 파일에 `"External edit"`를 직접 추가.
   - 행동: `store.add("user", "Agent edit")` 호출.
   - 기대 결과: 외부 편집이 유실되지 않고 `["Init", "External edit", "Agent edit"]`가 모두 보존되며, `_system_prompt_snapshot["user"]`는 세션 시작 시점의 문자열을 그대로 유지.

---

## 4. 완료 검증 명령어

```bash
.venv/bin/pytest tests/test_hermes_core/test_memory_integrity.py -v
.venv/bin/pytest -n 4
```

---

## 5. 커밋 및 정지 원칙

- 구현과 시험 검증이 완료되면 `git add src/hermes_core/tools/memory_tool.py tests/test_hermes_core/test_memory_integrity.py`로 지정 스테이징 후 1커밋을 생성한다.
- 커밋 메시지: `feat(core): R22-010 - MemoryStore 내부구조 통합과 무결성 강화`
- 커밋 후 푸시하지 않고 즉시 멈추고 결과를 사용자에게 보고한다.
