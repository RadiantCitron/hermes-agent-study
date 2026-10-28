"""도구 레지스트리 및 디스패처 단일 진입로 (Tool Dispatcher).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/model_tools.py (handle_function_call)
- 핵심 원리:
  - O(1) 단일 진입로: LLM의 tool_calls가 들어오면 if-elif 체인 대신 레지스트리 맵을 통해 일관된 방식으로 도구 실행.
  - 호출 전 인자 검증: 등록된 스키마로 필수·미정의·타입·enum을 검사하고, 위반이면 도구를 부르지 않고 실패 메시지를 반환.
  - 전역 의존성 해소: MemoryStore와 SessionDB를 명시적으로 주입받아 격리된 인스턴스를 유지.
"""

from collections.abc import Callable
import json
from typing import Any, Optional

from hermes_core.session_db import SessionDB
from hermes_core.tools.file_tools import (
    PATCH_SCHEMA,
    READ_FILE_SCHEMA,
    WRITE_FILE_SCHEMA,
    patch_tool,
    read_file_tool,
    write_file_tool,
)
from hermes_core.tools.memory_tool import MEMORY_SCHEMA, MemoryStore, memory_tool
from hermes_core.tools.session_search_tool import SESSION_SEARCH_SCHEMA, session_search_tool
from hermes_core.tools.skills_tool import SKILLS_LIST_SCHEMA, skills_list_tool
from hermes_core.tools.terminal_tool import TERMINAL_SCHEMA, terminal_tool


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _validate_arguments(schema: dict[str, Any], args: Any) -> Optional[str]:
    """등록된 스키마(function.parameters) 기준으로 인자를 검사. 위반 사유를 반환하고, 통과하면 None.

    검사: 필수 인자, 정의되지 않은 인자, 타입(integer/number에 bool 불인정), enum.
    스키마나 parameters가 비어 있으면 검증하지 않는다(기존 호환). 알 수 없는 타입 이름은 타입 검사를 건너뛴다.
    """
    params = (schema or {}).get("function", {}).get("parameters")
    if not params:
        return None
    if not isinstance(args, dict):
        return "arguments must be an object"

    properties = params.get("properties")
    for name in params.get("required", []):
        if name not in args:
            return f"missing required argument '{name}'"
    if properties is not None:
        for name in args:
            if name not in properties:
                return f"unexpected argument '{name}'"

    for name, value in args.items():
        spec = (properties or {}).get(name, {})
        expected = spec.get("type")
        allowed_types = _JSON_TYPES.get(expected)
        if allowed_types:
            is_bool_for_number = isinstance(value, bool) and expected in ("integer", "number")
            if is_bool_for_number or not isinstance(value, allowed_types):
                return f"argument '{name}' must be of type {expected}"
        if "enum" in spec and value not in spec["enum"]:
            return f"argument '{name}' must be one of {spec['enum']}"
    return None


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

        reason = _validate_arguments(self._schemas.get(function_name, {}), function_args)
        if reason:
            return json.dumps(
                {"success": False, "error": f"Invalid arguments for tool '{function_name}': {reason}"},
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

    # 4. Read File 도구 등록
    dispatcher.register_tool("read_file", read_file_tool, READ_FILE_SCHEMA)

    # 5. Write File 도구 등록
    dispatcher.register_tool("write_file", write_file_tool, WRITE_FILE_SCHEMA)

    # 6. Patch 도구 등록
    dispatcher.register_tool("patch", patch_tool, PATCH_SCHEMA)

    # 7. Skills List 도구 등록
    dispatcher.register_tool("skills_list", skills_list_tool, SKILLS_LIST_SCHEMA)

    return dispatcher
