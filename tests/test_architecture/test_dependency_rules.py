"""의존 방향 규칙 및 래칫(Ratchet) 검증 시험 (시나리오 1~10).

근거:
- ADR-0018 §2-3 (D-03)
- ROADMAP.md §4 (위험 7·8), §6-1
- 010_아키텍처_안전망_의존방향_래칫과_주석해석_시험.md §1-2
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import Any

# 알려진 의존 방향 위반 (래칫). 줄어들기만 한다.
# 항목을 갚으면 시험이 「목록에서 지워라」로 실패한다. 새 항목 추가는 사용자 승인 사항이다.
KNOWN_DEBT: dict[tuple[str, str, str], tuple[str, str]] = {
    # (importer, 대상, 규칙): (사유, 해소 예정)
    (
        "hermes_core.tools.delegate_tool",
        "hermes_core.agent.agent",
        "R3",
    ): ("위임 도구가 자식 에이전트를 직접 생성", "19번 이후: 자식 생성 팩토리 주입(16 대시보드 §6-2)"),
    (
        "hermes_core.tools.delegate_tool",
        "hermes_core.agent.agent.HermesAgent",
        "R3",
    ): ("위임 도구가 자식 에이전트를 직접 생성", "19번 이후: 자식 생성 팩토리 주입(16 대시보드 §6-2)"),
    (
        "hermes_core.tools.delegate_tool",
        "hermes_core.agent.prompt_builder",
        "R3",
    ): ("위임 도구가 자식 시스템 프롬프트 조립 함수 참조", "19번 이후: 자식 생성 팩토리 주입(16 대시보드 §6-2)"),
    (
        "hermes_core.tools.delegate_tool",
        "hermes_core.agent.prompt_builder.build_child_system_message",
        "R3",
    ): ("위임 도구가 자식 시스템 프롬프트 조립 함수 참조", "19번 이후: 자식 생성 팩토리 주입(16 대시보드 §6-2)"),
}

# 기본 기능 확장 모듈. 이 모듈을 import하는 src 모듈은 조립 지점 hermes_core.assembly뿐이다.
# 새 기능 확장을 만들면 여기에 더한다 — 시나리오 11이 빠뜨림을 잡는다.
FEATURE_EXTENSION_MODULES: tuple[str, ...] = (
    "hermes_core.agent.compression_hook",
    "hermes_core.agent.loop_guard",
)


def _resolve_relative_import(importer_mod: str, rel_module: str | None, level: int, is_package: bool) -> str:
    parts = importer_mod.split(".")
    if not is_package:
        parts = parts[:-1]
    cutoff = level - 1
    if cutoff > 0:
        base_parts = parts[:-cutoff] if cutoff <= len(parts) else []
    else:
        base_parts = parts

    base = ".".join(base_parts)
    if rel_module:
        return f"{base}.{rel_module}" if base else rel_module
    return base


def collect_imports(module_name: str, source: str, is_package: bool = False) -> set[str]:
    """AST를 순회하여 파일 전체의 Import / ImportFrom 대상을 수집한다."""
    targets: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level
            if level > 0:
                abs_mod = _resolve_relative_import(module_name, node.module, level, is_package)
            else:
                abs_mod = node.module or ""

            if abs_mod:
                targets.add(abs_mod)
                for alias in node.names:
                    targets.add(f"{abs_mod}.{alias.name}")
            else:
                for alias in node.names:
                    targets.add(alias.name)
    return targets


def _is_stdlib(module_root: str) -> bool:
    return module_root in sys.stdlib_module_names


def find_violations(imports: dict[str, set[str]]) -> set[tuple[str, str, str]]:
    """의존 방향 규칙(R1~R6)을 대조하여 위반 목록을 반환한다."""
    violations: set[tuple[str, str, str]] = set()

    for importer, targets in imports.items():
        is_importer_kernel = importer == "hermes_core.kernel" or importer.startswith("hermes_core.kernel.")
        is_importer_tools = importer == "hermes_core.tools" or importer.startswith("hermes_core.tools.")
        is_importer_contract = importer == "hermes_core.extensions.contract"
        is_importer_control = importer == "hermes_core.control" or importer.startswith("hermes_core.control.")

        for target in targets:
            root_pkg = target.split(".")[0]

            # R1: importer가 hermes_core.kernel 또는 하위이면 대상은 hermes_core.kernel(하위 포함)이거나 표준 라이브러리여야 함
            if is_importer_kernel:
                target_is_kernel = target == "hermes_core.kernel" or target.startswith("hermes_core.kernel.")
                target_is_stdlib = _is_stdlib(root_pkg)
                if not (target_is_kernel or target_is_stdlib):
                    violations.add((importer, target, "R1"))

            # R2: 대상이 hermes_core.cli 또는 hermes_core.cli_input이면 importer는 hermes_core.cli여야 함
            target_is_cli = (
                target == "hermes_core.cli"
                or target.startswith("hermes_core.cli.")
                or target == "hermes_core.cli_input"
                or target.startswith("hermes_core.cli_input.")
            )
            if target_is_cli:
                importer_is_cli = importer == "hermes_core.cli" or importer.startswith("hermes_core.cli.")
                if not importer_is_cli:
                    violations.add((importer, target, "R2"))

            # R3: importer가 hermes_core.tools 하위이면 대상이 hermes_core.agent(하위 포함)이면 위반
            if is_importer_tools:
                target_is_agent = target == "hermes_core.agent" or target.startswith("hermes_core.agent.")
                if target_is_agent:
                    violations.add((importer, target, "R3"))

            # R4: importer가 hermes_core.extensions.contract이면 대상은 표준 라이브러리이거나 hermes_core.kernel(하위 포함)
            if is_importer_contract:
                target_is_kernel = target == "hermes_core.kernel" or target.startswith("hermes_core.kernel.")
                target_is_stdlib = _is_stdlib(root_pkg)
                if not (target_is_stdlib or target_is_kernel):
                    violations.add((importer, target, "R4"))

            # R5: 대상이 FEATURE_EXTENSION_MODULES의 한 모듈과 같거나 모듈 + "."으로 시작하고 importer가 hermes_core.assembly가 아니면 위반
            for feat_mod in FEATURE_EXTENSION_MODULES:
                if target == feat_mod or target.startswith(f"{feat_mod}."):
                    if importer != "hermes_core.assembly":
                        violations.add((importer, target, "R5"))
                    break

            # R6 유입(inbound): target이 hermes_core.control 또는 하위일 때
            # importer는 assembly, cli, control(하위 포함)만 허용
            target_is_control = target == "hermes_core.control" or target.startswith("hermes_core.control.")
            if target_is_control:
                importer_allowed = (
                    importer == "hermes_core.assembly"
                    or importer == "hermes_core.cli"
                    or importer == "hermes_core.control"
                    or importer.startswith("hermes_core.control.")
                )
                if not importer_allowed:
                    violations.add((importer, target, "R6"))

            # R6 유출(outbound): importer가 hermes_core.control 또는 하위일 때
            # target은 표준 라이브러리, constants(하위 포함), control(하위 포함)만 허용
            if is_importer_control:
                target_allowed = (
                    _is_stdlib(root_pkg)
                    or target == "hermes_core.constants"
                    or target.startswith("hermes_core.constants.")
                    or target == "hermes_core.control"
                    or target.startswith("hermes_core.control.")
                )
                if not target_allowed:
                    violations.add((importer, target, "R6"))

    return violations


def compare_with_debt(
    actual: set[tuple[str, str, str]],
    debt: set[tuple[str, str, str]],
) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """(새 위반 actual - debt, 갚았는데 목록에 남은 부채 debt - actual)을 반환한다."""
    new_violations = actual - debt
    stale_debt = debt - actual
    return new_violations, stale_debt


def scan_source_tree(root_path: Path) -> dict[str, set[str]]:
    """src/hermes_core 경로의 모든 .py 파일을 읽어 {모듈명: import 집합}을 반환한다."""
    imports_by_module: dict[str, set[str]] = {}
    for py_file in root_path.rglob("*.py"):
        rel = py_file.relative_to(root_path)
        parts = list(rel.with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        mod_name = ".".join(["hermes_core"] + parts) if parts else "hermes_core"
        source = py_file.read_text(encoding="utf-8")
        imports_by_module[mod_name] = collect_imports(mod_name, source, is_package=is_pkg)
    return imports_by_module


# --- 단위 시험 시나리오 1~10 ---


def test_scenario_1_r1_internal_violation():
    """시나리오 1: R1 내부 의존 위반 검출."""
    violations = find_violations({"hermes_core.kernel.loop": {"hermes_core.agent.agent"}})
    assert violations == {("hermes_core.kernel.loop", "hermes_core.agent.agent", "R1")}


def test_scenario_2_r1_allowed_dependencies():
    """시나리오 2: R1 허용 (표준 라이브러리 및 커널 내부)."""
    violations = find_violations(
        {"hermes_core.kernel.loop": {"json", "collections.abc", "hermes_core.kernel.contract"}}
    )
    assert violations == set()


def test_scenario_3_r1_third_party_violation():
    """시나리오 3: R1 제3자 의존 위반 검출."""
    violations = find_violations({"hermes_core.kernel.ports": {"httpx"}})
    assert violations == {("hermes_core.kernel.ports", "httpx", "R1")}


def test_scenario_4_r2_cli_importer_rule():
    """시나리오 4: R2 위반 및 허용."""
    importer = "hermes_core.tools.x"
    targets = collect_imports(importer, "from hermes_core import cli")
    v1 = find_violations({importer: targets})
    assert ("hermes_core.tools.x", "hermes_core.cli", "R2") in v1

    v2 = find_violations({"hermes_core.cli": {"hermes_core.cli_input"}})
    assert v2 == set()


def test_scenario_5_delayed_import_captured():
    """시나리오 5: 함수 내부 지연 import 포착."""
    source = "def f():\n    from hermes_core.agent.agent import HermesAgent\n"
    collected = collect_imports("hermes_core.tools.x", source)
    assert "hermes_core.agent.agent" in collected
    v = find_violations({"hermes_core.tools.x": collected})
    assert ("hermes_core.tools.x", "hermes_core.agent.agent", "R3") in v


def test_scenario_6_relative_import_resolution():
    """시나리오 6: 상대 import 해석."""
    t1 = collect_imports("hermes_core.tools.x", "from ..agent import agent", is_package=False)
    assert "hermes_core.agent" in t1
    assert "hermes_core.agent.agent" in t1

    t2 = collect_imports("hermes_core.tools", "from . import dispatcher", is_package=True)
    assert "hermes_core.tools.dispatcher" in t2


def test_scenario_7_r4_extensions_contract():
    """시나리오 7: R4 규칙 (표준 라이브러리 및 커널 허용, constants 등 불허)."""
    v1 = find_violations({"hermes_core.extensions.contract": {"hermes_core.constants"}})
    assert v1 == {("hermes_core.extensions.contract", "hermes_core.constants", "R4")}

    v2 = find_violations({"hermes_core.extensions.contract": {"hermes_core.kernel.contract", "enum"}})
    assert v2 == set()


def test_scenario_8_ratchet_debt_comparison():
    """시나리오 8: 래칫 비교 함수 동작."""
    a = ("m", "t", "R3")
    b = ("n", "u", "R1")
    assert compare_with_debt({a, b}, {a}) == ({b}, set())
    assert compare_with_debt({a}, {a, b}) == (set(), {b})
    assert compare_with_debt({a}, {a}) == (set(), set())


def test_scenario_9_r5_feature_extension_modules():
    """시나리오 9: R5 규칙 판정 단위 검증."""
    # (a) agent.agent가 loop_guard를 직접 import -> 위반
    v_a = find_violations({"hermes_core.agent.agent": {"hermes_core.agent.loop_guard"}})
    assert v_a == {("hermes_core.agent.agent", "hermes_core.agent.loop_guard", "R5")}

    # (b) assembly가 compression_hook import -> 허용 (위반 없음)
    v_b = find_violations({
        "hermes_core.assembly": {
            "hermes_core.agent.compression_hook",
            "hermes_core.agent.compression_hook.CompressionExtension",
        }
    })
    assert v_b == set()

    # (c) cli가 loop_guard import -> 위반
    v_c = find_violations({"hermes_core.cli": {"hermes_core.agent.loop_guard.LoopGuardExtension"}})
    assert v_c == {("hermes_core.cli", "hermes_core.agent.loop_guard.LoopGuardExtension", "R5")}

    # (d) 접두사만 유사한 다른 모듈 -> 해당 없음
    v_d = find_violations({"hermes_core.x": {"hermes_core.agent.loop_guard_extra"}})
    assert v_d == set()


def test_scenario_9_actual_tree_scanner_smoke():
    """시나리오 9(기존 번호 보존): 실제 소스 트리 스캐너 동작 확인."""
    root = Path(__file__).resolve().parents[2] / "src" / "hermes_core"
    all_imports = scan_source_tree(root)

    # hermes_core.cli가 hermes_core.agent를 참조하는지 확인
    cli_targets = all_imports.get("hermes_core.cli", set())
    assert any(t == "hermes_core.agent" or t.startswith("hermes_core.agent.") for t in cli_targets)

    # hermes_core.tools.delegate_tool이 hermes_core.session_db를 참조하는지 확인
    del_targets = all_imports.get("hermes_core.tools.delegate_tool", set())
    assert any(t == "hermes_core.session_db" or t.startswith("hermes_core.session_db.") for t in del_targets)


def test_scenario_10_actual_tree_ratchet_enforcement():
    """시나리오 10: 실제 소스 트리 의존 방향 래칫 검증 (새 위반 없음, 갚은 부채 청산 확인)."""
    # KNOWN_DEBT 항목 값의 비어있지 않음 검증
    for key, (reason, planned) in KNOWN_DEBT.items():
        assert reason.strip(), f"KNOWN_DEBT {key} reason is empty"
        assert planned.strip(), f"KNOWN_DEBT {key} planned resolution is empty"

    root = Path(__file__).resolve().parents[2] / "src" / "hermes_core"
    all_imports = scan_source_tree(root)
    actual_violations = find_violations(all_imports)

    debt_set = set(KNOWN_DEBT.keys())
    new_violations, stale_debt = compare_with_debt(actual_violations, debt_set)

    error_messages = []
    if new_violations:
        error_messages.append(f"새로운 의존 방향 위반 발견 (R1~R6): {sorted(new_violations)}")
    if stale_debt:
        error_messages.append(f"갚은 부채를 KNOWN_DEBT에서 지워라: {sorted(stale_debt)}")

    assert not error_messages, " / ".join(error_messages)

    # 040 추가 확인: hermes_core.agent.agent의 대상에 기능 확장 모듈이 없고 hermes_core.assembly가 있다
    agent_targets = all_imports.get("hermes_core.agent.agent", set())
    assert not any(
        t == "hermes_core.agent.compression_hook"
        or t.startswith("hermes_core.agent.compression_hook.")
        or t == "hermes_core.agent.loop_guard"
        or t.startswith("hermes_core.agent.loop_guard.")
        for t in agent_targets
    ), f"agent.py still imports feature extensions: {agent_targets}"
    assert any(
        t == "hermes_core.assembly" or t.startswith("hermes_core.assembly.")
        for t in agent_targets
    ), f"agent.py missing hermes_core.assembly: {agent_targets}"

    # 010 추가 확인: all_imports에 hermes_core.control.settings가 존재하고,
    # 이를 import하는 모듈은 hermes_core.control, hermes_core.assembly, hermes_core.cli뿐임
    assert "hermes_core.control.settings" in all_imports
    importers_of_control = {
        m
        for m, targets in all_imports.items()
        if any(t == "hermes_core.control" or t.startswith("hermes_core.control.") for t in targets)
    }
    allowed_importers = {
        "hermes_core.control",
        "hermes_core.control.settings",
        "hermes_core.control.snapshot",
        "hermes_core.control.commands",
        "hermes_core.control.panel",
        "hermes_core.assembly",
        "hermes_core.cli",
    }
    assert importers_of_control.issubset(allowed_importers), (
        f"Unexpected importers of control: {importers_of_control - allowed_importers}"
    )


def test_scenario_11_feature_extension_modules_completeness():
    """시나리오 11: FEATURE_EXTENSION_MODULES 빠뜨림 방지 검증."""
    root = Path(__file__).resolve().parents[2] / "src" / "hermes_core"
    discovered_modules: set[str] = set()

    for py_file in root.rglob("*.py"):
        rel = py_file.relative_to(root)
        parts = list(rel.with_suffix("").parts)
        # kernel/ 내부는 제외
        if parts and parts[0] == "kernel":
            continue

        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        mod_name = ".".join(["hermes_core"] + parts) if parts else "hermes_core"

        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "install":
                        discovered_modules.add(mod_name)
                        break

    expected_modules = set(FEATURE_EXTENSION_MODULES)
    assert discovered_modules == expected_modules, (
        f"FEATURE_EXTENSION_MODULES mismatch. Discovered - Expected: {discovered_modules - expected_modules}, "
        f"Expected - Discovered: {expected_modules - discovered_modules}"
    )


def test_scenario_12_r6_control_boundary():
    """시나리오 12: R6 단위 검증 (유입·유출 양방향)."""
    # (a) 유입 위반: agent -> control.settings
    assert find_violations({"hermes_core.agent.agent": {"hermes_core.control.settings"}}) == {
        ("hermes_core.agent.agent", "hermes_core.control.settings", "R6")
    }

    # (b) 유입 위반: dispatcher -> control
    assert find_violations({"hermes_core.tools.dispatcher": {"hermes_core.control"}}) == {
        ("hermes_core.tools.dispatcher", "hermes_core.control", "R6")
    }

    # (c) 유입 위반: session_db -> control.settings
    assert find_violations({"hermes_core.session_db": {"hermes_core.control.settings"}}) == {
        ("hermes_core.session_db", "hermes_core.control.settings", "R6")
    }

    # (d) 유입 허용: assembly, cli, control -> control.settings
    assert find_violations({
        "hermes_core.assembly": {"hermes_core.control.settings"},
        "hermes_core.cli": {"hermes_core.control.settings"},
        "hermes_core.control": {"hermes_core.control.settings"},
    }) == set()

    # (e) 유출 위반: control.settings -> agent.agent
    assert find_violations({"hermes_core.control.settings": {"hermes_core.agent.agent"}}) == {
        ("hermes_core.control.settings", "hermes_core.agent.agent", "R6")
    }

    # (f) 유출 위반: control.settings -> assembly
    assert find_violations({"hermes_core.control.settings": {"hermes_core.assembly"}}) == {
        ("hermes_core.control.settings", "hermes_core.assembly", "R6")
    }

    # (g) 유출 허용: control.settings -> tomllib, constants, control
    assert find_violations({
        "hermes_core.control.settings": {
            "tomllib",
            "hermes_core.constants",
            "hermes_core.control",
        }
    }) == set()

