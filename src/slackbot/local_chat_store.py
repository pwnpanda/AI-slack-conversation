"""File-backed conversation history for local Matrix chats."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LocalConversation:
    """A Matrix thread and its ordered local-model messages."""

    room_id: str
    thread_root: str
    messages: list[dict[str, str]]


class LocalChatStore:
    """Persist each Matrix conversation as an inspectable JSON file."""

    def __init__(self, directory: str, max_prompt_chars: int = 12_000) -> None:
        self._directory = Path(directory)
        self._max_prompt_chars = max_prompt_chars

    def exists(self, room_id: str, thread_root: str) -> bool:
        return self._path(room_id, thread_root).is_file()

    def create(self, room_id: str, thread_root: str) -> LocalConversation:
        conversation = LocalConversation(room_id=room_id, thread_root=thread_root, messages=[])
        self._save(conversation)
        return conversation

    def append(self, room_id: str, thread_root: str, role: str, content: str) -> None:
        conversation = self.load(room_id, thread_root)
        conversation.messages.append({"role": role, "content": content})
        self._save(conversation)

    def replace_messages(
        self,
        room_id: str,
        thread_root: str,
        messages: list[dict[str, str]],
    ) -> None:
        conversation = LocalConversation(
            room_id=room_id,
            thread_root=thread_root,
            messages=list(messages),
        )
        self._save(conversation)

    def web_thread(self, conversation_id: str) -> tuple[str, str] | None:
        target = self._load_web_index().get(conversation_id)
        if not isinstance(target, dict):
            return None
        room_id = target.get("room_id")
        thread_root = target.get("thread_root")
        if not isinstance(room_id, str) or not isinstance(thread_root, str):
            return None
        if not self.exists(room_id, thread_root):
            return None
        return room_id, thread_root

    def save_web_thread(self, conversation_id: str, room_id: str, thread_root: str) -> None:
        index = self._load_web_index()
        index[conversation_id] = {"room_id": room_id, "thread_root": thread_root}
        self._write_json(self._directory / "web-conversations.json", index)

    def load(self, room_id: str, thread_root: str) -> LocalConversation:
        path = self._path(room_id, thread_root)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Local conversation does not exist: {thread_root}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Local conversation history is invalid JSON: {path}") from exc
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise RuntimeError(f"Local conversation history has no message list: {path}")
        return LocalConversation(
            room_id=str(payload["room_id"]),
            thread_root=str(payload["thread_root"]),
            messages=messages,
        )

    def recent_messages(self, room_id: str, thread_root: str) -> list[dict[str, str]]:
        messages = self.load(room_id, thread_root).messages
        selected: list[dict[str, str]] = []
        used = 0
        for message in reversed(messages):
            size = len(message.get("content", ""))
            if selected and used + size > self._max_prompt_chars:
                break
            selected.append(message)
            used += size
        selected.reverse()
        return selected

    def delete(self, room_id: str, thread_root: str) -> None:
        self._path(room_id, thread_root).unlink(missing_ok=True)
        index = self._load_web_index()
        filtered = {
            key: value
            for key, value in index.items()
            if value != {"room_id": room_id, "thread_root": thread_root}
        }
        if filtered != index:
            self._write_json(self._directory / "web-conversations.json", filtered)

    def load_idle_state(self) -> dict[str, object] | None:
        path = self._directory / "idle-state.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Local model idle state is invalid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Local model idle state is not an object: {path}")
        return payload

    def save_idle_state(
        self,
        last_activity: float,
        room_id: str,
        thread_root: str,
        warning_sent_at: float | None,
    ) -> None:
        payload = {
            "last_activity": last_activity,
            "room_id": room_id,
            "thread_root": thread_root,
            "warning_sent_at": warning_sent_at,
        }
        self._write_json(self._directory / "idle-state.json", payload)

    def clear_idle_state(self) -> None:
        (self._directory / "idle-state.json").unlink(missing_ok=True)

    def _load_web_index(self) -> dict[str, dict[str, str]]:
        path = self._directory / "web-conversations.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Local web conversation index is invalid JSON: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Local web conversation index is not an object: {path}")
        index: dict[str, dict[str, str]] = {}
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise RuntimeError(f"Local web conversation index has an invalid entry: {path}")
            room_id = value.get("room_id")
            thread_root = value.get("thread_root")
            if not isinstance(room_id, str) or not isinstance(thread_root, str):
                raise RuntimeError(f"Local web conversation index has an invalid target: {path}")
            index[key] = {"room_id": room_id, "thread_root": thread_root}
        return index

    def _path(self, room_id: str, thread_root: str) -> Path:
        key = hashlib.sha256(f"{room_id}\0{thread_root}".encode()).hexdigest()
        return self._directory / f"{key}.json"

    def _save(self, conversation: LocalConversation) -> None:
        path = self._path(conversation.room_id, conversation.thread_root)
        payload = {
            "room_id": conversation.room_id,
            "thread_root": conversation.thread_root,
            "messages": conversation.messages,
        }
        self._write_json(path, payload)

    def _write_json(self, path: Path, payload: object) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
