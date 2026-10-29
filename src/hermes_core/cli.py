"""Hermes Agent 터미널 대화형 CLI 진입점.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/cli.py
- 실행 방법:
  - `python -m hermes_core.cli --mock` (오프라인 테스트 및 스모크 실행 모드)
  - `python -m hermes_core.cli` (실제 모델 모드: OPENROUTER_API_KEY 환경변수 필요)
"""

import argparse
import os
from pathlib import Path
import sys
import uuid

from hermes_core.agent import HermesAgent, MockLLMClient
from hermes_core.cli_input import (
    hold_typeahead,
    is_interactive_terminal,
    multiline_input_available,
    read_user_input,
)
from hermes_core.constants import (
    CHECKPOINT_REFUSED_HINT,
    CLI_APPROVAL_DISABLED_NOTICE,
    CLI_APPROVAL_ENABLED_NOTICE,
    CLI_CHECKPOINT_DISABLED_NOTICE,
    CLI_CHECKPOINT_ENABLED_NOTICE,
    CLI_CHECKPOINT_REFUSED_NOTICE,
    CLI_MULTILINE_HINT,
    CLI_ROLLBACK_CANCELLED_NOTICE,
    CLI_ROLLBACK_DISABLED_NOTICE,
    CLI_ROLLBACK_ERROR_NOTICE,
    CLI_ROLLBACK_EXTERNAL_WARNING,
    CLI_ROLLBACK_HEADER,
    CLI_ROLLBACK_LINE_DELETE,
    CLI_ROLLBACK_LINE_RESTORE,
    CLI_ROLLBACK_LINE_SKIP,
    CLI_ROLLBACK_MODEL_UNAWARE_NOTICE,
    CLI_ROLLBACK_NOTHING_NOTICE,
    CLI_ROLLBACK_PENDING_WARNING,
    CLI_ROLLBACK_PROMPT,
    CLI_ROLLBACK_RESULT_DELETED,
    CLI_ROLLBACK_RESULT_RESTORED,
    CLI_ROLLBACK_RESULT_SKIPPED,
    CLI_ROLLBACK_RESULT_SUMMARY,
    CLI_ROLLBACK_USAGE_NOTICE,
    CONTEXT_WINDOW_TOKENS,
    DEFAULT_REAL_MODEL,
    OPENROUTER_API_KEY_ENV,
    get_default_workspace,
    get_hermes_home,
)
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import Approver, is_approval_answer
from hermes_core.tools.checkpoint import Checkpointer
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore


def _handle_rollback(tokens: list[str], checkpointer: Checkpointer | None) -> None:
    """REPL의 /rollback 명령을 처리한다."""
    if checkpointer is None:
        print(CLI_ROLLBACK_DISABLED_NOTICE)
        return

    if len(tokens) > 1:
        print(CLI_ROLLBACK_USAGE_NOTICE)
        return

    try:
        plan = checkpointer.plan_rollback()
        if not plan.items:
            print(CLI_ROLLBACK_NOTHING_NOTICE)
            return

        restore_count = sum(1 for item in plan.items if not item.will_skip)
        skip_count = sum(1 for item in plan.items if item.will_skip)
        print(
            CLI_ROLLBACK_HEADER.format(
                turn_index=plan.turn_index,
                restore_count=restore_count,
                skip_count=skip_count,
            )
        )
        for item in plan.items:
            if item.will_skip:
                print(
                    CLI_ROLLBACK_LINE_SKIP.format(
                        rel_path=item.rel_path,
                        tool_name=item.tool_name,
                        reason=item.skip_reason,
                    )
                )
            elif item.before_exists:
                print(
                    CLI_ROLLBACK_LINE_RESTORE.format(
                        rel_path=item.rel_path,
                        tool_name=item.tool_name,
                    )
                )
            else:
                print(
                    CLI_ROLLBACK_LINE_DELETE.format(
                        rel_path=item.rel_path,
                        tool_name=item.tool_name,
                    )
                )

        if plan.terminal_approvals_count > 0 or plan.subagent_count > 0:
            print(CLI_ROLLBACK_EXTERNAL_WARNING)
        if any(item.status == "pending" for item in plan.items):
            print(CLI_ROLLBACK_PENDING_WARNING)

        try:
            answer = read_user_input(CLI_ROLLBACK_PROMPT, discard_typeahead=True)
        except (EOFError, KeyboardInterrupt):
            print(CLI_ROLLBACK_CANCELLED_NOTICE)
            return

        if not is_approval_answer(answer):
            print(CLI_ROLLBACK_CANCELLED_NOTICE)
            return

        result = checkpointer.execute_rollback(plan)
        for item in result.items:
            if item.action == "restored":
                print(CLI_ROLLBACK_RESULT_RESTORED.format(rel_path=item.rel_path))
            elif item.action == "deleted":
                print(CLI_ROLLBACK_RESULT_DELETED.format(rel_path=item.rel_path))
            elif item.action == "skipped":
                print(
                    CLI_ROLLBACK_RESULT_SKIPPED.format(
                        rel_path=item.rel_path,
                        reason=item.reason,
                    )
                )
        print(
            CLI_ROLLBACK_RESULT_SUMMARY.format(
                rolled_back=result.rolled_back_count,
                skipped=result.skipped_count,
            )
        )
        if result.rolled_back_count >= 1:
            print(CLI_ROLLBACK_MODEL_UNAWARE_NOTICE)
    except Exception as e:
        print(CLI_ROLLBACK_ERROR_NOTICE.format(reason=str(e)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Agent Terminal CLI")
    parser.add_argument("--mock", action="store_true", help="Run with built-in Mock LLM without API keys")
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Model name (default: {DEFAULT_REAL_MODEL} in real mode)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=CONTEXT_WINDOW_TOKENS,
        help=f"Context window token limit (default: {CONTEXT_WINDOW_TOKENS})",
    )
    parser.add_argument(
        "--delegate",
        action="store_true",
        help="Enable subagent delegation (delegate_task) in real model mode",
    )
    parser.add_argument(
        "--no-skills-index",
        action="store_true",
        help="Disable the skills index in the system prompt (skills stay available via the skills_list/skill_view tools)",
    )
    parser.add_argument(
        "--no-approval",
        action="store_true",
        help="Disable command approval (commands outside the allowed list are denied, as before)",
    )
    parser.add_argument(
        "--no-checkpoints",
        action="store_true",
        help="Disable file checkpoints (write_file/patch changes cannot be rolled back with /rollback)",
    )
    args = parser.parse_args()

    if args.delegate and args.mock:
        print(
            "Error: --delegate cannot be used with --mock (delegation needs a real model).",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.context_window < 1:
        print(
            f"Error: --context-window must be a positive integer (got {args.context_window}).",
            file=sys.stderr,
        )
        sys.exit(2)

    model = args.model
    llm_client = None

    if args.mock:
        # 스모크 및 데모용 Mock 응답 사전 준비
        mock_client = MockLLMClient()
        mock_client.queue_response(
            content=None,
            tool_calls=[{
                "id": "mock_call_1",
                "function": {
                    "name": "memory",
                    "arguments": '{"action": "add", "target": "user", "content": "User prefers CLI interaction."}',
                },
            }],
        )
        mock_client.queue_response(
            content="[Mock] 사용자 선호 사항을 메모리에 저장했습니다. 무엇을 도와드릴까요?",
        )
        llm_client = mock_client
    else:
        # 실제 모델 모드: 환경변수 검사
        api_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
        if not api_key:
            print(
                f"Error: {OPENROUTER_API_KEY_ENV} environment variable is not set. "
                "Run with --mock to use the built-in Mock LLM without API keys.",
                file=sys.stderr,
            )
            sys.exit(2)

        # HERMES_HOME 상대경로 고정
        if "HERMES_HOME" in os.environ:
            os.environ["HERMES_HOME"] = str(get_hermes_home())

        # 작업 폴더 이동 (.hermes/workspace)
        workspace = get_default_workspace()
        os.chdir(workspace)

        if model is None:
            model = DEFAULT_REAL_MODEL

        llm_client = OpenRouterClient(api_key=api_key, model=model)

    approval_enabled = not args.no_approval and is_interactive_terminal()
    session_id = str(uuid.uuid4())
    session_db = SessionDB()
    memory_store = MemoryStore()
    approver = (
        Approver(
            session_db=session_db,
            session_id=session_id,
            ask=lambda p: read_user_input(p, discard_typeahead=True),
            is_interactive=is_interactive_terminal,
        )
        if approval_enabled
        else None
    )
    checkpointer = (
        Checkpointer(session_db, session_id, Path.cwd())
        if not args.no_checkpoints
        else None
    )
    dispatcher = create_default_dispatcher(
        memory_store=memory_store,
        session_db=session_db,
        approver=approver,
        checkpointer=checkpointer,
    )
    agent = HermesAgent(
        model=model,
        llm_client=llm_client,
        context_window_tokens=args.context_window,
        skills_index=not args.no_skills_index,
        session_id=session_id,
        session_db=session_db,
        memory_store=memory_store,
        dispatcher=dispatcher,
    )

    if not args.mock and args.delegate:
        register_delegate_tool(
            agent.dispatcher,
            session_db=agent.session_db,
            parent_session_id=agent.session_id,
            llm_client_factory=lambda: OpenRouterClient(api_key=api_key, model=model),
        )

    print("=" * 60)
    print(f"🤖 Hermes Core CLI Started (Session: {agent.session_id[:8]}...)")
    if args.mock:
        print("💡 Running in MOCK mode (Offline test).")
    else:
        print(f"🚀 Running in REAL mode (Model: {agent.model}).")
        print(f"📁 Working directory: {os.getcwd()}")
    if not args.mock and args.delegate:
        print("Delegation enabled (delegate_task).")
    if args.no_skills_index:
        print("Skills index disabled (--no-skills-index).")
    if args.no_approval:
        print(CLI_APPROVAL_DISABLED_NOTICE)
    elif approval_enabled:
        print(CLI_APPROVAL_ENABLED_NOTICE)
    if args.no_checkpoints:
        print(CLI_CHECKPOINT_DISABLED_NOTICE)
    else:
        print(CLI_CHECKPOINT_ENABLED_NOTICE)
    print("Type 'exit' or 'quit' to terminate.")
    if multiline_input_available():
        print(CLI_MULTILINE_HINT)
    print("=" * 60)

    # Mock 모드 단발성 실행 시 1턴 자동 구동 후 종료 가능하게 처리
    if args.mock and not sys.stdin.isatty():
        if checkpointer is not None:
            checkpointer.begin_turn()
        res = agent.run_conversation("Hello Hermes, remember that I prefer CLI.")
        print(f"\nAssistant: {res['final_response']}\n")
        agent.session_db.end_session(agent.session_id, "completed")
        print("Mock session finished cleanly.")
        return

    try:
        history = None
        checkpoint_refusal_notified = False
        while True:
            try:
                print()
                user_input = read_user_input().strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting session...")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                agent.session_db.end_session(agent.session_id, "user_exit")
                print("Session saved and closed. Goodbye!")
                break

            tokens = user_input.split()
            if tokens and tokens[0].lower() == "/rollback":
                _handle_rollback(tokens, checkpointer)
                continue

            if checkpointer is not None:
                checkpointer.begin_turn()

            # 처리 중에 미리 친 키는 다음 프롬프트로 그대로 넘긴다 (승인 질문은 따로 버린다)
            with hold_typeahead():
                result = agent.run_conversation(user_input, conversation_history=history)
                history = result["messages"]
                if result.get("stopped_reason") == "llm_error":
                    err = result.get("error", {})
                    kind = err.get("kind", "unknown")
                    msg = err.get("message", "Unknown error")
                    print(f"\nModel call failed ({kind}): {msg}")
                else:
                    print(f"\nAssistant: {result['final_response']}")

            if checkpointer is not None and not checkpoint_refusal_notified:
                messages = result.get("messages", [])
                has_checkpoint_refused = any(
                    isinstance(m, dict)
                    and m.get("role") == "tool"
                    and isinstance(m.get("content"), str)
                    and CHECKPOINT_REFUSED_HINT in m.get("content")
                    for m in messages
                )
                if has_checkpoint_refused:
                    print(CLI_CHECKPOINT_REFUSED_NOTICE)
                    checkpoint_refusal_notified = True
    finally:
        agent.session_db.close()


if __name__ == "__main__":
    main()
