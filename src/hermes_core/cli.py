"""Hermes Agent 터미널 대화형 CLI 진입점.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/cli.py
- 실행 방법:
  - `python -m hermes_core.cli --mock` (오프라인 테스트 및 스모크 실행 모드)
  - `python -m hermes_core.cli` (실제 모델 모드: OPENROUTER_API_KEY 환경변수 필요)
"""

import argparse
import os
import sys

from hermes_core.agent import HermesAgent, MockLLMClient
from hermes_core.constants import (
    DEFAULT_REAL_MODEL,
    OPENROUTER_API_KEY_ENV,
    get_default_workspace,
    get_hermes_home,
)
from hermes_core.llm_client import OpenRouterClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Agent Terminal CLI")
    parser.add_argument("--mock", action="store_true", help="Run with built-in Mock LLM without API keys")
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Model name (default: {DEFAULT_REAL_MODEL} in real mode)",
    )
    args = parser.parse_args()

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

    agent = HermesAgent(
        model=model,
        llm_client=llm_client,
    )

    print("=" * 60)
    print(f"🤖 Hermes Core CLI Started (Session: {agent.session_id[:8]}...)")
    if args.mock:
        print("💡 Running in MOCK mode (Offline test).")
    else:
        print(f"🚀 Running in REAL mode (Model: {agent.model}).")
        print(f"📁 Working directory: {os.getcwd()}")
    print("Type 'exit' or 'quit' to terminate.")
    print("=" * 60)

    # Mock 모드 단발성 실행 시 1턴 자동 구동 후 종료 가능하게 처리
    if args.mock and not sys.stdin.isatty():
        res = agent.run_conversation("Hello Hermes, remember that I prefer CLI.")
        print(f"\nAssistant: {res['final_response']}\n")
        agent.session_db.end_session(agent.session_id, "completed")
        print("Mock session finished cleanly.")
        return

    try:
        while True:
            try:
                user_input = input("\nUser > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting session...")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                agent.session_db.end_session(agent.session_id, "user_exit")
                print("Session saved and closed. Goodbye!")
                break

            result = agent.run_conversation(user_input)
            if result.get("stopped_reason") == "llm_error":
                err = result.get("error", {})
                kind = err.get("kind", "unknown")
                msg = err.get("message", "Unknown error")
                print(f"\nModel call failed ({kind}): {msg}")
            else:
                print(f"\nAssistant: {result['final_response']}")
    finally:
        agent.session_db.close()


if __name__ == "__main__":
    main()
