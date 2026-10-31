"""로드맵 14 040 단계: 실제 모델 클라이언트 경로 E2E 통합 테스트.

시나리오 E1, E2:
E1. 종단간 세션 1 -> 세션 2 (--resume + /rollback + 대화 이어가기)
E2. 압축된 세션의 재개 및 턴중 압축 후 취소 정합 (가정 B + C)
"""

import json
from pathlib import Path
import sys
from typing import Any, Optional, Union

import httpx
import pytest

from hermes_core import cli
from hermes_core.constants import (
    CLI_ROLLBACK_PROMPT,
    CLI_USER_PROMPT,
    OPENROUTER_API_KEY_ENV,
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
    get_default_workspace,
)
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB


FAKE_API_KEY = "sk-or-v1-fake-e2e-resume-key-9999"


@pytest.fixture
def e2e_resume_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, FAKE_API_KEY)
    monkeypatch.chdir(tmp_path)
    return {"hermes_home": hermes_home, "tmp_path": tmp_path}


class ScriptableInputWithArgs:
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
                "choices": [
                    {"message": {"content": "default response"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )


def _run_e2e_cli(
    monkeypatch: pytest.MonkeyPatch,
    handler: MockTransportHandler,
    user_inputs: list[str],
    rollback_answers: Optional[list[str]] = None,
    argv: Optional[list[str]] = None,
) -> None:
    transport = httpx.MockTransport(handler)

    def client_factory(api_key: str, model: str, **kwargs: Any) -> OpenRouterClient:
        return OpenRouterClient(api_key, model, transport=transport, **kwargs)

    monkeypatch.setattr(cli, "OpenRouterClient", client_factory)
    fake_input = ScriptableInputWithArgs(user_inputs, rollback_answers)
    monkeypatch.setattr(cli, "read_user_input", fake_input)
    args = argv or []
    monkeypatch.setattr(sys, "argv", ["hermes_core.cli", *args])

    cli.main()


def test_scenario_e1_cross_session_rollback_and_conversation_continuity(
    e2e_resume_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 E1: 종단간 세션 1 -> 세션 2 (--resume + /rollback + 대화 이어가기).

    세션 1:
      1턴: "make note" -> write_file(note.txt, "v1") -> "created"
      2턴: "bad edit" -> patch(note.txt, "v2") -> 모델 두 번째 호출에서 KeyboardInterrupt -> exit
    세션 2 (--resume):
      /rollback (y) -> "what did we make?" -> "note is v1" -> exit
    """
    ws = get_default_workspace()
    ws.mkdir(parents=True, exist_ok=True)

    # 1. 세션 1 대본 준비
    s1_resp1_tool = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_w1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "note.txt", "content": "original note content\n"}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 15, "completion_tokens": 15},
    }
    s1_resp1_asst = {
        "choices": [{
            "message": {"content": "created note.txt"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 25, "completion_tokens": 5},
    }
    s1_resp2_tool = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_p1",
                    "type": "function",
                    "function": {
                        "name": "patch",
                        "arguments": json.dumps({
                            "path": "note.txt",
                            "old_string": "original note content\n",
                            "new_string": "corrupted edit\n",
                        }),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 30, "completion_tokens": 20},
    }
    s1_resp2_interrupt = KeyboardInterrupt()

    handler1 = MockTransportHandler([
        s1_resp1_tool,
        s1_resp1_asst,
        s1_resp2_tool,
        s1_resp2_interrupt,
    ])

    _run_e2e_cli(
        monkeypatch,
        handler1,
        user_inputs=["make note", "bad edit", "exit"],
    )

    db_path = e2e_resume_env["hermes_home"] / "state.db"
    db = SessionDB(db_path)
    sessions = db._conn.execute("SELECT id FROM sessions").fetchall()
    assert len(sessions) == 1
    sid = sessions[0]["id"]

    # 세션 1 검증: get_messages_as_conversation은 "make note" 턴의 4건(user, tool_call, tool_res, asst)만 active=1
    conv1 = db.get_messages_as_conversation(sid)
    assert len(conv1) == 4
    assert conv1[0]["content"] == "make note"
    assert conv1[-1]["content"] == "created note.txt"

    # 취소된 턴 2의 patch로 note.txt는 "corrupted edit"인 상태
    note_path = ws / "note.txt"
    assert note_path.read_text() == "corrupted edit\n"
    db.close()

    # 2. 세션 2 실행 (--resume): /rollback (y) -> "what did we make?" -> exit
    s2_resp_final = {
        "choices": [{
            "message": {"content": "note.txt has original note content"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    }
    handler2 = MockTransportHandler([s2_resp_final])

    _run_e2e_cli(
        monkeypatch,
        handler2,
        user_inputs=["/rollback", "what did we make?", "exit"],
        rollback_answers=["y"],
        argv=["--resume"],
    )

    # 롤백으로 note.txt 원복 확인
    assert note_path.read_text() == "original note content\n"

    # 세션 2 HTTP 요청 본문(messages) 검증
    assert len(handler2.captured_requests) == 1
    req_body = json.loads(handler2.captured_requests[0].content)
    sent_msgs = req_body["messages"]

    # 순서: [system, "make note", tool_call(write_file), tool_result, assistant("created note.txt"), [Rollback notice ...]\n\nwhat did we make?]
    assert sent_msgs[0]["role"] == "system"
    assert sent_msgs[1]["role"] == "user"
    assert sent_msgs[1]["content"] == "make note"
    assert sent_msgs[2]["role"] == "assistant"
    assert sent_msgs[2]["tool_calls"] is not None
    assert sent_msgs[3]["role"] == "tool"
    assert sent_msgs[4]["role"] == "assistant"
    assert sent_msgs[4]["content"] == "created note.txt"

    last_user_msg = sent_msgs[5]
    assert last_user_msg["role"] == "user"
    assert "[Rollback notice - added by the CLI, not typed by the user]" in last_user_msg["content"]
    assert "what did we make?" in last_user_msg["content"]

    # 취소된 "bad edit"는 어디에도 실려 나가지 않음
    all_sent_contents = [str(m.get("content")) for m in sent_msgs]
    assert not any("bad edit" in c for c in all_sent_contents)

    # llm_calls 원장 검증
    db2 = SessionDB(db_path)
    calls = db2._conn.execute(
        "SELECT session_id, request_json FROM llm_calls ORDER BY id"
    ).fetchall()
    assert len(calls) >= 3
    for c in calls:
        assert c["session_id"] == sid
        # API 키 유출 방지 확인
        assert FAKE_API_KEY not in c["request_json"]
    db2.close()


def test_scenario_e2_resume_compacted_session_and_cancellation_protection(
    e2e_resume_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """시나리오 E2: 압축된 세션의 재개 및 턴중 압축 후 취소 정합 (가정 B + C).

    세션 1: 턴 1 완료 후, 턴 2 도중 압축(archive_and_compact) 실행된 뒤 모델 호출에서 KeyboardInterrupt 취소 -> exit.
    세션 2 (--resume): "continue after resume" 입력 시 압축 요약 행과 꼬리 복사본이 보존되어 HTTP 요청에 실림.
    """
    # 세션 1: 턴 1 정상 완료
    resp_t1 = {
        "choices": [{"message": {"content": "turn 1 ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    handler1 = MockTransportHandler([resp_t1])
    _run_e2e_cli(
        monkeypatch,
        handler1,
        user_inputs=["first turn", "exit"],
    )

    db_path = e2e_resume_env["hermes_home"] / "state.db"
    db = SessionDB(db_path)
    sid = db._conn.execute("SELECT id FROM sessions").fetchone()["id"]

    # 턴 1의 마지막 ID 확인
    last_t1_id = db.get_last_active_message_id(sid)

    # 턴 2 도중 압축 시뮬레이션: 요약본 삽입 및 턴 2 도중 취소 미완성 메시지 추가
    summary_text = f"{SUMMARY_PREFIX}\n요약: 첫 턴 대화 완료\n{SUMMARY_END_MARKER}"
    compact_new = [
        {"role": "user", "content": summary_text},
        {"role": "assistant", "content": "네, 기억하고 있습니다."},
    ]
    db.archive_and_compact(sid, compact_new, summary_index=0)

    # 취소 직전 미완성 메시지
    db.append_message(sid, "user", "cancelled turn 2 user message")
    db.append_message(sid, "assistant", "cancelled turn 2 assistant message")

    # CLI 취소 핸들러 동작: deactivate_messages_after(sid, last_t1_id)
    affected = db.deactivate_messages_after(sid, last_t1_id)
    assert affected == 2  # 취소된 2건만 비활성화되고 요약 행과 꼬리 행은 보존 (가정 B)
    db.close()

    # 세션 2 (--resume): "continue after resume"
    resp_t3 = {
        "choices": [{"message": {"content": "resumed ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5},
    }
    handler2 = MockTransportHandler([resp_t3])
    _run_e2e_cli(
        monkeypatch,
        handler2,
        user_inputs=["continue after resume", "exit"],
        argv=["--resume"],
    )

    # 세션 2의 첫 요청 본문 검증
    assert len(handler2.captured_requests) == 1
    req_body = json.loads(handler2.captured_requests[0].content)
    sent_msgs = req_body["messages"]

    sent_contents = [m.get("content") for m in sent_msgs]
    # 요약 행 보존 확인
    assert summary_text in sent_contents
    assert "네, 기억하고 있습니다." in sent_contents
    assert "continue after resume" in sent_contents
    # 취소된 행 배제 확인
    assert "cancelled turn 2 user message" not in sent_contents
    assert "cancelled turn 2 assistant message" not in sent_contents
