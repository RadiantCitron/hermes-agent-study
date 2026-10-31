"""도구 레지스트리 및 디스패처 단일 진입로 (Tool Dispatcher).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/model_tools.py (handle_function_call)
- 핵심 원리:
  - O(1) 단일 진입로: LLM의 tool_calls가 들어오면 if-elif 체인 대신 레지스트리 맵을 통해 일관된 방식으로 도구 실행.
  - 호출 전 인자 검증: 등록된 스키마로 필수·미정의·타입·enum을 검사하고, 위반이면 도구를 부르지 않고 실패 메시지를 반환.
  - 전역 의존성 해소: MemoryStore와 SessionDB를 명시적으로 주입받아 격리된 인스턴스를 유지.
"""

from collections.abc import Callable, Collection
import json
import logging
from typing import Any, Optional

from hermes_core.constants import (
    CHECKPOINT_PRE_RECORD_FAILED_MESSAGE,
    CHECKPOINT_REFUSED_HINT,
    CHECKPOINT_WORKSPACE_MISMATCH_MESSAGE,
    CHILD_TOOL_NAMES,
)
from hermes_core.kernel.contract import (
    FailurePolicy,
    MiddlewarePhase,
    PHASE_FAILURE_POLICY,
)
from hermes_core.kernel.tool_call import ToolCall, ToolMiddleware
from hermes_core.tools.ports import SessionSearchPort
from hermes_core.tools.approval import Approver
from hermes_core.tools.checkpoint import Checkpointer, CheckpointLimitError
from hermes_core.tools.file_tools import (
    PATCH_SCHEMA,
    READ_FILE_SCHEMA,
    SEARCH_FILES_SCHEMA,
    WRITE_FILE_SCHEMA,
    patch_tool,
    read_file_tool,
    search_files_tool,
    write_file_tool,
)
from hermes_core.tools.path_guard import resolve_workspace
from hermes_core.tools.memory_tool import MEMORY_SCHEMA, MemoryStore, memory_tool
from hermes_core.tools.session_search_tool import SESSION_SEARCH_SCHEMA, session_search_tool
from hermes_core.tools.skills_tool import (
    SKILLS_LIST_SCHEMA,
    SKILL_VIEW_SCHEMA,
    skills_list_tool,
    skill_view_tool,
)
from hermes_core.tools.terminal_tool import TERMINAL_SCHEMA, terminal_tool


logger = logging.getLogger(__name__)

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
        self._middlewares: list[tuple[MiddlewarePhase, int, str, ToolMiddleware]] = []
        self._middleware_counter = 0

    def register_tool(self, name: str, handler: Callable[..., str], schema: dict[str, Any]) -> None:
        """새로운 도구 핸들러와 OpenAI 규격 스키마 등록."""
        self._handlers[name] = handler
        self._schemas[name] = schema

    def add_middleware(
        self,
        middleware: ToolMiddleware,
        phase: MiddlewarePhase = MiddlewarePhase.POLICY,
        name: Optional[str] = None,
    ) -> None:
        """도구 실행 파이프라인 미들웨어 등록."""
        mw_name = name or getattr(middleware, "__name__", type(middleware).__name__)
        self._middlewares.append((phase, self._middleware_counter, mw_name, middleware))
        self._middleware_counter += 1

    def get_tools_schema(self) -> list[dict[str, Any]]:
        """LLM 호출 시 전달할 모든 등록된 도구의 OpenAI 함수 스키마 목록 반환."""
        return list(self._schemas.values())

    def _execute(self, call: ToolCall) -> str:
        """체인의 가장 안쪽 도구 핸들러 실행 진입점."""
        function_name = call.name
        function_args = call.args

        if call.parse_error is not None:
            return json.dumps(
                {"success": False, "error": f"Invalid JSON arguments for tool '{function_name}': {call.parse_error}"},
                ensure_ascii=False,
            )

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

    def dispatch(self, call: ToolCall) -> str:
        """단일 도구 실행 진입로. 미들웨어 체인을 거쳐 도구를 호출하고 결과를 문자열로 반환."""
        # (phase.value, 등록순서)로 정렬
        sorted_mws = sorted(self._middlewares, key=lambda item: (item[0].value, item[1]))

        def _make_chain(index: int) -> Callable[[ToolCall], str]:
            if index >= len(sorted_mws):
                return self._execute

            phase, _, name, mw = sorted_mws[index]
            policy = PHASE_FAILURE_POLICY.get(phase, FailurePolicy.CLOSED)
            next_fn = _make_chain(index + 1)

            def _wrapped(c: ToolCall) -> str:
                called_next = False
                inner_result: Optional[str] = None
                inner_exc: Optional[Exception] = None

                def _instrumented_next(next_c: ToolCall) -> str:
                    nonlocal called_next, inner_result, inner_exc
                    called_next = True
                    try:
                        inner_result = next_fn(next_c)
                        return inner_result
                    except Exception as exc:
                        inner_exc = exc
                        raise

                if policy == FailurePolicy.OPEN:
                    try:
                        return mw(c, _instrumented_next)
                    except Exception as mw_exc:
                        if inner_exc is not None and mw_exc is inner_exc:
                            # 안쪽 미들웨어/핸들러가 던진 예외가 그대로 전달된 것이면 현재 미들웨어의 자체 실패가 아님
                            raise
                        logger.warning(
                            "GUARD middleware '%s' failed in phase %s: %s",
                            name,
                            phase.name,
                            mw_exc,
                        )
                        if c.scope is not None:
                            c.scope.report_extension_error(name, phase.name, mw_exc)

                        if inner_exc is not None:
                            # 안쪽 결과를 아직 받지 못한 채(안쪽이 예외) 던진 경우 안쪽 예외를 위로
                            raise inner_exc
                        if called_next:
                            # 이미 불렀으면 안쪽 결과 그대로 반환 (도구 재실행 방지)
                            assert inner_result is not None
                            return inner_result
                        # next를 아직 부르지 않았으면 next(c)를 불러 그 결과를 반환
                        return next_fn(c)
                else:
                    # CLOSED 정책
                    try:
                        return mw(c, next_fn)
                    except Exception as mw_exc:
                        if c.scope is not None:
                            c.scope.report_extension_error(name, phase.name, mw_exc)
                        raise

            return _wrapped

        chain = _make_chain(0)
        try:
            result = chain(call)
        except Exception as e:
            return json.dumps(
                {"success": False, "error": f"Tool '{call.name}' failed: {str(e)}"},
                ensure_ascii=False,
            )

        if not isinstance(result, str):
            return json.dumps(
                {"success": False, "error": f"Tool '{call.name}' middleware returned a non-string result."},
                ensure_ascii=False,
            )
        return result

    def handle_function_call(
        self,
        function_name: str,
        function_args: dict[str, Any],
        task_id: Optional[str] = None,
    ) -> str:
        """단일 도구 실행 진입로 호환 메서드. dispatch로 위임한다."""
        return self.dispatch(ToolCall(name=function_name, args=function_args, task_id=task_id))


_DEFAULT_TOOL_NAMES = (
    "memory",
    "session_search",
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "skills_list",
    "skill_view",
)


def _refusal(message: str) -> str:
    """파일 도구의 실패 JSON과 같은 모양의 거부 결과."""
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _checkpointed_handler(
    tool_name: str, tool: Callable[..., str], checkpointer: Checkpointer
) -> Callable[..., str]:
    """파일 도구를 체크포인트 선기록 → 실행 → 확정으로 감싼 핸들러를 만든다.

    선기록에 실패하면 도구를 부르지 않는다(fail-closed).
    """

    def _handler(**kwargs: Any) -> str:
        path = kwargs.get("path")
        if not isinstance(path, str):
            return tool(**kwargs)

        if resolve_workspace(None) != resolve_workspace(checkpointer.workspace):
            return _refusal(CHECKPOINT_WORKSPACE_MISMATCH_MESSAGE + CHECKPOINT_REFUSED_HINT)

        try:
            cid = checkpointer.record_before(tool_name, path)
        except CheckpointLimitError as e:
            return _refusal(str(e) + CHECKPOINT_REFUSED_HINT)
        except Exception as e:
            return _refusal(
                CHECKPOINT_PRE_RECORD_FAILED_MESSAGE.format(reason=str(e))
                + CHECKPOINT_REFUSED_HINT
            )

        result_success: Optional[bool] = None
        exception: Optional[BaseException] = None
        result: Optional[str] = None
        try:
            result = tool(**kwargs)
            try:
                parsed = json.loads(result)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("success"), bool):
                result_success = parsed["success"]
            return result
        except BaseException as e:
            exception = e
            raise
        finally:
            if cid is not None:
                try:
                    checkpointer.finalize(
                        cid,
                        tool_name,
                        path,
                        result_success=result_success,
                        exception=exception,  # type: ignore[arg-type]
                    )
                except Exception as fin_err:
                    logger.warning(
                        "Checkpoint %s finalize failed for %s: %s", cid, tool_name, fin_err
                    )
            elif result_success is True:
                logger.warning(
                    "기록 없는 변경 (unrecorded modification): %s succeeded on %r without a checkpoint",
                    tool_name,
                    path,
                )

    return _handler


def create_default_dispatcher(
    memory_store: Optional[MemoryStore] = None,
    session_db: Optional[SessionSearchPort] = None,
    include: Optional[Collection[str]] = None,
    approver: Optional[Approver] = None,
    checkpointer: Optional[Checkpointer] = None,
) -> ToolDispatcher:
    """Hermes 핵심 도구들이 등록된 기본 디스패처 생성.

    checkpointer가 주어지면 write_file·patch를 쓰기 전 기록·쓴 뒤 확정으로 감싼다.
    None이면 기존처럼 도구를 그대로 등록한다.

    include가 주어지면 해당 이름의 도구만 등록한다.
    기본 도구 이름 목록에 없는 이름이 include에 있으면 ValueError를 발생시킨다.
    """
    if include is not None:
        unknown = [name for name in include if name not in _DEFAULT_TOOL_NAMES]
        if unknown:
            raise ValueError(f"Unknown tool name(s) in include: {unknown}")
        include_set = set(include)
    else:
        include_set = None

    def _should_register(name: str) -> bool:
        return include_set is None or name in include_set

    dispatcher = ToolDispatcher()

    # 1. Memory 도구 등록
    if _should_register("memory"):
        def _memory_handler(**kwargs):
            return memory_tool(store=memory_store, **kwargs)

        dispatcher.register_tool("memory", _memory_handler, MEMORY_SCHEMA)

    # 2. Session Search 도구 등록
    if _should_register("session_search"):
        def _search_handler(**kwargs):
            return session_search_tool(db=session_db, **kwargs)

        dispatcher.register_tool("session_search", _search_handler, SESSION_SEARCH_SCHEMA)

    # 3. Terminal 도구 등록
    if _should_register("terminal"):
        def _terminal_handler(**kwargs):
            return terminal_tool(approver=approver, **kwargs)

        dispatcher.register_tool("terminal", _terminal_handler, TERMINAL_SCHEMA)

    # 4. Read File 도구 등록
    if _should_register("read_file"):
        dispatcher.register_tool("read_file", read_file_tool, READ_FILE_SCHEMA)

    # 5. Write File 도구 등록
    if _should_register("write_file"):
        dispatcher.register_tool(
            "write_file",
            write_file_tool
            if checkpointer is None
            else _checkpointed_handler("write_file", write_file_tool, checkpointer),
            WRITE_FILE_SCHEMA,
        )

    # 6. Patch 도구 등록
    if _should_register("patch"):
        dispatcher.register_tool(
            "patch",
            patch_tool
            if checkpointer is None
            else _checkpointed_handler("patch", patch_tool, checkpointer),
            PATCH_SCHEMA,
        )

    # 7. Search Files 도구 등록 (17 로드맵)
    if _should_register("search_files"):
        dispatcher.register_tool("search_files", search_files_tool, SEARCH_FILES_SCHEMA)

    # 8. Skills List 도구 등록
    if _should_register("skills_list"):
        dispatcher.register_tool("skills_list", skills_list_tool, SKILLS_LIST_SCHEMA)

    # 9. Skill View 도구 등록
    if _should_register("skill_view"):
        dispatcher.register_tool("skill_view", skill_view_tool, SKILL_VIEW_SCHEMA)

    return dispatcher


def create_child_dispatcher() -> ToolDispatcher:
    """서브에이전트(자식) 전용 디스패처 생성.

    기억(memory)과 세션 검색(session_search)이 제외된 허용 목록 6종 도구만 등록된
    새로운 인스턴스를 반환한다.
    """
    return create_default_dispatcher(include=CHILD_TOOL_NAMES)
