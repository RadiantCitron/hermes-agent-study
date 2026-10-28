# [구현지시서 030] `skill_view` 도구 (이름 해석·경로 안전·상한·부속 파일)

> **상위 대시보드**: [06-README.md](../README.md)  
> **선행 조건**: 020 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §6-3, §4 위험 1~4·9·10·12, [ADR-0006](../../../adr/0006-스킬_시스템_MVP_범위와_로딩_방식.md) §2-3~§2-5·§2-7, [WHY.md](../WHY.md) 가정 C·E·G  
> **성격**: 이 로드맵에서 **가장 위험한 단계**다(모델이 이름과 경로를 정한다). 시험을 먼저 읽고 구현한다.

---

## 1. 구현 내용 명세

0. **상수** ([`constants.py`](../../../../src/hermes_core/constants.py)): `SKILL_VIEW_MAX_CHARS = 20_000`, `SKILL_LINKED_FILES_MAX_PER_KIND = 100`, `SKILL_LINKED_DIRS = ("references", "templates", "assets", "scripts")`(010에서 옮겨 온 것: 쓰는 단계에서 만든다).
1. **`skill_view_tool(name: str, file_path: Optional[str] = None, skills_root: Optional[Path] = None) -> str`** ([`skills_tool.py`](../../../../src/hermes_core/tools/skills_tool.py)): `skills_root`가 `None`이면 호출 시점의 `get_skills_dir()`, 어느 경우든 `resolve()`한 값을 기준으로 한다. 순서는 아래와 같다(앞 단계가 실패하면 뒤는 하지 않는다).
   1. `name`이 비어 있거나 공백뿐이면 실패.
   2. `path_guard.check_path(name, 스킬 루트)` — 제어문자, 루트 이탈(심볼릭 링크 포함), 보호 이름(`.env*`, `.git`)을 거부한다. [`path_guard.py`](../../../../src/hermes_core/tools/path_guard.py)는 **수정하지 않고 재사용**한다.
   3. `discover_skills`로 스킬 목록을 얻어 이름을 해석한다: `path == name` 우선, 아니면 폴더 이름 또는 머리말 `name`이 `name`과 같은 스킬 전부. 0개면 `available_skills`(처음 20개 `path`)를 담은 실패, 2개 이상이면 `candidates`(그 `path`들)를 담은 실패. 조용히 하나를 고르지 않는다.
   4. `file_path`가 없으면 **본문**: `SKILL.md`를 아래의 공통 읽기 절차로 읽어 `{"success": true, "name", "description", "category", "path", "content", "truncated", "total_chars", "linked_files"?}`. `content`는 파일 전문(머리말 포함)을 `SKILL_VIEW_MAX_CHARS`(문자 수 기준)로 자른 것이며 자르면 `truncated: true`, `total_chars`는 자르기 전 문자 수다(잘린 뒤 나머지를 읽는 방법은 06에 없으므로 모델이 잘렸음과 크기를 알 수 있게 한다). `linked_files`는 [ROADMAP §6-3](../ROADMAP.md) 규칙(4개 폴더, 종류별 `SKILL_LINKED_FILES_MAX_PER_KIND`개, 정렬, 실제 경로가 스킬 폴더 밖이거나 보호 이름인 것 제외, 비면 키 없음).
   5. `file_path`가 있으면 **부속 파일**: `check_path(file_path, 그 스킬 폴더)` 통과 → 존재·파일 여부 확인 → 공통 읽기 절차 → `{"success": true, "name", "path", "file", "content", "truncated", "total_chars"}`. 스킬 폴더 **안의 어떤 파일이든**(보호 이름 제외) 읽을 수 있고, 목록(`linked_files`·`available_files`)은 발견 편의를 위해 4개 폴더만 보여 준다. 파일이 없으면 `available_files`(폴더별, 종류별 상한 적용)를 담은 실패.
   - **공통 읽기 절차**(본문과 부속 파일이 같은 함수를 쓴다): 크기가 `FILE_MAX_BYTES`를 넘으면 실패 → 바이트로 읽어 NUL이 있으면 이진 파일 실패 → `utf-8-sig`로 디코드(BOM 제거, 실패하면 실패) → 문자 수 기준으로 자르기.
   6. 어떤 경우에도 파일을 쓰거나 만들거나 지우지 않는다. 예기치 못한 예외(`OSError` 등)는 실패 JSON으로 바꾼다.
2. **`SKILL_VIEW_SCHEMA`**: 이름 `skill_view`, `parameters.properties`는 `name`(string), `file_path`(string), `required == ["name"]`. `skills_root`는 노출하지 않는다.
3. **디스패처 등록**: `dispatcher.register_tool("skill_view", skill_view_tool, SKILL_VIEW_SCHEMA)`.
4. **작동원리 갱신**: 없음.

---

## 2. 변경할 파일 목록

- `src/hermes_core/constants.py` (수정), `src/hermes_core/tools/skills_tool.py` (수정), `src/hermes_core/tools/dispatcher.py` (수정)
- `tests/test_hermes_core/test_skill_view.py` (신규)
- `docs/로드맵/06-스킬-시스템-MVP/README.md`, `docs/인계.md` (갱신)

> **사전 점검 (착수 시)**: ① `check_path`가 `workspace` 인자로 받은 경로가 이미 `resolve()`된 값이어야 이탈을 올바르게 판정한다는 점을 코드로 다시 확인한다. ② 심볼릭 링크 시험은 이 환경(Python 3.14)에서 링크를 만들 수 있는지(`os.symlink`) 먼저 확인하고, 만들 수 없는 환경이면 해당 시나리오를 건너뛰지 말고 멈춰서 보고한다. ③ 파일 도구의 실패 JSON 형태(`{"success": false, "error": …}`)를 확인해 같은 형태를 쓴다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

공통 준비(`tmp_path`): `<HERMES_HOME>/skills/hello/SKILL.md`, `skills/devops/deploy/SKILL.md`(머리말 `name: Deploy App`) + `references/api.md`, `templates/t.yaml`, `assets/logo.bin`(NUL 포함), `assets/bad.txt`(UTF-8 아닌 바이트), `scripts/run.sh`, 스킬 폴더 안 `.env`와 `references/.env`(내용 `KEY=1`), 스킬 폴더 바로 아래 `notes.md`(`linked_files` 4개 폴더 밖의 파일). 스킬 루트 **밖**에 `<HERMES_HOME>/state.db`(내용 `SECRET`)와 `<HERMES_HOME>/outside.txt`(내용 `OUTSIDE`).

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | `name="hello"` | `success true`, `content`가 파일 전문과 같음, `truncated false`, `path "hello"` |
| 1b | 맨 앞에 BOM이 붙은 `SKILL.md` | `content`가 `\ufeff`로 시작하지 않음 |
| 2 | `name="devops/deploy"` (path) | 성공. `linked_files`에 `references`/`templates`/`assets`/`scripts` 키가 있고 값에 `references/api.md` 같은 스킬 폴더 기준 상대경로 |
| 3 | `name="deploy"`(폴더 이름) / `name="Deploy App"`(머리말 이름) | 둘 다 같은 스킬(`path "devops/deploy"`)로 성공 |
| 4 | `skills_list`가 돌려준 **모든 항목의 `path`**로 `skill_view` | 전부 성공 (목록 → 조회 왕복, 원본의 이름 불일치 재발 방지) |
| 5 | 이름이 같은 스킬 둘(`a/dup`, `b/dup`)에서 `name="dup"` / `name="a/dup"` | 앞은 실패 + `candidates == ["a/dup", "b/dup"]`, 뒤는 성공 |
| 5b | `foo/SKILL.md`(path `foo`)와 `x/foo/SKILL.md`(path `x/foo`, 폴더 이름 `foo`)가 함께 있을 때 `name="foo"` | `path` 일치가 우선하므로 `path "foo"` 스킬로 성공 (모호하지 않음) |
| 6 | 없는 이름 `name="nope"` / 스킬이 25개일 때 | 실패, `available_skills`에 `hello` 포함 / 개수가 정확히 20 |
| 7 | `name=""`, `name="   "` | 실패 |
| 8 | 이탈 `name`: `"../state.db"`, `"../../etc/passwd"`, `"/etc/passwd"`, `"devops/../../state.db"` | 모두 실패. 응답 어디에도 `SECRET` 없음 |
| 9 | 이탈 `file_path` (`name="deploy"`): `"../../../state.db"`, `"../../../outside.txt"`, `"../../hello/SKILL.md"`(다른 스킬 폴더), `"/etc/passwd"` | 모두 실패. 응답에 `SECRET`·`OUTSIDE` 없음. (원본 `skill_view`는 이 입력을 검사하지 않음) |
| 10 | 보호 이름: `file_path=".env"`, `file_path="references/.env"`, `file_path=".git/config"` | 모두 실패. 응답에 `KEY=1` 없음. `linked_files["references"]`에 `references/.env`가 없음 |
| 11 | 제어문자: `name="he\nllo"`, `file_path="refs\x00x"` | 실패 |
| 12 | 심볼릭 링크: `references/leak.md` → `outside.txt`, `skills/evil` → 스킬 루트 밖 폴더(`SKILL.md` 포함) | `file_path="references/leak.md"` 실패(응답에 `OUTSIDE` 없음), `linked_files`에 `leak.md` 없음, `name="evil"` 실패 |
| 13 | `file_path="references/api.md"` | 성공, `content`가 파일 전문, `file "references/api.md"`, `truncated false` |
| 14 | `file_path="SKILL.md"` / `file_path="notes.md"`(4개 폴더 밖) | 앞은 본문과 같은 내용, 뒤도 성공 (스킬 폴더 안이면 읽을 수 있음) |
| 15 | 없는 `file_path="references/none.md"` | 실패 + `available_files`에 `references/api.md` 포함 |
| 16 | 폴더를 가리킴 `file_path="references"` | 실패 (파일이 아님) |
| 17 | `file_path="assets/logo.bin"`(NUL 포함) / `file_path="assets/bad.txt"`(UTF-8 아님) | 둘 다 실패 (이진·디코드), 예외 없음 |
| 18 | 본문 경계: `SKILL.md` 길이 25,000자 / 정확히 20,000자 / 19,999자 (한글 문자로도 확인: 문자 수 기준이며 바이트 수가 아님) | `content` 길이 20,000이고 `truncated true`, `total_chars 25000` / 20,000이고 `truncated false` / 19,999이고 `truncated false`. 부속 파일도 같은 규칙 |
| 19 | 크기 1,000,001바이트 `SKILL.md`, 같은 크기 부속 파일 | 실패 (너무 큼) |
| 20 | `references/`에 파일 101개 | `linked_files["references"]` 길이 100 (정렬된 앞부분) |
| 21 | 위 모든 호출 전후 스킬 폴더(경로+내용 해시)와 `<HERMES_HOME>` 직하위 항목 목록 | 동일 (읽기 전용, 새 파일·폴더 없음) |
| 22 | `get_tools_schema()`의 `skill_view` | `properties` 키 집합 `{"name", "file_path"}`, `required == ["name"]`, `skills_root` 없음. 인자 `name` 누락 호출은 실패 JSON |
| 23 | 디스패처 경유 `handle_function_call("skill_view", {"name": "../state.db"})` | 예외 없이 `success false` JSON 문자열 |
| 24 | 스킬 루트 폴더가 없을 때 `name="hello"` | 실패(없음). 폴더가 생기지 않음 |
| 25 | 기존 코어 테스트(05 마감 시점 전체) | **수정 없이** 통과 |

---

## 4. 검증 기준 (Done 조건)

1. 시나리오 1~24가 통과한다 (`test_skill_view.py`). 특히 8·9·10·12는 응답 **전체 문자열**에 비밀 마커가 없음을 확인한다.
2. 기존 테스트가 모두 통과한다. 테스트 파일 diff에서 **삭제된 줄이 0**이다.
3. `git diff <단계 시작 커밋>..HEAD --stat`의 `src/` 변경이 `constants.py`, `skills_tool.py`, `dispatcher.py`뿐이다 (`path_guard.py` 무변경).
