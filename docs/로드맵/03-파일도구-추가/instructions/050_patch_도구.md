# [구현지시서 050] `patch` 도구

> **상위 대시보드**: [03-README.md](../README.md)  
> **선행 조건**: 040 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §4 (위험 3·6·7·8), §6-4, §6-5, [ADR-0003](../../../adr/0003-파일_도구_범위와_쓰기_안전_정책.md) §2-2, §3 대안 B, 결정 D-08

---

## 1. 구현 내용 명세

1. **`patch` 도구** (`src/hermes_core/tools/file_tools.py`, 020에서 신설): `patch_tool(path: str, old_string: str, new_string: str, replace_all: bool = False, workspace: Optional[Path] = None) -> str`와 `PATCH_SCHEMA`.
   - 순서: `resolve_workspace` → `check_write_path` → 대상 검사 → 문자열 검사 → 교체 → 결과 크기 검사 → 원자적 쓰기. 앞 단계에서 거부되면 파일을 바꾸지 않는다.
   - 대상 검사: 없는 경로 `not found`, 일반 파일이 아님 `not a regular file`, `FILE_MAX_BYTES` 초과 `too large`, NUL 바이트 `binary`, UTF-8이 아님 `UTF-8`.
   - 문자열 검사: `old_string`이 비어 있으면 `old_string must not be empty`, `old_string == new_string`이면 `must differ`.
   - 파일을 **바이트로 읽어 UTF-8로 해석**한다. 줄바꿈을 변환하지 않는다.
   - 일치: `text.count(old_string)`(겹치지 않게 셈)이 0이면 `old_string not found in file`(파일 내용을 오류에 싣지 않는다), 2 이상이고 `replace_all`이 아니면 `old_string found N times; make it unique or set replace_all`. 정확히 일치하는 문자열만 찾는다(공백·들여쓰기·줄바꿈 차이를 흡수하지 않음).
   - 교체 후 UTF-8 크기가 `FILE_MAX_BYTES`를 넘으면 `result too large`로 거부하고 파일을 바꾸지 않는다.
   - 원자적 쓰기: 040과 같은 방식(같은 폴더의 임시 파일 → `fsync` → `os.replace`). 임시 파일의 권한은 **원본 파일의 권한**을 따른다. 실패하면 임시 파일을 지우고 `Failed to write file ...` 실패 메시지를 돌려주며 원본은 그대로다.
   - 성공 결과(JSON): `success`(true), `path`(작업 폴더 기준 상대경로), `replacements`(교체 횟수), `chars_before`, `chars_after`.
2. **기본 디스패처 등록**: `patch`를 등록한다. 설명서에는 `path`, `old_string`, `new_string`(필수, 문자열), `replace_all`(불리언, 기본 거짓)만 둔다. **`workspace`는 넣지 않는다.** 설명에 「정확히 일치하는 문자열 하나를 교체한다, 여러 곳이면 더 길게 적거나 `replace_all`」을 적는다.
3. **작동원리 갱신** (01편): Q4의 파일 도구 서술, 5부 ① 「파일 수정」 행·② 색인·③ 검증 행, 「지금은 없는 방어선」에 「파일 통째 교체 수단 없음」 등 한계를 추가·수정한다. 02편은 ② 색인만 반영한다. 03편의 수정 도구 관련 `[계획]` 서술(Q3, Q8, 3부 지도, 5부 ①②③)도 사실로 고친다. `기준 커밋` 값은 건드리지 않는다.

---

## 2. 변경할 파일 목록

- `src/hermes_core/tools/file_tools.py`, `src/hermes_core/tools/dispatcher.py` (수정)
- `tests/test_hermes_core/test_file_tools.py` (수정, 시나리오 추가)
- `docs/작동원리/01-헤르메스코어-작동원리.md`, `docs/작동원리/02-코어-안전강화-작동원리.md`, `docs/작동원리/03-파일도구-작동원리.md` (수정, 위 3번 범위만)
- `docs/로드맵/03-파일도구-추가/README.md`, `docs/인계.md` (갱신)

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

작업 폴더 `W` 구성: `p.txt`(내용 `alpha\nbeta\nalpha\n`), `q.txt`(내용 `one two three\n`), `crlf.txt`(바이트 `a\r\nb\r\nc\r\n`), `sub/`(폴더), `binary.bin`(`\x00\x01`), `latin.txt`(`\xff\xfe`), `AGENTS.md`(내용 `rules\n`). `W` 밖에 `outside.txt`(내용 `OUTSIDE-SECRET`)가 있고 `W/link`가 이를 가리킨다. `W/agents_link`는 `W/AGENTS.md`를 가리킨다. 호출은 `patch_tool(path, old, new, ..., workspace=W)`. `HERMES_HOME`은 `W` 밖 임시 폴더.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `q.txt`, `two` → `2` | `success` true, `replacements == 1`, 파일 `one 2 three\n` |
| 2 | `p.txt`, `alpha` → `X` (`replace_all` 없음) | `success` false, 오류에 `2 times` 포함, 파일 그대로 |
| 3 | `p.txt`, `alpha` → `X`, `replace_all=True` | `replacements == 2`, 파일 `X\nbeta\nX\n` |
| 4 | `q.txt`, `missing` → `x` | `success` false, 오류에 `not found` 포함, 파일 그대로 |
| 5 | `q.txt`, `two` → `two` / `q.txt`, `` → `x` | 각각 `success` false, 오류에 `differ` / `empty` 포함 |
| 6 | `q.txt`, ` two` → `` (삭제) | 파일 `one three\n` |
| 7 | `p.txt`, `alpha\nbeta` → `Z` (여러 줄, 유일) | 파일 `Z\nalpha\n` |
| 8 | `crlf.txt`, `b` → `B` | 파일 바이트 `b"a\r\nB\r\nc\r\n"` (`\r\n` 유지) |
| 9 | `crlf.txt`, `a\nb` → `x` (LF로 적음) | `success` false, 오류에 `not found` 포함 (줄바꿈까지 정확히 일치해야 함), 파일 그대로 |
| 10 | 권한 0o755인 `exec.sh`(내용 `old\n`), `old` → `new` | `success` true, 이후 권한이 여전히 `0o755` |
| 11 | `nofile.txt` / `sub` | 각각 `success` false, 오류에 `not found` / `regular file` 포함 |
| 12 | `binary.bin` / `latin.txt` | 각각 `success` false, 오류에 `binary` / `UTF-8` 포함 |
| 13 | (a) 크기 1,000,001바이트 파일 `big.txt`, (b) `y` 999,989개와 `Z` 1개로 이뤄진 999,990바이트 파일 `near.txt`에서 `Z` → `Z`를 20개 이은 문자열로 교체(결과 1,000,009바이트) | (a) 오류에 `too large`, (b) 오류에 `too large` 포함, 두 파일 모두 그대로 |
| 14 | `AGENTS.md` / `agents_link` / (`HERMES_HOME=W/.hermes`일 때) `.hermes/memories/MEMORY.md` | 각각 `success` false, 파일 내용 그대로 (오류에 각각 `instruction` / `instruction` / `state folder` 포함) |
| 15 | `../outside.txt` / `link` | 각각 `success` false, 오류에 `outside` 포함, `outside.txt` 내용이 `OUTSIDE-SECRET` 그대로 |
| 16 | 경로 `a\nb` | `success` false, 오류에 `control` 포함 |
| 17 | `os.replace`가 `OSError`를 던지도록 바꾼 뒤 `q.txt`, `two` → `2` | `success` false, 오류에 `Failed to write` 포함, `q.txt` 원본 그대로, 같은 폴더에 임시 파일이 남지 않음 |
| 18 | 겹치는 일치: 파일 `aaa`, `aa` → `b`, `replace_all=True` | `replacements == 1`, 파일 `ba` (겹치지 않게 셈) |
| 19 | 한글 파일 `kr.txt`(`안녕 세계\n`), `세계` → `우주` | 파일 `안녕 우주\n` |
| 20 | 기본 디스패처의 `get_tools_schema()` | 이름 `patch`가 있고 `properties`에 `workspace`가 없음 |
| 21 | 위 디스패처로 `handle_function_call("patch", {"path": "q.txt", "old_string": "two", "new_string": "2", "workspace": "/"})` (현재 폴더는 `W`) | `success` false, 오류에 `workspace` 포함, `q.txt` 그대로 |
| 22 | 위 디스패처로 `replace_all`에 문자열 `"yes"` | `success` false, 오류에 `replace_all` 포함 (도구 통로의 타입 검사), 파일 그대로 |

---

## 4. 검증 기준 (Done 조건)

1. 위 22개 시나리오가 `test_file_tools.py`에서 통과한다.
2. 기존 테스트(010~040 포함)가 모두 통과한다.
3. 시나리오 13·14·15에서 대상 파일과 `outside.txt`가 변하지 않았음을 파일 내용으로 단언한다.
4. `grep -n "workspace" src/hermes_core/tools/file_tools.py`로, `PATCH_SCHEMA` 정의 안에 `workspace`가 없음을 확인한다.
