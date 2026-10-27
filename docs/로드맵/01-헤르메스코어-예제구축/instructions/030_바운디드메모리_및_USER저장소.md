# [구현지시서 030] 바운디드 메모리 및 USER 저장소

> **상위 대시보드**: [01-README.md](../README.md)  
> **선행 조건**: 020 단계 완료 (`Done`)  
> **기준 레퍼런스**: `hermes-agent` commit `6134939882`의 [`tools/memory_tool.py`](../../../../hermes-agent/tools/memory_tool.py)

---

## 1. 구현 내용 명세

1. **Curated Memory 저장소 클래스 (`MemoryStore` in `src/hermes_core/tools/memory_tool.py`)**:
   - 원본 `tools/memory_tool.py`의 `MemoryStore`에 대응 (독스트링에 원본 매핑 명시)
   - `MEMORY.md` (사실/환경 메모리, 기본 2,200자 제한)
   - `USER.md` (사용자 성향/선호 메모리, 기본 1,375자 제한)
   - 엔트리 구분자: `§` (섹션 기호) 단위로 사실 항목 분할
   - `format_for_system_prompt(target="memory")`: 시스템 프롬프트에 주입할 마크다운 스냅샷 텍스트 생성
2. **메모리 편집 도구 함수 (`def memory_tool` in `src/hermes_core/tools/memory_tool.py`)**:
   - 도구 실행 레일에서 호출 가능한 단일 함수 인터페이스:
     - `memory_tool(action, target="memory", content=None, old_text=None, store=None)`
   - `add`: 새로운 기억 항목을 `§`로 구분하여 끝에 추가 (용량 초과 시 거부 및 압축 가이드 반환)
   - `replace`: 기존 기억(`old_text`)을 찾아 새로운 내용(`content`)으로 부분 교체
   - `remove`: 불필요하거나 폐기된 기억 항목 삭제
   - 파일 쓰기 시 원자적(Atomic) 쓰기 처리 (`os.replace`)를 통해 예기치 않은 프로세스 종료 시 파일 손상 방지
3. **단위 테스트 작성**:
   - `tests/test_hermes_core/test_memory_tool.py`:
     - 항목 추가, 교체, 삭제 동작 검증
     - 용량 제한 초과 시 안전 거부 및 에러 메시지 반환 검증
     - 파일 손상 방지 및 엔트리 파싱 무결성 검증

---

## 2. 변경할 파일 목록

- `src/hermes_core/tools/__init__.py` (신규)
- `src/hermes_core/tools/memory_tool.py` (신규 - MemoryStore 및 memory_tool 포함)
- `tests/test_hermes_core/test_memory_tool.py` (신규)

---

## 3. 검증 기준 (Done 조건)

1. `pytest tests/test_hermes_core/test_memory_tool.py` 100% 통과.
2. 2,200자 제한의 `MEMORY.md`에 2,300자 항목을 `add` 시도시 예외 없이 실패 사유와 함께 용량 초과 에러 메시지가 정상 반환됨을 단언.
3. `replace` 시 `old_text`와 일치하는 부분만 정확히 치환되고 나머지 항목의 순서와 내용이 보존됨을 단언.
