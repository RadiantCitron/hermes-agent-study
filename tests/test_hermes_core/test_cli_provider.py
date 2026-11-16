"""CLI `--provider`, `.env` 로딩, 위임 연동 및 Fail-Closed 인수 테스트 (R23-040)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx
import pytest

from hermes_core import cli
from hermes_core.constants import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_API_VERSION,
    ANTHROPIC_MESSAGES_URL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_GEMINI_MODEL,
    GEMINI_API_KEY_ENV,
    GEMINI_OPENAI_CHAT_URL,
    OPENROUTER_API_KEY_ENV,
)
from hermes_core.session_db import SessionDB


def _run_cli_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    stdin_text: str,
    transport: httpx.BaseTransport,
) -> None:
    orig_create = cli.create_llm_client
    monkeypatch.setattr(
        cli,
        "create_llm_client",
        lambda resolved, **kw: orig_create(resolved, transport=transport, **kw),
    )
    monkeypatch.setattr(sys, "argv", ["hermes-core", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    cli.main()


def _openai_ok_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


def _anthropic_text_response(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg-test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 13, "output_tokens": 9},
        },
    )


def test_040_scenario_01_dotenv_loaded_before_chdir_and_preserves_shell_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 1: os.chdir 이전 시작 폴더의 .env를 읽고 기존 셸 env는 덮어쓰지 않는다."""
    start_dir = tmp_path / "project_root"
    start_dir.mkdir()
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    dotenv_gemini_key = "dotenv-gemini-key-should-be-ignored"
    shell_gemini_key = "shell-gemini-key-priority-040"
    (start_dir / ".env").write_text(
        f"{GEMINI_API_KEY_ENV}={dotenv_gemini_key}\nHERMES_PROVIDER=gemini\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(GEMINI_API_KEY_ENV, shell_gemini_key)
    monkeypatch.chdir(start_dir)

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return _openai_ok_response("gemini dotenv ok")

    try:
        _run_cli_with_transport(
            monkeypatch,
            [],
            "안녕\nexit\n",
            httpx.MockTransport(handler),
        )
    finally:
        monkeypatch.chdir(tmp_path)

    out = capsys.readouterr().out
    assert f"Provider: gemini, Model: {DEFAULT_GEMINI_MODEL}" in out
    assert "gemini dotenv ok" in out
    assert shell_gemini_key not in out
    assert dotenv_gemini_key not in out

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert str(req.url) == GEMINI_OPENAI_CHAT_URL
    assert req.headers["authorization"] == f"Bearer {shell_gemini_key}"


def test_040_scenario_02_mock_mode_ignores_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 2: --mock 모드에서는 시작 폴더에 .env가 있어도 로드하지 않는다."""
    start_dir = tmp_path / "project_root"
    start_dir.mkdir()
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    (start_dir / ".env").write_text(
        f"{ANTHROPIC_API_KEY_ENV}=dotenv-mock-ignored\nHERMES_PROVIDER=anthropic\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(start_dir)
    monkeypatch.setattr(sys, "argv", ["hermes-core", "--mock"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    try:
        cli.main()
    finally:
        monkeypatch.chdir(tmp_path)

    out = capsys.readouterr().out
    assert "Mock session finished cleanly." in out
    assert ANTHROPIC_API_KEY_ENV not in os.environ
    assert "HERMES_PROVIDER" not in os.environ


def test_040_scenario_03_cli_provider_gemini_e2e_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 3: --provider gemini 실행 시 Gemini 엔드포인트 호출, 배너, llm_calls 원장 및 Zero Leakage."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    fake_key = "test-gemini-cli-key-040"
    monkeypatch.setenv(GEMINI_API_KEY_ENV, fake_key)
    monkeypatch.chdir(tmp_path)

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return _openai_ok_response("gemini reply")

    _run_cli_with_transport(
        monkeypatch,
        ["--provider", "gemini"],
        "질문\nexit\n",
        httpx.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert f"Provider: gemini, Model: {DEFAULT_GEMINI_MODEL}" in captured.out
    assert "gemini reply" in captured.out
    assert fake_key not in captured.out
    assert fake_key not in captured.err

    assert len(captured_requests) == 1
    assert str(captured_requests[0].url) == GEMINI_OPENAI_CHAT_URL
    assert captured_requests[0].headers["authorization"] == f"Bearer {fake_key}"

    db = SessionDB(hermes_home / "state.db")
    try:
        sess = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1").fetchone()
        assert sess["model"] == DEFAULT_GEMINI_MODEL
        assert sess["mode"] == "real"

        llm_call_cnt = db._conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        assert llm_call_cnt == 1
        dump_text = "\n".join(db._conn.iterdump())
        assert fake_key not in dump_text
    finally:
        db.close()


def test_040_scenario_04_cli_provider_anthropic_e2e_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 4: --provider anthropic 실행 시 Messages API 호출, 배너, llm_calls 원장 및 Zero Leakage."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    fake_key = "test-anthropic-cli-key-040"
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, fake_key)
    monkeypatch.chdir(tmp_path)

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return _anthropic_text_response("claude reply")

    _run_cli_with_transport(
        monkeypatch,
        ["--provider", "anthropic"],
        "질문\nexit\n",
        httpx.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert f"Provider: anthropic, Model: {DEFAULT_ANTHROPIC_MODEL}" in captured.out
    assert "claude reply" in captured.out
    assert fake_key not in captured.out
    assert fake_key not in captured.err

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert str(req.url) == ANTHROPIC_MESSAGES_URL
    assert req.headers["x-api-key"] == fake_key
    assert req.headers["anthropic-version"] == ANTHROPIC_API_VERSION
    assert "authorization" not in req.headers

    db = SessionDB(hermes_home / "state.db")
    try:
        sess = db._conn.execute("SELECT model, mode FROM sessions LIMIT 1").fetchone()
        assert sess["model"] == DEFAULT_ANTHROPIC_MODEL
        assert sess["mode"] == "real"

        llm_call_cnt = db._conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        assert llm_call_cnt == 1
        dump_text = "\n".join(db._conn.iterdump())
        assert fake_key not in dump_text
    finally:
        db.close()


def test_040_scenario_05_fail_closed_and_invalid_provider_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 5: 지정 프로바이더 키 누락 시 타 키 우회 금지(Fail-Closed) 및 잘못된 --provider 거부."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "test-openrouter-only-040")
    monkeypatch.chdir(tmp_path)

    # (a) OpenRouter 키만 있는데 --provider gemini 지정 -> 타 키로 우회 금지
    monkeypatch.setattr(sys, "argv", ["hermes-core", "--provider", "gemini"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc_a:
        cli.main()
    assert exc_a.value.code == 2
    err_a = capsys.readouterr().err
    assert GEMINI_API_KEY_ENV in err_a
    assert not (hermes_home / "state.db").exists()

    # (b) 미지원 --provider 지정 -> SettingsError 종료 코드 2
    monkeypatch.setattr(sys, "argv", ["hermes-core", "--provider", "invalid"])
    with pytest.raises(SystemExit) as exc_b:
        cli.main()
    assert exc_b.value.code == 2
    err_b = capsys.readouterr().err
    assert "provider" in err_b.lower()
    assert not (hermes_home / "state.db").exists()


def test_040_scenario_06_provider_precedence_file_env_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 6: config.toml < HERMES_PROVIDER < --provider CLI 계층 우선순위 적용."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.toml").write_text('provider = "gemini"\n', encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PROVIDER", "openrouter")
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "test-anthropic-precedence-040")
    monkeypatch.chdir(tmp_path)

    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return _anthropic_text_response("precedence cli wins")

    _run_cli_with_transport(
        monkeypatch,
        ["--provider", "anthropic"],
        "확인\nexit\n",
        httpx.MockTransport(handler),
    )

    out = capsys.readouterr().out
    assert f"Provider: anthropic, Model: {DEFAULT_ANTHROPIC_MODEL}" in out
    assert captured_urls == [ANTHROPIC_MESSAGES_URL]


def test_040_scenario_07_delegate_uses_same_anthropic_provider_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """시나리오 7: --delegate 실행 시 자식 서브에이전트도 부모와 동일한 AnthropicClient로 동작한다."""
    hermes_home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    fake_key = "test-anthropic-delegate-key-040"
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, fake_key)
    monkeypatch.chdir(tmp_path)

    captured_requests: list[tuple[str, dict[str, Any]]] = []
    call_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        call_idx += 1
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append((str(request.url), body))
        if call_idx == 1:
            # 부모 1차: delegate_task 도구 호출
            return httpx.Response(
                200,
                json={
                    "id": "msg-p1",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_del_1",
                            "name": "delegate_task",
                            "input": {"goal": "Summarize workspace"},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 20, "output_tokens": 12},
                },
            )
        if call_idx == 2:
            # 자식 1차: 작업 요약 반환
            return _anthropic_text_response("child anthropic summary")
        # 부모 2차: 최종 응답
        return _anthropic_text_response("parent anthropic done")

    _run_cli_with_transport(
        monkeypatch,
        ["--provider", "anthropic", "--delegate"],
        "위임해줘\nexit\n",
        httpx.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert "parent anthropic done" in captured.out
    assert "Delegation enabled (delegate_task)." in captured.out
    assert fake_key not in captured.out
    assert fake_key not in captured.err

    assert len(captured_requests) == 3
    assert all(url == ANTHROPIC_MESSAGES_URL for url, _ in captured_requests)
    assert all(body["model"] == DEFAULT_ANTHROPIC_MODEL for _, body in captured_requests)

    db = SessionDB(hermes_home / "state.db")
    try:
        sessions = db._conn.execute(
            "SELECT id, source, model, parent_session_id, end_reason FROM sessions"
        ).fetchall()
        assert len(sessions) == 2
        parent_row = next(r for r in sessions if r["source"] == "cli")
        child_row = next(r for r in sessions if r["source"] == "subagent")
        assert parent_row["model"] == DEFAULT_ANTHROPIC_MODEL
        assert child_row["model"] == DEFAULT_ANTHROPIC_MODEL
        assert child_row["parent_session_id"] == parent_row["id"]
        assert child_row["end_reason"] == "completed"

        parent_calls = db._conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE session_id = ?", (parent_row["id"],)
        ).fetchone()[0]
        child_calls = db._conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE session_id = ?", (child_row["id"],)
        ).fetchone()[0]
        assert parent_calls == 2
        assert child_calls == 1

        dump_text = "\n".join(db._conn.iterdump())
        assert fake_key not in dump_text
    finally:
        db.close()
