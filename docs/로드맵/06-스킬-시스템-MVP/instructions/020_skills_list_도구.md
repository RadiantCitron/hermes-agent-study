# [구현지시서 020] `skills_list` 도구와 디스패처 등록

> **상위 대시보드**: [06-README.md](../README.md)  
> **선행 조건**: 010 단계 완료 (`Done`)  
> **근거**: [ROADMAP.md](../ROADMAP.md) §6-2, §4 위험 9·10, [ADR-0006](../../../adr/0006-스킬_시스템_MVP_범위와_로딩_방식.md) §2-2·§2-3·§2-5

---

## 1. 구현 내용 명세

1. **`skills_list_tool(category: Optional[str] = None, skills_root: Optional[Path] = None) -> str`** ([`skills_tool.py`](../../../../src/hermes_core/tools/skills_tool.py)): `skills_root`가 `None`이면 호출 시점의 `get_skills_dir()`를 쓰고 `resolve()`한다. 010의 `discover_skills`로 목록을 얻어 JSON 문자열을 돌려준다.
   - 성공: `{"success": true, "skills": [{name, description, category, path}, …], "categories": [정렬된 고유 분류], "count": N}`. `category`를 주면 그 분류와 정확히 일치하는 스킬만(`categories`는 필터 결과 기준). 빈 문자열·공백뿐인 `category`는 필터 없음으로 본다(모델이 빈 값을 보내는 경우가 있고 원본도 같다). JSON은 다른 도구처럼 `ensure_ascii=False`로 직렬화한다.
   - 스킬이 없거나 폴더가 없으면 `success: true`, `skills: []`, `categories: []`, `count: 0`, 안내 `message`. **스킬 폴더를 만들지 않는다.** (기본 경로를 쓸 때 `get_hermes_home()`이 상태 폴더 자체를 만드는 기존 동작은 그대로다.)
2. **`SKILLS_LIST_SCHEMA`** (OpenAI 함수 스키마): 이름 `skills_list`, 설명(이름·설명·분류·path만 반환하며 본문은 `skill_view`로 읽는다는 점 포함. 설명 문자열에 `skill_view`가 들어간다), `parameters.properties`는 `category`(string) 하나, `required` 없음. `skills_root`는 스키마에 노출하지 않는다([`file_tools.py`](../../../../src/hermes_core/tools/file_tools.py)의 `workspace`와 같은 패턴).
3. **디스패처 등록** ([`dispatcher.py`](../../../../src/hermes_core/tools/dispatcher.py)): `create_default_dispatcher`에 `dispatcher.register_tool("skills_list", skills_list_tool, SKILLS_LIST_SCHEMA)` 추가(기존 6종 등록은 그대로).
4. **작동원리 갱신**: 없음.

---

## 2. 변경할 파일 목록

- `src/hermes_core/tools/skills_tool.py` (수정), `src/hermes_core/tools/dispatcher.py` (수정)
- `tests/test_hermes_core/test_skills_list.py` (신규)
- `docs/로드맵/06-스킬-시스템-MVP/README.md`, `docs/인계.md` (갱신)

> **사전 점검 (착수 시)**: ① 이 단계의 모든 시험은 `HERMES_HOME`을 `monkeypatch`로 임시 폴더에 고정하거나 `skills_root`를 직접 넘겨, 실제 `.hermes`를 건드리지 않게 한다. ② 기존 시험이 도구 개수나 정확한 도구 이름 집합을 단언하는지 `grep`으로 다시 확인한다(계획 때는 없음을 확인). 있으면 임의로 고치지 않고 멈춰서 보고한다.

---

## 3. 인수 테스트 시나리오 (구현 전 작성, 임의 변경 금지)

| # | 입력 | 기대 결과 |
|---|---|---|
| 1 | 스킬 3개(`hello`, `devops/deploy`, `devops/backup`) 후 `skills_list_tool(skills_root=…)` | `success true`, `count 3`, `skills` 이름 순서 `hello, backup, deploy`(분류 없음 먼저, 그다음 분류 안에서 이름순), `categories == ["devops"]`. 각 항목에 `name, description, category, path` 모두 있고 `count == len(skills)` |
| 2 | `category="devops"` / `category="없는분류"` / `category=""` | 앞은 `backup, deploy` 2개이고 `categories == ["devops"]`, 가운데는 `skills []`·`categories []`·`count 0`이고 `success true`, 뒤는 필터 없음(시나리오 1과 같은 3개) |
| 3 | 스킬 루트 폴더 없음 | `success true`, `skills []`, `categories []`, `count 0`. 호출 뒤에도 폴더가 **없음** |
| 4 | 스킬 루트는 있으나 비어 있음 | 3과 같은 빈 결과 |
| 5 | 깨진 스킬(UTF-8 아님) 1개 + 정상 2개 | 정상 2개만 반환, `success true` |
| 6 | 호출 전후 스킬 폴더 전체(경로+내용 해시) | 동일 (읽기 전용) |
| 7 | `create_default_dispatcher().get_tools_schema()` | `skills_list` 존재, 설명에 `skill_view`가 들어 있음, `properties`의 키 집합이 `{"category"}`, `skills_root` 없음. 기존 도구 6종 이름도 모두 여전히 존재 |
| 8 | `HERMES_HOME`을 임시 폴더로 바꾸고 `dispatcher.handle_function_call("skills_list", {})` | 그 폴더 `skills/`의 스킬이 반환됨 (호출 시점에 경로를 읽음) |
| 9 | `handle_function_call("skills_list", {"skills_root": "/etc"})` | 도구를 부르지 않고 실패 JSON(미정의 인자). 모델이 스킬 루트를 바꿀 수 없음 |
| 10 | `handle_function_call("skills_list", {"category": 123})` | 실패 JSON (타입 위반) |
| 10b | 설명이 한글인 스킬(`description: 배포 절차`) | 반환 JSON 문자열에 `배포 절차`가 `\u` 이스케이프 없이 그대로 들어 있음 |
| 11 | 기존 코어 테스트(05 마감 시점 전체) | **수정 없이** 통과 |

---

## 4. 검증 기준 (Done 조건)

1. 시나리오 1~10b가 통과한다 (`test_skills_list.py`).
2. 기존 테스트가 모두 통과한다. 테스트 파일 diff에서 **삭제된 줄이 0**이다.
3. `git diff <단계 시작 커밋>..HEAD --stat`의 `src/` 변경이 `skills_tool.py`와 `dispatcher.py`뿐이다.
