"""로드맵 12 030 단계: 실제 모델 클라이언트 경로 E2E 및 압축 공존 통합 테스트.

인수 시나리오 1~7 구현:
1. 실제 OpenRouterClient + httpx.MockTransport 경로에서 HTTP 요청 body의 알림 확인
2. 원장 llm_calls.request_json에 저장된 body와 HTTP 본문 일치 (D-03)
3. 원장 llm_calls.request_json 내 API 키 마스킹 유지 확인
4. 원장 messages 테이블에 알림이 별도 행이 아닌 user 행 content에 포함됨 확인
5. find_previous_summary가 알림 메시지를 이전 요약으로 오인하지 않음 (D-07, 가정 D)
6. compress_messages 실행 시 중간 구간의 알림 메시지가 요약으로 대체됨 (가정 E)
7. compress_messages 실행 시 보호 꼬리에 위치한 알림 메시지가 그대로 보존됨
"""

import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Optional

import httpx
import pytest

from hermes_core import cli
from hermes_core.agent.compression import (
    compress_messages,
    find_previous_summary,
)
from hermes_core.constants import (
    CLI_ROLLBACK_PROMPT,
    CLI_USER_PROMPT,
    OPENROUTER_API_KEY_ENV,
    get_default_workspace,
)
from hermes_core.llm_client import LLMResponse, OpenRouterClient


FAKE_API_KEY = "sk-or-v1-fake-secret-key-12345"
MODEL_NAME = "deepseek/deepseek-v4-flash-0731"


@pytest.fixture
def e2e_test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """실제 OpenRouterClient E2E 테스트를 위한 환경 픽스처."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, FAKE_API_KEY)
    monkeypatch.chdir(tmp_path)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


class ScriptableInputWithArgs:
    """프롬프트와 추가 kwargs를 처리하는 스크립트 기반 입력 핸들러."""

    def __init__(
        self,
        user_inputs: list[str],
        rollback_answers: Optional[list[str]] = None,
    ) -> None:
        self.user_inputs = list(user_inputs)
        self.rollback_answers = list(rollback_answers or [])

    def __call__(self, prompt: str = CLI_USER_PROMPT, **kwargs: Any) -> str:
        if prompt == CLI_ROLLBACK_PROMPT:
            if not self.rollback_answers:
                raise AssertionError(f"Unexpected rollback prompt: {prompt}")
            return self.rollback_answers.pop(0)
        else:
            if not self.user_inputs:
                raise AssertionError(f"User inputs exhausted for prompt: {prompt}")
            return self.user_inputs.pop(0)


class MockTransportHandler:
    """대본 응답을 반환하고 요청을 캡처하는 httpx.MockTransport 핸들러."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.captured_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.captured_requests.append(request)
        if self.responses:
            payload = self.responses.pop(0)
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "default response"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


def _run_e2e_session(
    e2e_test_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict[str, Any]],
    user_inputs: list[str],
    rollback_answers: list[str],
) -> MockTransportHandler:
    handler = MockTransportHandler(responses)
    transport = httpx.MockTransport(handler)

    def client_factory(api_key: str, model: str, **kwargs: Any) -> OpenRouterClient:
        return OpenRouterClient(api_key, model, transport=transport, **kwargs)

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory)
    fake_input = ScriptableInputWithArgs(user_inputs, rollback_answers)
    monkeypatch.setattr(cli, "read_user_input", fake_input)
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])

    cli.main()
    return handler


def test_scenario_01_http_request_contains_notice_and_deleted(
    e2e_test_env, monkeypatch: pytest.MonkeyPatch
):
    """시나리오 1: read it에 대한 첫 HTTP 요청 본문(JSON)의 messages 마지막 user content 확인."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    # 응답 1: write_file notes.txt
    resp_turn1_call = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "notes.txt", "content": "hello notes"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    # 응답 2: turn 1 완료
    resp_turn1_done = {
        "choices": [{"message": {"content": "Created notes.txt"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10},
    }
    # 응답 3: read it 완료
    resp_turn2_done = {
        "choices": [{"message": {"content": "Read it done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    }

    handler = _run_e2e_session(
        e2e_test_env,
        monkeypatch,
        responses=[resp_turn1_call, resp_turn1_done, resp_turn2_done],
        user_inputs=["go", "/rollback", "read it", "exit"],
        rollback_answers=["y"],
    )

    # HTTP 요청은 3개: 1) go, 2) 도구 결과 포함, 3) read it
    assert len(handler.captured_requests) >= 3
    read_it_req = handler.captured_requests[2]
    body = json.loads(read_it_req.content.decode("utf-8"))

    user_msgs = [m for m in body["messages"] if m.get("role") == "user"]
    last_user_content = user_msgs[-1]["content"]

    assert last_user_content.startswith("[Rollback notice - added by the CLI, not typed by the user]")
    assert "  deleted: notes.txt" in last_user_content
    assert last_user_content.endswith("\n\nread it")


def test_scenario_02_ledger_llm_calls_matches_http_payload(
    e2e_test_env, monkeypatch: pytest.MonkeyPatch
):
    """시나리오 2: 원장 llm_calls의 request_json과 1의 HTTP 본문이 글자 그대로 같다 (D-03)."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    resp_turn1_call = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "notes.txt", "content": "hello notes"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    resp_turn1_done = {
        "choices": [{"message": {"content": "Created notes.txt"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10},
    }
    resp_turn2_done = {
        "choices": [{"message": {"content": "Read it done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    }

    handler = _run_e2e_session(
        e2e_test_env,
        monkeypatch,
        responses=[resp_turn1_call, resp_turn1_done, resp_turn2_done],
        user_inputs=["go", "/rollback", "read it", "exit"],
        rollback_answers=["y"],
    )

    read_it_req = handler.captured_requests[2]
    http_body = json.loads(read_it_req.content.decode("utf-8"))
    http_user_msgs = [m for m in http_body["messages"] if m.get("role") == "user"]
    http_content = http_user_msgs[-1]["content"]

    db_path = e2e_test_env["hermes_home"] / "state.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT request_json FROM llm_calls ORDER BY id ASC").fetchall()
        matching_rows = []
        for r in rows:
            req_data = json.loads(r["request_json"])
            u_msgs = [m for m in req_data.get("messages", []) if m.get("role") == "user"]
            if u_msgs and u_msgs[-1].get("content", "").endswith("read it"):
                matching_rows.append(u_msgs[-1]["content"])

        assert len(matching_rows) >= 1
        ledger_content = matching_rows[0]
        assert ledger_content == http_content
    finally:
        conn.close()


def test_scenario_03_no_api_key_leak_in_llm_calls(
    e2e_test_env, monkeypatch: pytest.MonkeyPatch
):
    """시나리오 3: 원장 llm_calls의 모든 request_json에 가짜 열쇠 문자열이 없다."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    resp_turn1 = {
        "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    _run_e2e_session(
        e2e_test_env,
        monkeypatch,
        responses=[resp_turn1],
        user_inputs=["go", "exit"],
        rollback_answers=[],
    )

    db_path = e2e_test_env["hermes_home"] / "state.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT request_json FROM llm_calls").fetchall()
        assert len(rows) >= 1
        for r in rows:
            assert FAKE_API_KEY not in r["request_json"]
    finally:
        conn.close()


def test_scenario_04_messages_table_two_user_rows_and_content_match(
    e2e_test_env, monkeypatch: pytest.MonkeyPatch
):
    """시나리오 4: 원장 messages의 user 행은 go, (알림+read it) 두 개뿐이고 별도 알림 행 없음."""
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    resp_turn1_call = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "notes.txt", "content": "hello notes"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    resp_turn1_done = {
        "choices": [{"message": {"content": "Created notes.txt"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10},
    }
    resp_turn2_done = {
        "choices": [{"message": {"content": "Read it done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    }

    handler = _run_e2e_session(
        e2e_test_env,
        monkeypatch,
        responses=[resp_turn1_call, resp_turn1_done, resp_turn2_done],
        user_inputs=["go", "/rollback", "read it", "exit"],
        rollback_answers=["y"],
    )

    read_it_req = handler.captured_requests[2]
    http_body = json.loads(read_it_req.content.decode("utf-8"))
    expected_content = [m for m in http_body["messages"] if m.get("role") == "user"][-1]["content"]

    db_path = e2e_test_env["hermes_home"] / "state.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        user_rows = conn.execute("SELECT content FROM messages WHERE role = 'user' ORDER BY id ASC").fetchall()
        assert len(user_rows) == 2
        assert user_rows[0]["content"] == "go"
        assert user_rows[1]["content"] == expected_content
    finally:
        conn.close()


def test_scenario_05_find_previous_summary_ignores_rollback_notice():
    """시나리오 5: find_previous_summary에 [system, user(알림+\\n\\nread it), assistant] -> None (가정 D)."""
    notice_text = (
        "[Rollback notice - added by the CLI, not typed by the user]\n"
        "The user ran /rollback and intentionally undid some of your earlier file changes. Those files are back to their earlier state.\n"
        "Turn 1:\n"
        "  deleted: notes.txt\n"
        "Do not re-apply these changes unless the user asks. Re-read a file before editing it.\n"
        "[End of rollback notice]\n\n"
        "read it"
    )
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": notice_text},
        {"role": "assistant", "content": "assistant response"},
    ]
    summary = find_previous_summary(messages)
    assert summary is None


class DummySummaryClient:
    """압축 요약 생성용 가짜 LLM 클라이언트."""

    def __init__(self, summary_text: str = "압축된 요약 텍스트"):
        self.summary_text = summary_text

    def chat_completion(
        self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None
    ) -> LLMResponse:
        return LLMResponse(content=self.summary_text)


def test_scenario_06_compress_messages_replaces_middle_notice_with_summary():
    """시나리오 6: 중간 구간에 위치한 알림 user 메시지는 요약으로 대체됨 (가정 E)."""
    notice_text = (
        "[Rollback notice - added by the CLI, not typed by the user]\n"
        "The user ran /rollback and intentionally undid some of your earlier file changes. Those files are back to their earlier state.\n"
        "Turn 1:\n"
        "  deleted: notes.txt\n"
        "Do not re-apply these changes unless the user asks. Re-read a file before editing it.\n"
        "[End of rollback notice]\n\n"
        "read it"
    )

    # 메시지 구성: system, head 1개, 중간 알림 + 긴 메시지들, 보호 tail 6개
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "head user"},
        {"role": "assistant", "content": "head assistant"},
        # 중간 구간 (요약 대상)
        {"role": "user", "content": notice_text},
        {"role": "assistant", "content": "assistant answering rollback: " + ("A" * 10000)},
        {"role": "user", "content": "another middle msg: " + ("B" * 10000)},
        {"role": "assistant", "content": "another assistant middle: " + ("C" * 10000)},
        # tail (최근 6개)
        {"role": "user", "content": "t1"},
        {"role": "assistant", "content": "t2"},
        {"role": "user", "content": "t3"},
        {"role": "assistant", "content": "t4"},
        {"role": "user", "content": "t5"},
        {"role": "assistant", "content": "t6"},
    ]

    fake_client = DummySummaryClient("요약본: 롤백 사실 요약 포함됨")
    res = compress_messages(
        messages,
        fake_client,
        head_messages=2,
        tail_tokens=500,
        min_tail_messages=6,
    )

    assert res is not None
    summary = find_previous_summary(res.messages)
    assert summary is not None
    assert "요약본: 롤백 사실 요약 포함됨" in summary

    # 결과 메시지 어디에도 요약 표지 바깥에 [Rollback notice 로 시작하는 줄이 없음
    non_summary_messages = [
        m for m in res.messages
        if not (m.get("role") == "user" and m.get("content", "").startswith("[대화 요약:"))
    ]
    for m in non_summary_messages:
        content = m.get("content", "")
        if isinstance(content, str):
            for line in content.split("\n"):
                assert not line.startswith("[Rollback notice")


def test_scenario_07_compress_messages_preserves_tail_notice():
    """시나리오 7: 보호 꼬리에 위치한 알림 user 메시지는 압축 뒤에도 그대로 남음."""
    notice_text = (
        "[Rollback notice - added by the CLI, not typed by the user]\n"
        "The user ran /rollback and intentionally undid some of your earlier file changes. Those files are back to their earlier state.\n"
        "Turn 1:\n"
        "  deleted: notes.txt\n"
        "Do not re-apply these changes unless the user asks. Re-read a file before editing it.\n"
        "[End of rollback notice]\n\n"
        "read it"
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "head user"},
        {"role": "assistant", "content": "head assistant"},
        # 중간 구간 (요약 대상)
        {"role": "user", "content": "large user: " + ("X" * 10000)},
        {"role": "assistant", "content": "large assistant: " + ("Y" * 10000)},
        # tail (최근 6개, 그 중 마지막 user가 알림 메시지)
        {"role": "user", "content": "t1"},
        {"role": "assistant", "content": "t2"},
        {"role": "user", "content": "t3"},
        {"role": "assistant", "content": "t4"},
        {"role": "user", "content": notice_text},
        {"role": "assistant", "content": "t6"},
    ]

    fake_client = DummySummaryClient("요약본")
    res = compress_messages(
        messages,
        fake_client,
        head_messages=2,
        tail_tokens=5000,
        min_tail_messages=6,
    )

    assert res is not None
    user_contents = [m.get("content") for m in res.messages if m.get("role") == "user"]
    assert notice_text in user_contents
