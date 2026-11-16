"""CLI 진입점 테스트: 실제 모델이 없을 때 가짜 모델을 조용히 쓰지 않는다 (로드맵 02 / 구현지시서 030 인수 시나리오 및 040 인수 시나리오)."""

import io
import os
from pathlib import Path
import sys
from typing import Any

import pytest

from hermes_core import cli
from hermes_core.constants import DEFAULT_REAL_MODEL, get_hermes_home
from hermes_core.llm_client import LLMError, LLMResponse
from hermes_core.session_db import SessionDB


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    # 표준 입력은 터미널이 아닌 가짜 입력으로 고정한다 (실행 환경에 기대지 않는다)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    cli.main()


def test_without_mock_exits_with_code_2(hermes_home, monkeypatch):  # 시나리오 1
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)
    assert exc.value.code == 2


def test_without_mock_message_mentions_mock_flag(hermes_home, monkeypatch, capsys):  # 시나리오 2
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _run(monkeypatch)
    err = capsys.readouterr().err
    assert "--mock" in err
    assert "OPENROUTER_API_KEY" in err


def test_without_mock_creates_no_session_db(hermes_home, monkeypatch):  # 시나리오 3
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _run(monkeypatch)
    assert not (hermes_home / "state.db").exists()


def test_mock_mode_finishes_cleanly(hermes_home, monkeypatch, capsys):  # 시나리오 4 (구현지시서 040 시나리오 1)
    _run(monkeypatch, "--mock")
    assert "Mock session finished cleanly." in capsys.readouterr().out

    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row["model"] == "mock"
    assert row["mode"] == "mock"
    db.close()


def test_mock_mode_keeps_mock_notice(hermes_home, monkeypatch, capsys):  # 시나리오 5
    _run(monkeypatch, "--mock")
    assert "MOCK mode" in capsys.readouterr().out


# =============================================================================
# 로드맵 04: 구현지시서 040 인수 테스트 시나리오 (시나리오 3 ~ 10)
# =============================================================================


def test_040_scenario_03_empty_and_whitespace_keys_exit_2(hermes_home, monkeypatch):
    """시나리오 3: --mock 없이, 키 환경변수가 ''와 '   ' -> 각각 종료 코드 2."""
    for empty_val in ["", "   "]:
        monkeypatch.setenv("OPENROUTER_API_KEY", empty_val)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch)
        assert exc.value.code == 2


class DummyMockOpenRouterClient:
    created_instances: list[Any] = []

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        DummyMockOpenRouterClient.created_instances.append(self)

    def chat_completion(self, messages, tools=None):
        return LLMResponse(content="dummy assistant answer", finish_reason="stop")


def test_040_scenario_04_05_07_real_mode_success_cwd_and_zero_leakage(hermes_home, monkeypatch, capsys):
    """시나리오 4, 5, 7: 키 있음(가짜 값), 가짜 클라이언트, 입력 exit
    -> 종료 코드 0, cwd == HERMES_HOME/workspace, model == DEFAULT_REAL_MODEL, mode == real, 키 출력 없음.
    """
    fake_key = "test-key-safe-12345"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)
    DummyMockOpenRouterClient.created_instances.clear()

    # 입력은 'exit'
    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        # cwd 확인 후 복원
        current_cwd = Path.cwd()
        monkeypatch.chdir(orig_cwd)

    expected_ws = (hermes_home / "workspace").resolve()
    assert current_cwd == expected_ws
    assert expected_ws.is_dir()

    # 시나리오 5: 세션 model == DEFAULT_REAL_MODEL, mode == real
    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row["model"] == DEFAULT_REAL_MODEL
    assert row["mode"] == "real"
    db.close()

    # 시나리오 7: 출력 검사 (키 없음, 모델명/작업 폴더 표시됨)
    captured = capsys.readouterr()
    full_output = captured.out + captured.err
    assert fake_key not in full_output
    assert DEFAULT_REAL_MODEL in full_output
    assert str(expected_ws) in full_output


def test_040_scenario_06_custom_model_flag(hermes_home, monkeypatch):
    """시나리오 6: --model a/b + 실제 모드 조건 -> 세션 model == 'a/b', 클라이언트 a/b로 생성."""
    fake_key = "test-key-safe-model-flag"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)
    DummyMockOpenRouterClient.created_instances.clear()

    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core", "--model", "custom/model-x"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    assert len(DummyMockOpenRouterClient.created_instances) == 1
    assert DummyMockOpenRouterClient.created_instances[0].model == "custom/model-x"

    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1")
    row = cursor.fetchone()
    assert row["model"] == "custom/model-x"
    assert row["mode"] == "real"
    db.close()


class ErrorThenExitClient:
    def __init__(self, api_key: str, model: str):
        self.model = model

    def chat_completion(self, messages, tools=None):
        raise LLMError("rate_limited", "Rate limit 429 exceeded")


def test_040_scenario_09_agents_md_not_injected_after_chdir(tmp_path, hermes_home, monkeypatch):
    """시나리오 9: 원래 현재 폴더의 AGENTS.md에 MARKER가 있어도 작업 폴더로 이동하여 프롬프트에 주입되지 않음."""
    start_dir = tmp_path / "original_dir"
    start_dir.mkdir()
    marker = "UNIQUE-REPO-MARKER-9999"
    (start_dir / "AGENTS.md").write_text(f"Rule: {marker}", encoding="utf-8")

    fake_key = "test-key-dir-isolation"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    captured_messages: list[list[dict]] = []

    class CapturingClient:
        def __init__(self, api_key: str, model: str):
            self.model = model

        def chat_completion(self, messages, tools=None):
            captured_messages.append(messages)
            return LLMResponse(content="captured", finish_reason="stop")

    monkeypatch.setattr(cli, "OpenRouterClient", CapturingClient)

    # start_dir에서 실행
    monkeypatch.chdir(start_dir)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    try:
        cli.main()
    finally:
        monkeypatch.chdir(tmp_path)

    # 시스템 프롬프트 확인
    assert len(captured_messages) == 1
    sys_content = captured_messages[0][0]["content"]
    assert marker not in sys_content


def test_040_scenario_10_relative_hermes_home_stays_consistent(tmp_path, monkeypatch):
    """시나리오 10: HERMES_HOME이 상대경로일 때 실행 후에도 get_hermes_home()이 절대경로로 고정되어 DB가 해당 폴더에 생성됨."""
    start_dir = tmp_path / "run_root"
    start_dir.mkdir()
    monkeypatch.chdir(start_dir)

    rel_home_name = "rel_hermes_home"
    monkeypatch.setenv("HERMES_HOME", rel_home_name)

    fake_key = "test-key-rel-home"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setattr(cli, "OpenRouterClient", DummyMockOpenRouterClient)

    monkeypatch.setattr(sys, "stdin", io.StringIO("exit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    expected_abs_home = (start_dir / rel_home_name).resolve()

    try:
        cli.main()
    finally:
        monkeypatch.chdir(tmp_path)

    # 실행 후에도 get_hermes_home()이 처음에 resolve된 절대경로와 일치
    assert get_hermes_home() == expected_abs_home
    assert (expected_abs_home / "state.db").exists()


# =============================================================================
# 로드맵 05: 구현지시서 010 인수 테스트 시나리오 (시나리오 1 ~ 7)
# =============================================================================


class QueueingMockClient:
    """호출별 messages를 복사하여 기록하고 큐잉된 응답을 순서대로 돌려주는 가짜 클라이언트."""

    def __init__(self, api_key: str = "mock-key", model: str = DEFAULT_REAL_MODEL):
        self.api_key = api_key
        self.model = model
        self.responses: list[Any] = []
        self.call_history: list[list[dict[str, Any]]] = []

    def queue_response(
        self,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.responses.append(LLMResponse(content=content, tool_calls=tool_calls, finish_reason="stop"))

    def queue_error(self, kind: str, message: str) -> None:
        self.responses.append(LLMError(kind=kind, message=message))

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.call_history.append([dict(m) for m in messages])
        if not self.responses:
            return LLMResponse(content="[Mock] default response", finish_reason="stop")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_010_scenario_03_three_turns_order_preserved(hermes_home, monkeypatch):
    """시나리오 3: 세 턴 입력 시 세 번째 호출 messages에 앞선 두 턴이 순서대로 있고 system은 정확히 1개."""
    fake_key = "test-key-010-sc3"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)

    mock_client = QueueingMockClient()
    mock_client.queue_response(content="응답1")
    mock_client.queue_response(content="응답2")
    mock_client.queue_response(content="응답3")

    monkeypatch.setattr(cli, "OpenRouterClient", lambda *args, **kwargs: mock_client)

    monkeypatch.setattr(sys, "stdin", io.StringIO("하나\n둘\n셋\nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    assert len(mock_client.call_history) == 3

    third_call_messages = mock_client.call_history[2]

    # system 메시지 1개
    system_messages = [m for m in third_call_messages if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert third_call_messages[0]["role"] == "system"

    # 순서 검증 (user '하나' -> assistant '응답1' -> user '둘' -> assistant '응답2' -> user '셋')
    contents = [
        (m.get("role"), m.get("content"))
        for m in third_call_messages
        if m.get("role") in ("user", "assistant")
    ]
    expected_order = [
        ("user", "하나"),
        ("assistant", "응답1"),
        ("user", "둘"),
        ("assistant", "응답2"),
        ("user", "셋"),
    ]
    assert contents == expected_order


def test_010_scenario_04_tool_call_pairs_preserved_across_turns(hermes_home, monkeypatch):
    """시나리오 4: 첫 턴의 도구 호출(assistant tool_calls -> tool)이 두 번째 턴의 messages에 온전히 짝지어 보존됨."""
    fake_key = "test-key-010-sc4"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)

    mock_client = QueueingMockClient()
    # 1턴: 도구 호출 -> 최종 응답
    mock_client.queue_response(
        content=None,
        tool_calls=[{
            "id": "call_mem_1",
            "function": {
                "name": "memory",
                "arguments": '{"action": "add", "target": "user", "content": "key fact"}',
            },
        }],
    )
    mock_client.queue_response(content="메모리에 저장 완료했습니다.")
    # 2턴: 일반 응답
    mock_client.queue_response(content="다음 질문에 대한 답입니다.")

    monkeypatch.setattr(cli, "OpenRouterClient", lambda *args, **kwargs: mock_client)

    monkeypatch.setattr(sys, "stdin", io.StringIO("기억해줘\n확인해줘\nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    # 1턴에서 2번 호출 + 2턴에서 1번 호출 = 총 3번 LLM 호출
    assert len(mock_client.call_history) == 3

    second_turn_first_call = mock_client.call_history[2]

    # 도구 호출 assistant 메시지 확인
    tc_msgs = [m for m in second_turn_first_call if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(tc_msgs) == 1
    assert tc_msgs[0]["tool_calls"][0]["id"] == "call_mem_1"

    # 도구 실행 결과 tool 메시지 확인
    tool_msgs = [m for m in second_turn_first_call if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_mem_1"
    assert "key fact" in tool_msgs[0]["content"]


def test_010_scenario_05_llm_error_in_first_turn_preserves_user_in_history_and_db(hermes_home, monkeypatch, capsys):
    """시나리오 5: 첫 턴 LLMError 종료 후 프로세스 지속, 두 번째 턴 messages에 첫 user 포함 및 세션 DB 행 수 일치."""
    fake_key = "test-key-010-sc5"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)

    mock_client = QueueingMockClient()
    mock_client.queue_error("timeout", "Request timed out")
    mock_client.queue_response(content="성공적으로 처리되었습니다.")

    monkeypatch.setattr(cli, "OpenRouterClient", lambda *args, **kwargs: mock_client)

    monkeypatch.setattr(sys, "stdin", io.StringIO("첫번째시도\n두번째시도\nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    # 에러 출력 및 정상 종료 메시지 확인
    captured = capsys.readouterr()
    assert "Model call failed (timeout): Request timed out" in captured.out
    assert "Session saved and closed. Goodbye!" in captured.out

    # 2번의 모델 호출 (1턴 실패 시 1회, 2턴 1회)
    assert len(mock_client.call_history) == 2
    second_call_messages = mock_client.call_history[1]

    # 두 번째 호출 messages에 첫 턴의 user 메시지 '첫번째시도' 포함
    user_msgs_in_call = [m for m in second_call_messages if m.get("role") == "user"]
    assert len(user_msgs_in_call) == 2
    assert user_msgs_in_call[0]["content"] == "첫번째시도"
    assert user_msgs_in_call[1]["content"] == "두번째시도"

    # 세션 DB의 messages 테이블에서 role == 'user'인 행 수 검증
    db = SessionDB(hermes_home / "state.db")
    cursor = db._conn.execute("SELECT COUNT(*) as cnt FROM messages WHERE role = 'user'")
    db_user_count = cursor.fetchone()["cnt"]
    db.close()

    assert db_user_count == 2
    assert db_user_count == len(user_msgs_in_call)


def test_010_scenario_06_blank_input_no_effect_on_history(hermes_home, monkeypatch):
    """시나리오 6: 공백 입력(엔터 등)은 이력에 영향 없고 모델 호출 횟수도 변하지 않음."""
    fake_key = "test-key-010-sc6"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)

    mock_client = QueueingMockClient()
    mock_client.queue_response(content="정상 응답")

    monkeypatch.setattr(cli, "OpenRouterClient", lambda *args, **kwargs: mock_client)

    # 공백 입력들 사이에 정상 입력 1회
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n\n\t\n정상입력\n   \nexit\n"))
    monkeypatch.setattr(sys, "argv", ["hermes-core"])

    orig_cwd = Path.cwd()
    try:
        cli.main()
    finally:
        monkeypatch.chdir(orig_cwd)

    assert len(mock_client.call_history) == 1
    call_messages = mock_client.call_history[0]
    user_msgs = [m for m in call_messages if m.get("role") == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "정상입력"


def test_050_scenario_24_context_window_option(hermes_home, monkeypatch):
    """시나리오 24: CLI --context-window 2000 -> 에이전트가 해당 창 크기로 생성되고 임계값 1000 계산."""
    created_agents = []
    orig_agent_init = cli.HermesAgent.__init__

    def tracked_agent_init(self, *args, **kwargs):
        orig_agent_init(self, *args, **kwargs)
        created_agents.append(self)

    monkeypatch.setattr(cli.HermesAgent, "__init__", tracked_agent_init)
    _run(monkeypatch, "--mock", "--context-window", "2000")

    assert len(created_agents) == 1
    agent = created_agents[0]
    assert agent.context_window_tokens == 2000
    from hermes_core.agent.context_budget import compression_threshold
    threshold = compression_threshold(agent.context_window_tokens, agent.compression_threshold_ratio)
    assert threshold == 1000


@pytest.mark.parametrize("invalid_arg", ["0", "abc", "-5"])
def test_050_scenario_25_invalid_context_window(hermes_home, monkeypatch, invalid_arg):
    """시나리오 25: CLI --context-window 0, abc, -5 -> 안내 후 종료 코드 2, 세션 DB 생성 없음."""
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--mock", "--context-window", invalid_arg)
    assert exc.value.code == 2
    assert not (hermes_home / "state.db").exists()
