from dataclasses import dataclass, field

import pytest

from slackbot.matrix_io import MatrixIO, _emoji_for


@dataclass
class FakeRoomSendResponse:
    event_id: str = "$evt:server"


@dataclass
class FakeMatrixClient:
    sent: list[dict] = field(default_factory=list)
    next_event_id: str = "$evt:server"

    async def room_send(self, room_id, message_type, content, **kwargs):
        self.sent.append({"room_id": room_id, "message_type": message_type, "content": content})
        return FakeRoomSendResponse(event_id=self.next_event_id)


@pytest.mark.asyncio
async def test_post_top_level_returns_event_id() -> None:
    fake = FakeMatrixClient(next_event_id="$top:server")
    io = MatrixIO(fake, room_id="!default:server", agent_rooms={"codex": "!codex:server"})
    eid = await io.post_top_level("🟢 myproj", room_id=io.room_for_agent("codex"))
    assert eid == "$top:server"
    assert fake.sent[0]["room_id"] == "!codex:server"
    assert fake.sent[0]["message_type"] == "m.room.message"
    assert fake.sent[0]["content"] == {"msgtype": "m.text", "body": "🟢 myproj"}
    # Unmapped agent falls back to default room.
    assert io.room_for_agent("gemini") == "!default:server"


@pytest.mark.asyncio
async def test_post_in_thread_sets_m_thread_relation() -> None:
    fake = FakeMatrixClient(next_event_id="$reply:server")
    io = MatrixIO(fake, room_id="!default:server")
    eid = await io.post_in_thread("$root:server", "👤 hi")
    assert eid == "$reply:server"
    sent = fake.sent[0]
    assert sent["room_id"] == "!default:server"
    assert sent["message_type"] == "m.room.message"
    assert sent["content"]["msgtype"] == "m.text"
    assert sent["content"]["body"] == "👤 hi"
    relates = sent["content"]["m.relates_to"]
    assert relates["rel_type"] == "m.thread"
    assert relates["event_id"] == "$root:server"
    # Reply-fallback fields so non-thread-aware clients still render as a reply.
    assert relates["is_falling_back"] is True
    assert relates["m.in_reply_to"] == {"event_id": "$root:server"}


@pytest.mark.asyncio
async def test_edit_top_level_uses_m_replace_with_new_content() -> None:
    fake = FakeMatrixClient()
    io = MatrixIO(fake, room_id="!default:server")
    await io.edit_top_level("$orig:server", "⚪ done")
    sent = fake.sent[0]
    assert sent["room_id"] == "!default:server"
    assert sent["message_type"] == "m.room.message"
    # Fallback body for clients that ignore edits.
    assert sent["content"]["body"] == "* ⚪ done"
    assert sent["content"]["m.new_content"] == {"msgtype": "m.text", "body": "⚪ done"}
    relates = sent["content"]["m.relates_to"]
    assert relates["rel_type"] == "m.replace"
    assert relates["event_id"] == "$orig:server"


@pytest.mark.asyncio
async def test_react_uses_m_reaction_annotation() -> None:
    fake = FakeMatrixClient()
    io = MatrixIO(fake, room_id="!default:server")
    await io.react("$orig:server", "white_check_mark")
    sent = fake.sent[0]
    assert sent["room_id"] == "!default:server"
    assert sent["message_type"] == "m.reaction"
    relates = sent["content"]["m.relates_to"]
    assert relates["rel_type"] == "m.annotation"
    assert relates["event_id"] == "$orig:server"
    # Unicode glyph, not the Slack name.
    assert relates["key"] == "✅"


@pytest.mark.asyncio
async def test_react_swallows_errors() -> None:
    class _Boom:
        async def room_send(self, **kwargs):
            raise RuntimeError("network down")

    io = MatrixIO(_Boom(), room_id="!default:server")
    # Must not raise; reactions are cosmetic.
    await io.react("$orig:server", "warning")


def test_emoji_for_maps_known_names() -> None:
    assert _emoji_for("white_check_mark") == "✅"
    assert _emoji_for("warning") == "⚠️"
    assert _emoji_for("x") == "❌"
    assert _emoji_for("hourglass_flowing_sand") == "⏳"


def test_emoji_for_returns_unknown_name_unchanged() -> None:
    assert _emoji_for("not-a-known-name") == "not-a-known-name"
