"""11번 로드맵 030 단계 디스패처 연동(checkpointer 주입) 인수 테스트.

근거: docs/로드맵/11-체크포인트-롤백-MVP/instructions/030_디스패처_연동.md
인수 테스트 시나리오 1~23 구현 (24번은 전체 회귀 실행으로 확인).
"""

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import uuid

import pytest

import hermes_core.tools.dispatcher as dispatcher_module
from hermes_core.constants import (
    CHECKPOINT_STATUS_APPLIED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_PENDING,
)
from hermes_core.session_db import SessionDB
from hermes_core.tools.checkpoint import Checkpointer
from hermes_core.tools.delegate_tool import register_delegate_tool
from hermes_core.tools.dispatcher import create_child_dispatcher, create_default_dispatcher
from hermes_core.tools.file_tools import patch_tool, write_file_tool


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    db = SessionDB(tmp_path / "state.db")
    sid = "s_030"
    db.create_session(sid)
    cp = Checkpointer(db, sid, ws)
    cp.begin_turn()
    d = create_default_dispatcher(session_db=db, checkpointer=cp)
    return db, sid, ws, cp, d, tmp_path


def call(d, name, **args):
    return json.loads(d.handle_function_call(name, args))


def raw(d, name, **args):
    return d.handle_function_call(name, args)


def rows(db, sid):
    return db.get_file_checkpoints(sid)


class FakeTool:
    """호출 횟수를 세고 지정한 동작을 하는 가짜 도구."""

    def __init__(self, action=None):
        self.calls = 0
        self.action = action

    def __call__(self, **kwargs):
        self.calls += 1
        if self.action is not None:
            return self.action(**kwargs)
        return write_file_tool(**kwargs)


def make_with_fake(monkeypatch, db, cp, name, fake):
    monkeypatch.setattr(dispatcher_module, f"{name}_tool", fake)
    return create_default_dispatcher(session_db=db, checkpointer=cp)


# --- 3-1 ---

def test_01_no_checkpointer_same_as_before(env, tmp_path):
    db, sid, ws, cp, d, _ = env
    plain = create_default_dispatcher()
    assert plain._handlers["write_file"] is write_file_tool
    assert plain._handlers["patch"] is patch_tool

    other = tmp_path / "other"
    other.mkdir()

    def sequence(disp):
        return [
            raw(disp, "write_file", path="a.txt", content="hello"),
            raw(disp, "patch", path="a.txt", old_string="hello", new_string="bye"),
            raw(disp, "write_file", path="a.txt", content="again"),
            raw(disp, "patch", path="a.txt", old_string="zzz", new_string="y"),
        ]

    with_cp = sequence(d)
    assert (ws / "a.txt").read_text() == "bye"
    import os

    os.chdir(other)
    without = sequence(plain)
    assert (other / "a.txt").read_text() == "bye"
    assert with_cp == without
    assert [json.loads(r)["success"] for r in without] == [True, True, False, False]


def test_02_child_has_no_checkpointer(env):
    db, sid, ws, cp, d, _ = env
    c = create_child_dispatcher()
    assert call(c, "write_file", path="child.txt", content="x")["success"] is True
    assert (ws / "child.txt").exists()
    assert rows(db, sid) == []
    assert call(d, "write_file", path="child2.txt", content="x")["success"] is True
    assert len(rows(db, sid)) == 1


def test_03_schema_unchanged(env):
    db, sid, ws, cp, d, _ = env
    plain = create_default_dispatcher(session_db=db)
    assert json.dumps(d.get_tools_schema()) == json.dumps(plain.get_tools_schema())
    for tool in d.get_tools_schema():
        props = tool["function"]["parameters"]["properties"]
        assert "workspace" not in props and "checkpointer" not in props


def test_04_include_without_file_tools(env):
    db, sid, ws, cp, d, _ = env
    for inc in (["read_file"], ["terminal"]):
        x = create_default_dispatcher(include=inc, checkpointer=cp)
        res = call(x, "write_file", path="q.txt", content="x")
        assert res["success"] is False and "Unknown tool" in res["error"]
        assert "write_file" not in x._handlers and "patch" not in x._handlers


# --- 3-2 ---

def test_05_write_file_applied(env):
    db, sid, ws, cp, d, _ = env
    assert call(d, "write_file", path="new.txt", content="hello")["success"] is True
    assert (ws / "new.txt").read_text() == "hello"
    r = rows(db, sid)
    assert len(r) == 1
    assert r[0]["status"] == "applied"
    assert r[0]["tool_name"] == "write_file"
    assert r[0]["rel_path"] == "new.txt"
    assert r[0]["before_exists"] == 0
    assert r[0]["before_content"] is None
    assert r[0]["turn_index"] == 1
    assert r[0]["after_sha256"] == sha("hello")


def test_06_patch_applied(env):
    db, sid, ws, cp, d, _ = env
    (ws / "a.txt").write_text("hello world")
    res = call(d, "patch", path="a.txt", old_string="world", new_string="there")
    assert res["success"] is True
    assert (ws / "a.txt").read_text() == "hello there"
    r = rows(db, sid)
    assert len(r) == 1
    assert r[0]["status"] == "applied"
    assert r[0]["before_exists"] == 1
    assert r[0]["before_content"] == "hello world"
    assert r[0]["after_sha256"] == sha("hello there")


def test_07_record_before_tool(env, monkeypatch):
    db, sid, ws, cp, _, _ = env
    seen = []

    def action(**kw):
        seen.append(rows(db, sid))
        return write_file_tool(**kw)

    d = make_with_fake(monkeypatch, db, cp, "write_file", FakeTool(action))
    call(d, "write_file", path="new.txt", content="hello")
    assert len(seen[0]) == 1
    assert seen[0][0]["status"] == "pending"
    assert seen[0][0]["rel_path"] == "new.txt"
    assert rows(db, sid)[0]["status"] == "applied"


def test_08_tool_refusal_failed(env):
    db, sid, ws, cp, d, tmp = env
    (ws / "a.txt").write_text("hello")
    res_a = raw(d, "write_file", path="a.txt", content="x")
    res_b = raw(d, "patch", path="a.txt", old_string="nope", new_string="y")
    plain = create_default_dispatcher()
    assert res_a == raw(plain, "write_file", path="a.txt", content="x")
    assert res_b == raw(plain, "patch", path="a.txt", old_string="nope", new_string="y")
    assert json.loads(res_a)["success"] is False and json.loads(res_b)["success"] is False
    assert (ws / "a.txt").read_text() == "hello"
    r = rows(db, sid)
    assert [x["status"] for x in r] == ["failed", "failed"]
    assert all(x["reason"] for x in r)


def test_09_consecutive_patches(env):
    db, sid, ws, cp, d, _ = env
    (ws / "a.txt").write_text("one")
    call(d, "patch", path="a.txt", old_string="one", new_string="two")
    call(d, "patch", path="a.txt", old_string="two", new_string="three")
    r = rows(db, sid)
    assert len(r) == 2 and r[0]["id"] < r[1]["id"]
    assert [x["status"] for x in r] == ["applied", "applied"]
    assert r[1]["before_content"] == "two"
    assert r[0]["before_content"] == "one"
    assert r[1]["after_sha256"] == sha("three")


def test_10_not_relying_on_result_json(env, monkeypatch):
    db, sid, ws, cp, _, _ = env
    cases = [
        ("a.txt", True, "not json", "applied"),
        ("b.txt", True, json.dumps({"success": "yes"}), "applied"),
        ("c.txt", True, json.dumps({"success": False}), "applied"),
        ("d.txt", False, json.dumps({"success": True}), "failed"),
    ]
    for name, writes, ret, expected in cases:
        def action(path, content, _w=writes, _r=ret):
            if _w:
                (ws / path).write_text(content)
            return _r

        d = make_with_fake(monkeypatch, db, cp, "write_file", FakeTool(action))
        assert raw(d, "write_file", path=name, content="zz") == ret
        last = rows(db, sid)[-1]
        assert last["rel_path"] == name
        assert last["status"] == expected, name
    c_row = [x for x in rows(db, sid) if x["rel_path"] == "c.txt"][0]
    assert c_row["reason"]


def test_11_tool_exception(env, monkeypatch):
    db, sid, ws, cp, _, _ = env

    def boom_after_write(path, content):
        (ws / path).write_text(content)
        raise RuntimeError("boom")

    def boom(path, content):
        raise RuntimeError("boom")

    for name, act, expected in (("a.txt", boom_after_write, "applied"), ("b.txt", boom, "failed")):
        d = make_with_fake(monkeypatch, db, cp, "write_file", FakeTool(act))
        res = call(d, "write_file", path=name, content="x")
        assert res["success"] is False
        assert "Tool 'write_file' failed: boom" in res["error"]
        assert rows(db, sid)[-1]["status"] == expected
    assert all(x["status"] != "pending" for x in rows(db, sid))


def test_12_keyboard_interrupt(env, monkeypatch):
    db, sid, ws, cp, _, _ = env

    def kb(path, content):
        raise KeyboardInterrupt

    d = make_with_fake(monkeypatch, db, cp, "write_file", FakeTool(kb))
    with pytest.raises(KeyboardInterrupt):
        d.handle_function_call("write_file", {"path": "k.txt", "content": "x"})
    r = rows(db, sid)
    assert len(r) == 1 and r[0]["status"] == "failed"


# --- 3-3 ---

def test_13_pre_record_failure(env, monkeypatch):
    db, sid, ws, cp, _, _ = env

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "record_file_checkpoint", locked)
    fake = FakeTool()
    d = make_with_fake(monkeypatch, db, cp, "write_file", fake)
    res = call(d, "write_file", path="x.txt", content="x")
    assert res["success"] is False
    assert res["error"].startswith("Failed to record checkpoint before modification (")
    for frag in ("database is locked", "The file was not modified.", "do not repeat the same write"):
        assert frag in res["error"]
    assert not (ws / "x.txt").exists()
    assert fake.calls == 0
    assert rows(db, sid) == []


def test_14_session_limit(env, monkeypatch):
    db, sid, ws, cp0, _, _ = env
    cp = Checkpointer(db, sid, ws, bytes_limit=10)
    cp.begin_turn()
    (ws / "big.txt").write_text("x" * 20)
    fake = FakeTool(lambda **kw: patch_tool(**kw))
    d = make_with_fake(monkeypatch, db, cp, "patch", fake)
    res = call(d, "patch", path="big.txt", old_string="x", new_string="y")
    assert res["success"] is False
    assert "Checkpoint storage limit exceeded (10 bytes)" in res["error"]
    assert "do not repeat the same write" in res["error"]
    assert (ws / "big.txt").read_text() == "x" * 20
    assert fake.calls == 0
    assert rows(db, sid) == []
    d2 = create_default_dispatcher(session_db=db, checkpointer=cp)
    assert call(d2, "write_file", path="fresh.txt", content="ok")["success"] is True
    r = rows(db, sid)
    assert len(r) == 1 and r[0]["status"] == "applied"


def test_15_turn_not_begun(env, monkeypatch):
    db, sid, ws, _, _, _ = env
    cp = Checkpointer(db, sid, ws)
    fake = FakeTool()
    d = make_with_fake(monkeypatch, db, cp, "write_file", fake)
    res = call(d, "write_file", path="x.txt", content="x")
    assert res["success"] is False
    assert res["error"].startswith("Failed to record checkpoint before modification (")
    assert "Turn has not begun" in res["error"]
    assert not (ws / "x.txt").exists() and fake.calls == 0 and rows(db, sid) == []


def test_16_workspace_mismatch(env, monkeypatch):
    db, sid, ws, cp, _, tmp = env
    other = tmp / "other"
    other.mkdir()
    # a) 처음부터 다른 폴더
    cp_a = Checkpointer(db, sid, other)
    cp_a.begin_turn()
    fake_a = FakeTool()
    d_a = make_with_fake(monkeypatch, db, cp_a, "write_file", fake_a)
    # b) 중간에 현재 폴더가 바뀜
    fake_b = FakeTool()
    d_b = make_with_fake(monkeypatch, db, cp, "write_file", fake_b)
    res_a = call(d_a, "write_file", path="x.txt", content="x")
    monkeypatch.chdir(other)
    res_b = call(d_b, "write_file", path="y.txt", content="y")
    for res in (res_a, res_b):
        assert res["success"] is False
        assert "Checkpoint workspace differs from the current workspace" in res["error"]
        assert "do not repeat the same write" in res["error"]
        assert str(tmp) not in res["error"]
    for name in ("x.txt", "y.txt"):
        assert not (ws / name).exists() and not (other / name).exists()
    assert fake_a.calls == 0 and fake_b.calls == 0
    assert rows(db, sid) == []


def test_17_non_recordable_inputs(env):
    db, sid, ws, cp, d, tmp = env
    plain = create_default_dispatcher()
    (ws / "sub").mkdir()
    cases = [
        ("write_file", {"path": "../outside.txt", "content": "x"}),
        ("write_file", {"path": ".env", "content": "x"}),
        ("write_file", {"path": "AGENTS.md", "content": "x"}),
        ("write_file", {"path": ".git/x", "content": "x"}),
        ("patch", {"path": "sub", "old_string": "a", "new_string": "b"}),
    ]
    for name, args in cases:
        assert raw(d, name, **args) == raw(plain, name, **args), args
    assert not (ws / ".env").exists() and not (ws / "AGENTS.md").exists()
    assert not (tmp / "outside.txt").exists()
    assert rows(db, sid) == []


def test_18_invalid_arguments(env, monkeypatch):
    db, sid, ws, cp, _, _ = env
    fw, fp = FakeTool(), FakeTool(lambda **kw: patch_tool(**kw))
    monkeypatch.setattr(dispatcher_module, "write_file_tool", fw)
    monkeypatch.setattr(dispatcher_module, "patch_tool", fp)
    d = create_default_dispatcher(session_db=db, checkpointer=cp)
    for name, args in (
        ("write_file", {"path": "x.txt"}),
        ("write_file", {"path": 5, "content": "x"}),
        ("patch", {"path": "a.txt", "old_string": "a"}),
    ):
        res = call(d, name, **args)
        assert res["success"] is False and "Invalid arguments for tool" in res["error"]
    assert fw.calls == 0 and fp.calls == 0
    assert rows(db, sid) == []


# --- 3-4 ---

def test_19_finalize_failure(env, monkeypatch, caplog):
    db, sid, ws, cp, d, _ = env

    def bad(*a, **k):
        raise RuntimeError("finalize broke")

    monkeypatch.setattr(cp, "finalize", bad)
    with caplog.at_level(logging.WARNING):
        res = call(d, "write_file", path="f.txt", content="x")
    assert res["success"] is True
    assert (ws / "f.txt").exists()
    r = rows(db, sid)
    assert len(r) == 1 and r[0]["status"] == CHECKPOINT_STATUS_PENDING
    assert any(str(r[0]["id"]) in rec.getMessage() for rec in caplog.records)
    plan = cp.plan_rollback()
    assert plan.items[0].will_skip is True
    assert "pending" in plan.items[0].skip_reason
    assert plan.has_warnings is True


def test_20_unrecorded_modification_warns(env, monkeypatch, caplog):
    db, sid, ws, cp, _, _ = env
    monkeypatch.setattr(cp, "record_before", lambda *a, **k: None)

    def action(path, content):
        (ws / path).write_text(content)
        return json.dumps({"success": True})

    d = make_with_fake(monkeypatch, db, cp, "write_file", FakeTool(action))
    with caplog.at_level(logging.WARNING):
        res = raw(d, "write_file", path="u.txt", content="x")
    assert res == json.dumps({"success": True})
    assert rows(db, sid) == []
    assert any("기록 없는 변경" in rec.getMessage() for rec in caplog.records)


def test_21_legit_none_has_no_warning(env, caplog):
    db, sid, ws, cp, d, _ = env
    with caplog.at_level(logging.WARNING):
        res = call(d, "write_file", path=".env", content="x")
    assert res["success"] is False
    assert not any("기록 없는 변경" in rec.getMessage() for rec in caplog.records)


# --- 3-5 ---

def _agent(db, sid, ws, scripted_path):
    from hermes_core.agent.agent import HermesAgent, MockLLMClient

    cp = Checkpointer(db, sid, ws)
    d = create_default_dispatcher(session_db=db, checkpointer=cp)
    mock = MockLLMClient()
    mock.queue_response(
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": scripted_path, "content": "hi"}),
                },
            }
        ]
    )
    mock.queue_response(content="done")
    agent = HermesAgent(
        session_id=sid,
        session_db=db,
        dispatcher=d,
        llm_client=mock,
        skip_memory=True,
        skip_context_files=True,
    )
    return cp, agent


def test_22_agent_end_to_end(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    db = SessionDB(tmp_path / "state.db")

    sid_a = str(uuid.uuid4())
    cp_a, agent_a = _agent(db, sid_a, ws, "new.txt")
    cp_a.begin_turn()
    assert agent_a.run_conversation("write it")["final_response"] == "done"
    assert (ws / "new.txt").exists()
    r = rows(db, sid_a)
    assert len(r) == 1 and r[0]["status"] == CHECKPOINT_STATUS_APPLIED
    tool_msg = next(m for m in db.get_messages(sid_a) if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["success"] is True

    sid_b = str(uuid.uuid4())
    cp_b, agent_b = _agent(db, sid_b, ws, "other.txt")
    assert agent_b.run_conversation("write it")["final_response"] == "done"
    assert not (ws / "other.txt").exists()
    assert rows(db, sid_b) == []
    tool_msg_b = next(m for m in db.get_messages(sid_b) if m["role"] == "tool")
    assert "Turn has not begun" in tool_msg_b["content"]


def test_23_delegate_registered_later(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    db = SessionDB(tmp_path / "state.db")
    sid = str(uuid.uuid4())
    cp, agent = _agent(db, sid, ws, "new.txt")
    cp.begin_turn()
    agent.run_conversation("write it")
    register_delegate_tool(
        agent.dispatcher,
        session_db=db,
        parent_session_id=sid,
        llm_client_factory=lambda: None,
    )
    (ws / "p.txt").write_text("a")
    before = len(rows(db, sid))
    assert call(agent.dispatcher, "patch", path="p.txt", old_string="a", new_string="b")["success"]
    assert len(rows(db, sid)) == before + 1
    child = create_child_dispatcher()
    (ws / "q.txt").write_text("a")
    assert call(child, "patch", path="q.txt", old_string="a", new_string="b")["success"]
    assert len(rows(db, sid)) == before + 1
