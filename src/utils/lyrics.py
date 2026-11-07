"""Lyrics lookup for the currently playing track.

Uses **LRCLib** (https://lrclib.net/docs) — a free, open database of plain
and time-synced lyrics that needs no API key. All we have to go on is the
song title (usually a YouTube-style ``Artist - Title (Official Video)``), so
this module:

1. cleans the noise out of the title and derives artist/title candidates
   (both split orders are tried — YouTube titles aren't consistent),
2. queries LRCLib's ``/api/search`` with each candidate, in parallel,
3. scores every result (title similarity, artist similarity, duration
   proximity) and returns the best match above a confidence threshold.

Results are cached in-memory so a spammy ``/lyrics`` doesn't hammer the
free service.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

log = logging.getLogger(__name__)

LRCLIB_API = "https://lrclib.net/api"
# LRCLib asks clients to send an identifying User-Agent.
USER_AGENT = (
    "MonikaDiscordBot/1.0 (https://github.com/AlV2201/Monika-Discord-Bot)"
)
HTTP_TIMEOUT = 10.0
MAX_CACHED = 64
# Below this the "match" is probably a different song — better to say no.
MIN_SCORE = 0.55
# Duration closeness window, in seconds, when both sides know it.
DURATION_WINDOW = 30.0


class LyricsError(RuntimeError):
    """LRCLib couldn't be reached or answered unexpectedly."""


class LyricsNotFound(LyricsError):
    """No plausible match for the requested song."""


@dataclass
class Lyrics:
    """A matched lyrics record."""

    track_name: str
    artist_name: str
    album_name: Optional[str] = None
    duration: Optional[int] = None
    text: str = ""                       # plain lyrics (timestamps stripped)
    instrumental: bool = False
    source_id: Optional[int] = None

    @property
    def source_url(self) -> Optional[str]:
        if self.source_id is None:
            return None
        return f"https://lrclib.net/track/{self.source_id}"


# ---------------------------------------------------------------------------
# Title cleanup / query derivation
# ---------------------------------------------------------------------------
# Groups like "(Official Video)" or "[Official Audio]" carry no information
# for a lyrics lookup; groups that aren't *entirely* made of these words are
# kept (a song may genuinely be called "(Live)" — same lyrics anyway).
_NOISE_WORDS = {
    "official", "oficial", "video", "vídeo", "audio", "music", "musical",
    "lyrics", "lyric", "letra", "visualizer", "hd", "uhd", "4k", "1080p",
    "720p", "fps", "vevo", "stereo", "explicit", "remastered", "remaster",
    "version", "subtitled", "subtitulado", "subtitle", "karaoke", "cover",
    "reaction", "tutorial", "live", "vivo", "clip", "full", "album",
    "single", "snippet", "teaser", "behind", "scenes", "commentary",
    "interview", "performance", "acoustic", "edit", "instrumental",
    "premiere", "promo", "session", "sessions",
}
_NOISE_GROUP = re.compile(r"[\(\[]([^)\]]{1,60})[\)\]]")
_DASH_SPLIT = re.compile(r"\s+[-–—|]\s+")
_TRAILING_MARK = re.compile(r"\s*//\s*[A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,20}\s*//\s*$")
_FEAT_TAIL = re.compile(r"\s+(?:ft|feat|featuring|con)\.?\s+.+$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^0-9a-záéíóúñü]+", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _NON_WORD.sub(" ", folded).strip().lower()


def _is_noise_only(fragment: str) -> bool:
    """True when a title fragment is just filler (``//LETRA//``, ``[4K]``)."""
    fragment = _TRAILING_MARK.sub("", fragment).strip(" -–—|/")
    words = _normalize(fragment).split()
    return bool(words) and all(word in _NOISE_WORDS for word in words)


def _clean_title(raw: str) -> str:
    """Drop bracketed noise and trailing ``//LETRA//``-style markers."""
    text = _TRAILING_MARK.sub("", raw).strip()

    def drop(match: re.Match[str]) -> str:
        return "" if _is_noise_only(match.group(0)) else match.group(0)

    # Repeat: stripping one group can expose another ("… (Video) [4K]").
    for _ in range(3):
        cleaned = _NOISE_GROUP.sub(drop, text)
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"\s{2,}", " ", text).strip(" -–—|")


@dataclass(frozen=True)
class _Query:
    """One candidate lookup: structured (artist+track) or free-text."""

    artist: Optional[str]
    track: str
    freetext: bool = False


def _derive_queries(raw: str) -> List[_Query]:
    """Turn a raw song title into lookup candidates, best guess first."""
    text = _clean_title(raw)
    parts = [part.strip() for part in _DASH_SPLIT.split(text) if part.strip()]
    parts = [part for part in parts if not _is_noise_only(part)]

    queries: List[_Query] = []
    if len(parts) >= 2:
        # Drop a trailing "ft. …" clause — LRCLib usually omits it — then
        # try both split orders: YouTube titles are both "Artist - Title"
        # and "Title - Artist".
        first = _FEAT_TAIL.sub("", parts[0])
        rest = _FEAT_TAIL.sub("", " ".join(parts[1:]))
        queries.append(_Query(artist=first, track=rest))
        queries.append(_Query(artist=rest, track=first))
    elif parts:
        queries.append(_Query(artist=None, track=_FEAT_TAIL.sub("", parts[0])))
    if text:
        queries.append(_Query(artist=None, track=text, freetext=True))
    return queries


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _score(
    record: Dict[str, Any],
    query: _Query,
    duration: Optional[int],
) -> float:
    """Confidence that *record* is the song behind *query* (0..1)."""
    rec_track = str(record.get("trackName") or record.get("name") or "")
    rec_artist = str(record.get("artistName") or "")
    rec_duration = record.get("duration")

    parts: List[float] = []
    weights: List[float] = []

    track_ratio = _ratio(query.track, rec_track)
    if query.freetext:
        # Free-text query often includes the artist: compare against both
        # the bare track name and "Artist - Track".
        track_ratio = max(
            track_ratio,
            _ratio(query.track, f"{rec_artist} {rec_track}"),
        )
    parts.append(track_ratio)
    weights.append(0.55)

    if query.artist:
        parts.append(_ratio(query.artist, rec_artist))
        weights.append(0.30)

    if duration and rec_duration:
        distance = abs(float(duration) - float(rec_duration))
        parts.append(max(0.0, 1.0 - distance / DURATION_WINDOW))
        weights.append(0.15)

    return sum(p * w for p, w in zip(parts, weights)) / sum(weights)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Optional[Lyrics]] = {}


def _remember(key: str, value: Optional[Lyrics]) -> None:
    if len(_CACHE) >= MAX_CACHED:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = value


def _to_lyrics(record: Dict[str, Any]) -> Lyrics:
    plain = record.get("plainLyrics") or ""
    if not plain:
        plain = _strip_timestamps(record.get("syncedLyrics") or "")
    return Lyrics(
        track_name=str(record.get("trackName") or record.get("name") or ""),
        artist_name=str(record.get("artistName") or ""),
        album_name=record.get("albumName"),
        duration=int(record["duration"]) if record.get("duration") else None,
        text=plain.strip(),
        instrumental=bool(record.get("instrumental")),
        source_id=record.get("id"),
    )


def _strip_timestamps(synced: str) -> str:
    """``[00:12.34] line`` -> ``line`` (LRCLib's time-synced format)."""
    return re.sub(r"^\s*\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s?", "", synced, flags=re.M)


async def fetch_lyrics(title: str, *, duration: Optional[int] = None) -> Lyrics:
    """Find lyrics for *title* (optionally using the known duration).

    Raises:
        LyricsNotFound: no plausible match.
        LyricsError: LRCLib unreachable or misbehaving.
    """
    key = _normalize(title)
    if key in _CACHE:
        cached = _CACHE[key]
        if cached is None:
            raise LyricsNotFound(title)
        return cached

    queries = _derive_queries(title)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            results = await asyncio.gather(
                *(_search(session, query) for query in queries),
                return_exceptions=True,
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise LyricsError(f"Couldn't reach LRCLib: {exc}") from exc

    best: Optional[Tuple[float, Lyrics]] = None
    seen: Dict[Any, float] = {}
    failures = 0
    for query, records in zip(queries, results):
        if isinstance(records, BaseException):
            failures += 1
            log.warning("LRCLib query %r failed: %r", query.track, records)
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            score = _score(record, query, duration)
            record_id = record.get("id") or (
                record.get("trackName"), record.get("artistName"),
            )
            if record_id in seen and seen[record_id] >= score:
                continue
            seen[record_id] = score
            lyrics = _to_lyrics(record)
            if not lyrics.text and not lyrics.instrumental:
                continue                      # matched a stub without lyrics
            if best is None or score > best[0]:
                best = (score, lyrics)

    if failures and failures == len(queries):
        raise LyricsError("Couldn't reach LRCLib (network problem?).")
    if best is None or best[0] < MIN_SCORE:
        _remember(key, None)
        raise LyricsNotFound(title)

    score, lyrics = best
    log.debug(
        "Lyrics for %r -> %r by %r (score %.2f)",
        title, lyrics.track_name, lyrics.artist_name, score,
    )
    _remember(key, lyrics)
    return lyrics


async def _search(
    session: aiohttp.ClientSession, query: _Query
) -> List[Dict[str, Any]]:
    params: Dict[str, str] = (
        {"q": query.track} if query.freetext else {"track_name": query.track}
    )
    if query.artist and not query.freetext:
        params["artist_name"] = query.artist
    async with session.get(f"{LRCLIB_API}/search", params=params) as response:
        if response.status == 404:
            return []
        if response.status != 200:
            raise LyricsError(f"LRCLib answered HTTP {response.status}")
        data = await response.json()
        return data if isinstance(data, list) else []
