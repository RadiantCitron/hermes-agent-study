# [구현지시서 020] `read_file` 도구

> **상위 대시보드**: [03-README.md](../README.md)  
> **선행 조건**: 010 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §6-1, §6-2, §6-5, [ADR-0003](../../../adr/0003-파일_도구_범위와_쓰기_안전_정책.md) §2-1, 결정 D-06

---

## 1. 구현 내용 명세

1. **상수 추가** ([`constants.py`](../../../../src/hermes_core/constants.py), 「파일 도구 제한 상수」 절 신설):
   - `FILE_READ_MAX_LINES = 2000` (한 번에 읽는 최대 줄 수이자 `limit`의 기본값·최댓값)
   - `FILE_READ_MAX_CHARS = 100_000` (줄 번호가 붙은 출력 합계 상한)
   - `FILE_MAX_BYTES = 1_000_000` (읽기·수정 대상 파일 크기 상한)
2. **경로 검사 함수 추가** (`path_guard.py`): `check_path(path: str, workspace: Path) -> tuple[Optional[Path], Optional[str]]`
   - 제어문자(`\x00`~`\x1f`, `\x7f`, `\x85`, ` `, ` `)가 있으면 `(None, "Path contains control characters.")`.
   - `is_inside(path, workspace)`가 거짓이면 `(None, "Path '<path>' is outside the workspace.")` (터미널과 같은 문구).
   - 통과하면 `((workspace / path).resolve(), None)`. `~`는 확장하지 않는다.
3. **`read_file` 도구** (신규 `src/hermes_core/tools/file_tools.py`): `read_file_tool(path: str, offset: int = 1, limit: int = FILE_READ_MAX_LINES, workspace: Optional[Path] = None) -> str`와 `READ_FILE_SCHEMA`.
   - 순서: `resolve_workspace` → `check_path` → `offset`·`limit` 범위 검사 → 대상 검사 → 읽기.
   - 범위 검사: `offset < 1`이면 `offset must be >= 1`, `limit < 1` 또는 `limit > 2000`이면 `limit must be between 1 and 2000`을 실패 메시지로 돌려준다.
   - 대상 검사 (실패 문구에 포함할 단어): 없는 경로 `not found`, 폴더 `is a directory`, 크기 `FILE_MAX_BYTES` 초과 `too large`, NUL 바이트가 든 파일 `binary`, UTF-8이 아님 `UTF-8`.
   - 읽기: 파일을 **바이트로 읽어 UTF-8로 해석**한다. `\n`으로 줄을 나누고 마지막 빈 조각(끝 줄바꿈)은 버린다. 각 줄 끝의 `\r` 하나는 표시에서 뺀다.
   - 출력: 각 줄을 `줄번호|내용`으로 만들어 `\n`으로 잇는다(끝 줄바꿈 없음). 줄번호는 파일 기준(1부터).
   - 구간: `offset` 줄부터 `limit`줄까지, 그리고 형식이 붙은 줄의 길이(+줄바꿈 1)를 합산해 `FILE_READ_MAX_CHARS`를 넘기 직전 줄에서 멈춘다. 첫 줄 하나가 예산을 넘으면 그 줄을 `FILE_READ_MAX_CHARS`자까지 자르고 `truncated`를 참으로 하며, `next_offset`은 다음 줄이 있으면 그 줄 번호, 없으면 `null`로 한다.
   - `offset`이 줄 수를 넘으면(`offset > total_lines`, 단 빈 파일은 `offset == 1`까지 허용) `offset N is beyond end of file (M lines)`.
   - 성공 결과(JSON): `success`(true), `content`, `total_lines`, `offset`, `lines_returned`, `truncated`(출력이 잘렸거나 뒤에 읽을 줄이 남았으면 true), `next_offset`(뒤에 읽을 줄이 남았으면 다음 줄 번호, 아니면 `null`).
   - 실패 결과: `{"success": false, "error": ...}` (예외를 밖으로 내지 않는다).
4. **기본 디스패처 등록** ([`dispatcher.py`](../../../../src/hermes_core/tools/dispatcher.py) `create_default_dispatcher`): `read_file`을 등록한다. 설명서에는 `path`(필수, 문자열), `offset`(정수, 기본 1), `limit`(정수, 기본 2000)만 둔다. **`workspace`는 설명서에 넣지 않는다.** 설명에 「줄 번호 형식, 작업 폴더 밖 거부, 쓰기는 `write_file`·`patch`」를 적는다.
5. **작동원리 갱신** (01편): Q4의 도구 목록 문장(「…3종입니다」)과 「파일을 읽고 쓰는 전용 도구는 지금은 없습니다」를 읽기 전용 도구가 생긴 사실에 맞게 고치고, 3부 지도의 도구 실행 라벨, 5부 ① 「파일 읽기」 행 신설·② 색인 항목 추가·③ 검증 행 신설을 한다. 02편은 ② 색인에 `file_tools.py`가 생겼음만 반영한다. `기준 커밋` 값은 건드리지 않는다.

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py`, `src/hermes_core/tools/path_guard.py`, `src/hermes_core/tools/dispatcher.py` (수정)
- `src/hermes_core/tools/file_tools.py` (신규)
- `tests/test_hermes_core/test_file_tools.py` (신규)
- `docs/작동원리/01-헤르메스코어-작동원리.md`, `docs/작동원리/02-코어-안전강화-작동원리.md` (수정, 위 5번 범위만)
- `docs/로드맵/03-파일도구-추가/README.md`, `docs/인계.md` (갱신)

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

작업 폴더 `W` 구성: `a.txt`(내용 `line1\nline2\nline3\n`), `empty.txt`(빈 파일), `crlf.txt`(내용 `a\r\nb\r\n`), `sub/`(폴더) 안의 `b.txt`, `binary.bin`(바이트 `\x00\x01`), `latin.txt`(바이트 `\xff\xfe`). `W` 밖에 `outside.txt`(내용 `OUTSIDE-SECRET`)가 있고 `W/link`가 이를 가리킨다. 호출은 `read_file_tool(path, ..., workspace=W)`이며, 어떤 반환값에도 `OUTSIDE-SECRET`이 나타나면 안 된다.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `a.txt` | `success` true, `content == "1\|line1\n2\|line2\n3\|line3"`, `total_lines == 3`, `lines_returned == 3`, `truncated` false, `next_offset` null |
| 2 | `a.txt`, `offset=2` | `content == "2\|line2\n3\|line3"`, `offset == 2`, `lines_returned == 2` |
| 3 | `a.txt`, `limit=1` | `content == "1\|line1"`, `truncated` true, `next_offset == 2` |
| 4 | `a.txt`, `offset=2`, `limit=1` | `content == "2\|line2"`, `next_offset == 3` |
| 5 | `a.txt`, `offset=4` | `success` false, 오류에 `beyond` 포함 |
| 6 | `offset=0` / `limit=0` / `limit=2001` | 각각 `success` false, 오류에 `offset` / `limit` / `limit` 포함 |
| 7 | `empty.txt` | `success` true, `content == ""`, `total_lines == 0`, `lines_returned == 0`, `truncated` false |
| 8 | `crlf.txt` | `content == "1\|a\n2\|b"` (줄 끝 `\r` 제외) |
| 9 | 2,500줄 파일 (각 줄 `x`) | `lines_returned == 2000`, `truncated` true, `next_offset == 2001` |
| 10 | 줄 3개(각 줄 `x`를 60,000개) | `lines_returned == 1`, `truncated` true, `next_offset == 2`, `len(content) <= 100000` |
| 11 | 한 줄에 `x`를 150,000개 (파일 크기는 상한 이내) | `content`가 `1\|`로 시작, `len(content) == 100000`, `truncated` true, `next_offset` null (다음 줄이 없음) |
| 12 | `binary.bin` | `success` false, 오류에 `binary` 포함 |
| 13 | `latin.txt` | `success` false, 오류에 `UTF-8` 포함 |
| 14 | `missing.txt` | `success` false, 오류에 `not found` 포함 |
| 15 | `sub` (폴더) | `success` false, 오류에 `is a directory` 포함 |
| 16 | `../outside.txt` / `/etc/hosts` / `link` | 각각 `success` false, 오류에 `outside` 포함. `OUTSIDE-SECRET`이 반환값에 없음 |
| 17 | 경로 `a\nb` (줄바꿈 문자가 든 경로) | `success` false, 오류에 `control` 포함 |
| 18 | 크기 1,000,001바이트 파일 | `success` false, 오류에 `too large` 포함 |
| 19 | 경로 `~/x` | `success` false, 오류에 `not found` 포함 (홈 폴더로 확장하지 않음) |
| 20 | 기본 디스패처(`create_default_dispatcher()`)로 `handle_function_call("read_file", {"path": "a.txt", "workspace": "/"})`, 현재 폴더는 `W` | `success` false, 오류에 `workspace` 포함 (도구 통로가 거부) |
| 21 | 위 디스패처로 `{"path": "a.txt"}` (현재 폴더는 `W`) | `success` true, `content`가 `1\|line1`로 시작 |
| 22 | 위 디스패처의 `get_tools_schema()` | 이름 `read_file`이 있고, 그 `properties`에 `workspace`가 없음 |

---

## 4. 검증 기준 (Done 조건)

1. `test_file_tools.py`의 22개 시나리오가 통과한다.
2. 기존 테스트(010 포함)가 모두 통과한다.
3. `grep -n "workspace" src/hermes_core/tools/file_tools.py`로, 설명서(`READ_FILE_SCHEMA`) 정의 안에 `workspace`가 없음을 확인한다.
4. 시나리오 16의 `OUTSIDE-SECRET`이 어떤 반환값에도 나타나지 않음을 단언한다.
