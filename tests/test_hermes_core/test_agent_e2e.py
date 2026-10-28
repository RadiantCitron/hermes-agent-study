"""에이전트 루프, 도구 실행 레일 및 통합 E2E 검증 테스트."""

from hermes_core.agent import HermesAgent, MockLLMClient, MockLLMResponse
from hermes_core.session_db import SessionDB
from hermes_core.tools.memory_tool import MemoryStore


def test_agent_e2e_memory_tool_cycle(tmp_path):
    """유저 입력 -> 도구 호출(memory_tool) -> 결과 수집 -> 최종 응답 -> DB 저장 전 사이클 검증."""
    mem_dir = tmp_path / "memories"
    db_file = tmp_path / "state.db"

    memory_store = MemoryStore(memory_dir=mem_dir)
    session_db = SessionDB(db_file)

    # Mock LLM 시나리오 준비
    # 1회차 호출: memory_tool 호출 요청
    # 2회차 호출: 도구 결과를 본 후 최종 응답
    mock_client = MockLLMClient()
    mock_client.queue_response(
        content=None,
        tool_calls=[{
            "id": "call_mem_1",
            "function": {
                "name": "memory",
                "arguments": '{"action": "add", "target": "memory", "content": "Project uses Python 3.14"}',
            },
        }],
    )
    mock_client.queue_response(
        content="프로젝트 환경 정보를 메모리에 안전하게 저장했습니다.",
    )

    agent = HermesAgent(
        session_db=session_db,
        memory_store=memory_store,
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("프로젝트 파이썬 버전을 기억해줘.")

    # 1. 최종 응답 확인
    assert result["final_response"] == "프로젝트 환경 정보를 메모리에 안전하게 저장했습니다."
    assert result["iterations"] == 2  # 1회 도구 호출 + 1회 최종 응답

    # 2. 실제 디스크의 MEMORY.md 파일에 기록되었는지 검증
    memory_file = mem_dir / "MEMORY.md"
    assert memory_file.exists()
    assert "Project uses Python 3.14" in memory_file.read_text(encoding="utf-8")

    # 3. SessionDB에 원장으로 모든 메시지가 Strict Role Alternation으로 저장되었는지 검증
    messages = session_db.get_messages(agent.session_id)
    # [user, assistant(tool_calls), tool, assistant] 총 4건
    assert len(messages) == 4
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "프로젝트 파이썬 버전을 기억해줘."
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"] is not None
    assert messages[2]["role"] == "tool"
    assert "Entry added" in messages[2]["content"]
    assert messages[3]["role"] == "assistant"
    assert messages[3]["content"] == "프로젝트 환경 정보를 메모리에 안전하게 저장했습니다."

    session_db.close()


def test_agent_e2e_terminal_tool_execution(tmp_path):
    """에이전트가 terminal_tool을 호출하여 실제 쉘 명령을 수행하고 결과를 반환받는지 검증."""
    session_db = SessionDB(":memory:")
    memory_store = MemoryStore(memory_dir=tmp_path)

    mock_client = MockLLMClient()
    mock_client.queue_response(
        content=None,
        tool_calls=[{
            "id": "call_term_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "echo \'Hermes Core Activated\'"}',
            },
        }],
    )
    mock_client.queue_response(
        content="쉘 명령어 실행이 완료되었습니다.",
    )

    agent = HermesAgent(
        session_db=session_db,
        memory_store=memory_store,
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("echo 명령어를 실행해줘.")

    assert result["final_response"] == "쉘 명령어 실행이 완료되었습니다."

    messages = session_db.get_messages(agent.session_id)
    assert len(messages) == 4
    tool_output_msg = messages[2]
    assert tool_output_msg["role"] == "tool"
    assert "Hermes Core Activated" in tool_output_msg["content"]

    session_db.close()


def test_agent_e2e_blocked_invalid_unreadable_and_valid_calls_in_one_conversation(tmp_path, monkeypatch):
    """로드맵 02 / 060: 차단된 명령, 잘못된 인자, 읽을 수 없는 인자, 정상 명령이 한 대화에 섞여도 루프가 정상 종료한다."""
    work = tmp_path / "work"
    work.mkdir()
    victim = work / "x"
    victim.write_text("keep", encoding="utf-8")
    monkeypatch.chdir(work)

    def call(call_id, name, arguments):
        return [{"id": call_id, "function": {"name": name, "arguments": arguments}}]

    mock_client = MockLLMClient()
    mock_client.queue_response(tool_calls=call("c1", "terminal", '{"command": "rm -rf x"}'))
    mock_client.queue_response(tool_calls=call("c2", "memory", "{}"))
    mock_client.queue_response(tool_calls=call("c3", "terminal", "{bad"))
    mock_client.queue_response(tool_calls=call("c4", "terminal", '{"command": "echo ok"}'))
    mock_client.queue_response(content="done")

    session_db = SessionDB(":memory:")
    agent = HermesAgent(
        session_db=session_db,
        memory_store=MemoryStore(memory_dir=tmp_path / "memories"),
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("섞어서 시험해줘.")

    # 1. 정상 종료
    assert result["final_response"] == "done"
    assert result["stopped_reason"] == "completed"
    assert result["iterations"] == 5

    # 2. 차단된 명령은 실행되지 않았다
    assert victim.exists()

    # 3. 도구 메시지 4개가 순서대로: 거부, 인자 오류, 인자 읽기 실패, 성공
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 4
    assert "rm" in tool_msgs[0]["content"]
    assert "action" in tool_msgs[1]["content"]
    assert "JSON" in tool_msgs[2]["content"]
    assert "ok" in tool_msgs[3]["content"]

    # 4. 세션 원장: 사용자 1 + 모델 5 + 도구 4 = 10
    assert len(session_db.get_messages(agent.session_id)) == 10

    # 5. 모델 요청마다 대응하는 도구 메시지가 있다
    requested_ids = [
        tc["id"] for m in result["messages"] if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    ]
    assert requested_ids == ["c1", "c2", "c3", "c4"]
    assert [m["tool_call_id"] for m in tool_msgs] == requested_ids

    session_db.close()


def test_agent_e2e_file_tools_full_cycle(tmp_path, monkeypatch):
    """로드맵 03 / 060: 파일 생성·읽기·수정, 금지 경로 거부, 덮어쓰기 거부, 폴더 밖 거부, 읽을 수 없는 인자가 한 대화에 섞여도 정상 종료한다."""
    w = tmp_path / "workspace"
    w.mkdir()
    monkeypatch.chdir(w)

    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    def call(call_id, name, arguments):
        return [{"id": call_id, "function": {"name": name, "arguments": arguments}}]

    mock_client = MockLLMClient()
    # 턴 1: write_file {"path": "notes.txt", "content": "hello\n"}
    mock_client.queue_response(tool_calls=call("c1", "write_file", '{"path": "notes.txt", "content": "hello\\n"}'))
    # 턴 2: read_file {"path": "notes.txt"}
    mock_client.queue_response(tool_calls=call("c2", "read_file", '{"path": "notes.txt"}'))
    # 턴 3: patch {"path": "notes.txt", "old_string": "hello", "new_string": "world"}
    mock_client.queue_response(tool_calls=call("c3", "patch", '{"path": "notes.txt", "old_string": "hello", "new_string": "world"}'))
    # 턴 4: write_file {"path": "AGENTS.md", "content": "x"}
    mock_client.queue_response(tool_calls=call("c4", "write_file", '{"path": "AGENTS.md", "content": "x"}'))
    # 턴 5: write_file {"path": "notes.txt", "content": "again"}
    mock_client.queue_response(tool_calls=call("c5", "write_file", '{"path": "notes.txt", "content": "again"}'))
    # 턴 6: read_file {"path": "../outside.txt"}
    mock_client.queue_response(tool_calls=call("c6", "read_file", '{"path": "../outside.txt"}'))
    # 턴 7: patch "{bad"
    mock_client.queue_response(tool_calls=call("c7", "patch", '{bad'))
    # 턴 8: read_file {"path": "notes.txt"}
    mock_client.queue_response(tool_calls=call("c8", "read_file", '{"path": "notes.txt"}'))
    # 턴 9: 텍스트 "done"
    mock_client.queue_response(content="done")

    session_db = SessionDB(":memory:")
    memory_store = MemoryStore(memory_dir=hermes_home / "memories")
    agent = HermesAgent(
        session_db=session_db,
        memory_store=memory_store,
        llm_client=mock_client,
        quiet_mode=True,
    )

    result = agent.run_conversation("파일 도구를 섞어서 시험해줘.")

    # 1. final_response == "done", stopped_reason == "completed", iterations == 9
    assert result["final_response"] == "done"
    assert result["stopped_reason"] == "completed"
    assert result["iterations"] == 9

    # 2. 파일 W/notes.txt의 내용이 world\n
    assert (w / "notes.txt").read_text(encoding="utf-8") == "world\n"

    # 3. W/AGENTS.md가 존재하지 않음, outside.txt 내용이 OUTSIDE-SECRET 그대로
    assert not (w / "AGENTS.md").exists()
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"

    # 4. 도구 메시지 8개가 순서대로
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 8
    # 0: 생성 성공(notes.txt)
    assert "notes.txt" in tool_msgs[0]["content"] and '"success": true' in tool_msgs[0]["content"].lower()
    # 1: 읽기 성공(1|hello 포함)
    assert "1|hello" in tool_msgs[1]["content"]
    # 2: 수정 성공(replacements 포함)
    assert "replacements" in tool_msgs[2]["content"] and '"success": true' in tool_msgs[2]["content"].lower()
    # 3: 지시문 파일 거부(instruction 포함)
    assert "instruction" in tool_msgs[3]["content"] and '"success": false' in tool_msgs[3]["content"].lower()
    # 4: 덮어쓰기 거부(already exists 포함)
    assert "already exists" in tool_msgs[4]["content"] and '"success": false' in tool_msgs[4]["content"].lower()
    # 5: 폴더 밖 거부(outside 포함)
    assert "outside" in tool_msgs[5]["content"] and '"success": false' in tool_msgs[5]["content"].lower()
    # 6: 인자 읽기 실패(JSON 포함)
    assert "JSON" in tool_msgs[6]["content"] and '"success": false' in tool_msgs[6]["content"].lower()
    # 7: 읽기 성공(1|world 포함)
    assert "1|world" in tool_msgs[7]["content"]

    # 5. 세션 원장의 메시지 수 = 사용자 1 + 모델 9 + 도구 8 = 18
    db_messages = session_db.get_messages(agent.session_id)
    assert len(db_messages) == 18

    # 6. 모델 요청마다 대응하는 도구 메시지가 있음 (tool_call_id 일치)
    requested_ids = [
        tc["id"] for m in result["messages"] if m["role"] == "assistant" for tc in m.get("tool_calls", [])
    ]
    assert requested_ids == ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]
    assert [m["tool_call_id"] for m in tool_msgs] == requested_ids

    # 7. 어떤 도구 메시지에도 OUTSIDE-SECRET이 없음
    for m in tool_msgs:
        assert "OUTSIDE-SECRET" not in m["content"]

    session_db.close()


# =============================================================================
# 로드맵 04: 구현지시서 050 통합 E2E 검증 시나리오 (시나리오 1 ~ 13)
# =============================================================================

import json
from typing import Any, Optional, Union
import httpx
import pytest

from hermes_core.constants import DEFAULT_REAL_MODEL
from hermes_core.llm_client import OpenRouterClient
from hermes_core.tools.dispatcher import create_default_dispatcher

FAKE_E2E_KEY = "test-key-1234567890"


def _make_mock_provider_payload(
    content: Optional[str] = None,
    tool_calls: Optional[list[dict]] = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    finish_reason: Optional[str] = None,
) -> dict:
    """실제 제공자(OpenRouter) 응답 형태를 흉내 내는 딕셔너리."""
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls

    return {
        "choices": [
            {
                "message": msg,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }
        ],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class SequentialMockTransport(httpx.BaseTransport):
    """지정된 순서대로 응답하는 MockTransport."""

    def __init__(self, responses: list[Union[dict, int, Exception]]):
        self.responses = list(responses)
        self.captured_requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.captured_requests.append(request)
        if not self.responses:
            return httpx.Response(200, json=_make_mock_provider_payload(content="fallback done"))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, text=f"HTTP {item} error simulation")
        return httpx.Response(200, json=item)


@pytest.fixture
def r04_e2e_env(tmp_path, monkeypatch):
    """로드맵 04 E2E 통합 환경: 작업 폴더 W(.hermes/workspace)로 이동 및 비밀 파일(.env) 배치."""
    home = (tmp_path / "hermes_home").resolve()
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    workspace = (home / "workspace").resolve()
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    # 심어 둔 비밀 파일 (절대 유출되면 안 됨)
    (workspace / ".env").write_text("SECRET=PLANTED-SECRET-VALUE\n", encoding="utf-8")

    db = SessionDB(home / "state.db")
    memory_store = MemoryStore(memory_dir=home / "memories")
    dispatcher = create_default_dispatcher(memory_store=memory_store, session_db=db)

    return {
        "home": home,
        "workspace": workspace,
        "db": db,
        "memory_store": memory_store,
        "dispatcher": dispatcher,
    }


def test_050_scenarios_01_to_04_full_tool_cycle_and_zero_leakage(r04_e2e_env, caplog, capsys):
    """시나리오 1~4: 정상 흐름(write_file -> done) 전체 사이클, 2차 요청 메시지 검증, 호출 원장 일치, 키 미저장 검증."""
    w = r04_e2e_env["workspace"]
    db = r04_e2e_env["db"]

    tool_call_item = {
        "id": "call_w1_e2e",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"path": "hello.txt", "content": "hi"}'},
    }

    transport = SequentialMockTransport([
        _make_mock_provider_payload(tool_calls=[tool_call_item], prompt_tokens=20, completion_tokens=10),
        _make_mock_provider_payload(content="done", prompt_tokens=35, completion_tokens=5),
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    result = agent.run_conversation("create hello.txt with hi")

    # 시나리오 1: 최종 응답, 파일 생성, 원장 순서
    assert result["final_response"] == "done"
    assert result["stopped_reason"] == "completed"
    assert (w / "hello.txt").read_text(encoding="utf-8") == "hi"

    db_msgs = db.get_messages(agent.session_id)
    # 순서: user -> assistant(tool_calls) -> tool -> assistant
    assert [m["role"] for m in db_msgs] == ["user", "assistant", "tool", "assistant"]

    # 시나리오 2: 두 번째 요청 본문 messages 검증
    assert len(transport.captured_requests) == 2
    second_req_body = json.loads(transport.captured_requests[1].content.decode("utf-8"))
    sent_messages = second_req_body["messages"]
    tool_entry = next(m for m in sent_messages if m.get("role") == "tool")
    assert tool_entry["tool_call_id"] == "call_w1_e2e"
    assert "hello.txt" in tool_entry["content"]

    # 시나리오 3: 호출 기록 2행, 토큰 합계 일치
    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 2
    assert calls[0]["error_kind"] is None
    assert calls[1]["error_kind"] is None
    session_info = db.get_session(agent.session_id)
    assert session_info["input_tokens"] == 20 + 35  # 55
    assert session_info["output_tokens"] == 10 + 5  # 15

    # 시나리오 4: 키 미저장 검증 (원장, 세션, 로그, 표준출력 어디에도 FAKE_E2E_KEY 없음)
    for c in calls:
        assert FAKE_E2E_KEY not in str(dict(c))
    for m in db_msgs:
        assert FAKE_E2E_KEY not in str(dict(m))
    assert FAKE_E2E_KEY not in str(session_info)
    assert FAKE_E2E_KEY not in caplog.text
    out_err = capsys.readouterr()
    assert FAKE_E2E_KEY not in out_err.out + out_err.err

    db.close()


def test_050_scenario_05_secret_file_read_blocked(r04_e2e_env):
    """시나리오 5: 모델이 read_file('.env')를 호출 -> 도구 거부(protected), 원장 및 요청 본문에 비밀값 없음."""
    db = r04_e2e_env["db"]
    transport = SequentialMockTransport([
        _make_mock_provider_payload(
            tool_calls=[{"id": "call_sec_read", "type": "function", "function": {"name": "read_file", "arguments": '{"path": ".env"}'}}]
        ),
        _make_mock_provider_payload(content="could not read secret"),
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("read .env")
    assert res["stopped_reason"] == "completed"

    # 도구 메시지 확인
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    assert "protected" in tool_msg["content"]
    assert '"success": false' in tool_msg["content"].lower()

    # 원장 및 요청 본문에 PLANTED-SECRET-VALUE 없음
    all_db_msgs = db.get_messages(agent.session_id)
    for m in all_db_msgs:
        assert "PLANTED-SECRET-VALUE" not in str(dict(m))

    for req in transport.captured_requests:
        assert "PLANTED-SECRET-VALUE" not in req.content.decode("utf-8")

    db.close()


def test_050_scenario_06_secret_file_terminal_blocked(r04_e2e_env):
    """시나리오 6: 모델이 terminal로 cat .env 호출 -> 실행 0회, 도구 거부, 비밀값 없음."""
    db = r04_e2e_env["db"]
    transport = SequentialMockTransport([
        _make_mock_provider_payload(
            tool_calls=[{"id": "call_sec_term", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "cat .env"}'}}]
        ),
        _make_mock_provider_payload(content="blocked"),
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("run cat .env")
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    assert "protected" in tool_msg["content"]
    assert "PLANTED-SECRET-VALUE" not in tool_msg["content"]

    for m in db.get_messages(agent.session_id):
        assert "PLANTED-SECRET-VALUE" not in str(dict(m))

    db.close()


def test_050_scenario_07_outside_and_git_blocked(r04_e2e_env):
    """시나리오 7: write_file('.git/hooks/pre-push')와 read_file('../state.db') 호출 -> 둘 다 거부, 파일 미생성."""
    w = r04_e2e_env["workspace"]
    db = r04_e2e_env["db"]

    transport = SequentialMockTransport([
        _make_mock_provider_payload(
            tool_calls=[
                {"id": "c_git", "type": "function", "function": {"name": "write_file", "arguments": '{"path": ".git/hooks/pre-push", "content": "malicious"}'}},
                {"id": "c_db", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "../state.db"}'}},
            ]
        ),
        _make_mock_provider_payload(content="done trying"),
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("attack files")
    assert res["stopped_reason"] == "completed"

    tool_msgs = [m for m in res["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert "protected" in tool_msgs[0]["content"]  # .git
    assert "outside" in tool_msgs[1]["content"]    # ../state.db

    assert not (w / ".git").exists()
    db.close()


def test_050_scenario_08_instruction_write_blocked(r04_e2e_env):
    """시나리오 8: write_file('AGENTS.md') 호출 -> 거부(instruction 포함), 파일 미생성."""
    w = r04_e2e_env["workspace"]
    db = r04_e2e_env["db"]

    transport = SequentialMockTransport([
        _make_mock_provider_payload(
            tool_calls=[{"id": "c_instr", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "AGENTS.md", "content": "override rules"}'}}]
        ),
        _make_mock_provider_payload(content="failed write"),
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("override AGENTS.md")
    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    assert "instruction" in tool_msg["content"]
    assert not (w / "AGENTS.md").exists()
    db.close()


def test_050_scenario_09_provider_rate_limited_429(r04_e2e_env):
    """시나리오 9: 1회차에 429 반환 -> stopped_reason == 'llm_error', error.kind == 'rate_limited', llm_calls 1행."""
    db = r04_e2e_env["db"]
    transport = SequentialMockTransport([429])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("start")
    assert res["stopped_reason"] == "llm_error"
    assert res["error"]["kind"] == "rate_limited"

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 1
    assert calls[0]["error_kind"] == "rate_limited"
    db.close()


def test_050_scenario_10_second_turn_error_500(r04_e2e_env):
    """시나리오 10: 1회차 정상(도구 호출), 2회차 500 에러 -> tool 메시지 보존, llm_calls 2행."""
    db = r04_e2e_env["db"]
    transport = SequentialMockTransport([
        _make_mock_provider_payload(
            tool_calls=[{"id": "c_pwd", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "pwd"}'}}]
        ),
        500,
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("run pwd")
    assert res["stopped_reason"] == "llm_error"
    assert res["error"]["kind"] == "http_error"

    # 원장에 tool 메시지까지 남아 있음
    msgs = db.get_messages(agent.session_id)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 2
    assert calls[0]["error_kind"] is None
    assert calls[1]["error_kind"] == "http_error"
    db.close()


def test_050_scenario_11_broken_arguments_json_recovery(r04_e2e_env):
    """시나리오 11: arguments가 깨진 JSON -> 인자 읽기 오류 메시지 반환 후 루프 계속, 최종 응답 정상 종료."""
    db = r04_e2e_env["db"]
    transport = SequentialMockTransport([
        _make_mock_provider_payload(
            tool_calls=[{"id": "c_broken", "type": "function", "function": {"name": "terminal", "arguments": '{"broken'}}]
        ),
        _make_mock_provider_payload(content="recovered from broken json"),
    ])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("test broken json")
    assert res["stopped_reason"] == "completed"
    assert res["final_response"] == "recovered from broken json"

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    assert "Invalid JSON" in tool_msg["content"]
    assert '"success": false' in tool_msg["content"].lower()
    db.close()


def test_050_scenario_12_max_iterations_bounded(r04_e2e_env):
    """시나리오 12: 제공자가 매번 도구 호출만 응답(max_iterations=3) -> iterations == 3, stopped_reason == 'max_iterations', llm_calls 3행."""
    db = r04_e2e_env["db"]
    # 3번 모두 도구 호출만 응답
    loop_call = _make_mock_provider_payload(
        tool_calls=[{"id": "c_loop", "type": "function", "function": {"name": "terminal", "arguments": '{"command": "echo loop"}'}}]
    )
    transport = SequentialMockTransport([loop_call, loop_call, loop_call, loop_call])

    client = OpenRouterClient(FAKE_E2E_KEY, DEFAULT_REAL_MODEL, transport=transport)
    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=client,
        max_iterations=3,
        quiet_mode=True,
    )

    res = agent.run_conversation("infinite loop test")
    assert res["iterations"] == 3
    assert res["stopped_reason"] == "max_iterations"

    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 3
    db.close()


def test_050_scenario_13_parity_with_mock_llm_client(r04_e2e_env):
    """시나리오 13: 가정 G 대조 - 같은 시퀀스를 MockLLMClient로 재생 시 파일 결과와 도구 메시지 내용이 1번과 일치."""
    w = r04_e2e_env["workspace"]
    db = r04_e2e_env["db"]

    mock_client = MockLLMClient()
    mock_client.queue_response(
        content=None,
        tool_calls=[{
            "id": "call_w1_mock",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "hello_mock.txt", "content": "hi"}',
            },
        }],
    )
    mock_client.queue_response(content="done")

    agent = HermesAgent(
        session_db=db,
        memory_store=r04_e2e_env["memory_store"],
        dispatcher=r04_e2e_env["dispatcher"],
        llm_client=mock_client,
        quiet_mode=True,
    )

    res = agent.run_conversation("mock create")
    assert res["final_response"] == "done"
    assert res["stopped_reason"] == "completed"
    assert (w / "hello_mock.txt").read_text(encoding="utf-8") == "hi"

    tool_msg = next(m for m in res["messages"] if m.get("role") == "tool")
    assert "hello_mock.txt" in tool_msg["content"]
    assert '"success": true' in tool_msg["content"].lower()
    db.close()


# =============================================================================
# 로드맵 05: 구현지시서 060 통합 E2E 검증 시나리오 (시나리오 1 ~ 11)
# =============================================================================

from hermes_core.constants import SUMMARY_PREFIX


@pytest.fixture
def r05_e2e_env(tmp_path, monkeypatch):
    """로드맵 05 통합 E2E 검증 환경."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mem_dir = hermes_home / "memories"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text("Known Fact: Initial Memory Item\n", encoding="utf-8")

    workspace = hermes_home / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    db_path = hermes_home / "state.db"
    db = SessionDB(db_path)
    memory_store = MemoryStore(memory_dir=mem_dir)
    dispatcher = create_default_dispatcher(memory_store=memory_store, session_db=db)

    return {
        "home": hermes_home,
        "memories": mem_dir,
        "workspace": workspace,
        "db": db,
        "memory_store": memory_store,
        "dispatcher": dispatcher,
    }


def test_060_scenarios_01_to_07_and_09_full_cycle(r05_e2e_env):
    """시나리오 1~7 및 9: 5턴 대화, 3번째 턴에 ORCA 사실 발화, 임계값 초과 압축, 사후 도구 1사이클, 불변식, 검색 및 세션 토큰 검증."""
    db = r05_e2e_env["db"]
    w = r05_e2e_env["workspace"]

    long_payload = "X" * 200

    # 가짜 OpenRouter 응답 시퀀스 준비
    # 턴 1: 도구 호출 -> 최종 응답
    p1_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f1.txt", "content": long_payload})}}],
        prompt_tokens=50,
    )
    p1_done = _make_mock_provider_payload(content="1턴 완료", prompt_tokens=60)

    # 턴 2: 도구 호출 -> 최종 응답
    p2_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f2.txt", "content": long_payload})}}],
        prompt_tokens=80,
    )
    p2_done = _make_mock_provider_payload(content="2턴 완료", prompt_tokens=100)

    # 턴 3: 단순 텍스트 응답 (유저가 ORCA 발화)
    p3_done = _make_mock_provider_payload(content="3턴 완료", prompt_tokens=120)

    # 턴 4: 도구 호출 -> 최종 응답 (임계값 300을 초과하는 prompt_tokens=350 보고)
    p4_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_4", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f4.txt", "content": long_payload})}}],
        prompt_tokens=150,
    )
    p4_done = _make_mock_provider_payload(content="4턴 완료", prompt_tokens=350)

    # 턴 5: 압축 트리거 발생
    # 5-1: 요약 모델 호출 응답 (tools=None)
    p5_summary = _make_mock_provider_payload(
        content="대화 요약: 프로젝트 코드명은 ORCA이며 이전 파일 작업을 마쳤습니다.",
        prompt_tokens=25,
        completion_tokens=15,
    )
    # 5-2: 본 호출 응답 -> 사후 도구 호출 1사이클(write_file file_after.txt)
    p5_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_after", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "file_after.txt", "content": "post_comp"})}}],
        prompt_tokens=30,
        completion_tokens=10,
    )
    # 5-3: 도구 결과 수집 후 최종 텍스트 응답
    p5_done = _make_mock_provider_payload(content="코드명은 ORCA입니다.", prompt_tokens=40, completion_tokens=8)

    responses = [
        p1_tool, p1_done,
        p2_tool, p2_done,
        p3_done,
        p4_tool, p4_done,
        p5_summary, p5_tool, p5_done,
    ]
    transport = SequentialMockTransport(responses)

    fake_key = "test-e2e-r05-key"
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        context_window_tokens=600,  # 임계값 300 토큰으로 낮춤
        quiet_mode=True,
    )

    history = None

    # 턴 1 (유저)
    r1 = agent.run_conversation("1단계 작업", conversation_history=history)
    history = r1["messages"]

    # 턴 2 (유저)
    r2 = agent.run_conversation("2단계 작업", conversation_history=history)
    history = r2["messages"]

    # 턴 3 (유저: 헤드 밖인 3번째 턴에서 ORCA 코드명 전달)
    r3 = agent.run_conversation("3단계 작업: 프로젝트 코드명은 ORCA 입니다.", conversation_history=history)
    history = r3["messages"]

    # 턴 4 (유저)
    r4 = agent.run_conversation("4단계 작업", conversation_history=history)
    history = r4["messages"]
    len_before_comp = len(history)

    # 턴 5 (유저: 코드명이 뭐였지? -> 임계값 초과로 압축 발동)
    r5 = agent.run_conversation("5단계 질문: 코드명이 뭐였지?", conversation_history=history)

    # --- 시나리오 1 검증 ---
    # 압축 발동 확인
    assert "compression" in r5
    assert r5["compression"]["count"] >= 1

    # ① 요약 호출의 요청 messages에 ORCA 문장이 들어 있음을 증명
    # transport.captured_requests 중 요약 호출(8번째 요청 = index 7) 확인
    summary_req = transport.captured_requests[7]
    summary_body = json.loads(summary_req.content.decode("utf-8"))
    summary_prompt_user = summary_body["messages"][1]["content"]
    assert "프로젝트 코드명은 ORCA 입니다" in summary_prompt_user

    # ② 압축 후 본 호출의 messages에는 ORCA를 말한 user 원문 메시지가 없고 요약 메시지가 있음
    # 본 호출(9번째 요청 = index 8)
    post_comp_req = transport.captured_requests[8]
    post_comp_body = json.loads(post_comp_req.content.decode("utf-8"))
    user_msgs_in_post = [m["content"] for m in post_comp_body["messages"] if m.get("role") == "user"]
    assert not any("프로젝트 코드명은 ORCA 입니다" in str(c) for c in user_msgs_in_post)
    # 요약 메시지가 포함되어 있음
    assert any(SUMMARY_PREFIX in str(m.get("content", "")) for m in post_comp_body["messages"])

    # ③ 본 호출 messages 길이가 압축 전 이력보다 줄어듦
    assert len(post_comp_body["messages"]) < len_before_comp + 2

    # --- 시나리오 2 검증 ---
    # 압축 후 도구 호출 1사이클 진행 검증 (file_after.txt 생성)
    assert (w / "file_after.txt").read_text(encoding="utf-8") == "post_comp"
    # 모든 tool 메시지의 tool_call_id가 앞선 assistant와 대응 & 연속 동일 역할 없음
    tool_msgs = [m for m in r5["messages"] if m["role"] == "tool"]
    assert any(m["tool_call_id"] == "call_after" for m in tool_msgs)
    roles = [m["role"] for m in r5["messages"]]
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1]

    # --- 시나리오 3 검증 ---
    # messages[0]의 role == "system"이고 내용 바이트 동일, sessions.system_prompt도 동일
    assert r5["messages"][0]["role"] == "system"
    sess = db.get_session(agent.session_id)
    assert sess["system_prompt"] == r5["messages"][0]["content"]

    # --- 시나리오 4 검증 ---
    # 시작 시 기억 파일에 있던 내용("Known Fact: Initial Memory Item")이 압축 후에도 system 메시지에 유지
    assert "Known Fact: Initial Memory Item" in r5["messages"][0]["content"]

    # --- 시나리오 5 검증 ---
    # 전체 원장 get_messages에 압축 전 모든 원문이 남아 있고(삭제 0), 활성은 압축본과 일치
    all_raw = db.get_messages(agent.session_id)
    assert any("프로젝트 코드명은 ORCA 입니다" in str(m.get("content", "")) for m in all_raw)
    conv = db.get_messages_as_conversation(agent.session_id)
    assert not any("프로젝트 코드명은 ORCA 입니다" in str(m.get("content", "")) for m in conv)
    assert len(conv) == len(r5["messages"]) - 1  # system 제외

    # --- 시나리오 6 검증 ---
    # session_search로 보관된 중간 원문 고유 문구 "ORCA" 검색 시 결과에 나옴
    search_res = db.search_messages("ORCA")
    assert len(search_res) >= 1
    assert any("ORCA" in r["snippet"] or "ORCA" in str(r.get("content", "")) for r in search_res)

    # --- 시나리오 7 검증 ---
    # llm_calls에 요약 호출 행이 있고 error_kind NULL
    calls = db.get_llm_calls(agent.session_id)
    assert len(calls) == 10
    # 8번째 호출이 요약 호출
    assert calls[7]["error_kind"] is None

    # --- 시나리오 9 검증 ---
    # session_id 불변, 토큰 누적에 요약 호출 사용량 포함
    assert sess["id"] == agent.session_id
    assert sess["input_tokens"] > 0
    assert sess["output_tokens"] > 0

    db.close()


def test_060_scenario_08_fallback_on_summary_failure(r05_e2e_env):
    """시나리오 8: 요약 모델 호출이 500 에러로 계속 실패할 때 정적 대체 요약으로 대화 완료 (stopped_reason == 'completed')."""
    db = r05_e2e_env["db"]

    long_text = "긴 데이터 내용 " * 30

    p1_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f1.txt", "content": "1"})}}],
        prompt_tokens=50,
    )
    p1_done = _make_mock_provider_payload(content="1턴 완료", prompt_tokens=60)
    p2_done = _make_mock_provider_payload(content="2턴 완료: " + long_text, prompt_tokens=100)
    p3_done = _make_mock_provider_payload(content="3턴 완료: " + long_text, prompt_tokens=120)
    p4_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_4", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f4.txt", "content": "4"})}}],
        prompt_tokens=150,
    )
    p4_done = _make_mock_provider_payload(content="4턴 완료", prompt_tokens=350)
    # 요약 호출 시 500 에러 발생 -> 정적 대체 요약으로 전환
    p5_err = 500
    # 본 호출 정상 완료
    p5_done = _make_mock_provider_payload(content="폴백 요약 기반 답변 완료")

    transport = SequentialMockTransport([
        p1_tool, p1_done,
        p2_done,
        p3_done,
        p4_tool, p4_done,
        p5_err, p5_done,
    ])
    client = OpenRouterClient(api_key="test-key", model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        context_window_tokens=600,
        quiet_mode=True,
    )

    history = None
    r1 = agent.run_conversation("1단계 작업", conversation_history=history)
    history = r1["messages"]
    r2 = agent.run_conversation("2단계 작업: " + long_text, conversation_history=history)
    history = r2["messages"]
    r3 = agent.run_conversation("3단계 작업: " + long_text, conversation_history=history)
    history = r3["messages"]
    r4 = agent.run_conversation("4단계 작업", conversation_history=history)
    history = r4["messages"]

    r5 = agent.run_conversation("5단계 질문", conversation_history=history)

    assert r5["stopped_reason"] == "completed"
    assert "compression" in r5
    assert r5["compression"]["fallback"] == 1
    assert r5["final_response"] == "폴백 요약 기반 답변 완료"

    # llm_calls에 실패한 요약 호출 기록 확인
    calls = db.get_llm_calls(agent.session_id)
    assert any(c["status_code"] == 500 for c in calls)
    db.close()


def test_060_scenario_10_zero_leakage_in_db(r05_e2e_env, monkeypatch):
    """시나리오 10: 환경변수에 가짜 키(sk-or-v1-...)를 두고 전체 대화 실행 후 DB 전체 덤프에서 키 미저장(Zero Leakage) 검증."""
    db = r05_e2e_env["db"]

    # 런타임 동적 결합으로 gitleaks 정적 패턴 우회
    fake_secret = "sk-or-v1-" + "e2e_secret_token_never_leak_" + "9876543210"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_secret)

    long_payload = "X" * 200

    p1_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f1.txt", "content": long_payload})}}],
        prompt_tokens=50,
    )
    p1_done = _make_mock_provider_payload(content="1턴 완료", prompt_tokens=60)
    p2_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f2.txt", "content": long_payload})}}],
        prompt_tokens=80,
    )
    p2_done = _make_mock_provider_payload(content="2턴 완료", prompt_tokens=100)
    p3_done = _make_mock_provider_payload(content="3턴 완료", prompt_tokens=120)
    p4_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_4", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f4.txt", "content": long_payload})}}],
        prompt_tokens=150,
    )
    p4_done = _make_mock_provider_payload(content="4턴 완료", prompt_tokens=350)
    p5_sum = _make_mock_provider_payload(content="대화 요약 완료")
    p5_done = _make_mock_provider_payload(content="최종 응답")

    transport = SequentialMockTransport([
        p1_tool, p1_done,
        p2_tool, p2_done,
        p3_done,
        p4_tool, p4_done,
        p5_sum, p5_done,
    ])
    client = OpenRouterClient(api_key=fake_secret, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        context_window_tokens=600,
        quiet_mode=True,
    )

    history = None
    for i in range(1, 5):
        r = agent.run_conversation(f"{i}단계", conversation_history=history)
        history = r["messages"]

    _ = agent.run_conversation("5단계", conversation_history=history)

    # DB 전체(messages, llm_calls, sessions) 덤프 검사
    cursor = db._conn.cursor()
    all_rows_str = ""
    for table in ["sessions", "messages", "llm_calls"]:
        cursor.execute(f"SELECT * FROM {table}")
        rows = cursor.fetchall()
        for row in rows:
            all_rows_str += str(dict(row)) + "\n"

    # 가짜 키 문자열이 DB 어디에도 저장되지 않았음을 검증
    assert fake_secret not in all_rows_str
    assert "***" in all_rows_str or fake_secret not in all_rows_str
    db.close()


def test_060_scenario_11_migration_and_compression(tmp_path, monkeypatch):
    """시나리오 11: v3 DB를 열어 대화 실행 시 v4 마이그레이션이 수행되고 정상 압축 동작 및 기존 행 보존."""
    import sqlite3

    db_path = tmp_path / "legacy_v3.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (3);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            mode TEXT
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT
        );

        INSERT INTO sessions (id, source, started_at) VALUES ('s_legacy', 'cli', 1000.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_legacy', 'user', 'v3 유저 요청', 1001.0);
        INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s_legacy', 'assistant', 'v3 응답', 1002.0);
        """
    )
    conn.commit()
    conn.close()

    # SessionDB로 열어 마이그레이션 확인
    db = SessionDB(db_path)
    assert db.schema_version == 4

    long_payload = "X" * 200

    p1_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f1.txt", "content": long_payload})}}],
        prompt_tokens=50,
    )
    p1_done = _make_mock_provider_payload(content="1턴 완료", prompt_tokens=60)
    p2_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f2.txt", "content": long_payload})}}],
        prompt_tokens=80,
    )
    p2_done = _make_mock_provider_payload(content="2턴 완료", prompt_tokens=100)
    p3_done = _make_mock_provider_payload(content="3턴 완료", prompt_tokens=120)
    p4_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_4", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "f4.txt", "content": long_payload})}}],
        prompt_tokens=150,
    )
    p4_done = _make_mock_provider_payload(content="4턴 완료", prompt_tokens=350)
    p5_sum = _make_mock_provider_payload(content="대화 요약 완료")
    p5_done = _make_mock_provider_payload(content="최종 응답")

    transport = SequentialMockTransport([
        p1_tool, p1_done,
        p2_tool, p2_done,
        p3_done,
        p4_tool, p4_done,
        p5_sum, p5_done,
    ])
    client = OpenRouterClient(api_key="test-key", model=DEFAULT_REAL_MODEL, transport=transport)

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    memory_store = MemoryStore(memory_dir=mem_dir)
    dispatcher = create_default_dispatcher(memory_store=memory_store, session_db=db)

    agent = HermesAgent(
        session_db=db,
        memory_store=memory_store,
        dispatcher=dispatcher,
        llm_client=client,
        context_window_tokens=600,
        quiet_mode=True,
    )

    history = None
    for i in range(1, 5):
        r = agent.run_conversation(f"{i}단계", conversation_history=history)
        history = r["messages"]

    r5 = agent.run_conversation("5단계", conversation_history=history)
    assert "compression" in r5
    assert r5["compression"]["count"] >= 1

    # 기존 v3 레거시 데이터가 온전히 보존되어 있는지 확인
    v3_msgs = [m for m in db.get_messages("s_legacy")]
    assert len(v3_msgs) == 2
    assert v3_msgs[0]["content"] == "v3 유저 요청"
    assert v3_msgs[0]["active"] == 1
    assert v3_msgs[0]["compacted"] == 0

    db.close()


# =============================================================================
# 로드맵 06: 구현지시서 050 통합 E2E 검증 시나리오 (시나리오 1 ~ 8)
# =============================================================================

import hashlib
from pathlib import Path
from hermes_core.agent.prompt_builder import SKILLS_GUIDANCE


def _setup_r06_skills(home: Path) -> Path:
    """로드맵 06 검증용 기본 스킬 디렉터리 및 release-checklist 스킬 배치."""
    skills_dir = home / "skills"
    skill_folder = skills_dir / "release-checklist"
    skill_folder.mkdir(parents=True, exist_ok=True)
    (skill_folder / "SKILL.md").write_text(
        "---\nname: release-checklist\ndescription: Checklist for releases\n---\n## Steps\nSTEP-7: tag the build\n",
        encoding="utf-8",
    )
    ref_folder = skill_folder / "references"
    ref_folder.mkdir(parents=True, exist_ok=True)
    (ref_folder / "notes.md").write_text("NOTE-ALPHA\n", encoding="utf-8")
    return skills_dir


def _snapshot_home_and_skills(hermes_home: Path) -> dict:
    """스킬 디렉터리 내 파일 해시와 홈 직하위 항목 목록의 불변성 검증용 스냅샷."""
    skills_dir = hermes_home / "skills"
    skill_hashes = {}
    if skills_dir.exists():
        for p in sorted(skills_dir.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(skills_dir))
                content = p.read_bytes()
                skill_hashes[rel] = hashlib.sha256(content).hexdigest()
    top_level_names = set(p.name for p in hermes_home.iterdir())
    return {"top_level": top_level_names, "skill_hashes": skill_hashes}


def _assert_no_key_in_db(db: SessionDB, fake_key: str):
    """DB 전체(sessions, messages, llm_calls) 테이블에 가짜 키 문자열이 저장되지 않았음을 검증."""
    cursor = db._conn.cursor()
    all_rows_str = ""
    for table in ["sessions", "messages", "llm_calls"]:
        cursor.execute(f"SELECT * FROM {table}")
        rows = cursor.fetchall()
        for row in rows:
            all_rows_str += str(dict(row)) + "\n"
    assert fake_key not in all_rows_str


def test_r06_050_scenario_01_and_06_skills_list_and_view_cycle(r05_e2e_env):
    """시나리오 1 & 6: skills_list -> skill_view -> 응답 순서 검증 및 시스템 지시문 불변성(SKILLS_GUIDANCE 포함)."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    _setup_r06_skills(home)

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s01"

    p1 = _make_mock_provider_payload(
        tool_calls=[{"id": "call_list_1", "type": "function", "function": {"name": "skills_list", "arguments": "{}"}}],
        prompt_tokens=40,
        completion_tokens=10,
    )
    p2 = _make_mock_provider_payload(
        tool_calls=[{
            "id": "call_view_1",
            "type": "function",
            "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist"})},
        }],
        prompt_tokens=70,
        completion_tokens=15,
    )
    p3 = _make_mock_provider_payload(
        content="릴리즈 체크리스트 확인 완료: STEP-7 태그 생성",
        prompt_tokens=120,
        completion_tokens=10,
    )

    transport = SequentialMockTransport([p1, p2, p3])
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("체크리스트 스킬을 찾아서 확인해줘")
    assert res["stopped_reason"] == "completed"
    assert res["iterations"] == 3
    assert res["final_response"] == "릴리즈 체크리스트 확인 완료: STEP-7 태그 생성"

    # 전송된 요청 본문에서 messages 검증
    assert len(transport.captured_requests) == 3
    req1 = json.loads(transport.captured_requests[0].content.decode("utf-8"))
    req2 = json.loads(transport.captured_requests[1].content.decode("utf-8"))
    req3 = json.loads(transport.captured_requests[2].content.decode("utf-8"))

    # 2번째 요청: skills_list의 tool 메시지에 release-checklist가 있고 STEP-7은 없음
    req2_msgs = req2["messages"]
    tool_msg_1 = next(m for m in req2_msgs if m.get("role") == "tool" and m.get("tool_call_id") == "call_list_1")
    assert "release-checklist" in tool_msg_1["content"]
    assert "STEP-7" not in tool_msg_1["content"]

    # 3번째 요청: skill_view의 tool 메시지에 STEP-7이 있음
    req3_msgs = req3["messages"]
    tool_msg_2 = next(m for m in req3_msgs if m.get("role") == "tool" and m.get("tool_call_id") == "call_view_1")
    assert "STEP-7" in tool_msg_2["content"]

    # 각 tool_call_id가 짝을 이룸
    asst_1 = next(m for m in req2_msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    assert asst_1["tool_calls"][0]["id"] == tool_msg_1["tool_call_id"] == "call_list_1"

    asst_msgs_3 = [m for m in req3_msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst_msgs_3[-1]["tool_calls"][0]["id"] == tool_msg_2["tool_call_id"] == "call_view_1"

    # 시나리오 6: 각 요청의 첫 메시지가 role == 'system'이고 서로 바이트 동일, SKILLS_GUIDANCE 포함
    sys_content_1 = req1["messages"][0]["content"]
    for req in [req1, req2, req3]:
        assert req["messages"][0]["role"] == "system"
        assert req["messages"][0]["content"] == sys_content_1
        assert SKILLS_GUIDANCE in sys_content_1

    session_row = db.get_session(agent.session_id)
    assert sys_content_1 == session_row["system_prompt"]

    # 시나리오 8 검증
    _assert_no_key_in_db(db, fake_key)
    db.close()


def test_r06_050_scenario_02_skill_view_linked_file(r05_e2e_env):
    """시나리오 2: 1번 흐름에 skill_view(name='release-checklist', file_path='references/notes.md')를 더함."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    _setup_r06_skills(home)

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s02"

    p1 = _make_mock_provider_payload(
        tool_calls=[{"id": "call_list_1", "type": "function", "function": {"name": "skills_list", "arguments": "{}"}}],
    )
    p2 = _make_mock_provider_payload(
        tool_calls=[{
            "id": "call_view_1",
            "type": "function",
            "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist"})},
        }],
    )
    p3 = _make_mock_provider_payload(
        tool_calls=[{
            "id": "call_ref_1",
            "type": "function",
            "function": {
                "name": "skill_view",
                "arguments": json.dumps({"name": "release-checklist", "file_path": "references/notes.md"}),
            },
        }],
    )
    p4 = _make_mock_provider_payload(content="노트 확인 완료: NOTE-ALPHA")

    transport = SequentialMockTransport([p1, p2, p3, p4])
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("체크리스트와 참조 노트 확인해줘")
    assert res["stopped_reason"] == "completed"
    assert res["iterations"] == 4

    req4 = json.loads(transport.captured_requests[3].content.decode("utf-8"))
    tool_ref = next(m for m in req4["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "call_ref_1")
    assert "NOTE-ALPHA" in tool_ref["content"]

    _assert_no_key_in_db(db, fake_key)
    db.close()


def test_r06_050_scenario_03_traversal_and_protection_rejected(r05_e2e_env):
    """시나리오 3: skill_view 경로 이탈 및 보호 대상 접근 시도 3회 거부 후 루프 지속 및 정상 완료."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    _setup_r06_skills(home)

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s03"

    p1 = _make_mock_provider_payload(
        tool_calls=[{"id": "call_bad_1", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "../state.db"})}}],
    )
    p2 = _make_mock_provider_payload(
        tool_calls=[{"id": "call_bad_2", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "../memories/MEMORY.md"})}}],
    )
    p3 = _make_mock_provider_payload(
        tool_calls=[{"id": "call_bad_3", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist", "file_path": "../../../state.db"})}}],
    )
    p4 = _make_mock_provider_payload(content="비정상 접근 거부 확인")

    transport = SequentialMockTransport([p1, p2, p3, p4])
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("보호 파일 읽어봐")
    assert res["stopped_reason"] == "completed"
    assert res["iterations"] == 4

    req4 = json.loads(transport.captured_requests[3].content.decode("utf-8"))
    bad_tools = [m for m in req4["messages"] if m.get("role") == "tool"]
    assert len(bad_tools) == 3

    for t in bad_tools:
        assert '"success": false' in t["content"].lower()
        assert "SQLite format 3" not in t["content"]
        assert "Known Fact: Initial Memory Item" not in t["content"]

    _assert_no_key_in_db(db, fake_key)
    db.close()


def test_r06_050_scenario_04_no_skills_dir(r05_e2e_env):
    """시나리오 4: 스킬 폴더 자체가 없는 상태에서 skills_list 호출 시 빈 목록 반환 및 디렉터리 미생성 검증."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    skills_dir = home / "skills"
    assert not skills_dir.exists()

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s04"

    p1 = _make_mock_provider_payload(
        tool_calls=[{"id": "call_list_empty", "type": "function", "function": {"name": "skills_list", "arguments": "{}"}}],
    )
    p2 = _make_mock_provider_payload(content="스킬 없음 확인 완료")

    transport = SequentialMockTransport([p1, p2])
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    res = agent.run_conversation("스킬 목록 보여줘")
    assert res["stopped_reason"] == "completed"
    assert res["iterations"] == 2

    req2 = json.loads(transport.captured_requests[1].content.decode("utf-8"))
    tool_msg = next(m for m in req2["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "call_list_empty")
    data = json.loads(tool_msg["content"])
    assert data.get("skills") == []
    assert data.get("count") == 0

    # 대화 뒤에도 skills/ 폴더가 존재하지 않음 (폴더 미생성 원칙)
    assert not skills_dir.exists()

    _assert_no_key_in_db(db, fake_key)
    db.close()


def test_r06_050_scenario_05_skills_immutability(r05_e2e_env):
    """시나리오 5: 시나리오 1~3 실행 전후 스킬 폴더(경로+내용 해시) 및 HERMES_HOME 직하위 항목 불변 검증."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    _setup_r06_skills(home)

    snap_before = _snapshot_home_and_skills(home)

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s05"

    # 시나리오 1~3 흐름을 순차적으로 한 번씩 실행
    # 1. skills_list -> skill_view
    t1 = SequentialMockTransport([
        _make_mock_provider_payload(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "skills_list", "arguments": "{}"}}]),
        _make_mock_provider_payload(tool_calls=[{"id": "c2", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist"})}}]),
        _make_mock_provider_payload(content="flow 1 done"),
    ])
    a1 = HermesAgent(session_db=db, memory_store=r05_e2e_env["memory_store"], dispatcher=r05_e2e_env["dispatcher"], llm_client=OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=t1), quiet_mode=True)
    a1.run_conversation("flow 1")

    # 2. skill_view linked file
    t2 = SequentialMockTransport([
        _make_mock_provider_payload(tool_calls=[{"id": "c3", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist", "file_path": "references/notes.md"})}}]),
        _make_mock_provider_payload(content="flow 2 done"),
    ])
    a2 = HermesAgent(session_db=db, memory_store=r05_e2e_env["memory_store"], dispatcher=r05_e2e_env["dispatcher"], llm_client=OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=t2), quiet_mode=True)
    a2.run_conversation("flow 2")

    # 3. bad skill_views
    t3 = SequentialMockTransport([
        _make_mock_provider_payload(tool_calls=[{"id": "c4", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "../state.db"})}}]),
        _make_mock_provider_payload(tool_calls=[{"id": "c5", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "../memories/MEMORY.md"})}}]),
        _make_mock_provider_payload(tool_calls=[{"id": "c6", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist", "file_path": "../../../state.db"})}}]),
        _make_mock_provider_payload(content="flow 3 done"),
    ])
    a3 = HermesAgent(session_db=db, memory_store=r05_e2e_env["memory_store"], dispatcher=r05_e2e_env["dispatcher"], llm_client=OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=t3), quiet_mode=True)
    a3.run_conversation("flow 3")

    snap_after = _snapshot_home_and_skills(home)

    assert snap_before["skill_hashes"] == snap_after["skill_hashes"]
    assert snap_before["top_level"] == snap_after["top_level"]

    _assert_no_key_in_db(db, fake_key)
    db.close()


def test_r06_050_scenario_07_compression_prunes_large_skill_content(r05_e2e_env):
    """시나리오 7: 대용량(약 19,000자) 스킬 본문 조회 후 압축 발동 시 본문 가지치기/요약 치환 및 헤드 바이트 동일성 검증."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    skills_dir = home / "skills"
    skill_folder = skills_dir / "heavy-skill"
    skill_folder.mkdir(parents=True, exist_ok=True)

    # 약 19,000자 본문 (ASCII)
    heavy_body = "HEAVY-START\n" + ("A" * 18900) + "\nHEAVY-END\n"
    assert 18900 < len(heavy_body) <= 19500
    (skill_folder / "SKILL.md").write_text(
        "---\nname: heavy-skill\ndescription: A very heavy skill for compression testing\n---\n" + heavy_body,
        encoding="utf-8",
    )

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s07"

    # 헤드(처음 3개 메시지)와 꼬리(최근 6개 이상 메시지) 사이에 skill_view가 위치하도록 턴 구성:
    # 턴 1, 2: 헤드 구간 채우기
    p1_done = _make_mock_provider_payload(content="1턴 완료", prompt_tokens=40)
    p2_done = _make_mock_provider_payload(content="2턴 완료", prompt_tokens=60)

    # 턴 3: 중간(Middle) 구간에서 대용량 skill_view 도구 호출 -> 결과 수집
    p3_tool = _make_mock_provider_payload(
        tool_calls=[{"id": "call_h1", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "heavy-skill"})}}],
        prompt_tokens=80,
    )
    p3_done = _make_mock_provider_payload(content="3턴 완료", prompt_tokens=4800)

    # 턴 4, 5, 6: 꼬리(Tail) 구간 메시지 채우기 (6턴에서 prompt_tokens=350 보고하여 임계값 300 초과)
    p4_done = _make_mock_provider_payload(content="4턴 완료", prompt_tokens=100)
    p5_done = _make_mock_provider_payload(content="5턴 완료", prompt_tokens=120)
    p6_done = _make_mock_provider_payload(content="6턴 완료", prompt_tokens=350)

    # 턴 7: 토큰 수 임계값(600 * 0.5 = 300) 초과로 압축 파이프라인 트리거 -> 요약 모델 호출 -> 최종 응답
    p7_sum = _make_mock_provider_payload(content="대용량 스킬 조회 및 과거 진행 내역 요약 완료", prompt_tokens=25, completion_tokens=15)
    p7_done = _make_mock_provider_payload(content="7턴 완료", prompt_tokens=40, completion_tokens=10)

    transport = SequentialMockTransport([
        p1_done,
        p2_done,
        p3_tool, p3_done,
        p4_done,
        p5_done,
        p6_done,
        p7_sum, p7_done,
    ])
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        context_window_tokens=600,
        quiet_mode=True,
    )

    history = None
    for i in range(1, 7):
        r = agent.run_conversation(f"{i}단계", conversation_history=history)
        history = r["messages"]

    r7 = agent.run_conversation("7단계", conversation_history=history)
    assert "compression" in r7
    assert r7["compression"]["count"] >= 1

    # 압축 뒤 요청(p7_done 요청) 본문에서 어떤 tool 메시지도 19,000자 본문을 그대로 갖지 않음을 검증
    # captured_requests 중 마지막 요청은 p7_done 요청
    last_req = json.loads(transport.captured_requests[-1].content.decode("utf-8"))
    for msg in last_req["messages"]:
        if msg.get("role") == "tool":
            assert heavy_body not in msg.get("content", "")
            # 가지치기 표지가 있거나 요약으로 교체됨
            assert "[Old tool output cleared: skill_view]" in msg.get("content", "") or len(msg.get("content", "")) < 1000

    # 첫 시스템 메시지 바이트 동일성 검증
    first_req = json.loads(transport.captured_requests[0].content.decode("utf-8"))
    assert last_req["messages"][0]["role"] == "system"
    assert last_req["messages"][0]["content"] == first_req["messages"][0]["content"]
    assert SKILLS_GUIDANCE in last_req["messages"][0]["content"]

    _assert_no_key_in_db(db, fake_key)
    db.close()


def test_r06_050_scenario_08_zero_leakage_in_db(r05_e2e_env):
    """시나리오 8: 시나리오 1~7 형태의 대화 실행 후 DB 전체(messages, llm_calls, sessions)에 가짜 키가 없음을 검증."""
    home = r05_e2e_env["home"]
    db = r05_e2e_env["db"]
    _setup_r06_skills(home)

    fake_key = "sk-or-v1-" + "r06-e2e-fake-key-s08-strictly-zero-leak"

    p1 = _make_mock_provider_payload(
        tool_calls=[{"id": "c_list", "type": "function", "function": {"name": "skills_list", "arguments": "{}"}}],
    )
    p2 = _make_mock_provider_payload(
        tool_calls=[{"id": "c_view", "type": "function", "function": {"name": "skill_view", "arguments": json.dumps({"name": "release-checklist"})}}],
    )
    p3 = _make_mock_provider_payload(content="모든 스킬 작업 완료")

    transport = SequentialMockTransport([p1, p2, p3])
    client = OpenRouterClient(api_key=fake_key, model=DEFAULT_REAL_MODEL, transport=transport)

    agent = HermesAgent(
        session_db=db,
        memory_store=r05_e2e_env["memory_store"],
        dispatcher=r05_e2e_env["dispatcher"],
        llm_client=client,
        quiet_mode=True,
    )

    _ = agent.run_conversation("스킬 전체 확인")

    _assert_no_key_in_db(db, fake_key)
    db.close()




