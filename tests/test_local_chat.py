import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from slackbot.local_chat import LocalChatManager
from slackbot.local_chat_store import LocalChatStore


@dataclass
class FakeLlama:
    chunks: list[str] = field(default_factory=lambda: ["Hello", " there"])
    requests: list[list[dict[str, str]]] = field(default_factory=list)
    keep_alive_calls: int = 0
    sleeping: bool = False

    async def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        self.requests.append(messages)
        for chunk in self.chunks:
            yield chunk

    async def keep_alive(self) -> None:
        self.keep_alive_calls += 1

    async def is_sleeping(self) -> bool:
        return self.sleeping


@dataclass
class FakeMatrix:
    posts: list[tuple[str, str, str]] = field(default_factory=list)
    edits: list[tuple[str, str, str]] = field(default_factory=list)
    user_posts: list[str] = field(default_factory=list)
    top_level_user_flags: list[bool] = field(default_factory=list)

    async def post_top_level(
        self,
        text: str,
        room_id: str | None = None,
        as_user: bool = False,
    ) -> str:
        event_id = f"$root-{len(self.posts)}"
        self.posts.append((event_id, text, room_id or ""))
        self.top_level_user_flags.append(as_user)
        return event_id

    async def post_in_thread(
        self,
        thread_root: str,
        text: str,
        room_id: str | None = None,
        as_user: bool = False,
    ) -> str:
        event_id = f"$reply-{len(self.posts)}"
        self.posts.append((thread_root, text, room_id or ""))
        if as_user:
            self.user_posts.append(text)
        return event_id

    async def edit_top_level(self, ts: str, text: str, room_id: str | None = None) -> None:
        self.edits.append((ts, text, room_id or ""))


def _manager(tmp_path, *, warning: float = 1800, grace: float = 180):
    llama = FakeLlama()
    matrix = FakeMatrix()
    store = LocalChatStore(str(tmp_path))
    manager = LocalChatManager(
        client=llama,
        store=store,
        matrix=matrix,
        idle_warning_seconds=warning,
        idle_grace_seconds=grace,
        edit_interval_seconds=0,
    )
    return manager, llama, matrix, store


@pytest.mark.asyncio
async def test_start_streams_answer_and_persists_history(tmp_path) -> None:
    manager, llama, matrix, store = _manager(tmp_path)

    await manager.start("!room", "$root", "Say hello")

    assert manager.is_conversation("!room", "$root") is True
    assert matrix.posts == [("$root", "…", "!room")]
    assert matrix.edits[-1][1] == "Hello there"
    conversation = store.load("!room", "$root")
    assert conversation.messages == [
        {"role": "user", "content": "Say hello"},
        {"role": "assistant", "content": "Hello there"},
    ]
    assert llama.requests[0][0]["role"] == "system"


@pytest.mark.asyncio
async def test_reply_sends_existing_history_to_model(tmp_path) -> None:
    manager, llama, _, _ = _manager(tmp_path)
    await manager.start("!room", "$root", "First")

    await manager.reply("!room", "$root", "Second")

    sent = llama.requests[-1]
    assert [message["content"] for message in sent[1:]] == [
        "First",
        "Hello there",
        "Second",
    ]


@pytest.mark.asyncio
async def test_web_mirror_creates_continuable_matrix_thread(tmp_path) -> None:
    manager, _, matrix, store = _manager(tmp_path)
    history = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "user", "content": "Browser prompt"},
    ]

    thread_root, response_event = await manager.start_web_mirror(
        "!room",
        "Browser prompt",
        history,
        "browser-conversation",
    )
    await manager.update_web_mirror("!room", response_event, "Browser")
    await manager.finish_web_mirror(
        "!room",
        thread_root,
        response_event,
        "Browser answer",
    )

    assert matrix.posts[0][1] == "🟢 Local: New Chat  ·  llama.cpp Web UI"
    assert matrix.top_level_user_flags == [False]
    assert matrix.posts[1] == (thread_root, "Browser prompt", "!room")
    assert matrix.user_posts == ["Browser prompt"]
    assert matrix.edits[0] == (
        thread_root,
        "🟢 Local: Earlier question  ·  llama.cpp Web UI",
        "!room",
    )
    assert manager.is_conversation("!room", thread_root) is True
    assert store.load("!room", thread_root).messages == [
        *history,
        {"role": "assistant", "content": "Browser answer"},
    ]

    next_history = [*history, {"role": "assistant", "content": "Browser answer"}]
    next_history.append({"role": "user", "content": "Follow-up"})
    reused_root, _ = await manager.start_web_mirror(
        "!room",
        "Follow-up",
        next_history,
        "browser-conversation",
    )

    assert reused_root == thread_root
    assert matrix.posts[-2] == (thread_root, "Follow-up", "!room")
    assert matrix.user_posts == ["Browser prompt", "Follow-up"]


@pytest.mark.asyncio
async def test_idle_warning_veto_keeps_cache_loaded(tmp_path) -> None:
    manager, llama, matrix, store = _manager(tmp_path, warning=0, grace=3)
    await manager.start("!room", "$root", "Hello")

    now = time.time()
    await manager.check_idle(now)
    assert "DO NOT CLEAR CACHE" in matrix.posts[-1][1]

    await manager.reply("!room", "$root", "DO NOT CLEAR CACHE")

    assert llama.keep_alive_calls == 1
    assert "retained" in matrix.posts[-1][1]
    contents = [message["content"] for message in store.load("!room", "$root").messages]
    assert "DO NOT CLEAR CACHE" not in contents


@pytest.mark.asyncio
async def test_idle_monitor_reports_unload_after_grace(tmp_path) -> None:
    manager, llama, matrix, _ = _manager(tmp_path, warning=0, grace=3)
    await manager.start("!room", "$root", "Hello")
    now = time.time()
    await manager.check_idle(now)
    llama.sleeping = True

    await manager.check_idle(now + 3)

    assert "cache unloaded" in matrix.posts[-1][1]


def test_delete_removes_persisted_conversation(tmp_path) -> None:
    manager, _, _, store = _manager(tmp_path)
    store.create("!room", "$root")

    manager.delete("!room", "$root")

    assert manager.is_conversation("!room", "$root") is False


@pytest.mark.asyncio
async def test_idle_deadline_survives_manager_restart(tmp_path) -> None:
    manager, _, _, store = _manager(tmp_path, warning=0)
    await manager.start("!room", "$root", "Hello")
    replacement = LocalChatManager(
        client=FakeLlama(),
        store=store,
        matrix=FakeMatrix(),
        idle_warning_seconds=0,
    )

    await replacement.check_idle(time.time())

    state = store.load_idle_state()
    assert state is not None
    assert state["warning_sent_at"] is not None
