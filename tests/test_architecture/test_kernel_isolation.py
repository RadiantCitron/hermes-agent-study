"""커널 격리 및 루트 지연 import 검증 (시나리오 24~27, 32)."""

import ast
import os
from pathlib import Path
import subprocess
import sys

from hermes_core.agent.agent import HermesAgent


def test_scenario_24_kernel_test_folder_isolation() -> None:
    """시나리오 24: tests/test_kernel/**/*.py의 import 격리 검사."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    test_kernel_dir = repo_root / "tests" / "test_kernel"
    py_files = list(test_kernel_dir.rglob("*.py"))
    assert len(py_files) >= 1, "test_kernel files must not be empty"

    stdlib = set(sys.stdlib_module_names)
    violations: list[str] = []

    for file_path in py_files:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(file_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_name = alias.name.split(".")[0]
                    if top_name not in stdlib and top_name != "pytest" and top_name != "hermes_core":
                        violations.append(f"{file_path.name}: import {alias.name}")
                    if top_name == "hermes_core":
                        if not (alias.name == "hermes_core.kernel" or alias.name.startswith("hermes_core.kernel.")):
                            violations.append(f"{file_path.name}: non-kernel import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_name = node.module.split(".")[0]
                    if top_name not in stdlib and top_name != "pytest" and top_name != "hermes_core":
                        violations.append(f"{file_path.name}: from {node.module} import ...")
                    if top_name == "hermes_core":
                        if not (node.module == "hermes_core.kernel" or node.module.startswith("hermes_core.kernel.")):
                            violations.append(f"{file_path.name}: from {node.module} (non-kernel) import ...")

    assert not violations, f"Kernel tests import non-kernel modules:\n" + "\n".join(violations)


def test_scenario_25_import_kernel_only_subprocess() -> None:
    """시나리오 25: 새 프로세스에서 import hermes_core.kernel.loop 후 sys.modules 검사."""
    code = """
import sys
import hermes_core.kernel.loop

mods = list(sys.modules.keys())
hermes_mods = [m for m in mods if m == "hermes_core" or m.startswith("hermes_core.")]
non_kernel_hermes = [m for m in hermes_mods if not (m == "hermes_core" or m == "hermes_core.kernel" or m.startswith("hermes_core.kernel."))]

assert not non_kernel_hermes, f"Non-kernel hermes modules loaded: {non_kernel_hermes}"
assert "sqlite3" not in sys.modules, "sqlite3 should not be loaded"
assert "httpx" not in sys.modules, "httpx should not be loaded"
print("OK")
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"Subprocess failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    assert "OK" in res.stdout


def test_scenario_26_features_broken_kernel_tests_pass() -> None:
    """시나리오 26: 기능 및 3rd-party/sqlite가 깨져도 커널 단위 시험은 성공."""
    code = """
import sys
import pytest

class BrokenMetaPathFinder:
    def find_spec(self, fullname, path, target=None):
        if fullname in ("httpx", "sqlite3", "_sqlite3") or fullname.startswith(("httpx.", "sqlite3.", "_sqlite3.")):
            raise ImportError(f"Simulated broken external dependency: {fullname}")
        if fullname.startswith("hermes_core.") and not (fullname == "hermes_core.kernel" or fullname.startswith("hermes_core.kernel.")):
            raise ImportError(f"Simulated broken hermes feature: {fullname}")
        return None

sys.meta_path.insert(0, BrokenMetaPathFinder())

exit_code = pytest.main(["tests/test_kernel", "-q", "-p", "no:cacheprovider"])
sys.exit(exit_code)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"Subprocess pytest failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    assert "passed" in res.stdout
    assert "failed" not in res.stdout
    assert "error" not in res.stdout.lower() or "no errors" in res.stdout.lower()


def test_scenario_27_root_package_compat() -> None:
    """시나리오 27: 루트 패키지 지연 import 호환."""
    import hermes_core
    from hermes_core import (
        AIAgent,
        HermesAgent,
        MemoryStore,
        SessionDB,
        ToolDispatcher,
        __version__,
    )
    import hermes_core.agent.agent as ag_mod
    import hermes_core.session_db as sdb_mod
    import hermes_core.tools.dispatcher as disp_mod
    import hermes_core.tools.memory_tool as mem_mod

    assert HermesAgent is ag_mod.HermesAgent
    assert AIAgent is ag_mod.AIAgent
    assert SessionDB is sdb_mod.SessionDB
    assert MemoryStore is mem_mod.MemoryStore
    assert ToolDispatcher is disp_mod.ToolDispatcher
    assert __version__ == "0.1.0"
    assert hermes_core.__all__ == [
        "HermesAgent",
        "AIAgent",
        "SessionDB",
        "MemoryStore",
        "ToolDispatcher",
        "__version__",
    ]

    import pytest
    with pytest.raises(AttributeError):
        _ = getattr(hermes_core, "NoSuchName")


def test_scenario_32_loop_in_one_place_ast() -> None:
    """시나리오 32: 루프는 kernel/loop.py 한 곳에만 존재한다."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    agent_path = repo_root / "src" / "hermes_core" / "agent" / "agent.py"

    with open(agent_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(agent_path))

    # HermesAgent.run_conversation 에 while / for 가 없다
    run_conv_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "HermesAgent":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "run_conversation":
                    run_conv_node = sub
                    break

    assert run_conv_node is not None, "run_conversation function not found in HermesAgent"
    loops_in_run_conv = [
        n for n in ast.walk(run_conv_node)
        if isinstance(n, (ast.While, ast.For))
    ]
    assert not loops_in_run_conv, f"Loop construct found in run_conversation: {loops_in_run_conv}"

    # HermesAgent에 _execute_tool_calls, _replace_transcript가 없다
    assert not hasattr(HermesAgent, "_execute_tool_calls")
    assert not hasattr(HermesAgent, "_replace_transcript")

    # agent.py가 hermes_core.kernel.loop에서 run_turn을 import한다
    imports_run_turn = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "hermes_core.kernel.loop":
            for alias in node.names:
                if alias.name == "run_turn":
                    imports_run_turn = True
    assert imports_run_turn, "agent.py must import run_turn from hermes_core.kernel.loop"
