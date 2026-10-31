"""tests/test_architecture/test_llm_entry_boundary.py — LLM 단일 입구 및 경계 아키텍처 테스트.

시나리오 17, 18.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent


def test_scenario_17_agent_py_oblivious_to_wiring_details() -> None:
    """시나리오 17: 겉면은 연결 방법을 모른다.

    agent.py의 AST를 검사하여 다음을 단언한다:
    - Name.id, Attribute.attr에 hasattr, set_call_recorder, record_llm_call, update_token_counts가 없다.
    - hermes_core.assembly에서 connect_call_recorder, default_llm_middlewares를 import한다.
    """
    agent_path = ROOT / "src" / "hermes_core" / "agent" / "agent.py"
    with open(agent_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(agent_path))

    forbidden_names = {
        "hasattr",
        "set_call_recorder",
        "record_llm_call",
        "update_token_counts",
    }

    found_forbidden: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            found_forbidden.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_names:
            found_forbidden.append((node.lineno, node.attr))

    assert not found_forbidden, f"agent.py contains forbidden names: {found_forbidden}"

    # import 확인
    assembly_imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "hermes_core.assembly":
            for alias in node.names:
                assembly_imported.add(alias.name)

    assert "connect_call_recorder" in assembly_imported
    assert "default_llm_middlewares" in assembly_imported


def test_scenario_18_kernel_single_llm_entry() -> None:
    """시나리오 18: LLM 입구는 한 곳.

    src/hermes_core/kernel/*.py를 ast로 읽음:
    - Attribute.attr == 'chat_completion'인 노드가 있는 파일은 llm_pipeline.py 하나.
    - loop.py의 run_turn 함수 본문 안에 update_token_counts 속성 접근이 없고,
      loop.py 전체에서는 _replace_transcript 안의 1건만 있다.
    """
    kernel_dir = ROOT / "src" / "hermes_core" / "kernel"
    chat_completion_files: list[str] = []

    for py_file in kernel_dir.glob("*.py"):
        with open(py_file, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "chat_completion":
                chat_completion_files.append(py_file.name)
                break

    assert chat_completion_files == ["llm_pipeline.py"]

    # loop.py 분석
    loop_path = kernel_dir / "loop.py"
    with open(loop_path, encoding="utf-8") as f:
        loop_tree = ast.parse(f.read(), filename=str(loop_path))

    total_update_token_counts = 0
    run_turn_update_token_counts = 0
    replace_transcript_update_token_counts = 0

    for node in ast.walk(loop_tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_turn":
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute) and child.attr == "update_token_counts":
                    run_turn_update_token_counts += 1
        elif isinstance(node, ast.FunctionDef) and node.name == "_replace_transcript":
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute) and child.attr == "update_token_counts":
                    replace_transcript_update_token_counts += 1

        if isinstance(node, ast.Attribute) and node.attr == "update_token_counts":
            total_update_token_counts += 1

    assert run_turn_update_token_counts == 0
    assert replace_transcript_update_token_counts == 1
    assert total_update_token_counts == 1
