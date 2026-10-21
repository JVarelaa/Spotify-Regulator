"""Sync GET /me/player/recently-played into storage and pair entries with detector events."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .storage import RecentPlay, Storage

log = logging.getLogger(__name__)

CURSOR_KEY = "recently_played_after_ms"
PAGE_LIMIT = 50  # API maximum; Spotify only keeps the last 50 anyway
# A detector event may be stored after Spotify already lists the play, so unpaired entries
# are retried for a while on every sync.
RELINK_WINDOW_MS = 24 * 3600 * 1000


class RecentlyPlayedSource(Protocol):
    async def recently_played(self, *, after_ms: int | None = None, limit: int = 50) -> dict | None: ...


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    added: int
    linked: int


def recent_play_from_item(item: dict) -> RecentPlay | None:
    track = item.get("track") or {}
    track_id = track.get("id") or track.get("uri")
    played_at = item.get("played_at")
    if not track_id or not played_at:
        return None
    album = track.get("album") or {}
    artists = track.get("artists") or []
    return RecentPlay(
        track_id=track_id,
        played_at_ms=int(datetime.fromisoformat(played_at).timestamp() * 1000),
        track_name=track.get("name") or "",
        artist_ids=tuple(a.get("id") or a.get("uri") or "" for a in artists),
        artist_names=tuple(a.get("name") or "" for a in artists),
        album_id=album.get("id"),
        album_name=album.get("name"),
        duration_ms=int(track.get("duration_ms") or 0),
        context_uri=(item.get("context") or {}).get("uri"),
    )


async def sync_recently_played(client: RecentlyPlayedSource, storage: Storage, *, now_ms: int) -> SyncResult:
    """Fetch what's new since the stored cursor, store it without duplicates and pair it."""
    cursor = storage.get_state(CURSOR_KEY)
    after_ms = int(cursor) if cursor else None
    data = await client.recently_played(after_ms=after_ms, limit=PAGE_LIMIT)
    plays = [p for item in (data or {}).get("items", []) if (p := recent_play_from_item(item))]
    if after_ms is not None and len(plays) >= PAGE_LIMIT:
        # More than 50 plays since the last sync: older ones are gone from Spotify's history.
        log.warning("recently_played_possible_gap", extra={"after_ms": after_ms})
    added = storage.add_recent_plays(plays)
    if plays:
        storage.set_state(CURSOR_KEY, str(max(p.played_at_ms for p in plays)))
    linked = storage.link_recent_plays(since_ms=now_ms - RELINK_WINDOW_MS)
    log.info("recently_played_synced", extra={"fetched": len(plays), "new": added, "linked": linked})
    return SyncResult(fetched=len(plays), added=added, linked=linked)
