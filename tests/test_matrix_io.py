from dataclasses import dataclass, field

import nio
import pytest
from aiohttp import web

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
    content = fake.sent[0]["content"]
    assert content["msgtype"] == "m.text"
    assert content["body"] == "🟢 myproj"
    # HTML rendering pairs with the plain body so Element/Element X
    # can render markdown (bold, lists, code blocks) instead of raw text.
    assert content["format"] == "org.matrix.custom.html"
    assert "🟢 myproj" in content["formatted_body"]
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
    new_content = sent["content"]["m.new_content"]
    assert new_content["msgtype"] == "m.text"
    assert new_content["body"] == "⚪ done"
    assert new_content["format"] == "org.matrix.custom.html"
    assert "⚪ done" in new_content["formatted_body"]
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


@pytest.mark.asyncio
async def test_post_renders_markdown_to_html_in_formatted_body() -> None:
    fake = FakeMatrixClient()
    io = MatrixIO(fake, room_id="!default:server")
    await io.post_top_level("**bold** and _italic_ and `code`")
    html = fake.sent[0]["content"]["formatted_body"]
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html
    # Plain text body is preserved verbatim (no HTML there).
    assert fake.sent[0]["content"]["body"] == "**bold** and _italic_ and `code`"


@pytest.mark.asyncio
async def test_single_newlines_become_br_so_multiline_bot_text_renders() -> None:
    """Bot text joins lines with single '\\n' (no double-newline). nl2br
    keeps each line on its own visual line in Element."""
    fake = FakeMatrixClient()
    io = MatrixIO(fake, room_id="!default:server")
    await io.post_top_level("line1\n**line2**\nline3")
    html = fake.sent[0]["content"]["formatted_body"]
    assert "<strong>line2</strong>" in html
    assert "<br" in html  # at least one explicit line break


@pytest.mark.asyncio
async def test_fenced_code_block_renders_as_pre_code() -> None:
    fake = FakeMatrixClient()
    io = MatrixIO(fake, room_id="!default:server")
    await io.post_in_thread("$root:server", "look:\n```\nhello\nworld\n```")
    html = fake.sent[0]["content"]["formatted_body"]
    assert "<pre>" in html
    assert "hello" in html and "world" in html


@pytest.mark.asyncio
async def test_markdown_tables_render_to_html_table() -> None:
    fake = FakeMatrixClient()
    io = MatrixIO(fake, room_id="!default:server")
    body = (
        "Here's a comparison:\n\n"
        "| col1 | col2 |\n"
        "|------|------|\n"
        "| a    | b    |\n"
        "| c    | d    |\n"
    )
    await io.post_top_level(body)
    html = fake.sent[0]["content"]["formatted_body"]
    assert "<table>" in html
    assert "<thead>" in html
    assert "<tbody>" in html
    assert "<td>a</td>" in html and "<td>d</td>" in html


@pytest.mark.asyncio
async def test_as_user_routes_through_user_client_and_records_self_post() -> None:
    bot_client = FakeMatrixClient(next_event_id="$bot:server")
    user_client = FakeMatrixClient(next_event_id="$user:server")
    self_posts: list[str] = []
    io = MatrixIO(
        bot_client,
        room_id="!default:server",
        user_client=user_client,
        on_self_post=self_posts.append,
    )
    assert io.has_user_client() is True
    eid = await io.post_in_thread("$root:server", "hi from me", as_user=True)
    assert eid == "$user:server"
    # Bot client untouched, user client received the post.
    assert bot_client.sent == []
    assert len(user_client.sent) == 1
    # The self-post hook fired so the daemon's dedupe set can ignore the
    # event when it comes back via the bot's sync.
    assert self_posts == ["$user:server"]


@pytest.mark.asyncio
async def test_as_user_false_keeps_bot_path_and_does_not_record_self_post() -> None:
    bot_client = FakeMatrixClient(next_event_id="$bot:server")
    user_client = FakeMatrixClient(next_event_id="$user:server")
    self_posts: list[str] = []
    io = MatrixIO(
        bot_client,
        room_id="!default:server",
        user_client=user_client,
        on_self_post=self_posts.append,
    )
    eid = await io.post_in_thread("$root:server", "from bot", as_user=False)
    assert eid == "$bot:server"
    assert len(bot_client.sent) == 1
    assert user_client.sent == []
    # No self-post recording — bot-account posts come back via sync and
    # are filtered by event.sender == bot.user_id, not by event_id dedupe.
    assert self_posts == []


@pytest.mark.asyncio
async def test_no_user_client_means_as_user_falls_back_to_bot_client() -> None:
    bot_client = FakeMatrixClient(next_event_id="$bot:server")
    io = MatrixIO(bot_client, room_id="!default:server")
    assert io.has_user_client() is False
    # Requesting as_user with no user client wired: silently use the bot.
    await io.post_in_thread("$root:server", "ok", as_user=True)
    assert len(bot_client.sent) == 1


def _sliding_relations_app(
    thread: list[dict], seen_requests: list[dict], status: int = 200
) -> web.Application:
    """Mimic Continuwuity's /relations: newest-first pages of 100 whose
    next_batch steps back only one event, so consecutive pages overlap."""

    async def relations(request: web.Request) -> web.Response:
        seen_requests.append(dict(request.query))
        if status != 200:
            return web.Response(status=status, text="nope")
        offset = int(request.query.get("from", "0"))
        limit = int(request.query["limit"])
        chunk = thread[offset : offset + limit]
        body: dict = {"chunk": chunk}
        if offset + limit < len(thread):
            body["next_batch"] = str(offset + 1)
        return web.json_response(body)

    app = web.Application()
    app.router.add_get("/_matrix/client/v1/rooms/{room}/relations/{root}", relations)
    return app


@dataclass
class FakeRestClient:
    homeserver: str
    access_token: str = "tok"
    redact_response: object = None

    async def room_redact(self, room_id, event_id, reason=None):
        return self.redact_response


def _child(n: int, redacted: bool = False) -> dict:
    event: dict = {"event_id": f"$c{n}"}
    if redacted:
        event["unsigned"] = {"redacted_because": {"event_id": "$r"}}
    return event


async def test_thread_event_ids_dedupes_overlapping_pages(aiohttp_server) -> None:
    thread = [_child(n) for n in range(250, 0, -1)]
    requests: list[dict] = []
    server = await aiohttp_server(_sliding_relations_app(thread, requests))
    io = MatrixIO(FakeRestClient(str(server.make_url(""))), room_id="!r:server")
    ids = await io.thread_event_ids("!r:server", "$root")
    assert ids == [e["event_id"] for e in thread] + ["$root"]
    assert requests[0] == {"recurse": "true", "limit": "100"}


async def test_thread_event_ids_skips_already_redacted(aiohttp_server) -> None:
    thread = [_child(3), _child(2, redacted=True), _child(1)]
    server = await aiohttp_server(_sliding_relations_app(thread, []))
    io = MatrixIO(FakeRestClient(str(server.make_url(""))), room_id="!r:server")
    assert await io.thread_event_ids("!r:server", "$root") == ["$c3", "$c1", "$root"]


async def test_thread_event_ids_raises_on_http_error(aiohttp_server) -> None:
    server = await aiohttp_server(_sliding_relations_app([], [], status=403))
    io = MatrixIO(FakeRestClient(str(server.make_url(""))), room_id="!r:server")
    with pytest.raises(RuntimeError, match="403"):
        await io.thread_event_ids("!r:server", "$root")


async def test_redact_raises_on_error_response() -> None:
    client = FakeRestClient(
        "http://unused", redact_response=nio.RoomRedactError("slow down", "M_LIMIT_EXCEEDED")
    )
    io = MatrixIO(client, room_id="!r:server")
    with pytest.raises(RuntimeError, match="M_LIMIT_EXCEEDED"):
        await io.redact("!r:server", "$e")
