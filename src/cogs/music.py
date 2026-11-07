"""Voice connection & playback commands.

Per-guild queue with history, shuffle, and Off/Single/Queue looping.
Accepts YouTube links, ``ytsearch:``/plain queries, and Spotify
track/album/playlist links (resolved via :mod:`utils.spotify_helper`).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.audio_source import (
    ExtractionError,
    Track,
    create_audio_source,
    extract_stream_url,
    extract_tracks,
)
from utils.lyrics import LyricsError, LyricsNotFound, fetch_lyrics
from utils.spotify_helper import SpotifyError, SpotifyHelper, is_spotify_url

log = logging.getLogger(__name__)

QUEUE_PAGE_SIZE = 15
MAX_HISTORY = 100
SEARCH_CONCURRENCY = 5

# Discord caps an embed description at 4096 chars and a message at 2000;
# long lyrics spill over into plain follow-up messages.
LYRICS_EMBED_LIMIT = 3900
LYRICS_MESSAGE_LIMIT = 1900
LYRICS_MAX_MESSAGES = 8       # don't drown the channel on huge lyrics


def _chunk_text(text: str, limit: int) -> List[str]:
    """Split lyrics into <=``limit`` pieces, preferring line breaks."""
    chunks: List[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:              # pathological single line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class LoopMode(Enum):
    OFF = "off"
    SINGLE = "single"
    QUEUE = "queue"

    @property
    def label(self) -> str:
        return {"off": "Off", "single": "Single Song", "queue": "Queue"}[self.value]


@dataclass
class GuildMusicState:
    """Everything the player needs for one guild."""

    guild_id: int
    queue: List[Track] = field(default_factory=list)
    history: List[Track] = field(default_factory=list)
    cycle: List[Track] = field(default_factory=list)   # play order for queue-looping
    current: Optional[Track] = None
    loop: LoopMode = LoopMode.OFF
    announce: Optional[discord.abc.Messageable] = None

    # Cross-thread signalling for the player loop.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_reason: Optional[str] = None       # set to "skip" before vc.stop()
    pending: Optional[Track] = None         # set by /previous
    task: Optional[asyncio.Task] = None


class Music(commands.Cog):
    """Voice connection & playback commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.states: Dict[int, GuildMusicState] = {}
        self.spotify = SpotifyHelper()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def cog_load(self) -> None:
        self._loop = asyncio.get_running_loop()
        log.info(
            "Music ready: Spotify %s",
            "configured" if self.spotify.available else "disabled (no credentials)",
        )

    def cog_unload(self) -> None:
        for state in self.states.values():
            if state.task and not state.task.done():
                state.task.cancel()
        for guild in self.bot.guilds:
            vc = guild.voice_client
            if isinstance(vc, discord.VoiceClient) and vc.is_connected():
                try:
                    asyncio.ensure_future(vc.disconnect(force=True))
                except RuntimeError:
                    pass

    # ------------------------------------------------------------------
    # State & player plumbing
    # ------------------------------------------------------------------
    def _state(self, guild_id: int) -> GuildMusicState:
        state = self.states.get(guild_id)
        if state is None:
            state = GuildMusicState(guild_id=guild_id)
            self.states[guild_id] = state
        return state

    def _voice(self, state: GuildMusicState) -> Optional[discord.VoiceClient]:
        guild = self.bot.get_guild(state.guild_id)
        vc = guild.voice_client if guild else None
        return vc if isinstance(vc, discord.VoiceClient) and vc.is_connected() else None

    def _ensure_player(self, state: GuildMusicState) -> None:
        if state.task and not state.task.done():
            return
        state.task = self.bot.loop.create_task(self._player_loop(state))

    def _on_playback_end(self, state: GuildMusicState, error: Optional[Exception]) -> None:
        """FFmpeg 'after' callback — runs in a worker thread."""
        if error:
            log.warning(
                "Playback ended abnormally in guild %s: %s", state.guild_id, error
            )
        if self._loop is not None:
            self._loop.call_soon_threadsafe(state.done_event.set)

    async def _reset_state(self, state: GuildMusicState, *, clear_queue: bool) -> None:
        task, state.task = state.task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state.current = None
        state.stop_reason = None
        state.pending = None
        state.cycle.clear()
        state.wake.clear()
        state.done_event.clear()
        if clear_queue:
            state.queue.clear()
            state.history.clear()

    async def _announce(self, state: GuildMusicState, content: str) -> None:
        if state.announce is None:
            return
        if len(content) > 2000:
            content = content[:1997] + "..."
        try:
            await state.announce.send(content)
        except discord.HTTPException:
            log.debug("Could not send announcement in guild %s", state.guild_id)

    # ------------------------------------------------------------------
    # Player loop
    # ------------------------------------------------------------------
    def _push_history(self, state: GuildMusicState, track: Track) -> None:
        state.history.append(track)
        if len(state.history) > MAX_HISTORY:
            del state.history[: len(state.history) - MAX_HISTORY]

    def _select_next(self, state: GuildMusicState) -> Optional[Track]:
        """Advance bookkeeping and pick the next track (None = go idle)."""
        if state.pending is not None:          # /previous already fixed up history
            track, state.pending = state.pending, None
            return track

        previous = state.current
        force_advance = state.stop_reason == "skip"
        state.stop_reason = None

        if previous is not None:
            if state.loop is LoopMode.SINGLE and not force_advance:
                return previous                # natural end: replay it
            if state.loop is LoopMode.QUEUE:
                state.cycle.append(previous)
            self._push_history(state, previous)

        if state.queue:
            return state.queue.pop(0)
        if state.loop is LoopMode.QUEUE and state.cycle:
            state.queue.extend(state.cycle)    # wrap the whole queue around
            state.cycle.clear()
            return state.queue.pop(0)

        state.current = None                   # idle: don't re-bookkeep later
        return None

    async def _player_loop(self, state: GuildMusicState) -> None:
        try:
            while True:
                if self._voice(state) is None:
                    # Disconnected: hold the queue until we're re-joined.
                    state.current = None
                    await state.wake.wait()
                    state.wake.clear()
                    continue

                track = self._select_next(state)
                if track is None:
                    await state.wake.wait()
                    state.wake.clear()
                    continue

                await self._play(state, track)
        except asyncio.CancelledError:
            raise
        finally:
            vc = self._voice(state)
            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()

    async def _play(self, state: GuildMusicState, track: Track) -> None:
        vc = self._voice(state)
        if vc is None:
            state.current = None
            return

        try:
            stream = await extract_stream_url(track.webpage_url)
        except ExtractionError as exc:
            log.warning("Skipping unplayable track %r: %s", track.title, exc)
            state.current = None               # drop it, no history entry
            await self._announce(
                state, f"⚠️ Couldn't play **{track.title}** — skipping it."
            )
            return

        if stream.duration and not track.duration:
            track.duration = stream.duration   # fill in flat playlist metadata

        state.stop_reason = None
        state.done_event.clear()
        state.current = track
        try:
            source = create_audio_source(stream.url, stream.headers)
            vc.play(source, after=lambda error: self._on_playback_end(state, error))
        except Exception as exc:
            log.exception("Failed to start playback in guild %s", state.guild_id)
            state.current = None
            await self._announce(
                state, f"⚠️ Couldn't start **{track.title}** ({exc}) — skipping it."
            )
            return

        requester = f" — requested by {track.requester_name}" if track.requester_name else ""
        await self._announce(
            state, f"▶️ **{track.title}** ({track.duration_label}){requester}"
        )
        await state.done_event.wait()

    # ------------------------------------------------------------------
    # Spotify -> YouTube search
    # ------------------------------------------------------------------
    async def _spotify_to_tracks(self, items: List[str]) -> List[Track]:
        semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)

        async def search(item: str) -> Optional[Track]:
            async with semaphore:
                try:
                    found = await extract_tracks(f"ytsearch1:{item}")
                except ExtractionError:
                    log.warning("No YouTube match for Spotify item %r", item)
                    return None
                return found[0] if found else None

        results = await asyncio.gather(*(search(item) for item in items))
        return [track for track in results if track is not None]

    # ------------------------------------------------------------------
    # Shared checks
    # ------------------------------------------------------------------
    async def _require_bot_vc(
        self, interaction: discord.Interaction
    ) -> Optional[discord.VoiceClient]:
        """Bot + user must share a connected voice channel."""
        vc = interaction.guild.voice_client
        if not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
            await interaction.response.send_message(
                "I'm not connected to a voice channel.", ephemeral=True
            )
            return None
        voice = interaction.user.voice
        if voice is None or voice.channel is None:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return None
        if voice.channel.id != vc.channel.id:
            await interaction.response.send_message(
                f"You need to be in **{vc.channel.name}** to do that.",
                ephemeral=True,
            )
            return None
        return vc

    async def _connect(
        self, interaction: discord.Interaction
    ) -> Optional[discord.VoiceChannel]:
        """Validate the user's voice channel and (re)connect the bot."""
        voice = interaction.user.voice
        if voice is None or voice.channel is None:
            await interaction.followup.send(
                "Join a voice channel first.", ephemeral=True
            )
            return None
        channel = voice.channel
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.followup.send(
                f"I don't have permission to play in **{channel.name}**.",
                ephemeral=True,
            )
            return None

        vc = interaction.guild.voice_client
        if isinstance(vc, discord.VoiceClient) and vc.is_connected():
            if vc.channel.id != channel.id:
                if vc.is_playing() or vc.is_paused():
                    await interaction.followup.send(
                        f"I'm already playing in **{vc.channel.name}** — "
                        "join me there or use /leave first.",
                        ephemeral=True,
                    )
                    return None
                await vc.move_to(channel)
        else:
            try:
                await channel.connect(self_deaf=True)
            except (discord.ClientException, OSError) as exc:
                await interaction.followup.send(
                    f"Couldn't connect to **{channel.name}**: {exc}", ephemeral=True
                )
                return None
        return channel

    # ------------------------------------------------------------------
    # Voice commands
    # ------------------------------------------------------------------
    @app_commands.guild_only()
    @app_commands.command(name="join", description="Make the bot join your voice channel.")
    @app_commands.describe(channel="Channel to join (defaults to your current one).")
    async def join(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        await interaction.response.defer()
        if channel is None:
            voice = interaction.user.voice
            if voice is None or voice.channel is None:
                await interaction.followup.send(
                    "Join a voice channel first, or pass one explicitly.",
                    ephemeral=True,
                )
                return
            channel = voice.channel

        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.followup.send(
                f"I don't have permission to play in **{channel.name}**.",
                ephemeral=True,
            )
            return

        vc = interaction.guild.voice_client
        if isinstance(vc, discord.VoiceClient) and vc.is_connected():
            if vc.channel.id == channel.id:
                await interaction.followup.send(
                    f"I'm already in **{channel.name}**."
                )
                return
            await vc.move_to(channel)
        else:
            try:
                await channel.connect(self_deaf=True)
            except (discord.ClientException, OSError) as exc:
                await interaction.followup.send(
                    f"Couldn't connect: {exc}", ephemeral=True
                )
                return

        state = self._state(interaction.guild_id)
        if state.announce is None:
            state.announce = interaction.channel
        state.wake.set()
        self._ensure_player(state)
        await interaction.followup.send(f"🔊 Joined **{channel.name}**.")

    @app_commands.guild_only()
    @app_commands.command(name="leave", description="Disconnect the bot from voice.")
    async def leave(self, interaction: discord.Interaction) -> None:
        vc = interaction.guild.voice_client
        if not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
            await interaction.response.send_message(
                "I'm not connected to a voice channel.", ephemeral=True
            )
            return
        await interaction.response.defer()
        state = self._state(interaction.guild_id)
        await self._reset_state(state, clear_queue=True)
        await vc.disconnect(force=True)
        await interaction.followup.send("👋 Left the voice channel and cleared the queue.")

    # ------------------------------------------------------------------
    # Playback commands
    # ------------------------------------------------------------------
    @app_commands.guild_only()
    @app_commands.command(
        name="play",
        description="Play a YouTube link, Spotify link, or search query.",
    )
    @app_commands.describe(query="YouTube/Spotify URL, ytsearch: query, or plain text")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        channel = await self._connect(interaction)
        if channel is None:
            return

        state = self._state(interaction.guild_id)
        state.announce = interaction.channel
        source_label = "YouTube"

        try:
            if is_spotify_url(query):
                result = await self.spotify.resolve(query)
                tracks = await self._spotify_to_tracks(result.items)
                source_label = f"Spotify {result.kind} “{result.title}”"
                if not tracks:
                    raise ExtractionError(
                        "No YouTube matches found for that Spotify content."
                    )
            else:
                tracks = await extract_tracks(query)
        except (SpotifyError, ExtractionError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        requester = interaction.user
        display = getattr(requester, "display_name", str(requester))
        for track in tracks:
            track.requester_id = requester.id
            track.requester_name = display

        state.queue.extend(tracks)
        state.wake.set()
        self._ensure_player(state)

        embed = discord.Embed(color=discord.Color.green(), title="Added to queue")
        if len(tracks) == 1:
            track = tracks[0]
            embed.description = f"[{track.title}]({track.webpage_url})"
            embed.add_field(name="Duration", value=track.duration_label, inline=True)
            if state.current is None:
                embed.title = "Starting playback"
            else:
                embed.add_field(
                    name="Position", value=f"#{len(state.queue)}", inline=True
                )
        else:
            embed.description = f"**{len(tracks)}** tracks from {source_label}"
            embed.add_field(name="Queue length", value=str(len(state.queue)), inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.guild_only()
    @app_commands.command(name="pause", description="Pause playback.")
    async def pause(self, interaction: discord.Interaction) -> None:
        vc = await self._require_bot_vc(interaction)
        if vc is None:
            return
        if vc.is_paused():
            await interaction.response.send_message(
                "Playback is already paused.", ephemeral=True
            )
            return
        if not vc.is_playing():
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return
        vc.pause()
        await interaction.response.send_message("⏸ Paused.")

    @app_commands.guild_only()
    @app_commands.command(name="resume", description="Resume playback.")
    async def resume(self, interaction: discord.Interaction) -> None:
        vc = await self._require_bot_vc(interaction)
        if vc is None:
            return
        if not vc.is_paused():
            await interaction.response.send_message(
                "Playback isn't paused.", ephemeral=True
            )
            return
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")

    async def _do_skip(self, interaction: discord.Interaction) -> None:
        vc = await self._require_bot_vc(interaction)
        if vc is None:
            return
        if not vc.is_playing() and not vc.is_paused():
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return
        state = self._state(interaction.guild_id)
        skipped = state.current
        state.stop_reason = "skip"
        vc.stop()                               # wakes the player loop
        title = skipped.title if skipped else "track"
        await interaction.response.send_message(f"⏭ Skipped **{title}**.")

    @app_commands.guild_only()
    @app_commands.command(name="skip", description="Skip the current track.")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._do_skip(interaction)

    @app_commands.guild_only()
    @app_commands.command(name="next", description="Skip to the next track (alias of /skip).")
    async def next_track(self, interaction: discord.Interaction) -> None:
        await self._do_skip(interaction)

    @app_commands.guild_only()
    @app_commands.command(name="previous", description="Replay the previously finished track.")
    async def previous(self, interaction: discord.Interaction) -> None:
        vc = await self._require_bot_vc(interaction)
        if vc is None:
            return
        state = self._state(interaction.guild_id)
        if not state.history:
            await interaction.response.send_message(
                "There's no previous track to go back to.", ephemeral=True
            )
            return
        if state.current is None and not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return

        previous_track = state.history.pop()
        state.pending = previous_track          # current track is intentionally dropped
        if vc.is_playing() or vc.is_paused():
            vc.stop()                           # done_event -> loop picks up pending
        else:
            state.done_event.set()
        await interaction.response.send_message(f"⏮ Playing **{previous_track.title}**.")

    # ------------------------------------------------------------------
    # Lyrics
    # ------------------------------------------------------------------
    @app_commands.guild_only()
    @app_commands.command(
        name="lyrics",
        description="Show lyrics for the song that's playing.",
    )
    @app_commands.describe(title="Song to look up instead of the current track.")
    async def lyrics(
        self,
        interaction: discord.Interaction,
        title: Optional[str] = None,
    ) -> None:
        await interaction.response.defer()
        state = self._state(interaction.guild_id)
        track = state.current

        if title is None:
            if track is None:
                await interaction.followup.send(
                    "Nothing is playing right now — give me a song with "
                    "`/lyrics <title>` instead.",
                    ephemeral=True,
                )
                return
            lookup, duration = track.title, track.duration
        else:
            lookup, duration = title.strip(), None
            if not lookup:
                await interaction.followup.send(
                    "Give me a title to look up, e.g. `/lyrics bad guy`.",
                    ephemeral=True,
                )
                return

        try:
            found = await fetch_lyrics(lookup, duration=duration)
        except LyricsNotFound:
            await interaction.followup.send(
                f"🙈 I couldn't find lyrics for **{lookup}** — try "
                "`/lyrics` with just the song title (and artist).",
                ephemeral=True,
            )
            return
        except LyricsError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        embed = discord.Embed(color=discord.Color.gold())
        embed.title = f"🎤 {found.track_name or lookup} — {found.artist_name}"[:256]
        embed.url = found.source_url or (track.webpage_url if track else None)
        credit = found.artist_name or "Unknown artist"
        if found.album_name and found.album_name.strip() != credit.strip():
            credit += f" • {found.album_name}"
        embed.set_footer(text=f"{credit[:200]} • lyrics via LRCLib")

        if found.instrumental:
            embed.description = "🎼 This one is instrumental — no lyrics to show."
            await interaction.followup.send(embed=embed)
            return
        if not found.text:
            await interaction.followup.send(
                f"🙈 Found **{found.track_name}** but it has no lyrics text "
                "attached.",
                ephemeral=True,
            )
            return

        chunks = _chunk_text(found.text, LYRICS_EMBED_LIMIT)
        embed.description = chunks[0]
        await interaction.followup.send(embed=embed)

        remaining = "\n".join(chunks[1:])
        if remaining:
            pieces = _chunk_text(remaining, LYRICS_MESSAGE_LIMIT)
            for piece in pieces[: LYRICS_MAX_MESSAGES - 1]:
                await interaction.followup.send(piece)
            if len(pieces) > LYRICS_MAX_MESSAGES - 1:
                await interaction.followup.send("…(lyrics truncated)")

    # ------------------------------------------------------------------
    # Queue management commands
    # ------------------------------------------------------------------
    @app_commands.guild_only()
    @app_commands.command(name="queue", description="Show the current queue.")
    async def queue(self, interaction: discord.Interaction) -> None:
        state = self._state(interaction.guild_id)
        embed = discord.Embed(title="Queue", color=discord.Color.blurple())

        if state.current is None and not state.queue:
            embed.description = "Nothing queued. Use /play to add something."
            await interaction.response.send_message(embed=embed)
            return

        if state.current is not None:
            embed.add_field(
                name="Now playing",
                value=f"[{state.current.title}]({state.current.webpage_url}) "
                f"({state.current.duration_label})",
                inline=False,
            )

        if state.queue:
            lines: List[str] = []
            total = 0
            shown = 0
            for index, track in enumerate(state.queue, start=1):
                if shown >= QUEUE_PAGE_SIZE:
                    break
                title = track.title if len(track.title) <= 60 else track.title[:59] + "…"
                line = (
                    f"`{index}.` [{title}]({track.webpage_url}) "
                    f"({track.duration_label})"
                )
                # Discord field values are capped at 1024 characters.
                if total + len(line) + 1 > 950:
                    break
                lines.append(line)
                total += len(line) + 1
                shown += 1
            hidden = len(state.queue) - shown
            if hidden > 0:
                lines.append(f"… and **{hidden}** more")
            embed.add_field(
                name=f"Up next ({len(state.queue)})",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Up next",
                value=f"Nothing else queued — looping mode: {state.loop.label}",
                inline=False,
            )

        if state.history:
            last = state.history[-1]
            embed.set_footer(
                text=f"Loop: {state.loop.label} • Previous: {last.title}"
            )
        else:
            embed.set_footer(text=f"Loop: {state.loop.label}")
        await interaction.response.send_message(embed=embed)

    @app_commands.guild_only()
    @app_commands.command(
        name="remove",
        description="Remove a song from the queue by its number.",
    )
    @app_commands.describe(
        index="Position as shown in /queue (1 = next up)."
    )
    async def remove(self, interaction: discord.Interaction, index: int) -> None:
        state = self._state(interaction.guild_id)
        if not state.queue:
            await interaction.response.send_message(
                "The queue is empty — there's nothing to remove.", ephemeral=True
            )
            return
        if index < 1 or index > len(state.queue):
            await interaction.response.send_message(
                f"Pick a position between **1** and **{len(state.queue)}** "
                "— see /queue.",
                ephemeral=True,
            )
            return

        removed = state.queue.pop(index - 1)
        embed = discord.Embed(color=discord.Color.orange(), title="Removed from queue")
        embed.description = (
            f"[{removed.title}]({removed.webpage_url}) ({removed.duration_label})"
        )
        embed.add_field(name="Position", value=f"#{index}", inline=True)
        embed.add_field(name="Remaining", value=str(len(state.queue)), inline=True)
        if removed.requester_name:
            embed.add_field(
                name="Requested by", value=removed.requester_name, inline=True
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.guild_only()
    @app_commands.command(name="shuffle", description="Shuffle the queue.")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        state = self._state(interaction.guild_id)
        if len(state.queue) < 2:
            await interaction.response.send_message(
                "Not enough tracks in the queue to shuffle.", ephemeral=True
            )
            return
        random.shuffle(state.queue)
        await interaction.response.send_message(
            f"🔀 Shuffled **{len(state.queue)}** tracks."
        )

    @app_commands.guild_only()
    @app_commands.command(
        name="loop",
        description="Set the loop mode (omit to cycle Off → Single → Queue).",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Single Song", value="single"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    @app_commands.describe(mode="Loop mode; omit to cycle through the modes.")
    async def loop(
        self,
        interaction: discord.Interaction,
        mode: Optional[str] = None,
    ) -> None:
        state = self._state(interaction.guild_id)
        if mode is None:
            order = [LoopMode.OFF, LoopMode.SINGLE, LoopMode.QUEUE]
            mode = order[(order.index(state.loop) + 1) % len(order)].value
        state.loop = LoopMode(mode)
        state.cycle.clear()                     # start the loop cycle fresh
        await interaction.response.send_message(f"🔁 Loop mode: **{state.loop.label}**.")

    # ------------------------------------------------------------------
    # Voice-state housekeeping
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.bot.user is None or member.id != self.bot.user.id:
            return
        # Bot was disconnected (or moved) by someone else.
        if after.channel is None:
            state = self.states.get(member.guild.id)
            if state is not None:
                # Keep the queue so a later /join can resume playback.
                await self._reset_state(state, clear_queue=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
