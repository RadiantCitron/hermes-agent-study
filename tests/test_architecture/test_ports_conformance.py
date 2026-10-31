"""포트 적합성 시험 (시나리오 28)."""

import inspect

from hermes_core.agent.agent import MockLLMClient
from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.ports import LLMPort, ToolExecutor, TranscriptStore
from hermes_core.kernel.tool_call import ToolCall
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher


def test_scenario_28_ports_conformance() -> None:
    """시나리오 28: 포트 적합성 및 실제 호출 바인딩 검증."""
    # 1. issubclass (runtime_checkable)
    assert issubclass(SessionDB, TranscriptStore), "SessionDB must satisfy TranscriptStore"
    assert issubclass(ToolDispatcher, ToolExecutor), "ToolDispatcher must satisfy ToolExecutor"
    assert issubclass(MockLLMClient, LLMPort), "MockLLMClient must satisfy LLMPort"
    assert issubclass(OpenRouterClient, LLMPort), "OpenRouterClient must satisfy LLMPort"

    # 2. inspect.signature().bind() 호출 형태 검증
    # append_message
    sig_append = inspect.signature(SessionDB.append_message)
    # (session_id, role, content)
    sig_append.bind(None, "s1", "user", "hi")
    # (session_id, role, content, tool_calls)
    sig_append.bind(None, "s1", "assistant", content=None, tool_calls=[])
    # (session_id, role, content, tool_call_id, tool_name)
    sig_append.bind(None, "s1", "tool", content="ok", tool_call_id="c1", tool_name="echo")

    # archive_and_compact
    sig_archive = inspect.signature(SessionDB.archive_and_compact)
    sig_archive.bind(None, "s1", [], summary_index=0)

    # update_token_counts
    sig_tokens = inspect.signature(SessionDB.update_token_counts)
    sig_tokens.bind(None, "s1", input_tokens=10, output_tokens=5)

    # chat_completion
    sig_mock_chat = inspect.signature(MockLLMClient.chat_completion)
    sig_mock_chat.bind(None, messages=[], tools=[])

    sig_openrouter_chat = inspect.signature(OpenRouterClient.chat_completion)
    sig_openrouter_chat.bind(None, messages=[], tools=[])

    # dispatch
    sig_dispatch = inspect.signature(ToolDispatcher.dispatch)
    fake_call = ToolCall(name="test", args={}, task_id="t1", scope=TurnScope(session_id="s1"))
    sig_dispatch.bind(None, fake_call)
