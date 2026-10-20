"""Matrix conversation controller for the local llama.cpp model."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Coroutine
from typing import Any, Protocol

from slackbot.events import top_level_text
from slackbot.local_chat_store import LocalChatStore

log = logging.getLogger(__name__)

_KEEP_CACHE_PHRASE = "DO NOT CLEAR CACHE"
_WEB_PLACEHOLDER_NAME = "New Chat"
_WEB_SOURCE = "llama.cpp Web UI"
_WEB_TITLE_MAX_CHARS = 120
_SYSTEM_PROMPT = (
    "Answer directly and concisely. Follow transformation instructions exactly. "
    "Preserve input wording and formatting unless the user explicitly requests changes."
)


def _web_title(history: list[dict[str, str]]) -> str:
    for message in history:
        if message["role"] != "user":
            continue
        for line in message["content"].splitlines():
            if line.strip():
                title = line.strip()
                if len(title) <= _WEB_TITLE_MAX_CHARS:
                    return title
                return title[: _WEB_TITLE_MAX_CHARS - 1] + "…"
    return ""


class _MatrixProto(Protocol):
    async def post_top_level(
        self,
        text: str,
        room_id: str | None = None,
        as_user: bool = False,
    ) -> str: ...

    async def post_in_thread(
        self,
        thread_root: str,
        text: str,
        room_id: str | None = None,
        as_user: bool = False,
    ) -> str: ...

    async def edit_top_level(self, ts: str, text: str, room_id: str | None = None) -> None: ...


class _LlamaProto(Protocol):
    def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...

    async def keep_alive(self) -> None: ...

    async def is_sleeping(self) -> bool: ...


class LocalChatManager:
    """Stream local-model answers into Matrix and manage idle-cache warnings."""

    def __init__(
        self,
        client: _LlamaProto,
        store: LocalChatStore,
        matrix: _MatrixProto,
        idle_warning_seconds: float = 1800,
        idle_grace_seconds: float = 180,
        edit_interval_seconds: float = 0.75,
    ) -> None:
        self._client = client
        self._store = store
        self._matrix = matrix
        self._idle_warning_seconds = idle_warning_seconds
        self._idle_grace_seconds = idle_grace_seconds
        self._edit_interval_seconds = edit_interval_seconds
        self._last_activity: float | None = None
        self._target: tuple[str, str] | None = None
        self._warning_sent_at: float | None = None
        self._lock = asyncio.Lock()
        self._response_tasks: set[asyncio.Task[None]] = set()
        self._restore_idle_state()

    def is_conversation(self, room_id: str, thread_root: str) -> bool:
        return self._store.exists(room_id, thread_root)

    async def start(self, room_id: str, thread_root: str, prompt: str) -> None:
        self._store.create(room_id, thread_root)
        await self.reply(room_id, thread_root, prompt)

    def start_background(self, room_id: str, thread_root: str, prompt: str) -> None:
        self._spawn_response(self.start(room_id, thread_root, prompt))

    async def reply(self, room_id: str, thread_root: str, text: str) -> None:
        if await self._handle_cache_veto(room_id, thread_root, text):
            return
        async with self._lock:
            self._record_activity(room_id, thread_root)
            self._store.append(room_id, thread_root, "user", text)
            await self._stream_response(room_id, thread_root)

    def reply_background(self, room_id: str, thread_root: str, text: str) -> None:
        self._spawn_response(self.reply(room_id, thread_root, text))

    async def start_web_mirror(
        self,
        room_id: str,
        prompt: str,
        history: list[dict[str, str]],
        conversation_id: str | None,
    ) -> tuple[str, str]:
        target = self._store.web_thread(conversation_id) if conversation_id else None
        if target is not None and target[0] == room_id:
            thread_root = target[1]
        else:
            thread_root = await self._matrix.post_top_level(
                top_level_text(
                    _WEB_PLACEHOLDER_NAME,
                    _WEB_SOURCE,
                    "active",
                    "local",
                ),
                room_id=room_id,
            )
            if conversation_id:
                self._store.save_web_thread(conversation_id, room_id, thread_root)
        title = _web_title(history)
        if title:
            await self._matrix.edit_top_level(
                thread_root,
                top_level_text(title, _WEB_SOURCE, "active", "local"),
                room_id=room_id,
            )
        await self._matrix.post_in_thread(
            thread_root,
            prompt,
            room_id=room_id,
            as_user=True,
        )
        self._store.replace_messages(room_id, thread_root, history)
        self._record_activity(room_id, thread_root)
        response_event = await self._matrix.post_in_thread(
            thread_root,
            "…",
            room_id=room_id,
        )
        return thread_root, response_event

    async def update_web_mirror(
        self,
        room_id: str,
        response_event: str,
        answer: str,
    ) -> None:
        await self._matrix.edit_top_level(response_event, answer, room_id=room_id)

    async def finish_web_mirror(
        self,
        room_id: str,
        thread_root: str,
        response_event: str,
        answer: str,
    ) -> None:
        answer = answer.strip() or "❌ Local model returned an empty response."
        await self.update_web_mirror(room_id, response_event, answer)
        self._store.append(room_id, thread_root, "assistant", answer)

    async def shutdown(self) -> None:
        for task in self._response_tasks:
            task.cancel()
        if self._response_tasks:
            await asyncio.gather(*self._response_tasks, return_exceptions=True)

    def delete(self, room_id: str, thread_root: str) -> None:
        self._store.delete(room_id, thread_root)
        if self._target == (room_id, thread_root):
            self._last_activity = None
            self._target = None
            self._warning_sent_at = None
            self._store.clear_idle_state()

    async def monitor_idle(self) -> None:
        while True:
            try:
                await self.check_idle(time.time())
            except Exception:
                log.exception("local-model idle check failed")
            await asyncio.sleep(5)

    async def check_idle(self, now: float) -> None:
        if self._last_activity is None or self._target is None:
            return
        if self._warning_sent_at is None:
            if now - self._last_activity < self._idle_warning_seconds:
                return
            room_id, thread_root = self._target
            await self._matrix.post_in_thread(
                thread_root,
                "⚠️ The local model has been idle for 30 minutes and will unload its "
                "model/KV cache in 3 minutes. Reply exactly `DO NOT CLEAR CACHE` in "
                "this thread to keep it loaded. Conversation history will stay on disk.",
                room_id=room_id,
            )
            self._warning_sent_at = now
            self._persist_idle_state()
            return
        if now - self._warning_sent_at < self._idle_grace_seconds:
            return
        if not await self._client.is_sleeping():
            return
        room_id, thread_root = self._target
        await self._matrix.post_in_thread(
            thread_root,
            "💤 Local model cache unloaded. The next prompt will reload it; conversation "
            "history is preserved.",
            room_id=room_id,
        )
        self._last_activity = None
        self._target = None
        self._warning_sent_at = None
        self._store.clear_idle_state()

    async def _handle_cache_veto(self, room_id: str, thread_root: str, text: str) -> bool:
        if text.strip() != _KEEP_CACHE_PHRASE:
            return False
        if self._warning_sent_at is None or self._target != (room_id, thread_root):
            return False
        await self._client.keep_alive()
        self._record_activity(room_id, thread_root)
        await self._matrix.post_in_thread(
            thread_root,
            "✅ Local model cache retained for another 30 minutes.",
            room_id=room_id,
        )
        return True

    def _record_activity(self, room_id: str, thread_root: str) -> None:
        self._last_activity = time.time()
        self._target = (room_id, thread_root)
        self._warning_sent_at = None
        self._persist_idle_state()

    def _restore_idle_state(self) -> None:
        state = self._store.load_idle_state()
        if state is None:
            return
        try:
            last_activity = state["last_activity"]
            room_id = state["room_id"]
            thread_root = state["thread_root"]
            warning = state.get("warning_sent_at")
            if not isinstance(last_activity, int | float):
                raise TypeError("last_activity must be numeric")
            if not isinstance(room_id, str) or not isinstance(thread_root, str):
                raise TypeError("room and thread identifiers must be strings")
            if warning is not None and not isinstance(warning, int | float):
                raise TypeError("warning_sent_at must be numeric or null")
            self._last_activity = float(last_activity)
            self._target = (room_id, thread_root)
            self._warning_sent_at = float(warning) if warning is not None else None
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Local model idle state is incomplete") from exc

    def _persist_idle_state(self) -> None:
        if self._last_activity is None or self._target is None:
            return
        room_id, thread_root = self._target
        self._store.save_idle_state(
            self._last_activity,
            room_id,
            thread_root,
            self._warning_sent_at,
        )

    def _spawn_response(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(self._run_response(coroutine))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    @staticmethod
    async def _run_response(coroutine: Coroutine[Any, Any, None]) -> None:
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background local-model response failed")

    async def _stream_response(self, room_id: str, thread_root: str) -> None:
        event_id = await self._matrix.post_in_thread(thread_root, "…", room_id=room_id)
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        messages.extend(self._store.recent_messages(room_id, thread_root))
        answer = ""
        last_edit = asyncio.get_running_loop().time()
        try:
            async for chunk in self._client.stream_chat(messages):
                answer += chunk
                now = asyncio.get_running_loop().time()
                if now - last_edit >= self._edit_interval_seconds:
                    await self._matrix.edit_top_level(event_id, answer, room_id=room_id)
                    last_edit = now
        except Exception as exc:
            log.exception("local model response failed")
            await self._matrix.edit_top_level(
                event_id,
                f"❌ Local model request failed: {exc}",
                room_id=room_id,
            )
            return
        answer = answer.strip()
        if not answer:
            answer = "❌ Local model returned an empty response."
        await self._matrix.edit_top_level(event_id, answer, room_id=room_id)
        self._store.append(room_id, thread_root, "assistant", answer)
