"""호환 층 검증 시험 (Compat Layers Tests).

ADR-0018 D-05 및 구현지시서 020 명세에 따른 호환 층 재수출 검증.
"""

import ast
import glob
import importlib
from pathlib import Path
import pkgutil
import sys

# 호환 층 재수출 목록. 줄어들기만 한다. 새 항목 추가는 사용자 승인 사항이다.
COMPAT_REEXPORTS: dict[tuple[str, str], str] = {
    # (구 모듈, 이름): 새 모듈
    ("hermes_core.extensions.contract", "Extension"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "ExtensionHost"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "ExtensionRecord"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "FailurePolicy"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "MiddlewarePhase"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "PHASE_FAILURE_POLICY"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "RESERVED_RESULT_KEYS"): "hermes_core.kernel.contract",
    ("hermes_core.extensions.contract", "TurnScope"): "hermes_core.kernel.contract",
    ("hermes_core.agent.turn_hooks", "AFTER_TOOL_CALLS"): "hermes_core.kernel.turn_hooks",
    ("hermes_core.agent.turn_hooks", "BEFORE_LLM"): "hermes_core.kernel.turn_hooks",
    ("hermes_core.agent.turn_hooks", "HOOK_POINTS"): "hermes_core.kernel.turn_hooks",
    ("hermes_core.agent.turn_hooks", "HookRegistry"): "hermes_core.kernel.turn_hooks",
    ("hermes_core.agent.turn_hooks", "TurnContext"): "hermes_core.kernel.turn_hooks",
    ("hermes_core.agent.turn_hooks", "TurnHook"): "hermes_core.kernel.turn_hooks",
    ("hermes_core.agent.events", "AgentEvent"): "hermes_core.kernel.events",
    ("hermes_core.agent.events", "EVENT_EXTENSION_ERROR"): "hermes_core.kernel.events",
    ("hermes_core.agent.events", "EVENT_HOOK_ERROR"): "hermes_core.kernel.events",
    ("hermes_core.agent.events", "EVENT_TOOL_CALL"): "hermes_core.kernel.events",
    ("hermes_core.agent.events", "EventSink"): "hermes_core.kernel.events",
    ("hermes_core.tools.dispatcher", "ToolCall"): "hermes_core.kernel.tool_call",
    ("hermes_core.llm_client", "LLMError"): "hermes_core.kernel.ports",
}

COMPAT_REASON = "기존 시험이 구 경로를 import한다(검증된 시험 무수정, ADR-0018 D-05)"
COMPAT_REMOVAL = "구 경로를 import하는 시험·코드가 0이 될 때(ADR-0018 D-10)"

# 재수출만 하는 모듈(정의 금지). agent/events.py와 tools/dispatcher.py는 자체 정의가 있으므로 넣지 않는다.
PURE_REEXPORT_MODULES = (
    "hermes_core.extensions.contract",
    "hermes_core.agent.turn_hooks",
)


def test_scenario_1_object_identity() -> None:
    """시나리오 1: 객체 동일성 (위험 4)."""
    assert len(COMPAT_REEXPORTS) == 21
    for (old_mod_name, attr_name), new_mod_name in COMPAT_REEXPORTS.items():
        old_mod = importlib.import_module(old_mod_name)
        new_mod = importlib.import_module(new_mod_name)
        old_obj = getattr(old_mod, attr_name)
        new_obj = getattr(new_mod, attr_name)
        assert old_obj is new_obj, f"Object mismatch for {old_mod_name}.{attr_name} vs {new_mod_name}.{attr_name}"


def test_scenario_2_pure_reexport_modules() -> None:
    """시나리오 2: 순수 재수출 모듈 AST 및 __all__ 검증."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    src_dir = repo_root / "src"

    for mod_name in PURE_REEXPORT_MODULES:
        rel_path = mod_name.replace(".", "/") + ".py"
        file_path = src_dir / rel_path
        assert file_path.is_file(), f"File not found: {file_path}"

        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(file_path))

        # 최상위 문장 검사: docstring(Expr(Constant(str))), ImportFrom(hermes_core.kernel...), Assign(__all__)
        all_names: list[str] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            elif isinstance(stmt, ast.ImportFrom):
                assert stmt.module and (
                    stmt.module == "hermes_core.kernel" or stmt.module.startswith("hermes_core.kernel.")
                ), f"Unexpected ImportFrom module {stmt.module} in {mod_name}"
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        assert isinstance(stmt.value, (ast.List, ast.Tuple))
                        for elt in stmt.value.elts:
                            assert isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            all_names.append(elt.value)
                        break
                else:
                    assert False, f"Unexpected assign statement in {mod_name}"
            else:
                assert False, f"Unexpected statement {type(stmt).__name__} in {mod_name}"

        expected_names = {name for (m, name) in COMPAT_REEXPORTS if m == mod_name}
        assert set(all_names) == expected_names, f"__all__ mismatch in {mod_name}"


def test_scenario_3_feature_and_ui_outside_kernel() -> None:
    """시나리오 3: 기능·화면은 커널 밖."""
    kernel_events = importlib.import_module("hermes_core.kernel.events")
    agent_events = importlib.import_module("hermes_core.agent.events")

    assert not hasattr(kernel_events, "EVENT_LOOP_GUARD")
    assert not hasattr(kernel_events, "ConsoleEventSink")

    assert agent_events.EVENT_LOOP_GUARD == "loop_guard"
    assert agent_events.ConsoleEventSink.__module__ == "hermes_core.agent.events"

    assert kernel_events.EVENT_TOOL_CALL == "tool_call"
    assert kernel_events.EVENT_HOOK_ERROR == "hook_error"
    assert kernel_events.EVENT_EXTENSION_ERROR == "extension_error"


def test_scenario_4_src_does_not_rely_on_compat_layers() -> None:
    """시나리오 4: src는 호환 층에 기대지 않는다."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    src_dir = repo_root / "src" / "hermes_core"
    py_files = list(src_dir.rglob("*.py"))

    # 검사 제외: 호환 층 자체인 extensions/contract.py, agent/turn_hooks.py
    exempt_files = {
        (src_dir / "extensions" / "contract.py").resolve(),
        (src_dir / "agent" / "turn_hooks.py").resolve(),
    }

    violations: list[str] = []

    for file_path in py_files:
        if file_path.resolve() in exempt_files:
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(file_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # (a) hermes_core.extensions.contract, hermes_core.agent.turn_hooks 금지
                if node.module in ("hermes_core.extensions.contract", "hermes_core.agent.turn_hooks"):
                    violations.append(f"{file_path}: imported {node.module}")
                # (b) hermes_core.tools.dispatcher에서 ToolCall, ToolMiddleware 금지
                if node.module == "hermes_core.tools.dispatcher":
                    for alias in node.names:
                        if alias.name in ("ToolCall", "ToolMiddleware"):
                            violations.append(f"{file_path}: imported {alias.name} from dispatcher")
                # (c) hermes_core.agent.events에서 EVENT_LOOP_GUARD, ConsoleEventSink 외 금지
                if node.module == "hermes_core.agent.events":
                    allowed_event_names = {"EVENT_LOOP_GUARD", "ConsoleEventSink"}
                    for alias in node.names:
                        if alias.name not in allowed_event_names:
                            violations.append(f"{file_path}: imported {alias.name} from agent.events")
                # (d) hermes_core.llm_client에서 LLMError 금지
                if node.module == "hermes_core.llm_client":
                    for alias in node.names:
                        if alias.name == "LLMError":
                            violations.append(f"{file_path}: imported LLMError from llm_client")

    assert not violations, f"Src files rely on compat layers:\n" + "\n".join(violations)


def test_scenario_5_compat_list_integrity() -> None:
    """시나리오 5: 목록 정합 (가정 G)."""
    assert bool(COMPAT_REASON.strip())
    assert bool(COMPAT_REMOVAL.strip())

    for (old_mod_name, attr_name), _ in COMPAT_REEXPORTS.items():
        old_mod = importlib.import_module(old_mod_name)
        assert hasattr(old_mod, attr_name), f"Attribute {attr_name} not found in {old_mod_name}"


def test_scenario_6_kernel_package_does_not_collect_god_names() -> None:
    """시나리오 6: 커널 패키지는 이름을 모으지 않는다."""
    import hermes_core.kernel

    public_names = [n for n in vars(hermes_core.kernel) if not n.startswith("_")]
    allowed_submodules = {name for _, name, _ in pkgutil.iter_modules(hermes_core.kernel.__path__)}
    assert set(public_names).issubset(allowed_submodules), f"Unexpected public names in kernel package: {public_names}"
