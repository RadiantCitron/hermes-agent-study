# [구현지시서 040] `write_file` 도구

> **상위 대시보드**: [03-README.md](../README.md)  
> **선행 조건**: 030 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §4 (위험 3·6·7·8), §6-4, §6-5, [ADR-0003](../../../adr/0003-파일_도구_범위와_쓰기_안전_정책.md) §2-2, 결정 D-02, D-07

---

## 1. 구현 내용 명세

1. **상수 추가** ([`constants.py`](../../../../src/hermes_core/constants.py)): `FILE_WRITE_MAX_CHARS = 100_000` (`write_file` 내용 상한, 글자 수).
2. **`write_file` 도구** (`src/hermes_core/tools/file_tools.py`, 020에서 신설): `write_file_tool(path: str, content: str, workspace: Optional[Path] = None) -> str`와 `WRITE_FILE_SCHEMA`.
   - 순서: `resolve_workspace` → `check_write_path` → 내용 크기 검사 → 이미 있는지 검사 → 부모 폴더 생성 → 원자적 쓰기. **앞 단계에서 거부되면 아무것도 만들지 않는다(부모 폴더 포함).**
   - 내용 크기: `len(content) > FILE_WRITE_MAX_CHARS`이면 `content is too large (...)`.
   - 이미 있는지: 풀린 경로가 존재하거나, 주어진 경로 자체가 (끊어진 링크를 포함해) 존재하면 `already exists`를 포함한 실패 메시지에 「수정하려면 `patch`를 사용한다」를 적는다. 폴더도 같다. **기존 파일을 바꾸지 않는다.**
   - 부모 폴더: 풀린 경로의 부모를 `mkdir(parents=True, exist_ok=True)`로 만든다 (작업 폴더 안임이 이미 판정되었다).
   - 원자적 쓰기: 같은 폴더에 임시 파일(`tempfile.mkstemp`)을 만들어 `content`를 **UTF-8 바이트로** 쓰고(`flush`, `fsync`) 권한을 0o644로 맞춘 뒤, 교체 직전에 대상이 아직 없는지 한 번 더 확인하고 `os.replace`로 옮긴다. 실패하면 임시 파일을 지우고 `Failed to write file ...`을 포함한 실패 메시지를 돌려준다. 줄바꿈을 변환하지 않는다.
   - 성공 결과(JSON): `success`(true), `path`(작업 폴더 기준 상대경로, `/` 구분), `chars_written`, `bytes_written`.
3. **기본 디스패처 등록** ([`dispatcher.py`](../../../../src/hermes_core/tools/dispatcher.py)): `write_file`을 등록한다. 설명서에는 `path`, `content`(둘 다 필수, 문자열)만 둔다. **`workspace`는 넣지 않는다.** 설명에 「새 파일만 만든다, 기존 파일은 `patch`로 수정한다, 지시문 파일과 상태 폴더는 쓸 수 없다」를 적는다.
4. **작동원리 갱신** (01편): Q4의 파일 도구 서술, 3부 방어선 요약(쓰기 도구와 금지 정책), 5부 ① 「파일 생성」 행·② 색인·③ 검증 행을 추가·수정한다. 02편은 ② 색인만 반영한다. `기준 커밋` 값은 건드리지 않는다.

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py`, `src/hermes_core/tools/file_tools.py`, `src/hermes_core/tools/dispatcher.py` (수정)
- `tests/test_hermes_core/test_file_tools.py` (수정, 시나리오 추가)
- `docs/작동원리/01-헤르메스코어-작동원리.md`, `docs/작동원리/02-코어-안전강화-작동원리.md` (수정, 위 4번 범위만)
- `docs/로드맵/03-파일도구-추가/README.md`, `docs/인계.md` (갱신)

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

작업 폴더 `W` 구성: `a.txt`(내용 `line1\n`), `sub/`(폴더). `W` 밖에 `outside.txt`(내용 `OUTSIDE-SECRET`)가 있고 `W/link`가 이를 가리킨다. `W/dang`은 `W` 안의 아직 없는 `ghost.txt`를 가리키는 끊어진 링크다. 호출은 `write_file_tool(path, content, workspace=W)`. `HERMES_HOME`은 `W` 밖 임시 폴더.

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `new.txt`, `hello\n` | `success` true, 파일 바이트가 `b"hello\n"`, `path == "new.txt"`, `chars_written == 6` |
| 2 | `d1/d2/new.txt`, `x` (부모 폴더 없음) | `success` true, 파일 생성됨, 부모 폴더 생성됨 |
| 3 | `a.txt`, `NEW` | `success` false, 오류에 `already exists`와 `patch` 포함, `a.txt` 내용이 `line1\n` 그대로 |
| 4 | `sub` (기존 폴더), `x` | `success` false, 오류에 `already exists` 포함 |
| 5 | `kr.txt`, `한글\r\n` | 파일 바이트가 `"한글\r\n".encode("utf-8")`와 정확히 같음 (줄바꿈 변환 없음) |
| 6 | 길이 100,000인 내용 → 성공, 길이 100,001인 내용(다른 파일 이름) | 전자 `success` true, 후자 `success` false·오류에 `too large` 포함·파일 미생성 |
| 7 | `empty.txt`, `""` | `success` true, 파일 크기 0 |
| 8 | `../x.txt` / `W` 밖 임시 폴더 `OUT` 안의 절대경로 `OUT/x.txt` / `../newdir/x.txt` | 각각 `success` false, 오류에 `outside` 포함, 어떤 파일·폴더도 만들어지지 않음 (`W`의 부모와 `OUT`에서 확인) |
| 9 | `AGENTS.md` / `.hermes/x.txt`(`HERMES_HOME=W/.hermes`) / `newdir/SOUL.md` | 각각 `success` false, 파일 미생성, **`newdir` 폴더도 만들어지지 않음** |
| 10 | `dang` (끊어진 링크), `x` | `success` false, 오류에 `already exists` 포함, `ghost.txt` 미생성 |
| 11 | `link`, `OVERWRITE` | `success` false, 오류에 `outside` 포함, `outside.txt` 내용이 `OUTSIDE-SECRET` 그대로 |
| 12 | 경로 `a\nb` | `success` false, 오류에 `control` 포함 |
| 13 | `os.replace`가 `OSError`를 던지도록 바꾼 뒤 `fail.txt`, `x` | `success` false, 오류에 `Failed to write` 포함, `fail.txt` 미생성, 같은 폴더에 임시 파일이 남지 않음 |
| 14 | 성공한 쓰기 직후 `W` 폴더 목록 | 임시 파일이 남아 있지 않음 (예상한 이름만 존재) |
| 15 | 새로 만든 파일의 권한 | `0o644` |
| 16 | 기본 디스패처(`create_default_dispatcher()`)의 `get_tools_schema()` | 이름 `write_file`이 있고 `properties`에 `workspace`가 없음 |
| 17 | 위 디스패처로 `handle_function_call("write_file", {"path": "n.txt", "content": "x", "workspace": "/"})` (현재 폴더는 `W`) | `success` false, 오류에 `workspace` 포함, `n.txt` 미생성 |
| 18 | 위 디스패처로 `{"path": "n.txt", "content": "x"}` (현재 폴더는 `W`) | `success` true, `W/n.txt` 생성 |

---

## 4. 검증 기준 (Done 조건)

1. 위 18개 시나리오가 `test_file_tools.py`에서 통과한다.
2. 기존 테스트(010~030 포함)가 모두 통과한다.
3. 시나리오 8·9·11에서 `outside.txt`와 기존 파일이 변하지 않았음을 단언한다 (반환값뿐 아니라 파일 내용으로 확인).
4. `grep -n "workspace" src/hermes_core/tools/file_tools.py`로, `WRITE_FILE_SCHEMA` 정의 안에 `workspace`가 없음을 확인한다.
