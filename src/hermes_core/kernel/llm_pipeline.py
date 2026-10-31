"""LLM 호출 파이프라인(미들웨어 체인).

루프는 LLM을 이 파이프라인을 통해서만 부른다.
미들웨어는 next를 한 번 불러 그 결과(또는 다른 응답)를 돌려주거나,
부르지 않고 응답을 돌려줄 수 있다(단락).
예외는 바꾸지 않고 그대로 올려 보낸다(체인은 아무것도 잡지 않는다).
요청의 messages를 고치면 안 된다 — 커널은 이를 검사하지 않으며,
등록 창구는 조립 지점뿐이다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from hermes_core.kernel.contract import TurnScope
from hermes_core.kernel.ports import LLMPort, LLMReply


@dataclass(frozen=True)
class LLMRequest:
    """LLM 호출 요청 데이터."""

    messages: list[dict[str, Any]]
    tools: Optional[list[dict[str, Any]]]
    scope: TurnScope


LLMNext = Callable[[LLMRequest], LLMReply]
LLMMiddleware = Callable[[LLMRequest, LLMNext], LLMReply]


def build_llm_chain(
    llm: LLMPort,
    middlewares: Sequence[LLMMiddleware],
) -> LLMNext:
    """LLM 포트와 미들웨어 목록으로 실행 체인을 조립한다.

    middlewares[0]이 가장 바깥에서 먼저 실행된다.
    """
    frozen_middlewares = tuple(middlewares)

    def base_call(request: LLMRequest) -> LLMReply:
        return llm.chat_completion(
            messages=request.messages,
            tools=request.tools,
        )

    current: LLMNext = base_call
    for mw in reversed(frozen_middlewares):
        nxt = current

        def make_step(m: LLMMiddleware, n: LLMNext) -> LLMNext:
            return lambda req: m(req, n)

        current = make_step(mw, nxt)

    return current
