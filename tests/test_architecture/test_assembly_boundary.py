"""조립 경계 및 import 순서 검증 시험 (시나리오 12~16).

근거:
- ADR-0018 §2-3, §2-7, §2-9 (D-03, D-07, D-09)
- 040_기본_확장_조립_분리.md §3-3
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

from hermes_core.agent.agent import HermesAgent
from hermes_core.assembly import default_extensions
from hermes_core.session_db import SessionDB


def test_scenario_12_agent_py_oblivious_to_feature_names():
    """시나리오 12: 겉면은 기능 이름을 모른다 (가정 D)."""
    agent_path = Path(__file__).resolve().parents[2] / "src" / "hermes_core" / "agent" / "agent.py"
    source = agent_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {"CompressionConfig", "CompressionExtension", "LoopGuardExtension"}
    found_forbidden: set[str] = set()
    imports_assembly_default_extensions = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            found_forbidden.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            found_forbidden.add(node.attr)
        elif isinstance(node, ast.alias):
            if node.name in forbidden:
                found_forbidden.add(node.name)
            if node.asname and node.asname in forbidden:
                found_forbidden.add(node.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "hermes_core.assembly":
                for alias in node.names:
                    if alias.name == "default_extensions":
                        imports_assembly_default_extensions = True

    assert not found_forbidden, f"agent.py contains forbidden feature names: {found_forbidden}"
    assert imports_assembly_default_extensions, "agent.py must import default_extensions from hermes_core.assembly"


def test_scenario_13_no_circular_imports_in_any_order():
    """시나리오 13: 어느 순서로 불러도 순환이 없다."""
    statements = [
        "import hermes_core.assembly",
        "import hermes_core.agent.compression_hook",
        "import hermes_core.agent.loop_guard",
        "import hermes_core.agent.agent",
        "from hermes_core.agent import HermesAgent",
        "import hermes_core.tools.delegate_tool",
        "import hermes_core.cli",
    ]

    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo_root / "src")}

    for stmt in statements:
        res = subprocess.run(
            [sys.executable, "-c", stmt],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
        )
        last_err = res.stderr.strip().splitlines()[-1] if res.stderr.strip() else ""
        assert res.returncode == 0, f"Statement '{stmt}' failed with exit code {res.returncode}. Last stderr: {last_err}"


def test_scenario_14_agent_package_lazy_import_and_compatibility():
    """시나리오 14: 패키지 이름 호환."""
    import hermes_core.agent as pkg
    import hermes_core.agent.agent as mod
    import pytest

    assert pkg.HermesAgent is mod.HermesAgent
    assert pkg.AIAgent is mod.AIAgent
    assert pkg.MockLLMClient is mod.MockLLMClient
    assert pkg.MockLLMResponse is mod.MockLLMResponse

    from hermes_core.agent import HermesAgent, AIAgent, MockLLMClient, MockLLMResponse

    assert HermesAgent is mod.HermesAgent
    assert AIAgent is mod.AIAgent
    assert MockLLMClient is mod.MockLLMClient
    assert MockLLMResponse is mod.MockLLMResponse

    assert pkg.__all__ == ["HermesAgent", "AIAgent", "MockLLMClient", "MockLLMResponse"]

    with pytest.raises(AttributeError):
        _ = pkg.NoSuchName

    from hermes_core.agent import prompt_builder
    import hermes_core.agent.prompt_builder as expected_pb

    assert prompt_builder is expected_pb


def test_scenario_15_feature_module_does_not_pull_agent():
    """시나리오 15: 기능 모듈은 겉면을 끌고 오지 않는다."""
    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo_root / "src")}

    code = """
import sys
import hermes_core.agent.compression_hook
modules = set(sys.modules.keys())
forbidden = {"hermes_core.agent.agent", "hermes_core.session_db", "hermes_core.assembly"}
pulled = modules & forbidden
if pulled:
    print(f"PULLED:{','.join(pulled)}")
else:
    print("OK")
"""
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "OK" in res.stdout, f"compression_hook pulled forbidden modules: {res.stdout.strip()}"


def test_scenario_16_default_extensions_originates_from_assembly(tmp_path: Path):
    """시나리오 16: 기본 목록은 조립 지점이 만든 것."""
    db = SessionDB(tmp_path / "test.db")
    agent = HermesAgent(
        session_db=db,
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )

    agent_ext_names = [r.name for r in agent.extensions]
    assembly_ext_names = [e.name for e in default_extensions(lambda: None)]

    assert agent_ext_names == assembly_ext_names == ["compression", "loop_guard"]
