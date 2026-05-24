from dataclasses import dataclass, field

import pytest

from slackbot.handlers import EventHandlers
from slackbot.registry import Registry


@dataclass
class FakeSlackIO:
    top_level_posts: list[str] = field(default_factory=list)
    thread_posts: list[tuple[str, str]] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    reacts: list[tuple[str, str]] = field(default_factory=list)
    _ts_counter: int = 0

    async def post_top_level(self, text: str) -> str:
        self.top_level_posts.append(text)
        self._ts_counter += 1
        return f"top.{self._ts_counter}"

    async def post_in_thread(self, thread_ts: str, text: str) -> str:
        self.thread_posts.append((thread_ts, text))
        self._ts_counter += 1
        return f"thr.{self._ts_counter}"

    async def edit_top_level(self, ts: str, text: str) -> None:
        self.edits.append((ts, text))

    async def react(self, ts: str, emoji: str) -> None:
        self.reacts.append((ts, emoji))


@pytest.fixture
def reg(tmp_db_path: str):
    r = Registry(tmp_db_path)
    r.open()
    yield r
    r.close()


def _start(sid: str = "s1", cwd: str = "/x", pane: str = "0") -> dict:
    return {
        "kind": "start",
        "session_id": sid,
        "cwd": cwd,
        "zellij_session": "main",
        "zellij_pane_id": pane,
        "resumed": False,
    }


@pytest.mark.asyncio
async def test_start_event_inserts_no_post_without_name(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    assert slack.top_level_posts == []
    assert reg.get_session("s1") is not None


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
    assert slack.top_level_posts[0] == "🟢 myproj  ·  /x"
    assert len(slack.thread_posts) == 1
    assert slack.thread_posts[0][1] == "👤 before-name"
    assert reg.drain_unposted("s1") == []


@pytest.mark.asyncio
async def test_prompt_after_name_posts_directly(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "prompt", "session_id": "s1", "text": "live"})
    assert len(slack.thread_posts) == 1
    assert slack.thread_posts[0][1] == "👤 live"


@pytest.mark.asyncio
async def test_rename_edits_top_level(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "old"})
    await h.handle({"kind": "name", "session_id": "s1", "name": "new"})
    assert len(slack.top_level_posts) == 1
    assert slack.edits[-1][1] == "🟢 new  ·  /x"


@pytest.mark.asyncio
async def test_end_event_edits_to_ended(reg: Registry) -> None:
    slack = FakeSlackIO()
    h = EventHandlers(reg, slack)
    await h.handle(_start())
    await h.handle({"kind": "name", "session_id": "s1", "name": "myproj"})
    await h.handle({"kind": "end", "session_id": "s1", "reason": "done"})
    assert slack.edits[-1][1] == "⚪ myproj  ·  /x  (ended)"
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
    assert slack.edits[-1][1] == "🟢 myproj  ·  /x"
