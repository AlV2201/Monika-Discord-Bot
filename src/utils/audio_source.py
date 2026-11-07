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
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import discord
import yt_dlp

# yt-dlp keeps this table private, but it is the source of truth for which
# Innertube clients accept cookies — clients that don't are skipped outright
# when cookie options are set (see _client_accepts_cookies).
try:
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
except Exception:  # pragma: no cover — yt-dlp internals moved
    INNERTUBE_CLIENTS = {}

log = logging.getLogger(__name__)

# Stability options for streaming remote media.
# FFmpeg needs reconnect hardening for long HTTP streams, and a sane
# User-Agent: YouTube returns 403 Forbidden to requests whose UA is missing
# or generic. When yt-dlp hands us the headers it extracted with, we forward
# those (see _ffmpeg_before_options) and only fall back to this literal.
FFMPEG_RECONNECT_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)
FFMPEG_OPTIONS = "-vn"

# Stream URLs are minted per Innertube player client, and without a PO token
# provider several clients (tv/tv_samsung/web/mweb/…) hand out URLs that
# googlevideo answers with 403 at playback time — something FFmpeg can only
# discover after the fact. These clients hand out URLs that play without a
# PO token; each candidate is probed before it is handed to FFmpeg.
YOUTUBE_PLAYER_CLIENTS = ("visionos", "android", "default")

# Responses that definitively mean "this signed URL was denied".
DENIED_HTTP_STATUS = frozenset({401, 403, 404, 410, 451})
STREAM_PROBE_TIMEOUT = 10.0

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

    Players that can't carry cookies are exempt: yt-dlp *drops* a client
    outright when cookie options are set, so pinned clients without cookie
    support are extracted without them (see ``_client_accepts_cookies``).
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


@dataclass
class Stream:
    """A direct audio stream URL, verified usable just before playback."""

    url: str
    duration: Optional[int] = None
    headers: dict[str, str] = field(default_factory=dict)   # yt-dlp's http_headers


def _is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def _is_youtube_url(url: str) -> bool:
    """True for YouTube watch/video URLs (where player clients apply)."""
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and (
        host.endswith("youtube.com") or host.endswith("youtu.be")
    )


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


def _stream_headers(info: dict[str, Any]) -> dict[str, str]:
    """The HTTP headers yt-dlp would use to fetch this stream."""
    headers = info.get("http_headers")
    if isinstance(headers, dict) and headers:
        return dict(headers)
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict) and isinstance(fmt.get("http_headers"), dict):
            return dict(fmt["http_headers"])
    return {}


def _stream_is_usable(stream_url: str, headers: dict[str, str]) -> bool:
    """Range-probe a signed stream URL before handing it to FFmpeg.

    Returns ``False`` only when the server definitively denies the request
    (403 & friends). Anything ambiguous — timeouts, connection resets, 5xx —
    is left for FFmpeg to report rather than spending another extraction on.
    """
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-1024"
    request = Request(stream_url, headers=probe_headers)
    try:
        with urlopen(request, timeout=STREAM_PROBE_TIMEOUT) as response:
            response.read(1024)
            return 200 <= response.status < 300
    except HTTPError as exc:
        return exc.code not in DENIED_HTTP_STATUS   # urllib raises HTTPError for 4xx/5xx
    except (URLError, TimeoutError, OSError):
        return True        # can't tell — let FFmpeg have a go at it


def _client_accepts_cookies(player_client: str) -> bool:
    """Can yt-dlp attach cookies to this Innertube client?

    Cookie options make yt-dlp *skip* clients flagged as not supporting them
    (``Skipping client "visionos" since it does not support cookies``), which
    leaves the extraction with storyboard images only — the format selector
    then fails and the client never gets a chance to prove that it didn't
    need cookies at all. Pinned clients therefore run without cookie settings
    unless they can genuinely use them.
    """
    if player_client in ("default", "all"):
        return True                      # yt-dlp's own chains pick their clients
    config = INNERTUBE_CLIENTS.get(player_client)
    if config is None:
        return True                      # unknown/future client: don't guess
    return bool(config.get("SUPPORTS_COOKIES", True))


def _extract_stream_sync(
    webpage_url: str, player_client: Optional[str] = None
) -> Stream:
    """One extraction attempt, optionally pinned to a single player client."""
    opts = dict(YTDL_OPTIONS)
    opts["noplaylist"] = True
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
        if not _client_accepts_cookies(player_client):
            for cookie_key in ("cookiefile", "cookiesfrombrowser"):
                if opts.pop(cookie_key, None):
                    log.debug(
                        "Dropping %s for player client %r (client does not "
                        "support cookies)",
                        cookie_key, player_client,
                    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc

    scope = f" with player client {player_client!r}" if player_client else ""
    if not info:
        raise ExtractionError(
            f"Could not resolve stream{scope}: {webpage_url}"
        )
    if info.get("entries") is not None:
        info = next((e for e in info["entries"] if e), None)
        if not info:
            raise ExtractionError(
                f"Could not resolve stream{scope}: {webpage_url}"
            )

    stream_url = _pick_stream_url(info)
    if not stream_url:
        raise ExtractionError(f"No audio stream found{scope}: {webpage_url}")
    return Stream(
        url=stream_url,
        duration=info.get("duration"),
        headers=_stream_headers(info),
    )


def _resolve_stream_sync(webpage_url: str) -> Stream:
    """Resolve *and verify* a playable stream URL.

    Stream URLs are minted per Innertube player client, and clients that need
    a GVS PO token (which we have no provider for) hand out URLs that googlevideo
    answers with 403. FFmpeg can only discover that after it has been started,
    so each candidate is probed here and the next client is tried on denial.
    """
    clients: list[Optional[str]] = (
        list(YOUTUBE_PLAYER_CLIENTS) if _is_youtube_url(webpage_url) else [None]
    )
    last_error: Optional[Exception] = None
    denied = False

    for client in clients:
        try:
            stream = _extract_stream_sync(webpage_url, client)
        except ExtractionError as exc:
            last_error = exc
            log.debug(
                "Player client %r yielded no stream for %r: %s",
                client, webpage_url, exc,
            )
            continue

        if _stream_is_usable(stream.url, stream.headers):
            if client:
                log.debug(
                    "Using player client %r for %r", client, webpage_url
                )
            return stream

        denied = True
        log.warning(
            "Stream URL from player client %r was denied (HTTP 403) for %r "
            "— trying the next client",
            client, webpage_url,
        )

    if last_error is not None:
        detail = str(last_error)
        if denied:
            detail = (
                f"{detail}; streams from the other client(s) were denied (HTTP 403)"
            )
        raise ExtractionError(detail)
    if denied:
        raise ExtractionError(
            f"YouTube denied every candidate stream for: {webpage_url}"
        )
    raise ExtractionError(f"No playable stream for: {webpage_url}")


async def extract_stream_url(webpage_url: str) -> Stream:
    """Fetch a direct, verified-usable audio stream for playback.

    Streams are resolved just-in-time because signed YouTube URLs expire.
    """
    return await asyncio.to_thread(_resolve_stream_sync, webpage_url)


def _ffmpeg_before_options(headers: Optional[dict[str, str]] = None) -> str:
    """Build FFmpeg's pre-input flags: reconnect hardening plus HTTP headers.

    discord.py runs this string through :func:`shlex.split`, so every value is
    quoted here. The User-Agent is passed with ``-user_agent`` (FFmpeg's own
    option) so it can't collide with a ``User-Agent:`` line in ``-headers``.
    """
    options = [FFMPEG_RECONNECT_OPTIONS]

    remaining = {key: str(value) for key, value in (headers or {}).items() if value}
    user_agent = None
    for key in list(remaining):
        if key.lower() == "user-agent":
            user_agent = remaining.pop(key)
            break
    options.append(f"-user_agent {shlex.quote(user_agent or FALLBACK_USER_AGENT)}")

    if remaining:
        blob = "".join(f"{key}: {value}\r\n" for key, value in remaining.items())
        options.append(f"-headers {shlex.quote(blob)}")
    return " ".join(options)


def create_audio_source(
    stream_url: str, headers: Optional[dict[str, str]] = None
) -> discord.FFmpegPCMAudio:
    """Wrap a direct stream URL in a reconnect-hardened FFmpeg source."""
    return discord.FFmpegPCMAudio(
        stream_url,
        before_options=_ffmpeg_before_options(headers),
        options=FFMPEG_OPTIONS,
    )
