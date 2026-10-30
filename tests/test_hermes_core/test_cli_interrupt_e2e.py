"""로드맵 13 050 단계: 실제 모델 클라이언트 경로 E2E 및 압축 공존 통합 테스트.

인수 시나리오 G1~G6 구현:
G1. 실제 OpenRouterClient + httpx.MockTransport 경로에서 세 번째 HTTP 요청 본문 확인 (취소 알림 포함, go 배제)
G2. 원장 llm_calls: 3개 행, 2번째 행(취소된 호출)의 request_json 존재 및 status_code/error_kind NULL, 3번째 행 일치
G3. 원장 llm_calls의 모든 request_json에 가짜 열쇠 문자열 부재
G4. 원장 messages: user 행 2개(go, 알림+read it), 취소된 요청의 assistant/tool 행 보존
G5. 중간 구간의 취소 알림 메시지가 압축 요약으로 대체됨 (요약 표지 밖 알림 부재)
G6. 보호 꼬리의 취소 알림 메시지가 압축 뒤에도 보존되고 find_previous_summary가 None 반환
"""

import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Optional, Union

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


@pytest.fixture
def e2e_interrupt_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """실제 OpenRouterClient E2E 테스트를 위한 격리 환경 픽스처."""
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


class MockInterruptTransportHandler:
    """대본 응답을 반환하고 요청을 캡처하는 httpx.MockTransport 핸들러.
    
    대본 항목이 BaseException(또는 인스턴스)이면 raise하고, 아니면 HTTP 200 응답 반환.
    """

    def __init__(self, responses: list[Union[dict[str, Any], BaseException]]):
        self.responses = list(responses)
        self.captured_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.captured_requests.append(request)
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return httpx.Response(200, json=item)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "default response"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


def _run_e2e_session(
    e2e_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    responses: list[Union[dict[str, Any], BaseException]],
    user_inputs: list[str],
    rollback_answers: Optional[list[str]] = None,
) -> MockInterruptTransportHandler:
    handler = MockInterruptTransportHandler(responses)
    transport = httpx.MockTransport(handler)

    def client_factory(api_key: str, model: str, **kwargs: Any) -> OpenRouterClient:
        return OpenRouterClient(api_key, model, transport=transport, **kwargs)

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory)
    fake_input = ScriptableInputWithArgs(user_inputs, rollback_answers)
    monkeypatch.setattr(cli, "read_user_input", fake_input)
    monkeypatch.setattr(sys, "argv", ["hermes_core.cli"])

    cli.main()
    return handler


class DummySummaryClient:
    """압축 테스트용 가짜 요약 클라이언트."""

    def __init__(self, summary_text: str):
        self.summary_text = summary_text

    def chat_completion(self, messages: Any, tools: Any = None) -> LLMResponse:
        return LLMResponse(content=self.summary_text, finish_reason="stop")


def test_scenario_050_g1_to_g4(
    e2e_interrupt_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1~G4 통합 검증:

    입력: go -> read it -> exit
    대본:
      1) notes.txt를 write_file로 만드는 도구 호출 응답
      2) 도구 결과를 받은 두 번째 요청에서 handler가 KeyboardInterrupt를 던짐
      3) read it에 대한 최종 응답 'Read it done'
    """
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    resp1_tool_call = {
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
    resp2_interrupt = KeyboardInterrupt()
    resp3_read_done = {
        "choices": [{"message": {"content": "Read it done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    }

    handler = _run_e2e_session(
        e2e_interrupt_env,
        monkeypatch,
        responses=[resp1_tool_call, resp2_interrupt, resp3_read_done],
        user_inputs=["go", "read it", "exit"],
    )

    # -------------------------------------------------------------
    # G1: 세 번째 HTTP 요청 본문 검증
    # -------------------------------------------------------------
    assert len(handler.captured_requests) == 3

    req3_body = json.loads(handler.captured_requests[2].content.decode("utf-8"))
    messages3 = req3_body["messages"]
    last_user_content = messages3[-1]["content"]

    assert last_user_content.startswith(
        "[Interruption notice - added by the CLI, not typed by the user]"
    )
    assert "  changed: notes.txt" in last_user_content
    assert last_user_content.endswith("\n\nread it")

    # 세 번째 HTTP 요청 본문의 어느 메시지 content에도 "go"는 없다 (취소된 요청이 대화에서 빠짐)
    for m in messages3:
        content = m.get("content")
        if isinstance(content, str):
            assert content != "go"
            assert "\ngo\n" not in content

    # -------------------------------------------------------------
    # G2: 원장 llm_calls 검증
    # -------------------------------------------------------------
    db_path = e2e_interrupt_env["hermes_home"] / "state.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    calls = conn.execute("SELECT * FROM llm_calls ORDER BY id ASC").fetchall()
    assert len(calls) == 3

    # 2번째 행 (취소된 호출)
    call2 = calls[1]
    assert call2["request_json"] is not None
    assert call2["status_code"] is None
    assert call2["error_kind"] is None
    req2_body = json.loads(call2["request_json"])
    # 2번째 호출의 요청에는 이전 턴의 user 내용인 "go"가 포함되어 있었음
    user_contents_in_call2 = [
        m["content"] for m in req2_body.get("messages", []) if m.get("role") == "user"
    ]
    assert "go" in user_contents_in_call2

    # 3번째 행
    call3 = calls[2]
    req3_logged_body = json.loads(call3["request_json"])
    assert req3_logged_body["messages"][-1]["content"] == last_user_content

    # -------------------------------------------------------------
    # G3: 원장 llm_calls의 모든 request_json에 가짜 열쇠 문자열 부재
    # -------------------------------------------------------------
    for c in calls:
        assert FAKE_API_KEY not in c["request_json"]

    # -------------------------------------------------------------
    # G4: 원장 messages 테이블 검증
    # -------------------------------------------------------------
    db_msgs = conn.execute("SELECT role, content FROM messages ORDER BY id ASC").fetchall()
    user_msgs = [m for m in db_msgs if m["role"] == "user"]
    assert len(user_msgs) == 2
    assert user_msgs[0]["content"] == "go"
    assert user_msgs[1]["content"] == last_user_content

    # 취소된 요청의 assistant(도구 호출) 및 tool 행이 지워지지 않고 남아 있다
    assistant_msgs = [m for m in db_msgs if m["role"] == "assistant"]
    tool_msgs = [m for m in db_msgs if m["role"] == "tool"]
    assert len(assistant_msgs) >= 1
    assert len(tool_msgs) >= 1

    conn.close()


def test_scenario_050_g5_compress_messages_summarizes_middle_notice() -> None:
    """G5: 중간 구간의 취소 알림 user 메시지는 압축 시 요약본으로 대체되고, 요약 표지 밖 알림 부재."""
    notice_text = (
        "[Interruption notice - added by the CLI, not typed by the user]\n"
        "Your previous turn was cancelled by the user (Ctrl+C). "
        "The conversation history does not contain that turn's messages, "
        "but the files below were modified by your tool calls before the turn was stopped.\n"
        "Files that may have changed:\n"
        "  changed: notes.txt\n"
        "Do not assume the previous task is continuing unless the user asks. "
        "Re-read any file you need before editing it.\n"
        "[End of interruption notice]\n\n"
        "read it"
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "head user"},
        {"role": "assistant", "content": "head assistant"},
        # 중간 구간 (요약 대상)
        {"role": "user", "content": notice_text},
        {"role": "assistant", "content": "assistant answering interrupt: " + ("A" * 10000)},
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

    fake_client = DummySummaryClient("요약본: 취소 알림 사실 요약 포함됨")
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
    assert "요약본: 취소 알림 사실 요약 포함됨" in summary

    # 결과 메시지 어디에도 요약 표지 바깥에 [Interruption notice 로 시작하는 줄이 없음
    non_summary_messages = [
        m for m in res.messages
        if not (m.get("role") == "user" and m.get("content", "").startswith("[대화 요약:"))
    ]
    for m in non_summary_messages:
        content = m.get("content", "")
        if isinstance(content, str):
            for line in content.split("\n"):
                assert not line.startswith("[Interruption notice")


def test_scenario_050_g6_compress_messages_preserves_tail_notice() -> None:
    """G6: 보호 꼬리에 위치한 취소 알림 user 메시지는 압축 뒤에도 그대로 남고, find_previous_summary는 None."""
    notice_text = (
        "[Interruption notice - added by the CLI, not typed by the user]\n"
        "Your previous turn was cancelled by the user (Ctrl+C). "
        "The conversation history does not contain that turn's messages, "
        "but the files below were modified by your tool calls before the turn was stopped.\n"
        "Files that may have changed:\n"
        "  changed: notes.txt\n"
        "Do not assume the previous task is continuing unless the user asks. "
        "Re-read any file you need before editing it.\n"
        "[End of interruption notice]\n\n"
        "read it"
    )

    # find_previous_summary 검증: 알림 메시지는 요약이 아님
    assert find_previous_summary([{"role": "user", "content": notice_text}]) is None

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
