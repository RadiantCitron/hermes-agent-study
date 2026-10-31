# [구현지시서 010] 안전한 코드 탐색 도구 (`search_files`) 구현 및 경로 가드·디스패처·프롬프트 배선

> **상위 대시보드**: [16-README.md](../README.md)  
> **선행 조건**: 없음(16의 첫 단계). 14 `Closed`.  
> **근거**: [ADR-0016](../../../adr/0016-코드_탐색_및_루프_정체_감지_MVP_범위와_결정.md) §2-2·§2-3·§2-4·§2-5, [ROADMAP.md](../ROADMAP.md) §2·§4(위험 2·3·4)·§6-1, [WHY.md](../WHY.md) 가정 A·B·C

---

## 1. 구현 내용 명세

이 단계는 [ADR-0003](../../../adr/0003-파일_도구_범위와_쓰기_안전_정책.md) §2-1에서 미뤄 두었던 `search_files` 도구를 구현해 부모 에이전트와 위임 자식 모두에게 배선한다. `agent.py`, `cli.py`, `session_db.py`, `checkpoint.py`는 수정하지 않는다.

### 1-1. 상수 및 자식 허용 도구 목록 갱신 ([`constants.py`](../../../../src/hermes_core/constants.py))

1. `CHILD_TOOL_NAMES`에 `"search_files"`를 추가한다 (총 7종):
   ```python
   CHILD_TOOL_NAMES = (
       "terminal",
       "read_file",
       "write_file",
       "patch",
       "search_files",
       "skills_list",
       "skill_view",
   )
   ```
2. 파일 도구 제한 상수 구역(`FILE_MAX_BYTES` 바로 아래)에 탐색 제한 상수 3종을 추가한다:
   ```python
   # 파일·내용 탐색 도구(search_files) 제한 상수 (16 로드맵)
   SEARCH_MAX_MATCHES = 50
   SEARCH_MAX_LINE_CHARS = 200
   SEARCH_MAX_FILES_SCANNED = 5000
   ```

### 1-2. 상태 폴더 경계 판정 함수 공용화 ([`tools/path_guard.py`](../../../../src/hermes_core/tools/path_guard.py))

[`check_write_path`](../../../../src/hermes_core/tools/path_guard.py)의 4단계에 있던 Hermes 상태 폴더(`get_hermes_home()`) 판정을 공용 함수로 추출하여 `check_write_path`와 `search_files_tool`이 함께 쓰게 한다.

```python
def is_hermes_home_path(target_path: Path, workspace: Path) -> bool:
    """대상 경로가 Hermes 상태 폴더(get_hermes_home()) 내부인지 판정한다.

    단, workspace 자체가 hermes_home의 안쪽(엄격한 하위 디렉터리, !=)인 경우(예: .hermes/workspace)에는
    작업 폴더 안의 정상 파일이 상태 폴더 내부로 오판되지 않도록 False를 반환한다.
    """
    try:
        hermes_home = get_hermes_home().resolve()
        resolved_workspace = workspace.resolve()
        resolved_target = target_path.resolve()
    except (OSError, ValueError, RuntimeError):
        return True
    is_workspace_inside_home = (
        resolved_workspace.is_relative_to(hermes_home)
        and resolved_workspace != hermes_home
    )
    return (not is_workspace_inside_home) and resolved_target.is_relative_to(hermes_home)
```

- `check_write_path`의 4단계는 `if is_hermes_home_path(target_path, workspace): return None, "Writing inside the Hermes state folder is not allowed."`로 정리한다(기존 동작 100% 유지).

### 1-3. `SEARCH_FILES_SCHEMA` 및 `search_files_tool` 구현 ([`tools/file_tools.py`](../../../../src/hermes_core/tools/file_tools.py))

1. **`SEARCH_FILES_SCHEMA`**:
   ```python
   SEARCH_FILES_SCHEMA = {
       "type": "function",
       "function": {
           "name": "search_files",
           "description": (
               "Search for files by glob pattern or search text content inside files within the working directory. "
               "Use this instead of find or grep in terminal. "
               "If 'pattern' is empty, returns matching file paths (file search mode). "
               "If 'pattern' is provided, returns matching lines with 1-based line numbers (content search mode). "
               "Protected paths (.env*, .git, state folder), symlinks, and binary files are automatically skipped."
           ),
           "parameters": {
               "type": "object",
               "properties": {
                   "path": {
                       "type": "string",
                       "description": "Relative directory or file path to search in (default '.').",
                       "default": ".",
                   },
                   "pattern": {
                       "type": "string",
                       "description": "Text or regex pattern to search inside file contents. Leave empty ('') to list files matching file_glob.",
                       "default": "",
                   },
                   "file_glob": {
                       "type": "string",
                       "description": "Filename or relative-path glob filter (e.g. '*.py', 'src/*.py', default '*').",
                       "default": "*",
                   },
                   "regex": {
                       "type": "boolean",
                       "description": "If true, treat 'pattern' as a regular expression. If false (default), match literal substring.",
                       "default": False,
                   },
               },
               "required": [],
           },
       },
   }
   ```

2. **`search_files_tool` 시그니처 및 동작 순서**:
   ```python
   def search_files_tool(
       path: str = ".",
       pattern: str = "",
       file_glob: str = "*",
       regex: bool = False,
       workspace: Optional[Path] = None,
   ) -> str:
   ```
   - **인자 정규화 및 검증**:
     - `path`가 `None`이거나 `(isinstance(path, str) and not path.strip())`이면 `"."`로 둔다. `isinstance(path, str)`가 아니면 `_failure("path must be a string.")`.
     - `pattern`이 `None`이면 `""`로 둔다. `isinstance(pattern, str)`가 아니면 `_failure("pattern must be a string.")`.
     - `file_glob`이 `None`이면 `"*"`로 둔다. `isinstance(file_glob, str)`가 아니면 `_failure("file_glob must be a string.")`.
     - `isinstance(regex, bool)`가 아니면 `_failure("regex must be a boolean.")`.
     - `pattern == ""`이고 `file_glob.strip() == ""`이면 `_failure("At least one of 'pattern' or 'file_glob' must be non-empty.")`.
     - `eff_glob = file_glob.strip() or "*"`
   - **시작 경로 보안 검증**:
     - `base = resolve_workspace(workspace)`
     - `raw_start = base / path`
     - `if raw_start.is_symlink(): return _failure(f"Path '{path}' is a symbolic link; symlinks are not searched.")`
     - `target_path, err = check_path(path, base)` → `if err or target_path is None: return _failure(err or "Invalid path.")`
     - `if is_hermes_home_path(target_path, base): return _failure("Searching inside the Hermes state folder is not allowed.")`
     - `if not target_path.exists(): return _failure(f"Path not found: '{path}'")`
   - **정규식 사전 컴파일**:
     - `compiled_re = None`
     - `if pattern and regex:` `re.compile(pattern)`을 시도하고 `re.error as e` 발생 시 `_failure(f"Invalid regex pattern: {e}")` 반환.
   - **안전한 결정론적 순회 및 필터링**:
     - `target_path`가 일반 파일이면 그 파일 하나만 검사하고, 디렉터리이면 사전순(`sorted(cur_dir.iterdir(), key=lambda p: p.name)`)으로 재귀 순회한다.
     - 순회 중 각 `entry`에 대해:
       1. `entry.is_symlink()`이면 파일이든 디렉터리든 무조건 건너뛴다(하위 진입 금지).
       2. `rel_str = entry.relative_to(base).as_posix()`
       3. `if not is_inside(rel_str, base) or is_protected_path(rel_str, base) or is_hermes_home_path(entry, base): continue`
       4. `entry.is_dir()`이면 재귀 탐색한다.
       5. `entry.is_file()`이면 `fnmatch.fnmatchcase(entry.name, eff_glob) or fnmatch.fnmatchcase(rel_str, eff_glob)`가 참일 때만 후보 파일로 채택한다(`target_path`가 단일 파일로 직접 지정된 경우에도 `file_glob`이 기본값 `"*"`가 아니면 동일하게 `file_glob` 일치 여부를 확인한다).
   - **상한 및 매칭**:
     - 후보 파일마다:
       - `if files_scanned >= SEARCH_MAX_FILES_SCANNED: truncated = True; break`
       - `files_scanned += 1`
     - **파일 찾기 모드 (`pattern == ""`)**:
       - `if len(matches) >= SEARCH_MAX_MATCHES: truncated = True; break`
       - `matches.append({"path": rel_str})`
     - **내용 찾기 모드 (`pattern != ""`)**:
       - `stat().st_size > FILE_MAX_BYTES`이거나 `read_bytes()` 중 `OSError`, `b"\x00" in raw_bytes`(바이너리), `raw_bytes.decode("utf-8")` 중 `UnicodeDecodeError`이면 조용히 건너뛴다.
       - 줄 분리(`read_file_tool`과 동일하게 `\n` 분리 및 끝 `\r` 제거) 후 1-based `line_num`으로 순회하며 매칭(`compiled_re.search(line) is not None` if `compiled_re` else `pattern in line`):
         - 매칭되었을 때 이미 `len(matches) >= SEARCH_MAX_MATCHES`이면 `truncated = True`로 설정하고 즉시 외부 루프까지 종료한다.
         - `preview = line[:SEARCH_MAX_LINE_CHARS] + "..." if len(line) > SEARCH_MAX_LINE_CHARS else line`
         - `matches.append({"path": rel_str, "line": line_num, "content": preview})`
   - **반환값**:
     ```python
     return json.dumps(
         {
             "success": True,
             "mode": "content" if pattern else "files",
             "matches": matches,
             "total_matches": len(matches),
             "files_scanned": files_scanned,
             "truncated": truncated,
         },
         ensure_ascii=False,
     )
     ```

### 1-4. 디스패처 및 프롬프트 빌더 배선 ([`tools/dispatcher.py`](../../../../src/hermes_core/tools/dispatcher.py), [`agent/prompt_builder.py`](../../../../src/hermes_core/agent/prompt_builder.py))

1. **`dispatcher.py`**:
   - `file_tools`에서 `SEARCH_FILES_SCHEMA`, `search_files_tool`을 추가로 import한다.
   - `_DEFAULT_TOOL_NAMES`의 `"patch"` 바로 뒤에 `"search_files"`를 추가한다 (총 9종).
   - `create_default_dispatcher`에서 `patch` 등록 바로 아래에:
     ```python
     if _should_register("search_files"):
         dispatcher.register_tool("search_files", search_files_tool, SEARCH_FILES_SCHEMA)
     ```
     를 추가한다 (읽기 전용이므로 `checkpointer`로 감싸지 않는다).
2. **`prompt_builder.py`**:
   - 정적 프롬프트 상수 구역에 추가:
     ```python
     SEARCH_FILES_GUIDANCE = (
         "To locate files by name or find text inside files in the workspace, use search_files "
         "before reading with read_file or editing with patch (prefer search_files over terminal find/grep)."
     )
     ```
   - `select_tools_guidance`에서 `"session_search"` 검사 바로 다음(또는 `"skills_list"` 앞)에:
     ```python
     if "search_files" in names_set:
         guidance.append(SEARCH_FILES_GUIDANCE)
     ```
     를 추가한다.

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py`
- `src/hermes_core/tools/path_guard.py`
- `src/hermes_core/tools/file_tools.py`
- `src/hermes_core/tools/dispatcher.py`
- `src/hermes_core/agent/prompt_builder.py`
- `tests/test_hermes_core/test_search_files.py` (신규): 아래 시나리오 1~15
- `docs/로드맵/16-루프-정체-감지-자가-교정-MVP/README.md`: 010 상태와 실행 기록

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

시험 파일: `tests/test_hermes_core/test_search_files.py`(신규). `tmp_path`를 작업 폴더(`workspace`)로 사용한다.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `a.py`, `sub/b.py`, `sub/note.txt`가 있을 때 `search_files_tool(file_glob="*.py", workspace=tmp_path)` (`pattern=""`) | `success: True`, `mode: "files"`, `matches`가 사전순 `[{"path": "a.py"}, {"path": "sub/b.py"}]` 2건이고 `truncated: False`이다 (가정 A) |
| 2 | 위 파일들에서 `search_files_tool(pattern="def login", file_glob="*.py", workspace=tmp_path)` (`regex=False`) | `mode: "content"`, `def login(user):`처럼 괄호가 들어간 줄도 정규식 이스케이프 없이 정확히 찾아 `path`, 1-based `line`, `content`를 반환한다 (가정 A) |
| 3 | `search_files_tool(pattern=r"user_\d+", regex=True, workspace=tmp_path)` | 정규식에 매칭되는 줄만 정확히 반환한다 |
| 4 | `search_files_tool(pattern="[unclosed", regex=True, workspace=tmp_path)` | 예외를 던지지 않고 `success: False`, `"Invalid regex pattern"` 에러 JSON을 반환한다 |
| 5 | `search_files_tool(pattern="", file_glob="", workspace=tmp_path)` | `success: False`, `"At least one of 'pattern' or 'file_glob' must be non-empty."` 에러 JSON을 반환한다 |
| 6 | **보호 파일·폴더 자동 제외 (가정 B)**: 작업 폴더 안에 `.env`, `.env.local`, `.git/config`, `sub/.env.prod`에 모두 `"SECRET_TOKEN"`이 들어 있고 `app.py`에도 `"SECRET_TOKEN"`이 있을 때 `search_files_tool(pattern="SECRET_TOKEN", workspace=tmp_path)` 및 `search_files_tool(file_glob="*", workspace=tmp_path)` | 두 호출 모두 `matches`에 `app.py`만 나오고 `.env*`·`.git` 경로는 단 1건도 포함되지 않는다 |
| 7 | **Hermes 상태 폴더 자동 제외 (가정 B)**: `HERMES_HOME`이 `tmp_path / ".hermes"`이고 그 안(`state.db`, `memories/MEMORY.md`)에 `"SECRET_MEMORY"`가 있을 때 `search_files_tool(pattern="SECRET_MEMORY", workspace=tmp_path)` 및 `search_files_tool(path=".hermes", pattern="SECRET_MEMORY", workspace=tmp_path)` | 루트 탐색에서는 `.hermes` 내부가 자동 제외되어 `matches == []`이고, `path=".hermes"` 직접 지정 시에는 `success: False`, `"Searching inside the Hermes state folder is not allowed."`로 거부된다. 반면 작업 폴더 자체가 `HERMES_HOME / "workspace"`일 때는 그 안의 정상 파일이 정상 검색된다 |
| 8 | **심볼릭 링크 및 작업 폴더 외부 탈출 방어 (가정 B)**: 작업 폴더 밖 파일을 가리키는 심볼릭 링크 파일 `ext.py`, 외부 폴더를 가리키는 심볼릭 링크 디렉터리 `ext_dir`, 자기 자신을 가리키는 순환 링크 `loop_dir`가 있을 때 전체 탐색 실행 | 무한 루프나 외부 내용 노출 없이 모든 심볼릭 링크를 건너뛰며, `path="ext.py"` 직접 지정 시에도 거부된다 |
| 9 | 작업 폴더 밖 시작 경로 `search_files_tool(path="../outside", pattern="x", workspace=tmp_path)` 또는 보호 경로 `search_files_tool(path=".env", pattern="x", workspace=tmp_path)` | `check_path`가 작동하여 `success: False`로 거부된다 |
| 10 | 바이너리 파일(`b"hello\x00world"`), 비UTF-8 파일(`b"\xff\xfe"`), `FILE_MAX_BYTES` 초과 파일에 검색어가 들어 있을 때 내용 검색 | 에러로 중단되지 않고 해당 파일들을 조용히 건너뛴다 |
| 11 | **매치 건수 상한 (`SEARCH_MAX_MATCHES = 50`, 가정 C)**: 80줄 모두 `" hit "`를 포함한 파일에서 내용 검색, 및 80개 파일에서 파일 검색 | 두 모드 모두 `len(matches) == 50`, `total_matches == 50`, `truncated is True`이다. 정확히 50건만 있을 때는 `truncated is False`이다 |
| 12 | **줄 길이 상한 (`SEARCH_MAX_LINE_CHARS = 200`, 가정 C)**: 350자짜리 줄이 매칭되었을 때 | 반환된 `content` 길이는 `203`자(`200`자 + `"..."`)이다 |
| 13 | **스캔 파일 수 상한 (`SEARCH_MAX_FILES_SCANNED = 5000`, 가정 C)**: `monkeypatch`로 `SEARCH_MAX_FILES_SCANNED = 5`로 낮춘 뒤 파일 10개가 있는 폴더에서 탐색 | `files_scanned == 5`, `truncated is True`로 조기 중단된다 |
| 14 | `create_default_dispatcher()` 및 `create_child_dispatcher()`에서 `handle_function_call("search_files", ...)` 호출 | 부모와 자식 디스패처 모두 `search_files`가 등록되어 정상 동작하고, 정의되지 않은 인자(`unexpected_arg`)는 스키마 검증기(`_validate_arguments`)가 거부한다 |
| 15 | `Checkpointer`가 연결된 `create_default_dispatcher(checkpointer=cp)`에서 `search_files` 호출 | `file_checkpoints` 테이블에 행이 추가되지 않는다(읽기 전용 확인). 또한 `select_tools_guidance(["search_files"])`에 `SEARCH_FILES_GUIDANCE`가 포함된다 |

---

## 4. 검증 기준 (Done 조건)

- 시나리오 1~15 전부 통과.
- 기존 시험 통과(도구 목록 개수 단정이 있는 기존 시험이 있다면 `search_files` 추가로 인한 기대 목록만 정합성 있게 맞춤), 전체 회귀 테스트 `-n 4` 및 `-m pty` 직렬 통과.
- 대시보드 010 `Done`, 실행 기록 갱신.
