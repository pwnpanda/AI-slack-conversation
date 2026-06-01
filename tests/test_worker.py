import asyncio
from dataclasses import dataclass, field

import pytest

from slackbot.worker import Worker


@dataclass
class FakeMatrixIO:
    posts: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    top_level_posts: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    _seq: int = 0

    def room_for_agent(self, agent: str) -> str:
        return f"!{agent}:server"

    async def post_in_thread(self, thread_root, text, room_id=None):
        self.posts.append((thread_root, text))
        self._seq += 1
        return f"$thr.{self._seq}"

    async def post_top_level(self, text, room_id=None):
        self.top_level_posts.append(text)
        self._seq += 1
        return f"$top.{self._seq}"

    async def edit_top_level(self, ts, text, room_id=None):
        self.edits.append((ts, text))

    async def react(self, ts, emoji, room_id=None):
        self.reacts.append((ts, emoji))


@dataclass
class FakeActuator:
    deliveries: list[tuple[str, str, str]] = field(default_factory=list)
    key_deliveries: list[tuple[str, str, list[str]]] = field(default_factory=list)

    async def deliver(self, session, pane_id, text):
        self.deliveries.append((session, pane_id, text))

    async def deliver_keys(self, session, pane_id, keys):
        self.key_deliveries.append((session, pane_id, list(keys)))


def _bound_session(reg, sid, agent="claude"):
    """Helper: create a registered+named session with a thread."""
    reg.upsert_session(sid, "/x", "main", "13", agent=agent, matrix_room_id="!claude:server")
    reg.set_name(sid, "myproj")
    reg.set_matrix_thread_root(sid, "$TOP1:server")


@pytest.mark.asyncio
async def test_worker_mirrors_assistant_response(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hello"})
    await worker.stop()

    assert matrix.posts == [("$TOP1:server", "[Claude] 🤖 hello")]
    reg.close()


@pytest.mark.asyncio
async def test_worker_dedupes_by_uuid(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    ev = {"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hello"}
    await worker.enqueue(ev)
    await worker.enqueue(ev)
    await worker.stop()

    assert len(matrix.posts) == 1
    reg.close()


@pytest.mark.asyncio
async def test_worker_delivers_matrix_reply_to_pane(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    actuator = FakeActuator()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=actuator)
    await worker.start()

    await worker.enqueue({"kind": "matrix_reply", "text": "do X", "msg_ts": "$MSG1:server"})
    await worker.stop()

    assert actuator.deliveries == [("main", "13", "do X")]
    assert ("$MSG1:server", "white_check_mark") in matrix.reacts
    reg.close()


@pytest.mark.asyncio
async def test_worker_suppresses_echoed_prompt_after_delivery(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    # Matrix reply is delivered to pane → echo expected.
    await worker.enqueue({"kind": "matrix_reply", "text": "ping", "msg_ts": "$MSG1:server"})
    # Transcript reader will see the user typing it and emit prompt with same text.
    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "ping"})
    await worker.stop()

    # The matrix reply's reaction was added, but the prompt was suppressed (not mirrored).
    assert ("$MSG1:server", "white_check_mark") in matrix.reacts
    assert matrix.posts == []  # no 👤 ping mirrored
    reg.close()


@pytest.mark.asyncio
async def test_worker_suppresses_echo_despite_trailing_whitespace(tmp_db_path: str) -> None:
    """Element X appends a trailing space to message bodies; CC strips it when
    recording the prompt. Echo suppression must survive that whitespace delta,
    otherwise every Matrix reply gets mirrored back into its own thread."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    # Delivered body carries Element X's trailing space.
    await worker.enqueue({"kind": "matrix_reply", "text": "ping ", "msg_ts": "$MSG1:server"})
    # Transcript reader sees CC's stripped prompt.
    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "ping"})
    await worker.stop()

    assert matrix.posts == []  # no 👤 echo mirrored back
    reg.close()


@pytest.mark.asyncio
async def test_worker_mirrors_organic_prompt(tmp_db_path: str) -> None:
    """A prompt that didn't come from a Matrix delivery IS mirrored."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "what's up"})
    await worker.stop()

    assert matrix.posts == [("$TOP1:server", "[Claude] 👤 what's up")]
    reg.close()


@pytest.mark.asyncio
async def test_worker_skips_unbound_session(tmp_db_path: str) -> None:
    """If the session has no thread (not /rn'd or auto-recovered), buffer or skip."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    reg.upsert_session("s1", "/x", "main", "13", agent="claude")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "hi"})
    await worker.stop()

    # No thread yet → no Matrix post. Event log holds it for replay.
    assert matrix.posts == []
    reg.close()


@pytest.mark.asyncio
async def test_notification_is_marked_resolved_on_next_prompt(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue(
        {
            "kind": "notification",
            "message": "Claude waiting",
            "tool_request": 'Bash({"command":"ls"})',
        }
    )
    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "go ahead"})
    await worker.stop()

    # We posted the notification, then the prompt, then EDITED the notification.
    assert len(matrix.posts) == 2  # notification + prompt
    assert len([e for e in matrix.edits if "resolved" in e[1]]) == 1
    edited_ts, edited_text = next(e for e in matrix.edits if "resolved" in e[1])
    assert "resolved" in edited_text
    assert "Claude waiting" in edited_text
    reg.close()


@pytest.mark.asyncio
async def test_notification_is_marked_resolved_on_response(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "notification", "message": "needs input"})
    await worker.enqueue({"kind": "response", "uuid": "a1", "parentUuid": "u1", "text": "done"})
    await worker.stop()

    assert len([e for e in matrix.edits if "resolved" in e[1]]) == 1
    assert any("resolved" in e[1] for e in matrix.edits)
    reg.close()


@pytest.mark.asyncio
async def test_notification_is_marked_resolved_on_matrix_reply(tmp_db_path: str) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "notification", "message": "approve?"})
    await worker.enqueue({"kind": "matrix_reply", "text": "1", "msg_ts": "$MSG1:server"})
    await worker.stop()

    assert len([e for e in matrix.edits if "resolved" in e[1]]) == 1
    assert any("resolved" in e[1] for e in matrix.edits)
    reg.close()


@pytest.mark.asyncio
async def test_back_to_back_notifications_resolve_previous(tmp_db_path: str) -> None:
    """When a second notification arrives, the first must be marked resolved
    and the second becomes the new pending row."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "notification", "message": "first?"})
    await worker.enqueue({"kind": "notification", "message": "second?"})
    await worker.stop()

    # Two notifications posted, the first one edited as resolved.
    assert len(matrix.posts) == 2
    first_ts = "$thr.1"
    second_ts = "$thr.2"
    assert len([e for e in matrix.edits if "resolved" in e[1]]) == 1
    edited_ts, edited_text = next(e for e in matrix.edits if "resolved" in e[1])
    assert edited_ts == first_ts
    assert "resolved" in edited_text
    assert "first?" in edited_text

    # The second notification is now the pending one.
    pending = reg.consume_pending_notification("s1")
    assert pending is not None
    assert pending["ts"] == second_ts
    assert "second?" in pending["text"]
    reg.close()


@pytest.mark.asyncio
async def test_no_pending_notification_is_a_noop(tmp_db_path: str) -> None:
    """A prompt without a preceding notification should not call edit."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()

    await worker.enqueue({"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "hi"})
    await worker.stop()

    assert [e for e in matrix.edits if "resolved" in e[1]] == []
    reg.close()


@pytest.mark.asyncio
async def test_numeric_reply_during_ask_user_question_uses_arrow_keys(
    tmp_db_path: str,
) -> None:
    """When a question is pending, replying '2' navigates the TUI with
    Down+Enter rather than typing the digit (which would land in 'Other')."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    reg.set_pending_question(
        "s1",
        [
            {"label": "Per-key RGB", "description": ""},
            {"label": "Underglow", "description": ""},
            {"label": "None", "description": ""},
        ],
    )
    matrix = FakeMatrixIO()
    actuator = FakeActuator()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=actuator)
    await worker.start()
    await worker.enqueue({"kind": "matrix_reply", "text": "2", "msg_ts": "$M1"})
    await worker.stop()

    assert actuator.deliveries == []
    assert actuator.key_deliveries == [("main", "13", ["Down", "Enter"])]
    assert reg.get_pending_question("s1") is None  # consumed on success
    reg.close()


@pytest.mark.asyncio
async def test_freeform_reply_during_ask_user_question_types_text_and_clears(
    tmp_db_path: str,
) -> None:
    """A non-numeric reply during a pending question goes verbatim into
    CC's 'Other' field, then clears the pending state."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    reg.set_pending_question("s1", [{"label": "A"}, {"label": "B"}])
    matrix = FakeMatrixIO()
    actuator = FakeActuator()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=actuator)
    await worker.start()
    await worker.enqueue(
        {"kind": "matrix_reply", "text": "I want a fourth option", "msg_ts": "$M2"}
    )
    await worker.stop()

    assert actuator.key_deliveries == []
    assert actuator.deliveries == [("main", "13", "I want a fourth option")]
    assert reg.get_pending_question("s1") is None
    reg.close()


@pytest.mark.asyncio
async def test_numeric_reply_without_pending_question_types_normally(
    tmp_db_path: str,
) -> None:
    """When no question is pending, '1' should NOT navigate — it should
    just be typed (so it works for permission prompts as before)."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    actuator = FakeActuator()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=actuator)
    await worker.start()
    await worker.enqueue({"kind": "matrix_reply", "text": "1", "msg_ts": "$M3"})
    await worker.stop()

    assert actuator.key_deliveries == []
    assert actuator.deliveries == [("main", "13", "1")]
    reg.close()


@pytest.mark.asyncio
async def test_ask_user_question_notification_stores_pending_question(
    tmp_db_path: str,
) -> None:
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()
    await worker.enqueue(
        {
            "kind": "notification",
            "message": "Claude needs your permission",
            "tool_request": (
                'AskUserQuestion({"questions":[{"question":"Pick","options":'
                '[{"label":"A"},{"label":"B"}]}]})'
            ),
        }
    )
    await worker.stop()

    pending = reg.get_pending_question("s1")
    assert pending is not None
    assert [o["label"] for o in pending["options"]] == ["A", "B"]
    reg.close()


@pytest.mark.asyncio
async def test_response_event_clears_pending_question(tmp_db_path: str) -> None:
    """A new assistant response means CC moved past the question; stale
    pending_question state should be wiped so a later numeric reply isn't
    misinterpreted."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    reg.set_pending_question("s1", [{"label": "A"}, {"label": "B"}])
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()
    await worker.enqueue(
        {"kind": "response", "uuid": "u1", "parentUuid": None, "text": "answer recorded"}
    )
    await worker.stop()
    assert reg.get_pending_question("s1") is None
    reg.close()


@pytest.mark.asyncio
async def test_option_reply_suppresses_label_echo_in_subsequent_prompt(
    tmp_db_path: str,
) -> None:
    """Replying '2' to AskUserQuestion → CC records the selected option's
    LABEL as the user prompt. The worker pre-stages that label in the echo
    set so the prompt event the transcript reader subsequently emits gets
    suppressed (no duplicate 👤 mirror)."""
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    reg.set_pending_question(
        "s1",
        [
            {"label": "Per-key RGB Matrix", "description": ""},
            {"label": "Underglow only", "description": ""},
        ],
    )
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    await worker.start()
    # User picks option 2 via Matrix.
    await worker.enqueue({"kind": "matrix_reply", "text": "2", "msg_ts": "$M1"})
    # CC's transcript reader subsequently emits a prompt event with the
    # selected option's label (because that's what CC recorded).
    await worker.enqueue(
        {"kind": "prompt", "uuid": "u1", "parentUuid": None, "text": "Underglow only"}
    )
    await worker.stop()
    # The option-label prompt was suppressed — no 👤 mirror back to Matrix.
    assert matrix.posts == []
    reg.close()


@pytest.mark.asyncio
async def test_mirror_marks_thread_fresh_with_yellow_marker(
    tmp_db_path: str, monkeypatch
) -> None:
    """A mirrored response edits the top-level to 🟡 once, and schedules a
    revert. Subsequent mirrors within the window reset the timer without
    re-editing (no edit storm during a burst)."""
    import slackbot.worker as worker_mod
    from slackbot.registry import Registry

    reg = Registry(tmp_db_path)
    reg.open()
    _bound_session(reg, "s1")
    matrix = FakeMatrixIO()
    worker = Worker(sid="s1", reg=reg, matrix=matrix, actuator=FakeActuator())
    # Disable the scheduled revert sleep — we only care about the mark side.
    monkeypatch.setattr(worker_mod, "_RECENT_MARKER_SECONDS", 0.01)
    await worker.start()
    await worker.enqueue({"kind": "response", "uuid": "r1", "parentUuid": "u1", "text": "first"})
    await worker.enqueue({"kind": "response", "uuid": "r2", "parentUuid": "u1", "text": "second"})
    await worker.enqueue({"kind": "response", "uuid": "r3", "parentUuid": "u1", "text": "third"})
    await worker._queue.join()
    # Three responses mirrored — but only one mark edit (🆕), because each
    # subsequent mirror saw _fresh=True and only reset the timer.
    fresh_edits = [e for e in matrix.edits if "🆕" in e[1]]
    assert len(fresh_edits) == 1
    # Both edits keep the leading 🟢 — the marker is a trailing suffix
    # so the colour doesn't flicker in Element's room list.
    assert all(e[1].startswith("🟢 ") for e in matrix.edits)
    # Wait long enough for the revert task to fire (using the shortened delay).
    await asyncio.sleep(0.1)
    revert_edits = [e for e in matrix.edits if "🆕" not in e[1]]
    assert len(revert_edits) >= 1
    await worker.stop()
    reg.close()
