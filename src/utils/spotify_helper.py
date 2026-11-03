"""Spotify URL parser & metadata extractor.

Resolves Spotify track/album/playlist links via spotipy and turns them
into ``"Artist Name - Track Title"`` strings, which the music cog then
forwards to :mod:`utils.audio_source` for YouTube search & streaming.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

log = logging.getLogger(__name__)

# Safety cap so a huge playlist can't stall the queue forever.
MAX_ITEMS = 100

SUPPORTED_KINDS = ("track", "album", "playlist")

# Matches open.spotify.com links (incl. episodes/shows so we can reject
# them with a friendly message) and spotify: URIs.
_SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/"
    r"(?P<kind>track|album|playlist|episode|show|artist)/"
    r"(?P<id>[A-Za-z0-9]+)",
    re.IGNORECASE,
)
_SPOTIFY_URI_RE = re.compile(
    r"spotify:(?P<kind>track|album|playlist|episode|show|artist):(?P<id>[A-Za-z0-9]+)",
    re.IGNORECASE,
)


class SpotifyError(Exception):
    """Raised when a Spotify link cannot be resolved."""


@dataclass
class SpotifyResult:
    """Resolved Spotify content ready for YouTube search."""

    kind: str
    title: str
    items: list[str] = field(default_factory=list)   # "Artist - Title"


def parse_spotify_url(value: str) -> Optional[tuple[str, str]]:
    """Return ``(kind, id)`` for a Spotify link/URI, else ``None``."""
    match = _SPOTIFY_URL_RE.search(value) or _SPOTIFY_URI_RE.match(value.strip())
    if not match:
        return None
    return match.group("kind").lower(), match.group("id")


def is_spotify_url(value: str) -> bool:
    """True for any Spotify content link or URI."""
    return parse_spotify_url(value) is not None


def _format_track(track: Any) -> Optional[str]:
    """Build an ``"Artist - Title"`` search string from a track object."""
    if not isinstance(track, dict):
        return None
    name = track.get("name")
    if not name:
        return None
    artists = track.get("artists") or []
    artist_names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
    if not artist_names:
        return name
    # First artist keeps the YouTube search tight.
    return f"{artist_names[0]} - {name}"


class SpotifyHelper:
    """Thin async wrapper around spotipy's Client Credentials flow."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ) -> None:
        self._client_id = client_id or os.getenv("SPOTIPY_CLIENT_ID")
        self._client_secret = client_secret or os.getenv("SPOTIPY_CLIENT_SECRET")
        self._sp: Optional[spotipy.Spotify] = None

    @property
    def available(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def client(self) -> spotipy.Spotify:
        if not self.available:
            raise SpotifyError(
                "Spotify credentials are missing — set SPOTIPY_CLIENT_ID and "
                "SPOTIPY_CLIENT_SECRET in your .env file."
            )
        if self._sp is None:
            self._sp = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                )
            )
        return self._sp

    def _paginate(self, page: Any) -> list[Any]:
        """Collect items across a spotipy paging object, capped at MAX_ITEMS."""
        items: list[Any] = []
        while isinstance(page, dict):
            for item in page.get("items") or []:
                items.append(item)
                if len(items) >= MAX_ITEMS:
                    return items
            if not page.get("next"):
                break
            try:
                page = self.client.next(page)
            except Exception as exc:
                log.warning("Stopped paginating Spotify results: %s", exc)
                break
        return items

    def _resolve_sync(self, url: str) -> SpotifyResult:
        parsed = parse_spotify_url(url)
        if parsed is None:
            raise SpotifyError("That doesn't look like a Spotify link.")
        kind, spotify_id = parsed
        if kind not in SUPPORTED_KINDS:
            raise SpotifyError(
                f"Only Spotify tracks, albums, and playlists are supported (got a {kind})."
            )

        try:
            if kind == "track":
                data = self.client.track(spotify_id)
                title = data.get("name") or "Track"
                item = _format_track(data)
                return SpotifyResult(kind, title, [item] if item else [])

            if kind == "album":
                data = self.client.album(spotify_id)
                title = data.get("name") or "Album"
                raw_items = self._paginate(data.get("tracks"))
            else:  # playlist
                data = self.client.playlist(spotify_id)
                title = data.get("name") or "Playlist"
                paging = (data.get("tracks") or {})
                raw_items = self._paginate(paging)
                # Playlist items wrap the track under a "track" key.
                raw_items = [
                    entry.get("track") if isinstance(entry, dict) else None
                    for entry in raw_items
                ]
        except SpotifyError:
            raise
        except Exception as exc:  # spotipy.SpotifyException and network errors
            raise SpotifyError(f"Spotify API error: {exc}") from exc

        items = [text for text in (_format_track(t) for t in raw_items) if text]
        if not items:
            raise SpotifyError(f"No playable tracks found in that {kind}.")
        log.info("Resolved Spotify %s %r -> %d track(s)", kind, title, len(items))
        return SpotifyResult(kind, title, items)

    async def resolve(self, url: str) -> SpotifyResult:
        """Resolve a Spotify URL into ``"Artist - Title"`` search strings."""
        return await asyncio.to_thread(self._resolve_sync, url)
