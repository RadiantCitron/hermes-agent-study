"""`.env` 로더 및 다중 LLM 키 보안·격리 기반 인수 테스트 (R23-010)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_core.constants import (
    ANTHROPIC_API_KEY_ENV,
    GEMINI_API_KEY_ENV,
    OPENROUTER_API_KEY_ENV,
    TERMINAL_STRIPPED_ENV_NAMES,
)
from hermes_core.control.snapshot import mask_secrets
from hermes_core.env_loader import load_dotenv_file
from hermes_core.tools.ports import ApprovalOutcome
from hermes_core.tools.terminal_tool import terminal_tool


def test_load_dotenv_basic_quotes_and_export(tmp_path: Path) -> None:
    """시나리오 1: 기본 키·값, 따옴표, export 접두사, 인라인 주석을 파싱한다."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "# comment\n\n"
        'export OPENROUTER_API_KEY="k1"\n'
        "GEMINI_API_KEY='k2'\n"
        "ANTHROPIC_API_KEY=k3 # inline\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}

    loaded = load_dotenv_file(dotenv_path, environ=env)

    assert env == {
        "OPENROUTER_API_KEY": "k1",
        "GEMINI_API_KEY": "k2",
        "ANTHROPIC_API_KEY": "k3",
    }
    assert set(loaded) == {"OPENROUTER_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"}


def test_load_dotenv_preserves_existing_shell_env(tmp_path: Path) -> None:
    """시나리오 2: 기존 셸 환경변수는 덮어쓰지 않고(setdefault) 누락된 키만 추가한다."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "GEMINI_API_KEY=file-key\nANTHROPIC_API_KEY=file-ant\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {"GEMINI_API_KEY": "shell-key"}

    loaded = load_dotenv_file(dotenv_path, environ=env)

    assert env["GEMINI_API_KEY"] == "shell-key"
    assert env["ANTHROPIC_API_KEY"] == "file-ant"
    assert loaded == ["ANTHROPIC_API_KEY"]


def test_load_dotenv_fills_empty_shell_env_and_ignores_empty_file_value(tmp_path: Path) -> None:
    """시나리오 3: 빈 셸 환경변수는 보충하되, `.env` 내 빈 값은 반영하지 않는다."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "GEMINI_API_KEY=from-file\nEMPTY_KEY=\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {"GEMINI_API_KEY": ""}

    loaded = load_dotenv_file(dotenv_path, environ=env)

    assert env["GEMINI_API_KEY"] == "from-file"
    assert "EMPTY_KEY" not in env
    assert loaded == ["GEMINI_API_KEY"]


def test_load_dotenv_missing_and_corrupted_utf8_are_safe(tmp_path: Path) -> None:
    """시나리오 4: `.env` 부재 및 손상된 UTF-8 바이트에도 예외 없이 무동작한다."""
    missing_path = tmp_path / "nonexistent.env"
    env: dict[str, str] = {"KEEP": "1"}

    assert load_dotenv_file(missing_path, environ=env) == []
    assert env == {"KEEP": "1"}

    corrupted_path = tmp_path / ".env"
    corrupted_path.write_bytes(b"\xff\xfe")

    assert load_dotenv_file(corrupted_path, environ=env) == []
    assert env == {"KEEP": "1"}


def test_load_dotenv_ignores_malformed_lines(tmp_path: Path) -> None:
    """시나리오 5: `=`가 없거나 키 이름이 빈 줄은 무시하고 유효한 줄만 반영한다."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "NO_EQUALS_LINE\n=missing_key\nVALID=ok\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}

    loaded = load_dotenv_file(dotenv_path, environ=env)

    assert env == {"VALID": "ok"}
    assert loaded == ["VALID"]


def test_terminal_strips_all_three_provider_keys(tmp_path: Path, monkeypatch) -> None:
    """시나리오 6: 터미널 자식 프로세스에서 3종 프로바이더 키가 모두 제거된다."""
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "dummy-openrouter")
    monkeypatch.setenv(GEMINI_API_KEY_ENV, "dummy-gemini")
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "dummy-anthropic")

    assert set(TERMINAL_STRIPPED_ENV_NAMES) >= {
        OPENROUTER_API_KEY_ENV,
        GEMINI_API_KEY_ENV,
        ANTHROPIC_API_KEY_ENV,
    }

    py_code = (
        "import os, json; "
        "keys = ['OPENROUTER_API_KEY', 'GEMINI_API_KEY', 'ANTHROPIC_API_KEY']; "
        "print(json.dumps({k: os.environ.get(k, '<unset>') for k in keys}))"
    )
    class _AlwaysApprove:
        def request(self, argv):
            return ApprovalOutcome(approved=True, decision="approved")

    raw = terminal_tool(
        command=f'python3 -c "{py_code}"',
        workspace=tmp_path,
        approver=_AlwaysApprove(),
    )
    payload = json.loads(raw)

    assert payload["success"] is True
    child_env = json.loads(payload["stdout"])
    assert child_env == {
        "OPENROUTER_API_KEY": "<unset>",
        "GEMINI_API_KEY": "<unset>",
        "ANTHROPIC_API_KEY": "<unset>",
    }


def test_mask_secrets_masks_gemini_and_anthropic_keys() -> None:
    """시나리오 7: `mask_secrets`가 Gemini(AIzaSy...)와 Anthropic(sk-ant-...) 키를 마스킹한다."""
    gemini_tok = "AIzaSy" + "DummyTokenValue12345678901234"
    anthropic_tok = "sk-ant-" + "DummyToken1234567890"

    masked_gemini = mask_secrets(f"key={gemini_tok}")
    masked_anthropic = mask_secrets(f"key={anthropic_tok}")

    assert masked_gemini == "key=***"
    assert gemini_tok not in masked_gemini
    assert masked_anthropic == "key=***"
    assert anthropic_tok not in masked_anthropic


def test_conftest_cleans_all_llm_env_keys_and_provider() -> None:
    """시나리오 9: 코어 테스트 시작 시 3종 키와 HERMES_PROVIDER가 모두 제거되어 있다."""
    for name in (
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HERMES_PROVIDER",
    ):
        assert name not in os.environ
