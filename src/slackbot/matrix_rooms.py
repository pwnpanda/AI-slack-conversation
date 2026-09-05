"""Resolve (and lazily create) the per-host Matrix room.

Each daemon host maps to a single room named after its hostname. All
providers (Claude/Codex/Gemini) on that host share it, so work and
private machines land in separate rooms with zero manual wiring — bring
up a daemon on a new machine and it creates its own room on first start.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)


async def resolve_host_room(
    homeserver: str,
    access_token: str,
    hostname: str,
    invite_user_id: str | None,
) -> str:
    """Return the room id for *hostname*, creating a private room if none
    exists yet. Matches an existing room by its `m.room.name` state.

    The bot creates the room, so it holds PL100 there — needed later to
    redact anyone's events for thread deletion.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    base = homeserver.rstrip("/")
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base}/_matrix/client/v3/joined_rooms", headers=headers) as resp:
            resp.raise_for_status()
            joined = (await resp.json()).get("joined_rooms", [])

        for room_id in joined:
            name = await _room_name(http, base, headers, room_id)
            if name == hostname:
                log.info("host room for %s already exists: %s", hostname, room_id)
                return room_id

        body: dict[str, object] = {
            "name": hostname,
            "topic": f"claude-slack-bot sessions on {hostname}",
            "preset": "private_chat",
            "visibility": "private",
        }
        if invite_user_id:
            body["invite"] = [invite_user_id]
        async with http.post(
            f"{base}/_matrix/client/v3/createRoom", headers=headers, json=body
        ) as resp:
            resp.raise_for_status()
            room_id = (await resp.json())["room_id"]
        log.info("created host room for %s: %s", hostname, room_id)
        return str(room_id)


async def ensure_joined(homeserver: str, access_token: str, room_id: str) -> None:
    """Idempotently join *room_id* with the given token.

    The puppet (@human) account must be a member of the host room or its
    prompt mirrors 403. The bot creates/owns the room and auto-invites the
    human, but an invite still needs accepting — this joins on the human's
    behalf. No-op if already joined.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    base = homeserver.rstrip("/")
    url = f"{base}/_matrix/client/v3/join/{room_id}"
    async with aiohttp.ClientSession() as http, http.post(url, headers=headers, json={}) as resp:
        if resp.status != 200:
            body = await resp.text()
            log.warning("join %s failed (%s): %s", room_id, resp.status, body[:200])


async def _room_name(
    http: aiohttp.ClientSession, base: str, headers: dict[str, str], room_id: str
) -> str | None:
    async with http.get(
        f"{base}/_matrix/client/v3/rooms/{room_id}/state/m.room.name/", headers=headers
    ) as resp:
        if resp.status != 200:
            return None
        return (await resp.json()).get("name")
