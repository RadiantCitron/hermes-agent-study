"""AI 에이전트 핵심 루프 및 턴 오케스트레이터 (Hermes Agent Loop).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/run_agent.py (AIAgent 클래스)
- 핵심 원리:
  1. 세션별 프롬프트 캐싱(Prompt Caching):
     - `_build_system_prompt()`는 세션당 1회만 조립되고 캐싱되어, 대화가 진행되는 동안 Prefix Cache를 보존.
  2. 메시지 역할 순서:
     - user -> assistant(tool_calls) -> tool -> assistant 의 순서대로 쌓는다. 순서를 검사하거나 강제하지는 않는다.
  3. 도구 실행 및 세션 원장 동기화:
     - 도구 실행 결과가 발생하는 즉시 SQLite SessionDB에 영구 기록.
  4. 테스트 격리를 위한 MockLLMClient 내장.
"""

from collections.abc import Collection
from dataclasses import dataclass
import json
import logging
from typing import Any, Optional
import uuid

from hermes_core.agent.compression_hook import CompressionConfig, CompressionExtension
from hermes_core.agent.events import (
    EVENT_EXTENSION_ERROR,
    EVENT_HOOK_ERROR,
    EVENT_TOOL_CALL,
    AgentEvent,
    ConsoleEventSink,
    EventSink,
)
from hermes_core.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    build_context_files_prompt,
    build_system_prompt,
    render_skills_index,
    select_tools_guidance,
)
from hermes_core.agent.turn_hooks import (
    AFTER_TOOL_CALLS,
    BEFORE_LLM,
    HookRegistry,
    TurnContext,
    TurnHook,
)
from hermes_core.extensions.contract import (
    Extension,
    ExtensionHost,
    ExtensionRecord,
    MiddlewarePhase,
    TurnScope,
)
from hermes_core.constants import (
    COMPRESSION_THRESHOLD_RATIO,
    CONTEXT_WINDOW_TOKENS,
    get_skills_dir,
)
from hermes_core.llm_client import LLMError
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolCall, ToolDispatcher, create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore
from hermes_core.tools.skills_tool import discover_skills

logger = logging.getLogger(__name__)


def _parse_tool_arguments(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """모델이 보낸 도구 인자를 사전으로 읽는다. 실패하면 (None, 사유)를 반환.

    빈 문자열(공백 포함)과 None은 「인자 없음」({})으로 본다.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as e:
        return None, str(e)
    if not isinstance(args, dict):
        return None, "arguments must be a JSON object"
    return args, None


# =============================================================================
# Mock LLM 클라이언트 (테스트 및 오프라인 환경 대응)
# =============================================================================

@dataclass
class MockToolCall:
    id: str
    function: dict[str, str]  # {"name": "...", "arguments": "{...}"}


@dataclass
class MockLLMResponse:
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


class MockLLMClient:
    """오프라인 및 단위 테스트를 위한 Mock LLM 응답 시퀀스 재생기."""

    def __init__(self, responses: Optional[list[MockLLMResponse]] = None):
        self.responses = responses or []
        self._index = 0
        self.call_history: list[dict[str, Any]] = []

    def queue_response(self, content: Optional[str] = None, tool_calls: Optional[list[dict[str, Any]]] = None) -> None:
        self.responses.append(MockLLMResponse(content=content, tool_calls=tool_calls))

    def chat_completion(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> MockLLMResponse:
        self.call_history.append({"messages": messages, "tools": tools})
        if self._index >= len(self.responses):
            return MockLLMResponse(content="[Mock] Default completion: No more queued responses.")
        resp = self.responses[self._index]
        self._index += 1
        return resp


# =============================================================================
# HermesAgent 메인 클래스
# =============================================================================

class HermesAgent:
    """Hermes AI 에이전트 턴 오케스트레이터 클래스."""

    def __init__(
        self,
        model: Optional[str] = None,
        system_message: Optional[str] = None,
        session_id: Optional[str] = None,
        session_db: Optional[SessionDB] = None,
        memory_store: Optional[MemoryStore] = None,
        dispatcher: Optional[ToolDispatcher] = None,
        llm_client: Optional[Any] = None,
        max_iterations: int = 50,
        quiet_mode: bool = False,
        context_window_tokens: int = CONTEXT_WINDOW_TOKENS,
        compression_threshold_ratio: float = COMPRESSION_THRESHOLD_RATIO,
        compression_enabled: bool = True,
        parent_session_id: Optional[str] = None,
        source: str = "cli",
        skip_memory: bool = False,
        skip_context_files: bool = False,
        resume: bool = False,
        event_sink: Optional[EventSink] = None,
        skills_index: bool = False,
    ):
        self.system_message = system_message
        self.session_id = session_id or str(uuid.uuid4())
        self.max_iterations = max_iterations
        self.quiet_mode = quiet_mode
        if event_sink is not None:
            self.event_sink: Optional[EventSink] = event_sink
        elif not quiet_mode:
            self.event_sink = ConsoleEventSink()
        else:
            self.event_sink = None
        self.context_window_tokens = context_window_tokens
        self.compression_threshold_ratio = compression_threshold_ratio
        self.compression_enabled = compression_enabled
        self.parent_session_id = parent_session_id
        self.source = source
        self.skip_memory = skip_memory
        self.skip_context_files = skip_context_files
        self.resume = resume
        self.skills_index = skills_index

        # 스킬 인덱스 블록 캐시 (세션 중 불변 고정)
        self._skills_index_block: Optional[str] = None

        # 세션 범위 슬롯 (확장 기능 간 세션 내 상태 공유)
        self._session_slots: dict[str, dict[str, Any]] = {}

        # 토큰 추정 상태 (세션 간 미보존)
        self._last_input_tokens: Optional[int] = None
        self._last_message_count: Optional[int] = None

        # 의존성 주입 및 격리
        self.session_db = session_db or SessionDB()
        self.memory_store = memory_store or MemoryStore()
        self.dispatcher = dispatcher or create_default_dispatcher(
            memory_store=self.memory_store,
            session_db=self.session_db,
        )
        self.llm_client = llm_client or MockLLMClient()

        # 효과 모델명 및 모드 판정
        if model is not None:
            self.model = model
        elif isinstance(self.llm_client, MockLLMClient):
            self.model = "mock"
        else:
            self.model = getattr(self.llm_client, "model", "unknown")

        if isinstance(self.llm_client, MockLLMClient):
            self.mode = "mock"
        else:
            self.mode = "real"

        # 콜백 레코더 등록
        if hasattr(self.llm_client, "set_call_recorder") and callable(getattr(self.llm_client, "set_call_recorder")):
            self.llm_client.set_call_recorder(
                lambda record: self.session_db.record_llm_call(session_id=self.session_id, **record)
            )

        # 메모리 디스크 로드 및 동결 스냅샷 캡처 (skip_memory=True면 생략)
        if not self.skip_memory:
            self.memory_store.load_from_disk()

        # 세션 DB 레코드 생성 또는 기존 세션 재개
        if self.resume:
            existing = self.session_db.get_session(self.session_id)
            if existing is None:
                raise ValueError(
                    f"Cannot resume session '{self.session_id}': session does not exist."
                )
            self.session_db.reopen_session(self.session_id)
            saved_prompt = existing.get("system_prompt")
            self._cached_system_prompt = (
                saved_prompt if isinstance(saved_prompt, str) and saved_prompt else None
            )
        else:
            self.session_db.create_session(
                session_id=self.session_id,
                source=self.source,
                model=self.model,
                mode=self.mode,
                system_prompt=None,
                parent_session_id=self.parent_session_id,
            )
            self._cached_system_prompt = None

        # 턴 훅 레지스트리 및 확장 관리자 초기화
        self.hooks = HookRegistry()
        self._installed_extensions: list[Extension] = []
        self._extension_registrations: list[dict[str, Any]] = []
        self._extension_failures: dict[str, int] = {}
        self._hook_to_extension: dict[int, str] = {}

        # 기본 압축 확장 설치
        self.install_extension(CompressionExtension(self._compression_config))

    @property
    def _rejected_at_tokens(self) -> Optional[int]:
        """압축 확장과의 하위 호환성을 위한 읽기 전용 프로퍼티."""
        return self._session_slots.get("compression", {}).get("rejected_at_tokens")

    def _compression_config(self) -> CompressionConfig:
        """현재 에이전트 상태 기반 CompressionConfig 생성."""
        return CompressionConfig(
            enabled=self.compression_enabled,
            context_window_tokens=self.context_window_tokens,
            threshold_ratio=self.compression_threshold_ratio,
            llm_client=self.llm_client,
        )

    def install_extension(self, ext: Extension) -> None:
        """코어 확장 설치 (중복 이름 시 ValueError)."""
        if any(e.name == ext.name for e in self._installed_extensions):
            raise ValueError(f"Extension '{ext.name}' is already installed.")

        self._installed_extensions.append(ext)
        host = _ExtensionHost(self, ext.name)
        ext.install(host)

    @property
    def extensions(self) -> tuple[ExtensionRecord, ...]:
        """설치된 확장의 읽기 전용 레코드 목록."""
        records = []
        for reg in self._extension_registrations:
            name = reg["name"]
            kind = reg["kind"]
            slot = reg["slot"]
            failures = self._extension_failures.get(name, 0)
            records.append(ExtensionRecord(name=name, kind=kind, slot=slot, failures=failures))
        return tuple(records)

    def _on_extension_error(self, name: str, slot: str, exc: Exception) -> None:
        """확장 실패 처리 및 집계."""
        self._extension_failures[name] = self._extension_failures.get(name, 0) + 1
        logger.warning("Extension '%s' failed at slot '%s': %s", name, slot, exc)
        self._emit(EVENT_EXTENSION_ERROR, name=name, slot=slot, error=str(exc))

    def _resolve_skills_index_block(self, tool_names: Collection[str]) -> str:
        """스킬 인덱스 블록을 1회 렌더링하여 보관하고 반환 (세션 중 불변 고정).

        - self.skills_index가 False이거나, tool_names에 skills_list와 skill_view가 모두 없으면 빈 문자열 반환.
        - 이미 보관된 블록이 있으면 재계산하지 않고 보관된 값 반환.
        """
        if not self.skills_index:
            return ""

        tool_names_set = set(tool_names) if tool_names else set()
        if "skills_list" not in tool_names_set or "skill_view" not in tool_names_set:
            return ""

        if self._skills_index_block is None:
            skills = discover_skills(get_skills_dir())
            self._skills_index_block = render_skills_index(skills)

        return self._skills_index_block

    def _build_system_prompt(self, system_message: Optional[str] = None) -> str:
        """시스템 프롬프트를 조립하고 캐싱. `system_message`를 넘기지 않으면 캐시를 재사용하고, 넘기면 다시 조립한다."""
        if self._cached_system_prompt is not None and system_message is None:
            return self._cached_system_prompt

        # 메모리 블록 렌더링 (skip_memory=True면 생략)
        if self.skip_memory:
            memory_full_block = None
        else:
            mem_parts = []
            mem_block = self.memory_store.format_for_system_prompt("memory")
            user_block = self.memory_store.format_for_system_prompt("user")
            if mem_block:
                mem_parts.append(mem_block)
            if user_block:
                mem_parts.append(user_block)
            memory_full_block = "\n\n".join(mem_parts) if mem_parts else None

        # 작업 디렉터리 컨텍스트 파일 (skip_context_files=True면 생략)
        if self.skip_context_files:
            context_block = None
        else:
            context_block = build_context_files_prompt()

        caller_prompt = system_message if system_message is not None else self.system_message

        # 등록된 도구 이름 기반 가이던스 선택
        tool_schemas = self.dispatcher.get_tools_schema()
        tool_names = [
            s.get("function", {}).get("name")
            for s in tool_schemas
            if s.get("function", {}).get("name")
        ]
        tools_guidance = select_tools_guidance(tool_names)
        skills_index_block = self._resolve_skills_index_block(tool_names)

        assembled = build_system_prompt(
            agent_identity=DEFAULT_AGENT_IDENTITY,
            tools_guidance=tools_guidance,
            skills_index_block=skills_index_block or None,
            caller_prompt=caller_prompt,
            memory_block=memory_full_block,
            context_files_block=context_block if context_block else None,
            platform="cli",
        )

        self._cached_system_prompt = assembled
        self.session_db.update_system_prompt(self.session_id, assembled)
        return assembled

    def load_conversation_history(self) -> Optional[list[dict[str, Any]]]:
        """원장의 활성(active=1) 메시지를 읽어 run_conversation에 넘길 conversation_history 목록([system, ...])으로 반환한다. 활성 메시지가 없으면 None."""
        active_msgs = self.session_db.get_messages_as_conversation(self.session_id)
        if not active_msgs:
            return None
        sys_prompt = self._build_system_prompt()
        return [{"role": "system", "content": sys_prompt}, *active_msgs]

    def _emit(self, kind: str, **data: Any) -> None:
        """이벤트 싱크가 등록되어 있으면 이벤트를 방출한다. 싱크 예외는 경고 로그 후 삼킨다."""
        if self.event_sink is None:
            return
        try:
            self.event_sink(AgentEvent(kind=kind, data=data))
        except Exception as e:
            logger.warning("EventSink failed for event '%s': %s", kind, e)

    def _on_hook_error(self, point: str, hook: Any, exc: Exception) -> None:
        """훅 실행 중 발생한 예외 처리: 경고 로그 및 hook_error 이벤트 방출."""
        hook_name = getattr(hook, "__name__", None) or getattr(type(hook), "__name__", str(hook))
        logger.warning("Hook '%s' at '%s' failed: %s", hook_name, point, exc)
        self._emit(EVENT_HOOK_ERROR, point=point, hook=hook_name, error=str(exc))

        # 만약 확장에서 등록된 훅이라면 확장 실패도 함께 집계
        ext_name = self._hook_to_extension.get(id(hook))
        if ext_name:
            self._on_extension_error(ext_name, point, exc)

    def _replace_transcript(
        self,
        messages: list[dict[str, Any]],
        new_messages: list[dict[str, Any]],
        *,
        summary_index: Optional[int] = None,
        usage: Optional[dict[str, Any]] = None,
    ) -> None:
        """대화 교체 및 세션 DB 원장 압축 단일 진입로."""
        if not new_messages:
            raise ValueError("new_messages cannot be empty.")
        if not messages:
            raise ValueError("active transcript cannot be empty.")

        first_orig = messages[0]
        first_new = new_messages[0]
        if first_new.get("role") != first_orig.get("role") or first_new.get("content") != first_orig.get("content"):
            raise ValueError("system message modified by replace_transcript")

        # 세션 DB 원장 압축 (실패 시 예외 전파되어 메모리 메시지는 불변 유지)
        self.session_db.archive_and_compact(
            self.session_id,
            new_messages[1:],
            summary_index=summary_index,
        )

        # 원장 압축 성공 시에만 메모리 목록 원자적 교체
        messages[:] = new_messages
        self._last_input_tokens = None
        self._last_message_count = None

        if usage and isinstance(usage, dict):
            in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
            if in_tok or out_tok:
                self.session_db.update_token_counts(
                    self.session_id, input_tokens=in_tok, output_tokens=out_tok
                )

    def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        scope: Optional[TurnScope] = None,
    ) -> None:
        """assistant_message의 tool_calls를 실행하고 결과를 messages에 append."""
        for tc in tool_calls:
            call_id = tc.get("id") or str(uuid.uuid4())
            func_obj = tc.get("function", {})
            func_name = func_obj.get("name") or tc.get("name", "unknown")
            args_str = func_obj.get("arguments", "{}")

            self._emit(EVENT_TOOL_CALL, name=func_name, arguments=args_str)

            args, parse_error = _parse_tool_arguments(args_str)
            if parse_error is not None:
                logger.warning("Invalid JSON arguments for tool '%s': %s", func_name, parse_error)

            call = ToolCall(
                name=func_name,
                args=args or {},
                task_id=call_id,
                scope=scope,
                raw_arguments=args_str if isinstance(args_str, str) else None,
                parse_error=parse_error,
            )
            result_str = self.dispatcher.dispatch(call)

            # messages에 추가 (Strict Role Alternation)
            tool_msg = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": result_str,
            }
            messages.append(tool_msg)

            # 세션 DB 원장 기록
            self.session_db.append_message(
                session_id=self.session_id,
                role="tool",
                content=result_str,
                tool_call_id=call_id,
                tool_name=func_name,
            )

    def run_conversation(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        conversation_history: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """단일 대화 턴 루프 실행. 최종 응답 문자열과 전체 messages 목록 반환."""
        sys_prompt = self._build_system_prompt(system_message)

        # 메시지 히스토리 조립
        messages: list[dict[str, Any]] = []
        if conversation_history:
            messages.extend(conversation_history)
        else:
            messages.append({"role": "system", "content": sys_prompt})

        # 사용자 메시지 추가 및 원장 기록
        messages.append({"role": "user", "content": user_message})
        self.session_db.append_message(
            session_id=self.session_id,
            role="user",
            content=user_message,
        )

        final_response: str = ""
        stopped_reason = "max_iterations"
        iterations = 0
        error_info: Optional[dict[str, str]] = None

        scope = TurnScope(
            session_id=self.session_id,
            session_slots=self._session_slots,
            on_extension_error=self._on_extension_error,
        )
        ctx = TurnContext(
            messages=messages,
            tools_schema=None,
            iteration=0,
            session_id=self.session_id,
            scope=scope,
            last_input_tokens=self._last_input_tokens,
            last_message_count=self._last_message_count,
            _live_messages=messages,
            _replace_fn=lambda new_msgs, summary_index=None, usage=None: self._replace_transcript(
                messages, new_msgs, summary_index=summary_index, usage=usage
            ),
        )

        while iterations < self.max_iterations:
            iterations += 1

            tools_schema = self.dispatcher.get_tools_schema()
            schema_list = tools_schema if tools_schema else None

            ctx.iteration = iterations
            scope.iteration = iterations
            ctx.tools_schema = schema_list
            ctx.last_input_tokens = self._last_input_tokens
            ctx.last_message_count = self._last_message_count

            self.hooks.run(BEFORE_LLM, ctx, on_error=self._on_hook_error)
            if ctx.stop_reason is not None:
                stopped_reason = ctx.stop_reason
                final_response = ""
                break

            # LLM 호출 직전 메시지 수 기록
            msgs_len_before_call = len(messages)

            # LLM 호출
            try:
                resp = self.llm_client.chat_completion(
                    messages=messages,
                    tools=schema_list,
                )
            except LLMError as e:
                logger.warning("LLM call failed with error: %s", e)
                stopped_reason = "llm_error"
                final_response = ""
                error_info = {"kind": e.kind, "message": str(e)}
                break

            # 응답에 usage가 있으면 update_token_counts로 누적 및 추정 상태 보관
            usage = getattr(resp, "usage", None)
            if usage and isinstance(usage, dict):
                in_tok = usage.get("input_tokens") or 0
                out_tok = usage.get("output_tokens") or 0
                if in_tok or out_tok:
                    self.session_db.update_token_counts(
                        self.session_id, input_tokens=in_tok, output_tokens=out_tok
                    )
                if in_tok:
                    self._last_input_tokens = in_tok
                    self._last_message_count = msgs_len_before_call

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if resp.content:
                assistant_msg["content"] = resp.content
            if resp.tool_calls:
                assistant_msg["tool_calls"] = resp.tool_calls

            messages.append(assistant_msg)

            # 세션 DB 기록
            self.session_db.append_message(
                session_id=self.session_id,
                role="assistant",
                content=resp.content,
                tool_calls=resp.tool_calls,
            )

            # 도구 호출이 있으면 실행 후 루프 계속
            if resp.tool_calls:
                self._execute_tool_calls(resp.tool_calls, messages, scope=scope)
                self.hooks.run(AFTER_TOOL_CALLS, ctx, on_error=self._on_hook_error)
                if ctx.stop_reason is not None:
                    stopped_reason = ctx.stop_reason
                    final_response = ""
                    break
            else:
                # 도구 호출이 없으면 최종 텍스트 응답으로 루프 종료
                final_response = resp.content or ""
                stopped_reason = "completed"
                break

        if stopped_reason == "max_iterations":
            logger.warning("Agent loop stopped at max_iterations=%d without a final response.", self.max_iterations)

        result: dict[str, Any] = {
            "final_response": final_response,
            "messages": messages,
            "iterations": iterations,
            "session_id": self.session_id,
            "stopped_reason": stopped_reason,
        }
        if error_info is not None:
            result["error"] = error_info
        result.update(scope.reports)

        return result


class _ExtensionHost:
    """ExtensionHost 프로토콜을 구현하는 에이전트 비공개 클래스."""

    def __init__(self, agent: "HermesAgent", extension_name: str) -> None:
        self._agent = agent
        self._extension_name = extension_name

    def add_tool_middleware(
        self,
        phase: MiddlewarePhase,
        middleware: Callable[..., Any],
    ) -> None:
        self._agent.dispatcher.add_middleware(
            middleware,
            phase=phase,
            name=self._extension_name,
        )
        self._agent._extension_registrations.append({
            "name": self._extension_name,
            "kind": "tool_middleware",
            "slot": phase.name,
        })

    def add_turn_hook(
        self,
        point: str,
        hook: Callable[..., Any],
    ) -> None:
        self._agent.hooks.register(point, hook, name=self._extension_name)
        self._agent._hook_to_extension[id(hook)] = self._extension_name
        self._agent._extension_registrations.append({
            "name": self._extension_name,
            "kind": "turn_hook",
            "slot": point,
        })

    def emit(self, kind: str, **data: Any) -> None:
        self._agent._emit(kind, **data)


# 원본 인터페이스 호환을 위한 클래스 별칭
AIAgent = HermesAgent
