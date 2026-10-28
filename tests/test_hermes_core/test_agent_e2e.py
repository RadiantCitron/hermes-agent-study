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

