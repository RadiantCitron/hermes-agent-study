"""외부 의존성 없이 `.env` 파일을 읽는 경량 환경변수 로더.

[원본 레퍼런스 매핑]
- 대응 원본: hermes-agent/hermes_cli/env_loader.py
- 연구/격리 차이점:
  1. 원본은 `python-dotenv`와 다중 프로필·외부 시크릿 소스(Bitwarden 등) 캐시를 사용하지만,
     본 학습용 코어는 표준 라이브러리만 사용해 단일 `.env` 파일을 파싱한다.
  2. 이미 셸에 설정된 비어 있지 않은 환경변수는 절대 덮어쓰지 않고(`setdefault` 의미론)
     비어 있거나 누락된 키만 보충하며, 파일이 없거나 손상된 경우에도 예외 없이 경고 후 무동작한다.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import MutableMapping, Optional

logger = logging.getLogger(__name__)

_ESCAPE_MAP = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
    "\\": "\\",
}
_ESCAPE_RE = re.compile(r'\\([nrt"\\])')


def _strip_inline_comment(raw: str) -> str:
    """따옴표 없는 값에서 인라인 주석(` #...` 또는 `#...` 시작)을 제거하고 공백을 정리한다."""
    trimmed = raw.strip()
    if not trimmed:
        return ""
    if trimmed.startswith("#"):
        return ""
    comment_idx = trimmed.find(" #")
    if comment_idx != -1:
        trimmed = trimmed[:comment_idx].rstrip()
    return trimmed


def _parse_value(raw_val: str) -> str:
    """`.env` 우변 값을 따옴표·이스케이프·인라인 주석 규칙에 따라 정규화한다."""
    trimmed = raw_val.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in ('"', "'"):
        inner = trimmed[1:-1]
        if trimmed[0] == '"':
            return _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(1)], inner)
        return inner
    return _strip_inline_comment(raw_val)


def parse_dotenv_lines(text: str) -> dict[str, str]:
    """`.env` 텍스트 본문을 파싱해 `키 -> 값` 딕셔너리를 반환한다."""
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key or any(ch.isspace() for ch in key):
            continue
        parsed[key] = _parse_value(raw_val)
    return parsed


def load_dotenv_file(
    dotenv_path: Optional[Path] = None,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
) -> list[str]:
    """`.env` 파일을 읽어 비어 있거나 없는 환경변수만 채우고, 반영된 키 목록을 반환한다."""
    target = Path(".env") if dotenv_path is None else Path(dotenv_path)
    if not target.is_file():
        return []

    try:
        text = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        logger.warning("Failed to read dotenv file %s: %s", target, exc)
        return []

    parsed = parse_dotenv_lines(text)
    target_env: MutableMapping[str, str] = os.environ if environ is None else environ
    loaded_keys: list[str] = []

    for key, value in parsed.items():
        existing = target_env.get(key)
        if existing is not None and existing.strip() != "":
            continue
        if not value:
            continue
        target_env[key] = value
        loaded_keys.append(key)

    return loaded_keys
