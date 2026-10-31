"""Hermes Agent 터미널 대화형 CLI 진입점.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/cli.py
- 실행 방법:
  - `python -m hermes_core.cli --mock` (오프라인 테스트 및 스모크 실행 모드)
  - `python -m hermes_core.cli` (실제 모델 모드: OPENROUTER_API_KEY 환경변수 필요)
"""

import argparse
import logging
import os
from pathlib import Path
import sys
from typing import Optional
import uuid

from hermes_core.agent import HermesAgent, MockLLMClient
from hermes_core.assembly import apply_runtime_settings, create_agent
from hermes_core.control import ControlPanel, SettingsError, load_settings
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
    CLI_INTERRUPT_MODEL_WILL_BE_TOLD_NOTICE,
    CLI_MULTILINE_HINT,
    CLI_RESUME_AMBIGUOUS_ERROR,
    CLI_RESUME_MOCK_ERROR,
    CLI_RESUME_NOT_FOUND_ERROR,
    CLI_RESUMED_BANNER_NOTICE,
    CLI_ROLLBACK_ABORTED_NOTICE,
    CLI_ROLLBACK_CANCELLED_NOTICE,
    CLI_ROLLBACK_DISABLED_NOTICE,
    CLI_ROLLBACK_ERROR_NOTICE,
    CLI_ROLLBACK_EXTERNAL_WARNING,
    CLI_ROLLBACK_HEADER,
    CLI_ROLLBACK_LINE_DELETE,
    CLI_ROLLBACK_LINE_RESTORE,
    CLI_ROLLBACK_LINE_SKIP,
    CLI_ROLLBACK_MODEL_WILL_BE_TOLD_NOTICE,
    CLI_ROLLBACK_NOTHING_NOTICE,
    CLI_ROLLBACK_PENDING_WARNING,
    CLI_ROLLBACK_PROMPT,
    CLI_ROLLBACK_RESULT_DELETED,
    CLI_ROLLBACK_RESULT_RESTORED,
    CLI_ROLLBACK_RESULT_SKIPPED,
    CLI_ROLLBACK_RESULT_SUMMARY,
    CLI_ROLLBACK_USAGE_NOTICE,
    CLI_STUCK_LOOP_NOTICE,
    CLI_TURN_CANCELLED_NOTICE,
    CLI_UNSENT_NOTICE_ON_EXIT,
    CONTEXT_WINDOW_TOKENS,
    DEFAULT_REAL_MODEL,
    OPENROUTER_API_KEY_ENV,
    SESSION_END_EOF,
    SESSION_END_ERROR,
    SESSION_END_USER_EXIT,
    SESSION_END_USER_INTERRUPT,
    get_default_workspace,
    get_hermes_home,
)
from hermes_core.llm_client import OpenRouterClient
from hermes_core.session_db import SessionDB
from hermes_core.tools.approval import Approver, is_approval_answer
from hermes_core.tools.checkpoint import (
    Checkpointer,
    InterruptedTurn,
    RollbackResult,
    format_pending_notices,
    format_rollback_notice,
)
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import create_default_dispatcher
from hermes_core.tools.memory_tool import MemoryStore

logger = logging.getLogger(__name__)


def _close_session(session_db: SessionDB, session_id: str, end_reason: str) -> None:
    """세션에 종료 사유를 한 번만 남긴다. 이미 끝난 세션은 건드리지 않는다."""
    try:
        session = session_db.get_session(session_id)
        if session is None:
            return
        if session.get("end_reason"):
            return
        session_db.end_session(session_id, end_reason)
    except Exception as e:
        logger.warning(
            "Failed to close session %s with end_reason '%s': %s",
            session_id,
            end_reason,
            e,
        )


def _handle_rollback(
    tokens: list[str], checkpointer: Checkpointer | None
) -> Optional[RollbackResult]:
    """REPL의 /rollback 명령을 처리한다.

    되돌리기가 실행된 경우 RollbackResult를 반환하고, 취소·오류·미실행 시 None을 반환한다.
    """
    if checkpointer is None:
        print(CLI_ROLLBACK_DISABLED_NOTICE)
        return None

    if len(tokens) > 1:
        print(CLI_ROLLBACK_USAGE_NOTICE)
        return None

    try:
        plan = checkpointer.plan_rollback()
        if not plan.items:
            print(CLI_ROLLBACK_NOTHING_NOTICE)
            return None

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
            return None

        if not is_approval_answer(answer):
            print(CLI_ROLLBACK_CANCELLED_NOTICE)
            return None

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
        if result.error:
            print(CLI_ROLLBACK_ABORTED_NOTICE.format(reason=result.error))
        if result.rolled_back_count >= 1:
            print(CLI_ROLLBACK_MODEL_WILL_BE_TOLD_NOTICE)
        return result
    except Exception as e:
        print(CLI_ROLLBACK_ERROR_NOTICE.format(reason=str(e)))
        return None


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
        default=None,
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
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="Resume the latest CLI session, or a session matching the given ID/prefix",
    )
    args = parser.parse_args()

    if args.delegate and args.mock:
        print(
            "Error: --delegate cannot be used with --mock (delegation needs a real model).",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.resume is not None and args.mock:
        print(CLI_RESUME_MOCK_ERROR, file=sys.stderr)
        sys.exit(2)

    cli_overrides: dict[str, Any] = {}
    if args.model is not None:
        cli_overrides["model"] = args.model
    if args.context_window is not None:
        cli_overrides["context_window_tokens"] = args.context_window
    if args.delegate:
        cli_overrides["delegate"] = True
    if args.no_skills_index:
        cli_overrides["skills_index"] = False
    if args.no_approval:
        cli_overrides["approval_enabled"] = False
    if args.no_checkpoints:
        cli_overrides["checkpoints_enabled"] = False

    try:
        loaded = load_settings(cli_overrides=cli_overrides)
    except SettingsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    settings = loaded.settings
    model = settings.model
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

    approval_enabled = settings.approval_enabled and is_interactive_terminal()
    session_db = SessionDB()

    is_resuming = args.resume is not None
    if is_resuming:
        target_arg = args.resume.strip()
        matches = session_db.find_resumable_session(target_arg)
        if not matches:
            detail = (
                ""
                if target_arg.lower() == "latest"
                else f" matching '{target_arg}'"
            )
            print(CLI_RESUME_NOT_FOUND_ERROR.format(detail=detail), file=sys.stderr)
            session_db.close()
            sys.exit(2)
        if len(matches) > 1:
            short_ids = ", ".join(m["id"][:8] for m in matches[:5])
            print(
                CLI_RESUME_AMBIGUOUS_ERROR.format(prefix=target_arg, matches=short_ids),
                file=sys.stderr,
            )
            session_db.close()
            sys.exit(2)
        session_id = matches[0]["id"]
    else:
        session_id = str(uuid.uuid4())

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
        if settings.checkpoints_enabled
        else None
    )
    restored_turn = (
        checkpointer.restore_from_db()
        if (is_resuming and checkpointer is not None)
        else 0
    )
    dispatcher = create_default_dispatcher(
        memory_store=memory_store,
        session_db=session_db,
        approver=approver,
        checkpointer=checkpointer,
    )
    agent = create_agent(
        settings,
        model=model,
        llm_client=llm_client,
        session_id=session_id,
        session_db=session_db,
        memory_store=memory_store,
        dispatcher=dispatcher,
        resume=is_resuming,
    )
    panel = ControlPanel(
        agent,
        loaded,
        session_db,
        apply_settings_fn=apply_runtime_settings,
    )

    if not args.mock and settings.delegate:
        register_delegate_tool(
            agent.dispatcher,
            session_db=agent.session_db,
            parent_session_id=agent.session_id,
            llm_client_factory=lambda: OpenRouterClient(api_key=api_key, model=model),
        )

    print("=" * 60)
    print(f"🤖 Hermes Core CLI Started (Session: {agent.session_id[:8]}...)")
    if is_resuming:
        initial_history = agent.load_conversation_history()
        restored_msg_count = (
            0 if initial_history is None else max(0, len(initial_history) - 1)
        )
        print(
            CLI_RESUMED_BANNER_NOTICE.format(
                session_id=agent.session_id[:8],
                message_count=restored_msg_count,
                turn_index=restored_turn,
            )
        )
    else:
        initial_history = None
    if args.mock:
        print("💡 Running in MOCK mode (Offline test).")
    else:
        print(f"🚀 Running in REAL mode (Model: {agent.model}).")
        print(f"📁 Working directory: {os.getcwd()}")
    if not args.mock and settings.delegate:
        print("Delegation enabled (delegate_task).")
    if not settings.skills_index:
        print("Skills index disabled (--no-skills-index).")
    if not settings.approval_enabled:
        print(CLI_APPROVAL_DISABLED_NOTICE)
    elif approval_enabled:
        print(CLI_APPROVAL_ENABLED_NOTICE)
    if not settings.checkpoints_enabled:
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

    session_end_reason = SESSION_END_ERROR
    pending_notices: list[RollbackResult | InterruptedTurn] = []
    try:
        history = initial_history
        checkpoint_refusal_notified = False
        while True:
            try:
                print()
                user_input = read_user_input().strip()
            except KeyboardInterrupt:
                session_end_reason = SESSION_END_USER_INTERRUPT
                print("\nExiting session...")
                break
            except EOFError:
                session_end_reason = SESSION_END_EOF
                print("\nExiting session...")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                session_end_reason = SESSION_END_USER_EXIT
                print("Session saved and closed. Goodbye!")
                break

            tokens = user_input.split()
            if tokens and tokens[0].lower() == "/rollback":
                rollback_res = _handle_rollback(tokens, checkpointer)
                if rollback_res is not None:
                    pending_notices.append(rollback_res)
                continue

            panel.apply_pending()

            if checkpointer is not None:
                checkpointer.begin_turn()

            notice = format_pending_notices(pending_notices)
            delivered = list(pending_notices)
            pending_notices.clear()
            message = f"{notice}\n\n{user_input}" if notice else user_input

            last_active_msg_id = agent.session_db.get_last_active_message_id(
                agent.session_id
            )
            # 처리 중에 미리 친 키는 다음 프롬프트로 그대로 넘긴다 (승인 질문은 따로 버린다)
            try:
                with hold_typeahead():
                    result = agent.run_conversation(message, conversation_history=history)
                    history = result["messages"]
                    if result.get("stopped_reason") == "llm_error":
                        err = result.get("error", {})
                        kind = err.get("kind", "unknown")
                        msg = err.get("message", "Unknown error")
                        print(f"\nModel call failed ({kind}): {msg}")
                    elif result.get("stopped_reason") == "stuck_loop":
                        print(f"\n{CLI_STUCK_LOOP_NOTICE}")
                    else:
                        print(f"\nAssistant: {result['final_response']}")
            except KeyboardInterrupt:
                print(f"\n{CLI_TURN_CANCELLED_NOTICE}")
                pending_notices[:0] = delivered
                try:
                    agent.session_db.deactivate_messages_after(
                        agent.session_id, last_active_msg_id
                    )
                except Exception as e:
                    logger.warning("deactivate_messages_after failed: %s", e)
                if checkpointer is not None:
                    try:
                        interrupted = checkpointer.collect_interrupted_turn()
                    except Exception as e:
                        logger.warning("collect_interrupted_turn failed: %s", e)
                        interrupted = None
                    if interrupted is not None:
                        pending_notices.append(interrupted)
                        print(CLI_INTERRUPT_MODEL_WILL_BE_TOLD_NOTICE)
                continue

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
        if format_pending_notices(pending_notices) is not None:
            print(CLI_UNSENT_NOTICE_ON_EXIT)
        _close_session(agent.session_db, agent.session_id, session_end_reason)
        agent.session_db.close()


if __name__ == "__main__":
    main()
