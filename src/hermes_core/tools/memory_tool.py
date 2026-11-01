"""바운디드 영구 메모리(Curated Bounded Memory) 저장소 및 도구.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/memory_tool.py
- 핵심 원리:
  1. 문자 수 상한(Bounded Capacity):
     - MEMORY.md: 사실/환경 기억 (기본 2,200자 제한)
     - USER.md: 사용자 성향/선호 기억 (기본 1,375자 제한)
  2. 세션별 프롬프트 캐싱 보존(Frozen Snapshot Pattern):
     - 세션 시작 시 `load_from_disk()`에서 시스템 프롬프트 스냅샷을 1회 캡처.
     - 턴 도중 `add`/`replace`/`remove` 시 디스크에는 즉시 원자적(Atomic)으로 기록되나,
       시스템 프롬프트 주입 스냅샷은 변경하지 않아 Prefix Cache를 온전히 보존함.
  3. 섹션 구분자 `§` 기반 부분 편집:
     - 복잡한 JSON/DB 대신 마크다운과 `§` 구분자로 관리하여 LLM이 가독성 높게 파싱 및 편집 가능.
  4. 원자적 파일 쓰기(Atomic Write):
     - 임시 파일 작성 후 `os.replace`로 치환하여 프로세스 비정상 종료 시 파일 깨짐 방지.
"""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional

from hermes_core.constants import (
    ENTRY_DELIMITER,
    MEMORY_MAX_CHARS,
    USER_MAX_CHARS,
    get_hermes_home,
)


MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": "Manage persistent bounded memory (add, replace, or remove durable facts).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "remove"],
                    "description": "Action to perform on memory.",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "Target store: 'memory' for environment facts, 'user' for user preferences.",
                    "default": "memory",
                },
                "content": {
                    "type": "string",
                    "description": "New fact content to add or replacement text.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Substring of existing entry to find for replace or remove.",
                },
            },
            "required": ["action"],
        },
    },
}


class MemoryStore:
    """바운디드 큐레이티드 메모리 관리 클래스.

    에이전트 인스턴스당 1개 생성되어 디스크 영속성과 시스템 프롬프트 스냅샷을 분리 관리함.
    """

    def __init__(
        self,
        memory_dir: Optional[Path] = None,
        memory_char_limit: int = MEMORY_MAX_CHARS,
        user_char_limit: int = USER_MAX_CHARS,
    ):
        self.memory_dir = memory_dir or (get_hermes_home() / "memories")
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit

        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []

        # 시스템 프롬프트 주입용 불변 동결 스냅샷
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}

        # 비정상 파일 잠금(Fail-Closed) 및 디스크 해시 추적
        self._unreadable_targets: set[str] = set()
        self._disk_sha256: dict[str, Optional[str]] = {"memory": None, "user": None}
        self._disk_loaded: bool = False

    def load_from_disk(self) -> None:
        """디스크의 MEMORY.md와 USER.md로부터 엔트리를 읽고, 시스템 프롬프트용 스냅샷을 동결."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        for target, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
            path = self.memory_dir / filename
            entries, sha256_hash, is_err = self._inspect_disk_file(path)
            if is_err:
                self._unreadable_targets.add(target)
                self._set_entries(target, [])
                self._disk_sha256[target] = None
            else:
                self._unreadable_targets.discard(target)
                self._set_entries(target, list(dict.fromkeys(entries)))
                self._disk_sha256[target] = sha256_hash

        self._disk_loaded = True

        # 세션 시작 시점의 스냅샷 캡처 (세션 내내 불변)
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    def save_to_disk(self, target: str, entries: Optional[list[str]] = None) -> None:
        """메모리 변경 사항을 해당 디스크 파일에 즉시 영구 저장."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        to_write = entries if entries is not None else self._entries_for(target)
        path = self.memory_dir / ("USER.md" if target == "user" else "MEMORY.md")
        self._write_file(path, to_write)
        content = ENTRY_DELIMITER.join(to_write) if to_write else ""
        self._disk_sha256[target] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._unreadable_targets.discard(target)

    def _sync_from_disk_if_modified(self, target: str) -> None:
        """외부 파일 변경 시 인메모리 목록을 동기화 (단, 시스템 프롬프트 스냅샷은 동결 유지)."""
        if not self._disk_loaded or target in self._unreadable_targets:
            return
        path = self.memory_dir / ("USER.md" if target == "user" else "MEMORY.md")
        if not path.exists():
            if self._disk_sha256.get(target) is not None:
                self._set_entries(target, [])
                self._disk_sha256[target] = None
            return
        entries, file_hash, is_err = self._inspect_disk_file(path)
        if is_err:
            self._unreadable_targets.add(target)
            return
        if file_hash != self._disk_sha256.get(target):
            self._set_entries(target, list(dict.fromkeys(entries)))
            self._disk_sha256[target] = file_hash

    def _commit_entries(self, target: str, new_entries: list[str]) -> None:
        """디스크 원자적 기록 성공 후에만 인메모리 엔트리와 해시를 갱신하는 Write-First 커밋 관문."""
        self.save_to_disk(target, new_entries)
        self._set_entries(target, new_entries)

    def _entries_for(self, target: str) -> list[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

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

    def add(self, target: str, content: str) -> dict[str, Any]:
        """새로운 기억 항목 추가. 용량 한도 초과 시 거부 및 압축 가이드 반환."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        if target in self._unreadable_targets:
            return {"success": False, "error": f"Cannot modify memory: '{target}' storage is in an unreadable state."}

        val_err = self._validate_entry_content(content)
        if val_err:
            return {"success": False, "error": val_err}

        self._sync_from_disk_if_modified(target)

        entries = self._entries_for(target)
        limit = self._char_limit(target)

        # 완전 중복 거부
        if content in entries:
            return self._success_response(target, "Entry already exists (no duplicate added).")

        new_entries = entries + [content]
        new_total = len(ENTRY_DELIMITER.join(new_entries))

        if new_total > limit:
            current = self._char_count(target)
            return {
                "success": False,
                "error": (
                    f"Memory at {current:,}/{limit:,} chars. "
                    f"Adding this entry ({len(content)} chars) would exceed the limit. "
                    f"Replace or remove existing entries first."
                ),
                "current_entries": entries,
                "usage": f"{current:,}/{limit:,}",
            }

        self._commit_entries(target, new_entries)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """old_text 문자열을 포함하는 기억 항목을 찾아 new_content로 치환."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        if target in self._unreadable_targets:
            return {"success": False, "error": f"Cannot modify memory: '{target}' storage is in an unreadable state."}

        val_err = self._validate_entry_content(new_content)
        if val_err:
            return {"success": False, "error": val_err}

        self._sync_from_disk_if_modified(target)

        entries = self._entries_for(target)
        idx, err_resp = self._resolve_entry_index(entries, old_text)
        if err_resp is not None:
            return err_resp

        assert idx is not None
        if entries[idx] == new_content:
            return self._success_response(target, "Entry already contains this content (no change needed).")

        if any(e == new_content for i, e in enumerate(entries) if i != idx):
            return {"success": False, "error": "Replacement content already exists as another entry."}

        limit = self._char_limit(target)
        test_entries = entries.copy()
        test_entries[idx] = new_content
        new_total = len(ENTRY_DELIMITER.join(test_entries))

        if new_total > limit:
            return {
                "success": False,
                "error": (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                    f"Shorten the new content or remove other entries first."
                ),
            }

        self._commit_entries(target, test_entries)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """old_text 문자열을 포함하는 기억 항목 삭제."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        if target in self._unreadable_targets:
            return {"success": False, "error": f"Cannot modify memory: '{target}' storage is in an unreadable state."}

        self._sync_from_disk_if_modified(target)

        entries = self._entries_for(target)
        idx, err_resp = self._resolve_entry_index(entries, old_text)
        if err_resp is not None:
            return err_resp

        assert idx is not None
        new_entries = [e for i, e in enumerate(entries) if i != idx]
        self._commit_entries(target, new_entries)

        return self._success_response(target, "Entry removed.")

    def format_for_system_prompt(self, target: str = "memory") -> Optional[str]:
        """시스템 프롬프트 주입용 동결 스냅샷 반환 (비어있으면 None)."""
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- 내부 보조 함수 --

    def _success_response(self, target: str, message: Optional[str] = None) -> dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = int((current / limit) * 100) if limit > 0 else 0

        resp: dict[str, Any] = {
            "success": True,
            "target": target,
            "entries": entries,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: list[str]) -> str:
        """시스템 프롬프트용 마크다운 블록 렌더링."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = int((current / limit) * 100) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _inspect_disk_file(path: Path) -> tuple[list[str], Optional[str], bool]:
        """디스크 파일 검사 및 엔트리 분할.

        Returns:
            (entries, sha256_hex, is_error)
            - 파일 부재: ([], None, False)
            - 정상 읽기: (entries, sha256_hex, False)
            - I/O 또는 디코딩 오류: ([], None, True)
        """
        if not path.exists():
            return [], None, False
        try:
            raw_bytes = path.read_bytes()
            file_hash = hashlib.sha256(raw_bytes).hexdigest()
            raw = raw_bytes.decode("utf-8")
        except (OSError, IOError, UnicodeDecodeError):
            return [], None, True

        if not raw.strip():
            return [], file_hash, False

        entries = [e.strip() for e in raw.split("§")]
        return [e for e in entries if e], file_hash, False

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        """메모리 파일을 읽어 § 구분자 단위로 엔트리 리스트 분할 (하위 호환성 유지)."""
        entries, _, _ = MemoryStore._inspect_disk_file(path)
        return entries

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        """임시 파일 작성 후 os.replace를 통한 원자적(Atomic) 파일 쓰기."""
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def memory_tool(
    action: str,
    target: str = "memory",
    content: Optional[str] = None,
    old_text: Optional[str] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """도구 실행 레일(Dispatcher)에서 호출되는 단일 함수 진입로.

    Returns:
        JSON 직렬화된 문자열 결과.
    """
    if store is None:
        return json.dumps(
            {"success": False, "error": "Memory is not available in this environment."},
            ensure_ascii=False,
        )

    if target not in ("memory", "user"):
        return json.dumps(
            {"success": False, "error": f"Invalid target '{target}'. Use 'memory' or 'user'."},
            ensure_ascii=False,
        )

    if action == "add":
        if not content:
            return json.dumps(
                {"success": False, "error": "Content is required for 'add' action."},
                ensure_ascii=False,
            )
        result = store.add(target, content)

    elif action == "replace":
        if not old_text:
            return json.dumps(
                {"success": False, "error": "old_text is required for 'replace' action."},
                ensure_ascii=False,
            )
        if not content:
            return json.dumps(
                {"success": False, "error": "content is required for 'replace' action."},
                ensure_ascii=False,
            )
        result = store.replace(target, old_text, content)

    elif action == "remove":
        if not old_text:
            return json.dumps(
                {"success": False, "error": "old_text is required for 'remove' action."},
                ensure_ascii=False,
            )
        result = store.remove(target, old_text)

    else:
        return json.dumps(
            {"success": False, "error": f"Unknown action '{action}'. Use: add, replace, remove"},
            ensure_ascii=False,
        )

    return json.dumps(result, ensure_ascii=False)
