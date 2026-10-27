"""Hermes Agent 터미널 대화형 CLI 진입점.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/cli.py
- 실행 방법:
  - `python -m hermes_core.cli`
  - `python -m hermes_core.cli --mock` (오프라인 테스트 및 스모크 실행 모드)
"""

import argparse
import sys

from hermes_core.agent import HermesAgent, MockLLMClient, MockLLMResponse


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes Agent Terminal CLI")
    parser.add_argument("--mock", action="store_true", help="Run with built-in Mock LLM without API keys")
    parser.add_argument("--model", type=str, default="gpt-4o", help="Model name (default: gpt-4o)")
    args = parser.parse_args()

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

    agent = HermesAgent(
        model=args.model,
        llm_client=llm_client,
    )

    print("=" * 60)
    print(f"🤖 Hermes Core CLI Started (Session: {agent.session_id[:8]}...)")
    if args.mock:
        print("💡 Running in MOCK mode (Offline test).")
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
            print(f"\nAssistant: {result['final_response']}")
    finally:
        agent.session_db.close()


if __name__ == "__main__":
    main()
