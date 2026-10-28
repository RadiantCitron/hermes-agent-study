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

from dataclasses import dataclass
import json
import logging
from typing import Any, Optional
import uuid

from hermes_core.agent.compression import (
    compress_messages,
    find_previous_summary,
    split_for_compression,
)
from hermes_core.agent.context_budget import (
    compression_tail_budget,
    compression_threshold,
    estimate_current_tokens,
    may_retry_after_reject,
    should_compress,
)
from hermes_core.agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    build_context_files_prompt,
    build_system_prompt,
    select_tools_guidance,
)
from hermes_core.constants import (
    COMPRESSION_HEAD_MESSAGES,
    COMPRESSION_MAX_PER_RUN,
    COMPRESSION_MIN_TAIL_MESSAGES,
    COMPRESSION_THRESHOLD_RATIO,
    CONTEXT_WINDOW_TOKENS,
    SUMMARY_END_MARKER,
    SUMMARY_PREFIX,
)
from hermes_core.llm_client import LLMError
from hermes_core.session_db import SessionDB
from hermes_core.tools.dispatcher import ToolDispatcher, create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore

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
    ):
        self.system_message = system_message
        self.session_id = session_id or str(uuid.uuid4())
        self.max_iterations = max_iterations
        self.quiet_mode = quiet_mode
        self.context_window_tokens = context_window_tokens
        self.compression_threshold_ratio = compression_threshold_ratio
        self.compression_enabled = compression_enabled
        self.parent_session_id = parent_session_id
        self.source = source
        self.skip_memory = skip_memory
        self.skip_context_files = skip_context_files

        # 토큰 추정 및 압축 거부 추적 상태 (세션 간 미보존)
        self._last_input_tokens: Optional[int] = None
        self._last_message_count: Optional[int] = None
        self._rejected_at_tokens: Optional[int] = None

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

        # 세션 DB 레코드 생성
        self.session_db.create_session(
            session_id=self.session_id,
            source=self.source,
            model=self.model,
            mode=self.mode,
            system_prompt=None,
            parent_session_id=self.parent_session_id,
        )

        # 시스템 프롬프트 캐시 변수 (Prompt Caching 보존)
        self._cached_system_prompt: Optional[str] = None

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

        assembled = build_system_prompt(
            agent_identity=DEFAULT_AGENT_IDENTITY,
            tools_guidance=tools_guidance,
            caller_prompt=caller_prompt,
            memory_block=memory_full_block,
            context_files_block=context_block if context_block else None,
            platform="cli",
        )

        self._cached_system_prompt = assembled
        self.session_db.update_system_prompt(self.session_id, assembled)
        return assembled

    def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        """assistant_message의 tool_calls를 실행하고 결과를 messages에 append."""
        for tc in tool_calls:
            call_id = tc.get("id") or str(uuid.uuid4())
            func_obj = tc.get("function", {})
            func_name = func_obj.get("name") or tc.get("name", "unknown")
            args_str = func_obj.get("arguments", "{}")

            if not self.quiet_mode:
                print(f"  [Tool Call] {func_name}({args_str})")

            args, parse_error = _parse_tool_arguments(args_str)
            if parse_error is None:
                # 디스패처 단일 진입로 실행
                result_str = self.dispatcher.handle_function_call(func_name, args)
            else:
                # 읽지 못한 인자로는 도구를 부르지 않고, 사유를 모델에게 돌려줘 스스로 고치게 한다.
                logger.warning("Invalid JSON arguments for tool '%s': %s", func_name, parse_error)
                result_str = json.dumps(
                    {"success": False, "error": f"Invalid JSON arguments for tool '{func_name}': {parse_error}"},
                    ensure_ascii=False,
                )

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

        # 컨텍스트 압축 추적 카운터
        compression_attempts = 0
        compression_count = 0
        compression_fallback_count = 0

        while iterations < self.max_iterations:
            iterations += 1

            tools_schema = self.dispatcher.get_tools_schema()
            schema_list = tools_schema if tools_schema else None

            # LLM 호출 직전 컨텍스트 압축 판정 및 실행
            if self.compression_enabled and compression_attempts < COMPRESSION_MAX_PER_RUN:
                threshold = compression_threshold(
                    self.context_window_tokens,
                    self.compression_threshold_ratio,
                )
                current_tokens = estimate_current_tokens(
                    messages=messages,
                    last_input_tokens=self._last_input_tokens,
                    last_message_count=self._last_message_count,
                    tools=schema_list,
                )
                if should_compress(current_tokens, threshold) and may_retry_after_reject(
                    current_tokens, self._rejected_at_tokens
                ):
                    compression_attempts += 1
                    tail_budget = compression_tail_budget(threshold)

                    prev_sum = find_previous_summary(messages)
                    eff_head = 0 if prev_sum is not None else COMPRESSION_HEAD_MESSAGES
                    can_split = (
                        split_for_compression(
                            messages,
                            head_messages=eff_head,
                            tail_tokens=tail_budget,
                            min_tail_messages=COMPRESSION_MIN_TAIL_MESSAGES,
                        )
                        is not None
                    )

                    comp_res = compress_messages(
                        messages,
                        self.llm_client,
                        head_messages=COMPRESSION_HEAD_MESSAGES,
                        tail_tokens=tail_budget,
                        min_tail_messages=COMPRESSION_MIN_TAIL_MESSAGES,
                        tools=schema_list,
                    )
                    if comp_res is not None:
                        # DB 저장용 새 메시지 목록 (system 제외)
                        sys_offset = 1 if (comp_res.messages and comp_res.messages[0].get("role") == "system") else 0
                        db_new_msgs = comp_res.messages[sys_offset:]

                        summary_idx = None
                        for i, m in enumerate(db_new_msgs):
                            content = m.get("content") or ""
                            # 독립 요약 행인지 판별 (PREFIX로 시작하고 END_MARKER로 끝남)
                            if (
                                content.startswith(SUMMARY_PREFIX)
                                and content.endswith(SUMMARY_END_MARKER)
                                and m.get("role") in ("user", "assistant")
                            ):
                                summary_idx = i
                                break

                        try:
                            self.session_db.archive_and_compact(
                                self.session_id,
                                db_new_msgs,
                                summary_index=summary_idx,
                            )
                            # DB 기록 성공 시에만 메모리 목록 원자적 교체
                            messages[:] = comp_res.messages
                            self._last_input_tokens = None
                            self._last_message_count = None
                            self._rejected_at_tokens = None
                            compression_count += 1
                            if comp_res.used_fallback:
                                compression_fallback_count += 1
                            if comp_res.usage:
                                in_tok = comp_res.usage.get("prompt_tokens") or comp_res.usage.get("input_tokens") or 0
                                out_tok = comp_res.usage.get("completion_tokens") or comp_res.usage.get("output_tokens") or 0
                                if in_tok or out_tok:
                                    self.session_db.update_token_counts(
                                        self.session_id, input_tokens=in_tok, output_tokens=out_tok
                                    )
                            logger.info(
                                "[Compression] Session %s compressed: %d -> %d tokens (fallback=%s)",
                                self.session_id,
                                comp_res.tokens_before,
                                comp_res.tokens_after,
                                comp_res.used_fallback,
                            )
                        except Exception as e:
                            logger.warning(
                                "[Compression] archive_and_compact failed: %s. Keeping original messages.",
                                e,
                            )
                    else:
                        if can_split:
                            self._rejected_at_tokens = current_tokens

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
                self._execute_tool_calls(resp.tool_calls, messages)
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
        if compression_count > 0:
            result["compression"] = {
                "count": compression_count,
                "fallback": compression_fallback_count,
            }
        if error_info is not None:
            result["error"] = error_info

        return result


# 원본 인터페이스 호환을 위한 클래스 별칭
AIAgent = HermesAgent
