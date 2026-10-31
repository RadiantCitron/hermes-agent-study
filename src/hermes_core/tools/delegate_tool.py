"""서브에이전트 위임 도구 (`delegate_task`).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/delegate_tool.py (또는 서브에이전트 오케스트레이션)
- 핵심 원리:
  - 단일 순차 자식 위임: 부모 대화와 격리된 새 세션에서 자식 에이전트를 동기 실행.
  - 4대 격벽 원칙:
    1. 대화 격리: 자식은 빈 대화에서 시작하며 goal이 첫 user 메시지가 됨.
    2. 도구 격리: 자식은 create_child_dispatcher()로 허용 목록 6종 도구만 보유 (위임·기억·검색 원천 차단).
    3. 원장 격리: llm_client_factory를 통해 독립된 클라이언트를 사용하여 자식 세션에만 호출 기록.
    4. 상태 격리: 자식의 실행 기록 및 토큰은 자식 세션 DB에만 귀속되며 부모 합계에 미합산.
"""

from collections.abc import Callable
import json
from typing import Any, Optional

from hermes_core.agent.agent import HermesAgent
from hermes_core.agent.prompt_builder import build_child_system_message
from hermes_core.constants import (
    DELEGATE_CHILD_MAX_ITERATIONS,
    DELEGATE_SUMMARY_MAX_CHARS,
    SUBAGENT_SOURCE,
)
from hermes_core.tools.ports import DelegateStorePort
from hermes_core.tools.dispatcher import ToolDispatcher, create_child_dispatcher

DELEGATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a task to a child subagent. The subagent runs in an isolated "
            "conversation with restricted tools (terminal, file operations, and skills) "
            "and returns a summary. The subagent knows nothing about your conversation. "
            "Put everything it needs in goal and context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Clear and specific objective for the subagent to accomplish.",
                },
                "context": {
                    "type": "string",
                    "description": "Background information, file paths, or constraints needed to accomplish the goal.",
                },
            },
            "required": ["goal"],
        },
    },
}


def register_delegate_tool(
    dispatcher: ToolDispatcher,
    *,
    session_db: DelegateStorePort,
    parent_session_id: str,
    llm_client_factory: Callable[[], Any],
    max_iterations: int = DELEGATE_CHILD_MAX_ITERATIONS,
    summary_max_chars: int = DELEGATE_SUMMARY_MAX_CHARS,
) -> None:
    """디스패처에 delegate_task 도구를 등록한다."""

    def delegate_task(goal: str, context: Optional[str] = None) -> str:
        # 1. 입력 검증
        if not isinstance(goal, str) or not goal.strip():
            return json.dumps(
                {"success": False, "error": "goal must be a non-empty string"},
                ensure_ascii=False,
            )

        # 2. 클라이언트 팩토리 호출
        try:
            child_client = llm_client_factory()
        except Exception as e:
            return json.dumps(
                {"success": False, "error": f"Failed to create child client: {str(e)}"},
                ensure_ascii=False,
            )

        # 3. 자식 에이전트 생성
        child_agent = HermesAgent(
            session_db=session_db,
            dispatcher=create_child_dispatcher(),
            llm_client=child_client,
            max_iterations=max_iterations,
            quiet_mode=True,
            system_message=build_child_system_message(context),
            parent_session_id=parent_session_id,
            source=SUBAGENT_SOURCE,
            skip_memory=True,
            skip_context_files=True,
        )
        child_session_id = child_agent.session_id

        # 4. 실행 및 예외 포획
        caught_exception = None
        run_result = None
        try:
            run_result = child_agent.run_conversation(goal)
        except Exception as e:
            caught_exception = e

        # 5. 상태 및 결과 판정
        if caught_exception is not None:
            stopped_reason = "exception"
            final_response = ""
            iterations = 0
            error_payload: Optional[dict[str, Any]] = {
                "kind": "exception",
                "message": str(caught_exception),
            }
        else:
            final_response = (run_result.get("final_response", "") or "") if run_result else ""
            iterations = run_result.get("iterations", 0) if run_result else 0
            stopped_reason = run_result.get("stopped_reason", "completed") if run_result else "completed"
            error_payload = run_result.get("error") if run_result else None

        if stopped_reason == "completed":
            if not final_response.strip():
                status = "failed"
                end_reason = "empty_response"
                error_payload = {
                    "kind": "empty_response",
                    "message": "Subagent returned an empty response.",
                }
            else:
                status = "completed"
                end_reason = "completed"
        else:
            status = "failed"
            end_reason = stopped_reason
            if error_payload is None:
                error_payload = {
                    "kind": stopped_reason,
                    "message": f"Subagent stopped with reason '{stopped_reason}'.",
                }

        # 6. 세션 종료 기록 (finally 보장)
        try:
            session_db.end_session(child_session_id, end_reason=end_reason)
        except Exception:
            pass

        # 7. 요약 절삭
        truncated = len(final_response) > summary_max_chars
        summary = final_response[:summary_max_chars]

        # 8. 토큰 집계 (자식 세션 원장에서 취득, 부모에 합산하지 않음)
        sess_data = session_db.get_session(child_session_id) or {}
        tokens = {
            "input": sess_data.get("input_tokens") or 0,
            "output": sess_data.get("output_tokens") or 0,
        }

        # 9. 결과 조립 및 반환
        result_dict: dict[str, Any] = {
            "success": (status == "completed"),
            "status": status,
            "summary": summary,
            "child_session_id": child_session_id,
            "iterations": iterations,
            "stopped_reason": stopped_reason,
            "tokens": tokens,
            "truncated": truncated,
        }
        if status != "completed" and error_payload is not None:
            result_dict["error"] = error_payload

        return json.dumps(result_dict, ensure_ascii=False)

    dispatcher.register_tool("delegate_task", delegate_task, DELEGATE_TASK_SCHEMA)
