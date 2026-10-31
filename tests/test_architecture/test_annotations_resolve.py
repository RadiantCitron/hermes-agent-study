"""모듈별 타입 주석 해석 정합성 시험 (시나리오 11~13).

근거:
- ADR-0018 §2-4 (D-04)
- ROADMAP.md §4 (위험 8)
- 010_아키텍처_안전망_의존방향_래칫과_주석해석_시험.md §1-3
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import types
import typing

import hermes_core

# 해석 실패를 허용하는 예외 목록: {(모듈명, qualname): 사유}
# 010 시점에는 비어 있어야 하며, 추가는 사용자 승인 사항이다.
ALLOWED_UNRESOLVED: dict[tuple[str, str], str] = {}


def unresolved_annotations(module: types.ModuleType) -> list[tuple[str, str]]:
    """모듈에 정의된 모든 함수 및 클래스 메서드의 get_type_hints 해석 실패 항목을 반환한다."""
    failures: list[tuple[str, str]] = []

    for _, obj in inspect.getmembers(module):
        # 최상위 함수
        if inspect.isfunction(obj) and getattr(obj, "__module__", None) == module.__name__:
            try:
                typing.get_type_hints(obj)
            except Exception as e:
                failures.append((obj.__qualname__, str(e)))
        # 클래스 내부 함수/메서드 (비공개 메서드 포함)
        elif inspect.isclass(obj) and getattr(obj, "__module__", None) == module.__name__:
            for attr_name, attr_val in obj.__dict__.items():
                if inspect.isfunction(attr_val):
                    try:
                        typing.get_type_hints(attr_val)
                    except Exception as e:
                        failures.append((attr_val.__qualname__, str(e)))

    return failures


def test_scenario_11_unresolved_detector_self_check():
    """시나리오 11: 주석 해석 탐지기 자체 검증 (합성 모듈)."""
    fake_mod = types.ModuleType("fake_mod")
    code = (
        "from __future__ import annotations\n"
        "def f(x: Undefined) -> None: pass\n"
        "class C:\n"
        "    def m(self) -> int: return 1\n"
    )
    exec(code, fake_mod.__dict__)

    # 모듈 멤버들이 fake_mod에 속하도록 설정
    fake_mod.f.__module__ = "fake_mod"
    fake_mod.C.__module__ = "fake_mod"

    failures = unresolved_annotations(fake_mod)
    assert len(failures) == 1
    qualname, err = failures[0]
    assert qualname == "f"
    assert "Undefined" in err


def test_scenario_12_all_hermes_core_annotations_resolve():
    """시나리오 12: hermes_core 전체 모듈 주석 해석 검증."""
    assert ALLOWED_UNRESOLVED == {}, "ALLOWED_UNRESOLVED는 비어 있어야 함"

    all_failures: list[tuple[str, str, str]] = []  # (module_name, qualname, err)

    for info in pkgutil.walk_packages(hermes_core.__path__, "hermes_core."):
        mod = importlib.import_module(info.name)
        failures = unresolved_annotations(mod)
        for qualname, err in failures:
            key = (mod.__name__, qualname)
            if key not in ALLOWED_UNRESOLVED:
                all_failures.append((mod.__name__, qualname, err))

    assert not all_failures, f"미해결 타입 주석 발견: {all_failures}"
