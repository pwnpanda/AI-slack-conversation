from dataclasses import dataclass, field

import pytest

from slackbot.handlers import EventHandlers
from slackbot.registry import Registry


@dataclass
class FakeSlackIO:
    top_level_posts: list[str] = field(default_factory=list)
    top_level_channels: list[str | None] = field(default_factory=list)
    thread_posts: list[tuple[str, str]] = field(default_factory=list)
    thread_channels: list[str | None] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    edit_channels: list[str | None] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    _ts_counter: int = 0

    def channel_for_agent(self, agent: str) -> str:
        return f"C-{agent.upper()}"

    async def post_top_level(self, text: str, channel: str | None = None) -> str:
        self.top_level_posts.append(text)
        self.top_level_channels.append(channel)
        self._ts_counter += 1
        return f"top.{self._ts_counter}"

    async def post_in_thread(self, thread_ts: str, text: str, channel: str | None = None) -> str:
        self.thread_posts.append((thread_ts, text))
        self.thread_channels.append(channel)
        self._ts_counter += 1
        return f"thr.{self._ts_counter}"

    async def edit_top_level(self, ts: str, text: str, channel: str | None = None) -> None:
        self.edits.append((ts, text))
        self.edit_channels.append(channel)

    async def react(self, ts: str, emoji: str, channel: str | None = None) -> None:
        self.reacts.append((ts, emoji))


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


def _start(sid: str = "s1", cwd: str = "/x", pane: str = "0", agent: str = "claude") -> dict:
    return {
        "kind": "start",
        "session_id": sid,
        "cwd": cwd,
        "zellij_session": "main",
        "zellij_pane_id": pane,
        "agent": agent,
        "resumed": False,
    }


@pytest.mark.asyncio
async def test_claude_start_inserts_no_post_without_name(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start(agent="claude"))
    assert slack.top_level_posts == []
    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.agent == "claude"
    assert sess.name is None
    assert sess.slack_channel == "C-CLAUDE"
    assert reg.get_session("s1") is not None


@pytest.mark.asyncio
async def test_codex_start_auto_registers_without_rn_command(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(
        _start(
            sid="abcdef123456",
            cwd="/home/r/claude-slack-bot",
            agent="codex",
        )
    )

    sess = reg.get_session("abcdef123456")
    assert sess is not None
    assert sess.name == "codex-claude-slack-bot-abcdef12"
    assert sess.slack_thread_ts == "top.1"
    assert slack.top_level_posts == [
        "🟢 [Codex] codex-claude-slack-bot-abcdef12  ·  /home/r/claude-slack-bot"
    ]
    assert slack.top_level_channels == ["C-CODEX"]


@pytest.mark.asyncio
async def test_gemini_start_auto_registers_without_rn_command(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start(sid="1234567890", cwd="/tmp/with space", agent="gemini"))

    sess = reg.get_session("1234567890")
    assert sess is not None
    assert sess.name == "gemini-with-space-12345678"
    assert slack.top_level_posts == ["🟢 [Gemini] gemini-with-space-12345678  ·  /tmp/with space"]


@pytest.mark.asyncio
async def test_prompt_before_name_is_buffered(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "hello"})
    assert slack.thread_posts == []
    buffered = reg.drain_unposted("s1")
    assert len(buffered) == 1
    assert buffered[0].kind == "prompt"


@pytest.mark.asyncio
async def test_name_event_posts_top_level_and_drains_buffer(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "before-name"})
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})

    assert len(slack.top_level_posts) == 1
    assert slack.top_level_posts[0] == "🟢 [Claude] myproj  ·  /x"
    assert slack.top_level_channels[0] == "C-CLAUDE"
    assert len(slack.thread_posts) == 1
    assert slack.thread_posts[0][1] == "[Claude] 👤 before-name"
    assert slack.thread_channels[0] == "C-CLAUDE"
    assert reg.drain_unposted("s1") == []


@pytest.mark.asyncio
async def test_prompt_after_name_posts_directly(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "live"})
    assert len(slack.thread_posts) == 1
    assert slack.thread_posts[0][1] == "[Claude] 👤 live"


@pytest.mark.asyncio
async def test_rename_edits_top_level(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "old"})
    await h.handle({"kind": "name", "session_id": "s1", "name": "new"})
    assert len(slack.top_level_posts) == 1
    assert slack.edits[-1][1] == "🟢 [Claude] new  ·  /x"


@pytest.mark.asyncio
async def test_end_event_edits_to_ended(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "done"})
    assert slack.edits[-1][1] == "⚪ [Claude] myproj  ·  /x  (ended)"
    sess = reg.get_session("s1")
    assert sess is not None and sess.status == "ended"


@pytest.mark.asyncio
async def test_name_reclaim_posts_divider(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start("s1"))
    await h.handle({"kind": "name", "session_id": "s1", "name": "shared"})
    await h.handle(_start("s2", pane="1"))
    await h.handle({"kind": "name", "session_id": "s2", "name": "shared"})

    assert len(slack.top_level_posts) == 1
    divider_posts = [t for _, t in slack.thread_posts if "resumed" in t]
    assert len(divider_posts) == 1


@pytest.mark.asyncio
async def test_resumed_start_flips_back_to_active(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "x"})
    # resume: same session_id, new pane id
    resumed = _start(pane="2")
    resumed["resumed"] = True
    await h.handle(resumed)
    assert slack.edits[-1][1] == "🟢 [Claude] myproj  ·  /x"


@pytest.mark.asyncio
async def test_claude_start_auto_recovers_named_session_in_same_workspace(
    reg: Registry,
) -> None:
    """A new Claude CC session in the same (zellij_session, cwd, agent) workspace
    as a dead named predecessor should inherit the name and Slack thread
    automatically — no /rn required."""
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack, stale_after_seconds=21600)

    # Predecessor: previously /rn'd as 'Finance' but now dead (status=ended).
    await h.handle(_start(sid="old-sid", cwd="/home/r/Finance", pane="6"))
    await h.handle({"kind": "name", "session_id": "old-sid", "name": "Finance"})
    await h.handle({"kind": "end", "session_id": "old-sid", "reason": "x"})
    slack.top_level_posts.clear()
    slack.thread_posts.clear()
    slack.edits.clear()

    # New CC starts in the same cwd; should auto-rebind.
    await h.handle(_start(sid="new-sid", cwd="/home/r/Finance", pane="42"))

    new_sess = reg.get_session("new-sid")
    assert new_sess is not None
    assert new_sess.name == "Finance"
    assert new_sess.slack_thread_ts is not None
    # No new top-level message — we reused the existing thread.
    assert slack.top_level_posts == []
    # A divider posted into the existing thread marking auto-rebind.
    assert any("auto-rebound" in t for _, t in slack.thread_posts)


@pytest.mark.asyncio
async def test_auto_recover_skips_when_predecessor_is_still_fresh(
    reg: Registry,
) -> None:
    """If the prior named session is still active and fresh (recent events), do
    NOT hijack its name — there's a live peer in the same cwd."""
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack, stale_after_seconds=21600)

    await h.handle(_start(sid="alive-sid", cwd="/home/r/Finance", pane="6"))
    await h.handle({"kind": "name", "session_id": "alive-sid", "name": "Finance"})
    slack.top_level_posts.clear()
    slack.thread_posts.clear()

    # New CC in same cwd while old one is still active and fresh.
    await h.handle(_start(sid="other-sid", cwd="/home/r/Finance", pane="42"))

    other = reg.get_session("other-sid")
    assert other is not None
    assert other.name is None  # not hijacked
    alive = reg.get_session("alive-sid")
    assert alive is not None
    assert alive.name == "Finance"  # original still owns it


@pytest.mark.asyncio
async def test_auto_recover_does_not_cross_agents(reg: Registry) -> None:
    """A codex session must not auto-recover a claude name."""
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack, stale_after_seconds=21600)

    await h.handle(_start(sid="claude-old", cwd="/p", agent="claude"))
    await h.handle({"kind": "name", "session_id": "claude-old", "name": "Finance"})
    await h.handle({"kind": "end", "session_id": "claude-old", "reason": "x"})
    slack.top_level_posts.clear()
    slack.thread_posts.clear()

    # New codex session in same cwd should NOT inherit the Claude "Finance" name.
    await h.handle(_start(sid="codex-new", cwd="/p", agent="codex"))

    codex_sess = reg.get_session("codex-new")
    assert codex_sess is not None
    # Codex auto-registers under its own name pattern, never "Finance".
    assert codex_sess.name != "Finance"


@pytest.mark.asyncio
async def test_non_start_events_refresh_cc_pid_and_pane(reg: Registry) -> None:
    """Hooks other than SessionStart now carry cc_pid and pane id so a
    long-running CC heals legacy rows that pre-date the schema."""
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    # Simulate a legacy row: name + thread set, but no cc_pid and stale pane.
    reg.upsert_session("s1", "/p", "main", "OLD", agent="claude")
    reg.set_name("s1", "Finance")
    reg.set_thread_ts("s1", "T.1")

    # A prompt event arrives with current pane + pid.
    await h.handle(
        {
            "kind": "prompt",
            "session_id": "s1",
            "agent": "claude",
            "text": "anything",
            "zellij_session": "main",
            "zellij_pane_id": "NEW",
            "cc_pid": 42424,
        }
    )

    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.cc_pid == 42424
    assert sess.zellij_pane_id == "NEW"


@pytest.mark.asyncio
async def test_refresh_does_not_clobber_with_missing_fields(reg: Registry) -> None:
    """Events that don't carry zellij/pid fields must not wipe known values."""
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    reg.upsert_session("s1", "/p", "main", "13", agent="claude", cc_pid=999)
    reg.set_name("s1", "p")
    reg.set_thread_ts("s1", "T.1")

    # Event with no zellij/pid info (e.g. a kind we don't track).
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "x"})

    sess = reg.get_session("s1")
    assert sess is not None
    assert sess.cc_pid == 999
    assert sess.zellij_pane_id == "13"
