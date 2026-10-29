"""CLI 스킬 인덱스 플래그 (`--no-skills-index`) 연동 테스트 (로드맵 08 / 구현지시서 025 인수 시나리오)."""

import io
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from hermes_core import cli
from hermes_core.agent.agent import MockLLMResponse
from hermes_core.session_db import SessionDB

BLOCK = (
    "<available_skills>\n"
    "- deploy-guide: Deploy steps\n"
    "- release-procedure: Team release procedure\n"
    "</available_skills>"
)

FAKE_API_KEY = "test-fake-key-skills-index-secret-999"


class ScriptableClient:
    """테스트용 대본형 가짜 OpenRouter 클라이언트."""

    created_instances: list["ScriptableClient"] = []
    responses_by_instance: dict[int, list[Any]] = {}

    def __init__(self, api_key: str, model: str, **kwargs: Any) -> None:
        self.api_key = api_key
        self.model = model
        self.instance_idx = len(ScriptableClient.created_instances)
        ScriptableClient.created_instances.append(self)
        self.call_history: list[dict[str, Any]] = []
        self.responses = list(ScriptableClient.responses_by_instance.get(self.instance_idx, []))
        self._call_recorder = None

    def set_call_recorder(self, recorder: Any) -> None:
        self._call_recorder = recorder

    def chat_completion(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> Any:
        self.call_history.append({"messages": messages, "tools": tools})
        if self._call_recorder:
            self._call_recorder({
                "model": self.model,
                "status_code": 200,
                "input_tokens": 10,
                "output_tokens": 5,
            })
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return MockLLMResponse(content="[Scriptable] default response")


@pytest.fixture(autouse=True)
def reset_scriptable_client():
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()
    yield
    ScriptableClient.created_instances.clear()
    ScriptableClient.responses_by_instance.clear()


def _setup_skills(hermes_home: Path) -> None:
    skills_dir = hermes_home / "skills"
    release_dir = skills_dir / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    (release_dir / "SKILL.md").write_text(
        "---\n"
        "name: release-procedure\n"
        "description: Team release procedure\n"
        "---\n"
        "# Release\n",
        encoding="utf-8",
    )

    deploy_dir = skills_dir / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / "SKILL.md").write_text(
        "---\n"
        "name: deploy-guide\n"
        "description: Deploy steps\n"
        "---\n"
        "# Deploy\n",
        encoding="utf-8",
    )


@pytest.fixture
def cli_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI 테스트 환경 픽스처."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_API_KEY)
    monkeypatch.setattr(cli, "OpenRouterClient", ScriptableClient)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


def test_scenario_01_real_mode_default_skills_index_enabled(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 1: 실제 모드, 인자 없음, 스킬 2개 -> BLOCK 1회 주입, 배너에 Skills index 미출력."""
    hermes_home = cli_test_env["hermes_home"]
    _setup_skills(hermes_home)

    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(content="hello back"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert len(ScriptableClient.created_instances) == 1
    client = ScriptableClient.created_instances[0]
    assert len(client.call_history) >= 1
    first_call_sys_msg = client.call_history[0]["messages"][0]["content"]

    assert first_call_sys_msg.count(BLOCK) == 1
    assert "Skills index" not in captured.out


def test_scenario_02_real_mode_no_skills_index_flag(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 2: 실제 모드, --no-skills-index, 스킬 2개 -> 태그/스킬 미포함, 배너에 정확히 1회 출력."""
    hermes_home = cli_test_env["hermes_home"]
    _setup_skills(hermes_home)

    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--no-skills-index"])
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(content="hello back"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert len(ScriptableClient.created_instances) == 1
    client = ScriptableClient.created_instances[0]
    first_call_sys_msg = client.call_history[0]["messages"][0]["content"]

    assert "<available_skills>" not in first_call_sys_msg
    assert "deploy-guide" not in first_call_sys_msg
    assert "release-procedure" not in first_call_sys_msg
    assert captured.out.count("Skills index disabled (--no-skills-index).") == 1


def test_scenario_03_real_mode_no_skills_dir(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 3: 실제 모드, 인자 없음, 스킬 폴더 없음 -> <available_skills> 없고 폴더 미생성 유지."""
    hermes_home = cli_test_env["hermes_home"]
    # 스킬 폴더를 생성하지 않음

    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\nexit\n"))

    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(content="hello back"),
    ]

    cli.main()
    captured = capsys.readouterr()

    assert len(ScriptableClient.created_instances) == 1
    client = ScriptableClient.created_instances[0]
    first_call_sys_msg = client.call_history[0]["messages"][0]["content"]

    assert "<available_skills>" not in first_call_sys_msg
    assert not (hermes_home / "skills").exists()


def test_scenario_04_mock_mode_default_skills_index_enabled(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 4: 모의 모드(--mock), 인자 없음, 스킬 2개 -> 세션 DB system_prompt에 BLOCK 포함, 배너 미출력."""
    hermes_home = cli_test_env["hermes_home"]
    _setup_skills(hermes_home)

    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock"])
    # 비터미널 가짜 입력 (sys.stdin.isatty() == False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    cli.main()
    captured = capsys.readouterr()

    db = SessionDB(hermes_home / "state.db")
    rows = db._conn.execute("SELECT system_prompt FROM sessions ORDER BY started_at DESC LIMIT 1").fetchall()
    assert len(rows) == 1
    system_prompt = rows[0]["system_prompt"]

    assert BLOCK in system_prompt
    assert "Skills index" not in captured.out


def test_scenario_05_mock_mode_no_skills_index_flag(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 5: 모의 모드, --mock --no-skills-index, 스킬 2개 -> DB에 미포함, 배너에 정확히 1회 출력."""
    hermes_home = cli_test_env["hermes_home"]
    _setup_skills(hermes_home)

    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--mock", "--no-skills-index"])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    cli.main()
    captured = capsys.readouterr()

    db = SessionDB(hermes_home / "state.db")
    rows = db._conn.execute("SELECT system_prompt FROM sessions ORDER BY started_at DESC LIMIT 1").fetchall()
    assert len(rows) == 1
    system_prompt = rows[0]["system_prompt"]

    assert "<available_skills>" not in system_prompt
    assert captured.out.count("Skills index disabled (--no-skills-index).") == 1


def test_scenario_06_real_mode_delegate_child_isolation(
    cli_test_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 6: 실제 모드, --delegate, 스킬 2개 -> 부모 첫 호출에 BLOCK 포함, 자식 첫 호출에 <available_skills> 없음."""
    hermes_home = cli_test_env["hermes_home"]
    _setup_skills(hermes_home)

    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--delegate"])
    monkeypatch.setattr("sys.stdin", io.StringIO("go\nexit\n"))

    # 부모 0번: 1차 delegate_task 호출, 2차 최종 parent done
    ScriptableClient.responses_by_instance[0] = [
        MockLLMResponse(
            tool_calls=[
                {
                    "id": "p_call_1",
                    "type": "function",
                    "function": {"name": "delegate_task", "arguments": json.dumps({"goal": "G1"})},
                }
            ]
        ),
        MockLLMResponse(content="parent done"),
    ]
    # 자식 1번: 최종 child summary
    ScriptableClient.responses_by_instance[1] = [
        MockLLMResponse(content="child summary"),
    ]

    cli.main()
    capsys.readouterr()

    assert len(ScriptableClient.created_instances) == 2
    parent_client = ScriptableClient.created_instances[0]
    child_client = ScriptableClient.created_instances[1]

    # 부모 첫 호출
    parent_sys_msg = parent_client.call_history[0]["messages"][0]["content"]
    assert BLOCK in parent_sys_msg
    parent_tool_names = {t["function"]["name"] for t in parent_client.call_history[0]["tools"]}
    assert "delegate_task" in parent_tool_names

    # 자식 첫 호출
    child_sys_msg = child_client.call_history[0]["messages"][0]["content"]
    assert "<available_skills>" not in child_sys_msg

