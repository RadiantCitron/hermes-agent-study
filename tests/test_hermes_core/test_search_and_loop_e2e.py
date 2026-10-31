"""17 로드맵 030 단계: Search-Read-Edit 및 LoopGuard E2E 종단간 검증.

[검증 범위]
- 시나리오 E1: Search -> Read -> Edit 자율 3박자 E2E (보호 경로 자동 제외, 승인 0회, 패치 완료 및 체크포인트)
- 시나리오 E2: 반복 실패 -> 3회째 _loop_warning Nudge -> search_files 자가 교정 E2E
- 시나리오 E3: 5회 연속 동일 호출 stuck_loop 차단 -> 다음 턴 및 --resume 세션 재개 무결성 E2E
"""

import io
import json
from pathlib import Path
from typing import Any
import httpx
import pytest

from hermes_core import cli
from hermes_core.agent import HermesAgent
from hermes_core.constants import CLI_STUCK_LOOP_NOTICE, DEFAULT_REAL_MODEL
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import Approver
from hermes_core.tools.checkpoint import Checkpointer
from hermes_core.tools.dispatcher import create_default_dispatcher


class SequentialMockTransport(httpx.BaseTransport):
    """지정된 순서대로 응답하는 httpx MockTransport."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "default completion"}}]},
            )
        resp = self.responses.pop(0)
        return httpx.Response(200, json=resp)


def _make_tool_call_resp(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _make_text_resp(content: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                }
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_scenario_e1_search_read_edit_autonomous_cycle(tmp_path: Path):
    """시나리오 E1: Search -> Read -> Edit 자율 3박자 E2E 검증."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # 1. 파일 및 환경 준비
    (workspace / ".env").write_text("SECRET_KEY=123", encoding="utf-8")
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("SECRET_GIT_KEY=456", encoding="utf-8")

    src_dir = workspace / "src"
    src_dir.mkdir()
    auth_file = src_dir / "auth.py"
    auth_file.write_text("def verify_token():\n    return False\n", encoding="utf-8")

    db = SessionDB(tmp_path / "state.db")
    checkpointer = Checkpointer(db, "s_e1", workspace=workspace)

    approval_calls = 0

    def fake_ask(cmd, reason):
        nonlocal approval_calls
        approval_calls += 1
        return True

    approver = Approver(
        session_db=db,
        session_id="s_e1",
        ask=fake_ask,
        is_interactive=lambda: True,
        say=lambda t: None,
    )
    checkpointer.begin_turn()
    dispatcher = create_default_dispatcher(
        session_db=db,
        approver=approver,
        checkpointer=checkpointer,
    )

    # 2. LLM 응답 시퀀스 정의: search_files -> read_file -> patch -> final text
    resp_search = _make_tool_call_resp("c1", "search_files", {"pattern": "verify_token", "path": "."})
    resp_read = _make_tool_call_resp("c2", "read_file", {"path": "src/auth.py"})
    resp_patch = _make_tool_call_resp(
        "c3",
        "patch",
        {"path": "src/auth.py", "old_string": "return False", "new_string": "return True"},
    )
    resp_done = _make_text_resp("Authentication verified and fixed.")

    transport = SequentialMockTransport([resp_search, resp_read, resp_patch, resp_done])
    client = OpenRouterClient(api_key="fake-key", model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_id="s_e1",
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        quiet_mode=True,
    )

    # workspace 컨텍스트로 실행
    import os
    orig_cwd = os.getcwd()
    os.chdir(workspace)
    try:
        res = agent.run_conversation("find verify_token and fix it to return True")
    finally:
        os.chdir(orig_cwd)

    # 3. 단정 검증
    # (1) 사람 승인(Approver) 0회 (search, read, patch는 승인 대상 아님)
    assert approval_calls == 0
    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "Authentication verified and fixed."

    # (2) search_files 결과에 .env와 .git 노출 안 됨, auth.py 포함
    search_tool_msg = next(m for m in res["messages"] if m.get("tool_call_id") == "c1")
    search_data = json.loads(search_tool_msg["content"])
    assert search_data["success"] is True
    paths = [m["path"] for m in search_data["matches"]]
    assert "src/auth.py" in paths
    assert not any(".env" in p or ".git" in p for p in paths)

    # (3) 파일 수정 확인 및 체크포인트 1건 applied 확인
    assert "return True" in auth_file.read_text(encoding="utf-8")
    cps = db.get_file_checkpoints("s_e1")
    assert len(cps) == 1
    assert cps[0]["status"] == "applied"
    assert cps[0]["rel_path"] == "src/auth.py"


def test_scenario_e2_loop_guard_warning_and_self_correction_e2e(tmp_path: Path):
    """시나리오 E2: 반복 실패 -> 3회째 _loop_warning Nudge -> search_files 자가 교정 E2E."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target_file = workspace / "logic.py"
    target_file.write_text("def calculate(x):\n    result = x * 2\n    return result\n", encoding="utf-8")

    db = SessionDB(tmp_path / "state.db")
    checkpointer = Checkpointer(db, "s_e2", workspace=workspace)
    checkpointer.begin_turn()
    dispatcher = create_default_dispatcher(session_db=db, checkpointer=checkpointer)

    # 잘못된 patch 호출 3회 (키 순서 다름 포함)
    bad_patch_1 = _make_tool_call_resp(
        "call_p1", "patch", {"path": "logic.py", "old_string": "wrong string", "new_string": "x * 3"}
    )
    bad_patch_2 = _make_tool_call_resp(
        "call_p2", "patch", {"new_string": "x * 3", "path": "logic.py", "old_string": "wrong string"}
    )
    bad_patch_3 = _make_tool_call_resp(
        "call_p3", "patch", {"path": "logic.py", "old_string": "wrong string", "new_string": "x * 3"}
    )
    # 3회째 _loop_warning을 보고 search_files로 올바른 문자열 탐색
    search_call = _make_tool_call_resp(
        "call_s4", "search_files", {"pattern": "result =", "path": "."}
    )
    # 5회차 올바른 patch 수행
    good_patch = _make_tool_call_resp(
        "call_p5", "patch", {"path": "logic.py", "old_string": "result = x * 2", "new_string": "result = x * 3"}
    )
    final_done = _make_text_resp("logic successfully patched to x * 3")

    transport = SequentialMockTransport([bad_patch_1, bad_patch_2, bad_patch_3, search_call, good_patch, final_done])
    client = OpenRouterClient(api_key="fake-key", model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_id="s_e2",
        session_db=db,
        dispatcher=dispatcher,
        llm_client=client,
        quiet_mode=True,
    )

    import os
    orig_cwd = os.getcwd()
    os.chdir(workspace)
    try:
        res = agent.run_conversation("fix calculate logic")
    finally:
        os.chdir(orig_cwd)

    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "logic successfully patched to x * 3"

    # 4번째 HTTP 요청 본문(즉 3번째 patch 실패 후 모델로 전달되는 요청)의 3번째 tool 메시지 확인
    fourth_req_body = json.loads(transport.requests[3].content.decode("utf-8"))
    tool_msgs = [m for m in fourth_req_body["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    t3 = json.loads(tool_msgs[2]["content"])
    assert "_loop_warning" in t3
    assert "Loop guard warning: tool 'patch' was called 3 times consecutively" in t3["_loop_warning"]

    # 파일이 올바르게 수정되었는지 확인
    assert "result = x * 3" in target_file.read_text(encoding="utf-8")


def test_scenario_e3_stuck_loop_block_and_resume_integrity_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """시나리오 E3: 5회 연속 동일 호출 stuck_loop 차단 -> 다음 턴 및 --resume 세션 재개 무결성 E2E."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hermes_home = tmp_path / "hermes_home"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-e2e-key")
    monkeypatch.chdir(workspace)

    # 턴 1: read_file 5회 반복 시도 -> stuck_loop 종료
    # 턴 2: "hello" 질의에 "world" 텍스트 응답
    # 세션 2 (--resume): "are you there?" 질의에 "yes I am" 텍스트 응답
    repeat_read = _make_tool_call_resp("c_read", "read_file", {"path": "missing.txt"})
    turn2_resp = _make_text_resp("world")
    resume_resp = _make_text_resp("yes I am")

    all_responses = [
        repeat_read,
        repeat_read,
        repeat_read,
        repeat_read,
        repeat_read,  # 5회째에서 stuck_loop으로 끊김
        turn2_resp,   # 턴 2 질의에 대한 응답
        resume_resp,  # 세션 2 (--resume) 질의에 대한 응답
    ]

    transport = SequentialMockTransport(all_responses)

    # OpenRouterClient가 공유 transport를 쓰도록 monkeypatch
    class BoundClient(OpenRouterClient):
        def __init__(self, **kwargs):
            super().__init__(api_key="fake-key", model=DEFAULT_REAL_MODEL, transport=transport)

    monkeypatch.setattr(cli, "OpenRouterClient", BoundClient)

    # 1. 첫 번째 세션 실행: 턴 1 (루프 차단) -> 턴 2 (정상 응답) -> exit
    monkeypatch.setattr("sys.argv", ["hermes_core.cli"])
    monkeypatch.setattr("sys.stdin", io.StringIO("trigger repeated read\nhello\nexit\n"))

    cli.main()
    out1 = capsys.readouterr().out

    # 턴 1에서 CLI_STUCK_LOOP_NOTICE 출력 확인
    assert CLI_STUCK_LOOP_NOTICE in out1
    assert "Assistant: world" in out1

    # DB 상태 및 짝 맞춤 검증
    db_path = hermes_home / "state.db"
    db = SessionDB(db_path)
    sessions = db._conn.execute("SELECT id, end_reason FROM sessions ORDER BY started_at ASC").fetchall()
    assert len(sessions) == 1
    session_id = sessions[0]["id"]
    assert sessions[0]["end_reason"] == "user_exit"

    db_msgs_s1 = db.get_messages_as_conversation(session_id)
    # assistant tool_calls와 tool 메시지 짝 맞춤 검증
    for idx, m in enumerate(db_msgs_s1):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tc_id = m["tool_calls"][0]["id"]
            next_m = db_msgs_s1[idx + 1]
            assert next_m.get("role") == "tool"
            assert next_m.get("tool_call_id") == tc_id
            if idx == 8:  # 5번째 tool call
                content = json.loads(next_m["content"])
                assert content["success"] is False
                assert "Loop guard blocked: tool 'read_file'" in content["error"]

    # 2. 두 번째 세션 실행: --resume 플래그로 직전 세션 이어받기
    monkeypatch.setattr("sys.argv", ["hermes_core.cli", "--resume", session_id[:8]])
    monkeypatch.setattr("sys.stdin", io.StringIO("are you there?\nexit\n"))

    cli.main()
    out2 = capsys.readouterr().out

    assert "Assistant: yes I am" in out2

    # 재개 세션의 메시지 짝 정합성 재확인
    db_msgs_resumed = db.get_messages_as_conversation(session_id)
    last_assistant_msg = db_msgs_resumed[-1]
    assert last_assistant_msg["role"] == "assistant"
    assert last_assistant_msg["content"] == "yes I am"
