"""로드맵 14 S14-010: 원장 정합 및 세션 재개 DB 계약 인수 테스트."""

import pytest

from hermes_core.agent.agent import HermesAgent
from hermes_core.constants import SUMMARY_END_MARKER, SUMMARY_PREFIX
from hermes_core.session_db import SessionDB
from hermes_core.tools.checkpoint import Checkpointer, CheckpointLimitError
from hermes_core.tools.memory_tool import MemoryStore


def test_scenario_01_last_active_id_empty_session():
    """시나리오 1: 새 세션 생성 직후(메시지 0건) get_last_active_message_id(sid) -> 0 반환."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")
    assert db.get_last_active_message_id(sid) == 0


def test_scenario_02_last_active_id_multiple_messages_and_sessions():
    """시나리오 2: 메시지 3건 추가 후 get_last_active_message_id(sid) -> m3 반환 및 타 세션 격리."""
    db = SessionDB(":memory:")
    sid1 = db.create_session("sess-1")
    sid2 = db.create_session("sess-2")

    m1 = db.append_message(sid1, "user", "hello")
    m2 = db.append_message(sid1, "assistant", "world")
    m3 = db.append_message(sid1, "user", "again")

    assert db.get_last_active_message_id(sid1) == m3

    # 다른 세션에 메시지를 추가해도 sid1의 반환값은 바뀌지 않는다
    m_other = db.append_message(sid2, "user", "other")
    assert m_other > m3
    assert db.get_last_active_message_id(sid1) == m3
    assert db.get_last_active_message_id(sid2) == m_other


def test_scenario_03_deactivate_messages_after_preserves_records_as_inactive():
    """시나리오 3: 턴 1(m1, m2) 후 턴 2(m3, m4, m5) 취소 시 deactivate_messages_after(sid, m2)."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    m1 = db.append_message(sid, "user", "turn 1 user")
    m2 = db.append_message(sid, "assistant", "turn 1 assistant")

    m3 = db.append_message(sid, "user", "turn 2 user")
    m4 = db.append_message(
        sid,
        "assistant",
        "turn 2 calling tool",
        tool_calls=[{"id": "call_1", "function": {"name": "read_file"}}],
    )
    m5 = db.append_message(
        sid, "tool", "file content", tool_name="read_file", tool_call_id="call_1"
    )

    affected = db.deactivate_messages_after(sid, m2)
    assert affected == 3

    # 활성 대화는 턴 1의 2건만 조회됨
    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 2
    assert [m["content"] for m in conv] == ["turn 1 user", "turn 1 assistant"]

    # 전체 원장은 5건 모두 보존 (가정 A, 무삭제 보존)
    all_msgs = db.get_messages(sid)
    assert len(all_msgs) == 5
    m3_rec = next(m for m in all_msgs if m["id"] == m3)
    m4_rec = next(m for m in all_msgs if m["id"] == m4)
    m5_rec = next(m for m in all_msgs if m["id"] == m5)

    for rec in (m3_rec, m4_rec, m5_rec):
        assert rec["active"] == 0
        assert rec["compacted"] == 0


def test_scenario_04_deactivate_messages_after_zero_first_turn():
    """시나리오 4: 첫 턴 시작 전(last_id = 0) 메시지 2건 후 deactivate_messages_after(sid, 0)."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    db.append_message(sid, "user", "hello")
    db.append_message(sid, "assistant", "thinking...")

    affected = db.deactivate_messages_after(sid, 0)
    assert affected == 2
    assert db.get_messages_as_conversation(sid) == []
    assert db.get_last_active_message_id(sid) == 0


def test_scenario_05_deactivate_messages_after_idempotent():
    """시나리오 5: 추가 메시지 없을 때 deactivate_messages_after 호출 (멱등성)."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    m1 = db.append_message(sid, "user", "turn 1 user")
    m2 = db.append_message(sid, "assistant", "turn 1 assistant")

    affected = db.deactivate_messages_after(sid, m2)
    assert affected == 0

    conv = db.get_messages_as_conversation(sid)
    assert len(conv) == 2
    assert db.get_last_active_message_id(sid) == m2


def test_scenario_06_compaction_cancel_protection():
    """시나리오 6: 압축 후 취소 보호 (가정 B).

    턴 1(m1~m4, last_id = m4) 후 턴 2 도중 압축(archive_and_compact)으로 요약본과 꼬리 복사본 삽입.
    이어서 턴 2 미완성 행(u1, u2) 추가 후 deactivate_messages_after(sid, m4) 호출 시
    압축본은 active=1 보존, u1, u2만 active=0.
    """
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    m1 = db.append_message(sid, "user", "turn 1 req 1")
    m2 = db.append_message(sid, "assistant", "turn 1 res 1")
    m3 = db.append_message(sid, "user", "turn 1 req 2")
    m4 = db.append_message(sid, "assistant", "turn 1 res 2")

    last_id_turn1 = m4

    # 턴 2 도중 압축 실행: 요약 행(idx 0)과 꼬리 복사본(idx 1, 2)
    summary_content = (
        f"{SUMMARY_PREFIX}\n이전 대화 요약: 사용자 질문 및 응답 요약\n{SUMMARY_END_MARKER}"
    )
    compacted_new_messages = [
        {"role": "user", "content": summary_content},
        {"role": "assistant", "content": "네, 이해했습니다."},
        {"role": "user", "content": "이어서 작업할게요."},
    ]
    db.archive_and_compact(sid, compacted_new_messages, summary_index=0)

    # 압축 직후 턴 2에서 모델 응답 및 도구 실행 중 취소 발생 (u1, u2)
    u1 = db.append_message(sid, "assistant", "도구 실행할게요")
    u2 = db.append_message(sid, "tool", "결과", tool_name="bash", tool_call_id="call_x")

    affected = db.deactivate_messages_after(sid, last_id_turn1)
    assert affected == 2  # u1, u2만 비활성화됨

    all_msgs = db.get_messages(sid)
    u1_rec = next(m for m in all_msgs if m["id"] == u1)
    u2_rec = next(m for m in all_msgs if m["id"] == u2)
    assert u1_rec["active"] == 0
    assert u2_rec["active"] == 0

    # 압축 요약 행과 꼬리 복사본은 active=1 유지
    active_conv = db.get_messages_as_conversation(sid)
    assert len(active_conv) == 3
    assert active_conv[0]["content"] == summary_content
    assert active_conv[1]["content"] == "네, 이해했습니다."
    assert active_conv[2]["content"] == "이어서 작업할게요."


def test_scenario_07_invalid_after_id_raises_value_error():
    """시나리오 7: 음수이거나 bool 타입인 after_id는 ValueError 발생."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    with pytest.raises(ValueError):
        db.deactivate_messages_after(sid, -1)

    with pytest.raises(ValueError):
        db.deactivate_messages_after(sid, True)  # bool은 int 서브클래스이므로 엄격 차단

    with pytest.raises(ValueError):
        db.deactivate_messages_after(sid, "0")  # type: ignore


def test_scenario_08_reopen_session():
    """시나리오 8: end_session 후 reopen_session 호출 시 ended_at, end_reason NULL 복귀."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    db.end_session(sid, "user_exit")
    s_ended = db.get_session(sid)
    assert s_ended is not None
    assert s_ended["ended_at"] is not None
    assert s_ended["end_reason"] == "user_exit"

    reopened = db.reopen_session(sid)
    assert reopened is True

    s_reopened = db.get_session(sid)
    assert s_reopened is not None
    assert s_reopened["ended_at"] is None
    assert s_reopened["end_reason"] is None

    # 존재하지 않는 ID로 부르면 False
    assert db.reopen_session("non-existent-sid") is False


def test_scenario_09_find_resumable_session_latest_and_excludes_subagent():
    """시나리오 9: CLI 세션 2개와 subagent 세션 1개 존재 시 latest는 최신 CLI 세션만 반환."""
    db = SessionDB(":memory:")
    s_old = db.create_session("sess-cli-old", source="cli")
    s_new = db.create_session("sess-cli-new", source="cli")
    # subagent 세션은 더 늦게 생성되었어도 제외
    s_sub = db.create_session(
        "sess-subagent", source="subagent", parent_session_id=s_new
    )

    res_none = db.find_resumable_session(None)
    assert len(res_none) == 1
    assert res_none[0]["id"] == s_new

    res_latest = db.find_resumable_session("latest")
    assert len(res_latest) == 1
    assert res_latest[0]["id"] == s_new

    res_latest_caps = db.find_resumable_session("  LATEST  ")
    assert len(res_latest_caps) == 1
    assert res_latest_caps[0]["id"] == s_new


def test_scenario_10_find_resumable_session_prefix_matching():
    """시나리오 10: prefix 매칭 및 최신순 정렬."""
    db = SessionDB(":memory:")
    db.create_session("abc11111-0000", source="cli")
    db.create_session("abc22222-0000", source="cli")
    db.create_session("def33333-0000", source="cli")

    res_abc1 = db.find_resumable_session("abc1")
    assert len(res_abc1) == 1
    assert res_abc1[0]["id"] == "abc11111-0000"

    res_abc = db.find_resumable_session("abc")
    assert len(res_abc) == 2
    assert [r["id"] for r in res_abc] == ["abc22222-0000", "abc11111-0000"]

    res_xyz = db.find_resumable_session("xyz")
    assert res_xyz == []


def test_scenario_11_find_resumable_session_wildcards_and_whitespace():
    """시나리오 11: %, _ 와일드카드 이스케이프 및 공백 단독 처리."""
    db = SessionDB(":memory:")
    db.create_session("abc11111-0000", source="cli")
    db.create_session("abc22222-0000", source="cli")
    db.create_session("def33333-0000", source="cli")

    assert db.find_resumable_session("%") == []
    assert db.find_resumable_session("_") == []
    assert db.find_resumable_session("   ") == []


def test_scenario_12_fts_search_after_deactivation():
    """시나리오 12: deactivate_messages_after 전후 FTS5 전문 검색 동작 확인."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")

    m1 = db.append_message(sid, "user", "특수 키워드 아나콘다 검색어")
    m2 = db.append_message(sid, "assistant", "답변입니다")
    m3 = db.append_message(sid, "user", "취소될 키워드 아나콘다 질문")

    results_before = db.search_messages("아나콘다")
    assert len(results_before) == 2

    # m1 이후 비활성화
    db.deactivate_messages_after(sid, m2)

    # FTS 검색이 에러 없이 정상 작동해야 함
    results_after = db.search_messages("아나콘다")
    assert len(results_after) >= 1
    assert any(r["session_id"] == sid for r in results_after)


def test_scenario_13_checkpointer_restore_empty_session():
    """시나리오 13: 체크포인트가 0건인 세션에서 Checkpointer.restore_from_db() 호출."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")
    cp = Checkpointer(db, sid)

    restored_turn = cp.restore_from_db()
    assert restored_turn == 0
    assert cp.turn_index == 0
    assert cp._session_before_bytes == 0
    assert cp._turn_start_times == {}
    assert cp.turn_started_at == 0.0


def test_scenario_14_checkpointer_restore_and_begin_turn_continuity(tmp_path):
    """시나리오 14: 세션 1의 체크포인트 복원 후 새 turn_index 단조 증가 (가정 E)."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")
    ws = tmp_path / "workspace"
    ws.mkdir()
    f_a = ws / "a.txt"
    f_a.write_text("0123456789")  # 10 bytes

    # 턴 1: a.txt 수정
    cp1 = Checkpointer(db, sid, workspace=ws)
    t1 = cp1.begin_turn()
    cid1 = cp1.record_before("write_file", "a.txt")
    f_a.write_text("updated content")
    cp1.finalize(cid1, "write_file", "a.txt", result_success=True)

    # 턴 2: b.txt 신규 생성
    t2 = cp1.begin_turn()
    cid2 = cp1.record_before("write_file", "b.txt")
    (ws / "b.txt").write_text("new file")
    cp1.finalize(cid2, "write_file", "b.txt", result_success=True)

    # 새 Checkpointer 인스턴스로 복원
    cp2 = Checkpointer(db, sid, workspace=ws)
    restored = cp2.restore_from_db()
    assert restored == 2
    assert cp2._session_before_bytes == 10
    assert set(cp2._turn_start_times.keys()) == {1, 2}

    # 다음 begin_turn()은 3이어야 함
    t3 = cp2.begin_turn()
    assert t3 == 3


def test_scenario_15_cross_session_rollback(tmp_path):
    """시나리오 15: 세션 간 /rollback (가정 D, 12 한계 1)."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")
    ws = tmp_path / "workspace"
    ws.mkdir()
    f_a = ws / "a.txt"
    f_a.write_text("v1")

    # 세션 1 턴 1
    cp1 = Checkpointer(db, sid, workspace=ws)
    cp1.begin_turn()
    cid1 = cp1.record_before("write_file", "a.txt")
    f_a.write_text("v2")
    cp1.finalize(cid1, "write_file", "a.txt", result_success=True)

    # 세션 1 턴 2
    cp1.begin_turn()
    cid2 = cp1.record_before("write_file", "b.txt")
    f_b = ws / "b.txt"
    f_b.write_text("new")
    cp1.finalize(cid2, "write_file", "b.txt", result_success=True)
    db.end_session(sid, "user_exit")

    # 새 세션 인스턴스로 재개하여 롤백 연속 2회 실행
    cp2 = Checkpointer(db, sid, workspace=ws)
    cp2.restore_from_db()

    # 1회차 롤백: 턴 2 (b.txt 삭제)
    plan1 = cp2.plan_rollback()
    assert plan1 is not None
    assert plan1.turn_index == 2
    res1 = cp2.execute_rollback(plan1)
    assert res1.rolled_back_count == 1
    assert not f_b.exists()

    # 2회차 롤백: 턴 1 (a.txt "v1" 복원)
    plan2 = cp2.plan_rollback()
    assert plan2 is not None
    assert plan2.turn_index == 1
    res2 = cp2.execute_rollback(plan2)
    assert res2.rolled_back_count == 1
    assert f_a.read_text() == "v1"


def test_scenario_16_checkpointer_budget_restoration(tmp_path):
    """시나리오 16: 복원된 누적 바이트 예산 초과 시 CheckpointLimitError 발생."""
    db = SessionDB(":memory:")
    sid = db.create_session("sess-1")
    ws = tmp_path / "workspace"
    ws.mkdir()
    f1 = ws / "f1.txt"
    f1.write_text("x" * 80)

    cp1 = Checkpointer(db, sid, workspace=ws, bytes_limit=100)
    cp1.begin_turn()
    cid1 = cp1.record_before("write_file", "f1.txt")
    cp1.finalize(cid1, "write_file", "f1.txt", result_success=True)

    # 새 Checkpointer로 복원
    cp2 = Checkpointer(db, sid, workspace=ws, bytes_limit=100)
    cp2.restore_from_db()
    assert cp2._session_before_bytes == 80

    # 새 턴에서 30바이트 파일 record_before 시도 (80 + 30 > 100)
    cp2.begin_turn()
    f2 = ws / "f2.txt"
    f2.write_text("y" * 30)

    with pytest.raises(CheckpointLimitError):
        cp2.record_before("write_file", "f2.txt")


def test_scenario_17_resume_non_existent_session_raises_value_error():
    """시나리오 17: 존재하지 않는 session_id로 resume=True 호출 시 ValueError."""
    db = SessionDB(":memory:")
    with pytest.raises(ValueError, match="Cannot resume session"):
        HermesAgent(session_id="no-such-session", session_db=db, resume=True)


def test_scenario_18_resume_preserves_system_prompt_snapshot_and_reopens(tmp_path):
    """시나리오 18: reopen_session 확인 및 디스크 MEMORY.md 변경에도 원래 system_prompt 스냅샷 보존 (가정 C, F)."""
    db = SessionDB(":memory:")
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    mem_file = mem_dir / "MEMORY.md"
    mem_file.write_text("- 초기 기억 내용", encoding="utf-8")
    store1 = MemoryStore(memory_dir=mem_dir)

    sid = "sess-prompt-test"
    agent1 = HermesAgent(
        session_id=sid,
        session_db=db,
        memory_store=store1,
        skip_context_files=True,
    )
    prompt1 = agent1._build_system_prompt()
    assert "초기 기억 내용" in prompt1
    db.end_session(sid, "user_exit")

    # 디스크의 기억 내용 수정
    mem_file.write_text("- 완전히 바뀐 기억 내용", encoding="utf-8")
    store2 = MemoryStore(memory_dir=mem_dir)

    # resume=True로 세션 재개
    agent2 = HermesAgent(
        session_id=sid,
        session_db=db,
        memory_store=store2,
        skip_context_files=True,
        resume=True,
    )

    # DB의 세션이 다시 열렸는지 확인 (ended_at is None)
    sess_rec = db.get_session(sid)
    assert sess_rec is not None
    assert sess_rec["ended_at"] is None
    assert sess_rec["end_reason"] is None

    # system_prompt는 바뀐 기억이 아니라 원래 스냅샷 그대로 보존
    prompt2 = agent2._build_system_prompt()
    assert prompt2 == prompt1
    assert "초기 기억 내용" in prompt2
    assert "완전히 바뀐 기억 내용" not in prompt2


def test_scenario_19_resume_conversation_continuity():
    """시나리오 19: agent2.load_conversation_history() 후 run_conversation 연속 대화."""
    db = SessionDB(":memory:")
    sid = "sess-conv-test"

    # 세션 1: 1턴 완료
    agent1 = HermesAgent(session_id=sid, session_db=db, skip_context_files=True)
    agent1.run_conversation("hello")
    db.end_session(sid, "user_exit")

    # 세션 2: 재개 후 load_conversation_history
    agent2 = HermesAgent(
        session_id=sid, session_db=db, skip_context_files=True, resume=True
    )
    history = agent2.load_conversation_history()
    assert history is not None
    assert len(history) >= 3
    assert history[0]["role"] == "system"
    assert history[1]["role"] == "user"
    assert history[1]["content"] == "hello"
    assert history[2]["role"] == "assistant"

    # 이어서 2턴 대화 실행
    res2 = agent2.run_conversation("second", conversation_history=history)
    assert res2 is not None

    # 원장에도 연속적으로 쌓였는지 확인
    conv = db.get_messages_as_conversation(sid)
    assert [m["content"] for m in conv[:2]] == ["hello", history[2]["content"]]
    assert conv[-1]["role"] == "assistant"


def test_scenario_20_resume_ignores_deactivated_messages():
    """시나리오 20: 취소되어 deactivate된 메시지는 load_conversation_history에 미포함."""
    db = SessionDB(":memory:")
    sid = "sess-deact-test"

    agent1 = HermesAgent(session_id=sid, session_db=db, skip_context_files=True)
    m1 = db.append_message(sid, "user", "turn 1 user")
    m2 = db.append_message(sid, "assistant", "turn 1 assistant")

    # 턴 2 도중 추가된 미완성 메시지
    db.append_message(sid, "user", "turn 2 cancelled user")
    db.append_message(sid, "assistant", "turn 2 cancelled assistant")

    # 턴 1 이후 비활성화
    db.deactivate_messages_after(sid, m2)
    db.end_session(sid, "user_exit")

    agent2 = HermesAgent(
        session_id=sid, session_db=db, skip_context_files=True, resume=True
    )
    history = agent2.load_conversation_history()
    assert history is not None
    contents = [m.get("content") for m in history]
    assert "turn 1 user" in contents
    assert "turn 1 assistant" in contents
    assert "turn 2 cancelled user" not in contents
    assert "turn 2 cancelled assistant" not in contents


def test_scenario_21_resume_empty_session_handles_delayed_system_prompt():
    """시나리오 21: 빈 세션(메시지 0건) 재개 시 load_conversation_history는 None이고 첫 대화 시 조립 성공 (위험 5)."""
    db = SessionDB(":memory:")
    sid = "sess-empty-test"

    # 빈 세션 생성 후 바로 닫음
    db.create_session(sid, source="cli")
    db.end_session(sid, "user_exit")

    agent = HermesAgent(
        session_id=sid, session_db=db, skip_context_files=True, resume=True
    )
    assert agent.load_conversation_history() is None

    # 첫 턴 실행 시 정상적으로 시스템 프롬프트 조립 및 대화 성공
    res = agent.run_conversation("first message")
    assert res is not None

    sess = db.get_session(sid)
    assert sess is not None
    assert sess["system_prompt"] is not None

