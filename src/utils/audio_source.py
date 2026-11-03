"""yt-dlp & FFmpeg audio stream handler.

Resolves YouTube links, ``ytsearch:`` queries, plain-text searches, and
YouTube playlist URLs into :class:`Track` objects without ever writing
media to disk, and wraps direct audio streams in
:class:`discord.FFmpegPCMAudio` sources.

Note: any YouTube URL carrying a ``list=`` parameter (e.g. a shared
``watch?v=X&list=Y`` link) is treated as a playlist request.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp

log = logging.getLogger(__name__)

# Stability options for streaming remote media.
# The User-Agent header is critical; YouTube frequently returns 403 Forbidden
# to FFmpeg if it's missing or generic.
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    '-headers "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36\r\n"'
)
FFMPEG_OPTIONS = "-vn"

YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "default_search": "auto",
    "noplaylist": True,       # flipped off for playlist URLs
    "skip_download": True,    # never touch the disk
    "ignoreerrors": True,     # one broken entry must not kill a whole playlist
    "quiet": True,
    "no_warnings": True,
}


def _apply_cookie_options() -> None:
    """Optional YouTube bot-check bypass, configured via the environment.

    YouTube sometimes answers searches with "Sign in to confirm you're not
    a bot". Passing cookies from a logged-in browser session fixes it:

    - ``YTDLP_COOKIES_FILE``: path to a cookies.txt export (takes priority)
    - ``YTDLP_COOKIES_FROM_BROWSER``: browser name (firefox, chrome, ...)
    """
    cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

    # Idempotent: drop anything applied by a previous run, then re-apply.
    YTDL_OPTIONS.pop("cookiefile", None)
    YTDL_OPTIONS.pop("cookiesfrombrowser", None)

    if cookies_file and browser and os.path.isfile(cookies_file):
        log.warning(
            "Both YTDLP_COOKIES_FILE and YTDLP_COOKIES_FROM_BROWSER are set; "
            "using the cookies file."
        )

    if cookies_file:
        if os.path.isfile(cookies_file):
            YTDL_OPTIONS["cookiefile"] = cookies_file
            log.info("yt-dlp authenticating with cookies file: %s", cookies_file)
            return
        log.warning(
            "YTDLP_COOKIES_FILE points to a missing file: %s", cookies_file
        )

    if browser:
        # yt-dlp expects e.g. ("firefox",); it reads the browser's cookie DB.
        YTDL_OPTIONS["cookiesfrombrowser"] = (browser.lower(),)
        log.info("yt-dlp reading cookies from browser: %s", browser.lower())


_apply_cookie_options()


class ExtractionError(Exception):
    """Raised when yt-dlp cannot resolve a query into playable tracks."""


@dataclass
class Track:
    """Metadata for a single queued track."""

    title: str
    webpage_url: str
    duration: Optional[int] = None          # seconds, may be unknown for flat entries
    uploader: Optional[str] = None
    requester_id: Optional[int] = None
    requester_name: Optional[str] = None

    @property
    def duration_label(self) -> str:
        if self.duration is None:
            return "?:??"
        seconds = int(self.duration)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _is_playlist_url(url: str) -> bool:
    """True for YouTube URLs that carry a ``list=`` playlist parameter."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if "youtube.com" in host or "youtu.be" in host:
        return "list" in parse_qs(parsed.query)
    return False


def _track_from_entry(entry: Any) -> Optional[Track]:
    """Build a :class:`Track` from a yt-dlp info dict (flat or full)."""
    if not isinstance(entry, dict) or not entry:
        return None

    title = entry.get("title") or entry.get("fulltitle") or "Unknown title"
    webpage_url = entry.get("webpage_url")
    if not webpage_url:
        candidate = entry.get("url") or entry.get("id") or ""
        if candidate.startswith(("http://", "https://")):
            webpage_url = candidate
        elif candidate:
            # Flat playlist entries expose bare video IDs.
            webpage_url = f"https://www.youtube.com/watch?v={candidate}"
    if not webpage_url:
        return None

    uploader = (
        entry.get("uploader")
        or entry.get("channel")
        or entry.get("artist")
        or entry.get("creator")
    )
    return Track(
        title=title,
        webpage_url=webpage_url,
        duration=entry.get("duration"),
        uploader=uploader,
    )


def _collect_tracks(info: Any) -> list[Track]:
    """Flatten an extraction result (single video, search, or playlist)."""
    if not isinstance(info, dict):
        return []

    entries = info.get("entries")
    if entries is None:
        # Search results and single videos that came back as a bare dict.
        if info.get("_type") in ("url", "url_transparent") and info.get("entries") is None:
            single = _track_from_entry(info)
            return [single] if single else []
        track = _track_from_entry(info)
        return [track] if track else []

    tracks: list[Track] = []
    for entry in entries:
        if not entry:
            continue
        if entry.get("entries") is not None:
            tracks.extend(_collect_tracks(entry))   # nested playlist
        else:
            track = _track_from_entry(entry)
            if track:
                tracks.append(track)
    return tracks


def _extract_tracks_sync(query: str) -> list[Track]:
    query = query.strip()
    if not query:
        raise ExtractionError("Empty query.")

    opts = dict(YTDL_OPTIONS)
    if _is_url(query):
        if _is_playlist_url(query):
            opts["noplaylist"] = False
            opts["extract_flat"] = "in_playlist"
    elif not query.lower().startswith("ytsearch"):
        # Plain text: grab exactly the top YouTube match.
        query = f"ytsearch1:{query}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception as exc:  # DownloadError and friends
        raise ExtractionError(str(exc)) from exc

    if info is None:
        raise ExtractionError(f"Nothing found for: {query}")

    tracks = _collect_tracks(info)
    if not tracks:
        raise ExtractionError(f"No playable results for: {query}")
    log.info("Extracted %d track(s) for %r", len(tracks), query)
    return tracks


async def extract_tracks(query: str) -> list[Track]:
    """Resolve a URL / ``ytsearch:`` query / plain text into tracks."""
    return await asyncio.to_thread(_extract_tracks_sync, query)


def _pick_stream_url(info: dict[str, Any]) -> Optional[str]:
    """Choose the best audio stream URL from an info dict."""
    formats = info.get("formats") or []
    audio_formats = [
        f for f in formats
        if isinstance(f, dict) and f.get("url") and f.get("acodec") not in (None, "none")
    ]
    if audio_formats:
        # Prefer pure audio-only formats, then highest available bitrate.
        audio_only = [f for f in audio_formats if f.get("vcodec") in (None, "none")]
        pool = audio_only or audio_formats
        best = max(pool, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        return best["url"]
    # Format selection already happened inside yt-dlp.
    return info.get("url")


def _extract_stream_sync(webpage_url: str) -> tuple[str, Optional[int]]:
    opts = dict(YTDL_OPTIONS)
    opts["noplaylist"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc

    if not info:
        raise ExtractionError(f"Could not resolve stream: {webpage_url}")
    if info.get("entries") is not None:
        info = next((e for e in info["entries"] if e), None)
        if not info:
            raise ExtractionError(f"Could not resolve stream: {webpage_url}")

    stream_url = _pick_stream_url(info)
    if not stream_url:
        raise ExtractionError(f"No audio stream found for: {webpage_url}")
    return stream_url, info.get("duration")


async def extract_stream_url(webpage_url: str) -> tuple[str, Optional[int]]:
    """Fetch a direct audio stream URL (and duration) for playback.

    Streams are resolved just-in-time because signed YouTube URLs expire.
    """
    return await asyncio.to_thread(_extract_stream_sync, webpage_url)


def create_audio_source(stream_url: str) -> discord.FFmpegPCMAudio:
    """Wrap a direct stream URL in a reconnect-hardened FFmpeg source."""
    return discord.FFmpegPCMAudio(
        stream_url,
        before_options=FFMPEG_BEFORE_OPTIONS,
        options=FFMPEG_OPTIONS,
    )
