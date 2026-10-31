"""세션 과거 대화 검색 도구 (Session Search Tool).

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/tools/session_search_tool.py
- 핵심 원리:
  - SQLite FTS5 전문 검색을 통해 이전 세션의 키워드 및 하이라이트 스니펫을 조회.
"""

import json
from typing import Optional

from hermes_core.tools.ports import SessionSearchPort

SESSION_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_search",
        "description": "Search past conversation transcripts using SQLite FTS5 full-text search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or search phrases to find in past conversations.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of matching sessions/messages to return (default: 3).",
                    "default": 3,
                },
            },
            "required": ["query"],
        },
    },
}


def session_search_tool(
    query: str,
    limit: int = 3,
    db: Optional[SessionSearchPort] = None,
) -> str:
    """SessionDB의 FTS5 검색 엔진을 호출하여 검색 결과를 JSON 문자열로 반환."""
    if db is None:
        return json.dumps(
            {"success": False, "error": "Session database is not available."},
            ensure_ascii=False,
        )

    try:
        matches = db.search_messages(query=query, limit=limit)
        results = []
        for m in matches:
            results.append({
                "session_id": m.get("session_id"),
                "role": m.get("role"),
                "snippet": m.get("snippet"),
                "timestamp": m.get("timestamp"),
            })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(results),
            "results": results,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Search failed: {str(e)}",
        }, ensure_ascii=False)
