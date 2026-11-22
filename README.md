# hermes-agent-study

[Hermes Agent](https://github.com/NousResearch/hermes-agent)를 참고해 AI 에이전트의 코어를 맨바닥에서부터 단계별로 만들어 본 학습용 프로젝트입니다.

에이전트를 공부하는 분들을 위한 학습 자료이자, 제가 구조를 어떻게 판단하고 구현했는지 보여 주는 기록입니다. 완성된 코드보다 **만들어 가는 과정**이 중요하다고 생각해 커밋 이력을 그대로 공개합니다. 커밋을 따라가면 기능이 하나씩 쌓이는 모습을 볼 수 있고, 로드맵 문서를 함께 읽으면 왜 그렇게 만들었는지 알 수 있습니다.

개발은 AI 코딩 에이전트와 함께 진행했습니다. 사람이 방향과 구조를 정하고, 구현 지시서를 먼저 쓴 뒤 에이전트가 구현하는 방식입니다.

## 먼저 읽을 로드맵

시간이 많지 않다면 구조를 다룬 아래 세 로드맵의 `WHY.md`부터 보시길 권합니다.

| 로드맵 | 다루는 문제 |
|---|---|
| [15번 코어 확장 파이프라인](docs/로드맵/15-코어-확장-파이프라인-MVP/WHY.md) | 기능을 추가할 때마다 핵심 루프를 고치지 않도록 붙는 자리를 만든다 |
| [18번 코어 커널 경계 정비](docs/로드맵/18-코어-커널-경계-정비-MVP/WHY.md) | 메인 루프를 독립적으로 시험할 수 있게 경계를 세운다 |
| [20번 의존성 역전과 도메인 분리](docs/로드맵/20-의존성-역전-도메인-분리-완결-MVP/WHY.md) | 도메인 규칙과 하부 구현을 분리한다 |

## 이 레포를 보는 법

로드맵 23개가 순서대로 기능을 쌓습니다. 전체 목록은 [로드맵 색인](docs/로드맵/README.md)에 있습니다.

| 보고 싶은 것 | 찾는 곳 |
|---|---|
| 각 로드맵의 설계와 범위 | `docs/로드맵/<번호-이름>/ROADMAP.md` |
| 왜 이 정도까지만 만들었는가 | `docs/로드맵/<번호-이름>/WHY.md` |
| 단계별 구현 지시서 | `docs/로드맵/<번호-이름>/instructions/` |
| 목표와 결과, 아쉬운 점 | `docs/로드맵/<번호-이름>/결과정리및리뷰.md` |
| 어느 로드맵이 끝난 시점의 코드 | 태그 `roadmap-04-closed` ~ `roadmap-23-closed` |

- 커밋 제목의 `R11-040` 같은 식별자는 대체로 로드맵 11번의 지시서 040에 대응합니다.
- 01~03번 로드맵은 태그가 없고, 커밋 제목의 번호와 날짜로 구분합니다.
- 초기 로드맵은 코드가 작아서 해당 시점의 코드를 통째로 읽어도 부담이 없습니다.

```bash
git tag                          # 로드맵 마감 시점 목록
git checkout roadmap-10-closed   # 10번 로드맵이 끝난 시점의 코드로 이동
git checkout main                # 최신으로 돌아오기
```

## 무엇을 만들었나

| 영역 | 내용 |
|---|---|
| 코어 | 프롬프트 조립, 도구 실행, 메모리와 세션 저장, 턴 루프 |
| 도구 | 터미널(가드·사람 승인), 파일 읽기·쓰기·수정, 코드 검색 |
| 모델 연결 | OpenRouter·Gemini·Anthropic 다중 프로바이더와 키 없이 쓰는 Mock |
| 대화 관리 | 컨텍스트 압축, 세션 재개, 중단·종료 정합 |
| 확장 기능 | 스킬, 서브에이전트, 체크포인트와 롤백, 루프 정체 감지 |
| 구조 | 확장 파이프라인, 커널 경계, 단일 제어판, 의존성 역전 |

## 구조

```text
src/hermes_core/   에이전트 본체 (agent, kernel, control, extensions, tools)
tests/             테스트
docs/로드맵/        로드맵별 설계·지시서·결과 문서
```

## 설치와 테스트

Python 3.10 이상이 필요합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

pytest                 # 전체 테스트
pytest -m "not pty"    # 느린 터미널 시험 제외
pytest -n 4            # 병렬 실행
```

과거 태그에는 이 README가 없으므로 `pip install -e .` 대신 아래처럼 테스트합니다.

```bash
PYTHONPATH=src pytest
```

## 실행

```bash
# API 키 없이 Mock 모델로 실행
python -m hermes_core.cli --mock

# OpenRouter 실제 모델로 실행
export OPENROUTER_API_KEY=<your-key>
python -m hermes_core.cli
python -m hermes_core.cli --model <model-id>
```

전체 옵션은 `python -m hermes_core.cli --help`로 볼 수 있습니다.

## 알아둘 점

- 학습용 구현이며 운영 환경을 전제로 하지 않습니다.
- 문서의 일부 링크는 공개하지 않은 자료(내부 계획, 대화 기록, 운영 스크립트, 작동원리 문서 등)를 가리켜 열리지 않습니다.
- 문서에 적힌 커밋 해시는 공개용으로 이력을 정리하기 전의 값이라 이 레포의 해시와 다릅니다. 커밋 제목의 식별자로 찾아 주세요.

## 출처와 라이선스

[Nous Research](https://nousresearch.com)의 Hermes Agent를 참고한 학습용 구현입니다. [MIT License](LICENSE)를 따릅니다.
