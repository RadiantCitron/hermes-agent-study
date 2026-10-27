"""도구 레지스트리 및 디스패처 단일 진입로 (Tool Dispatcher).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/model_tools.py (handle_function_call)
- 핵심 원리:
  - O(1) 단일 진입로: LLM의 tool_calls가 들어오면 if-elif 체인 대신 레지스트리 맵을 통해 일관된 방식으로 도구 실행.
  - 전역 의존성 해소: MemoryStore와 SessionDB를 명시적으로 주입받아 격리된 인스턴스를 유지.
"""

from collections.abc import Callable
import json
from typing import Any, Optional

from hermes_core.session_db import SessionDB
from hermes_core.tools.memory_tool import MEMORY_SCHEMA, MemoryStore, memory_tool
from hermes_core.tools.session_search_tool import SESSION_SEARCH_SCHEMA, session_search_tool
from hermes_core.tools.terminal_tool import TERMINAL_SCHEMA, terminal_tool


class ToolDispatcher:
    """도구 등록 및 실행 디스패처 클래스."""

    def __init__(self):
        self._handlers: dict[str, Callable[..., str]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register_tool(self, name: str, handler: Callable[..., str], schema: dict[str, Any]) -> None:
        """새로운 도구 핸들러와 OpenAI 규격 스키마 등록."""
        self._handlers[name] = handler
        self._schemas[name] = schema

    def get_tools_schema(self) -> list[dict[str, Any]]:
        """LLM 호출 시 전달할 모든 등록된 도구의 OpenAI 함수 스키마 목록 반환."""
        return list(self._schemas.values())

    def handle_function_call(
        self,
        function_name: str,
        function_args: dict[str, Any],
        task_id: Optional[str] = None,
    ) -> str:
        """단일 도구 실행 진입로. 등록된 도구를 찾아 호출하고 결과를 문자열로 반환."""
        handler = self._handlers.get(function_name)
        if not handler:
            return json.dumps(
                {"success": False, "error": f"Unknown tool '{function_name}'."},
                ensure_ascii=False,
            )

        try:
            return handler(**function_args)
        except TypeError as e:
            return json.dumps(
                {"success": False, "error": f"Invalid arguments for tool '{function_name}': {str(e)}"},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {"success": False, "error": f"Tool '{function_name}' failed: {str(e)}"},
                ensure_ascii=False,
            )


def create_default_dispatcher(
    memory_store: Optional[MemoryStore] = None,
    session_db: Optional[SessionDB] = None,
) -> ToolDispatcher:
    """Hermes 핵심 도구 3종(memory, session_search, terminal)이 등록된 기본 디스패처 생성."""
    dispatcher = ToolDispatcher()

    # 1. Memory 도구 등록
    def _memory_handler(**kwargs):
        return memory_tool(store=memory_store, **kwargs)

    dispatcher.register_tool("memory", _memory_handler, MEMORY_SCHEMA)

    # 2. Session Search 도구 등록
    def _search_handler(**kwargs):
        return session_search_tool(db=session_db, **kwargs)

    dispatcher.register_tool("session_search", _search_handler, SESSION_SEARCH_SCHEMA)

    # 3. Terminal 도구 등록
    dispatcher.register_tool("terminal", terminal_tool, TERMINAL_SCHEMA)

    return dispatcher
