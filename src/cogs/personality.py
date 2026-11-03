"""Text listener & Ollama LLM interaction.

Intercepts messages that mention the bot or land in configured chat
channels, keeps per-(channel, user) conversation memory, and asks the
local Ollama model for a reply without blocking Discord's event loop.

The system prompt defaults to the built-in Monika persona
(:data:`persona.MONIKA_PERSONA`); ``BOT_CHARACTER_PROMPT`` overrides it.

Mention markup is converted to plain names before it reaches the model,
replies are scrubbed of any leftover ``@handle`` echoes, and
mention-triggered replies ping their author.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from typing import Deque, Dict, List, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import ollama

from persona import MONIKA_PERSONA

log = logging.getLogger(__name__)

DEFAULT_PERSONA = MONIKA_PERSONA
CHAR_LIMIT = 2000
MENTION_RE = re.compile(r"<@!?(\d+)>")


def _parse_channels() -> set[int]:
    """Parse PERSONALITY_CHANNELS (comma-separated channel IDs) from the env."""
    raw = os.getenv("PERSONALITY_CHANNELS", "")
    channels: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            channels.add(int(part))
        else:
            log.warning("Ignoring invalid PERSONALITY_CHANNELS entry: %r", part)
    return channels


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        log.warning("Invalid %s=%r, using %d", name, raw, default)
        return default


def _response_content(response: object) -> str:
    """Handle both legacy dict and modern object ollama responses."""
    if isinstance(response, dict):
        message = response.get("message", {})
    else:
        message = getattr(response, "message", None)
    if isinstance(message, dict):
        return (message.get("content") or "").strip()
    return (getattr(message, "content", "") or "").strip()


def split_message(text: str, limit: int = CHAR_LIMIT) -> List[str]:
    """Split a reply into Discord-safe chunks, preferring line breaks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


class Personality(commands.Cog):
    """In-character chat backed by a local Ollama model."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = os.getenv("OLLAMA_MODEL", "llama3.2")
        self.system_prompt = (
            os.getenv("BOT_CHARACTER_PROMPT", "").strip() or DEFAULT_PERSONA
        )
        self.memory_size = _parse_int_env("MEMORY_SIZE", 20)
        self.channel_ids = _parse_channels()
        self.memory: Dict[Tuple[int, int], Deque[dict]] = defaultdict(
            lambda: deque(maxlen=self.memory_size)
        )
        # Lazy client: constructing it does not open a connection.
        self._client = ollama.Client(host=self.host)

    async def cog_load(self) -> None:
        log.info(
            "Personality ready: model=%s host=%s memory=%d messages, "
            "listen channels=%s",
            self.model,
            self.host,
            self.memory_size,
            sorted(self.channel_ids) or "none (mention-only)",
        )

    # ------------------------------------------------------------------
    # Trigger logic
    # ------------------------------------------------------------------
    def _should_respond(self, message: discord.Message) -> bool:
        if message.author.bot or not message.content:
            return False
        if self.bot.user and self.bot.user in message.mentions:
            return True
        return message.channel.id in self.channel_ids

    def _clean_content(self, message: discord.Message) -> str:
        """Replace raw mention markup with plain, readable names.

        The bot's own mention becomes its name and other mentions become
        the mentioned member's server nickname, so the LLM sees natural
        prose instead of ``@handles`` — and therefore never echoes them.
        """
        names: Dict[int, str] = {
            user.id: user.display_name for user in message.mentions
        }
        if self.bot.user:
            names[self.bot.user.id] = self.bot.user.name

        def _replace(match: re.Match[str]) -> str:
            return names.get(int(match.group(1)), "@someone")

        return MENTION_RE.sub(_replace, message.content).strip()

    def _name_candidates(self, message: discord.Message) -> List[str]:
        """Names a reply may echo back, most specific first."""
        people = [message.author]
        if self.bot.user:
            people.append(self.bot.user)
        candidates: List[str] = []
        for person in people:
            for value in (
                person.display_name,
                person.name,
                getattr(person, "global_name", None),
            ):
                if value and value not in candidates:
                    candidates.append(value)
        return candidates

    @staticmethod
    def _sanitize(text: str, names: Sequence[str]) -> str:
        """Strip leftover ``@handle`` echoes and mention markup from a reply.

        Word-boundary matching keeps legitimate text like ``foo@bar.com``
        intact. The sanitized text is what gets stored in memory, so the
        model stops learning to write handles in the first place.
        """
        for name in sorted({n for n in names if n}, key=len, reverse=True):
            text = re.sub(
                rf"@{re.escape(name)}(?![\w])",
                name,
                text,
                flags=re.IGNORECASE,
            )
        text = re.sub(r"@someone\b", "someone", text, flags=re.IGNORECASE)
        text = re.sub(r"<@!?\d+>", " ", text)          # hallucinated markup
        text = re.sub(r"\s{2,}", " ", text)            # tidy the gaps it left
        return re.sub(r"\s+([,.;:!?)])", r"\1", text).strip()

    # ------------------------------------------------------------------
    # Ollama interaction (runs in a worker thread)
    # ------------------------------------------------------------------
    def _chat(self, messages: List[dict]) -> str:
        response = self._client.chat(model=self.model, messages=messages)
        return _response_content(response)

    async def _generate(
        self,
        content: str,
        key: Tuple[int, int],
        context: str,
        names: Sequence[str],
    ) -> str:
        history: Deque[dict] = self.memory[key]
        system = (
            f"{self.system_prompt}\n\n{context}" if context else self.system_prompt
        )
        payload: List[dict] = [{"role": "system", "content": system}]
        payload.extend(history)
        payload.append({"role": "user", "content": content})

        reply = await asyncio.to_thread(self._chat, payload)
        reply = self._sanitize(reply, names)

        # Commit to memory only after a successful round-trip.
        history.append({"role": "user", "content": content})
        if reply:
            history.append({"role": "assistant", "content": reply})
        return reply

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._should_respond(message):
            return

        content = self._clean_content(message)
        if not content:
            return

        key = (message.channel.id, message.author.id)
        mentioned = bool(self.bot.user and self.bot.user in message.mentions)

        # Tell the model who it is talking to, so it can use a real name
        # instead of echoing an @handle. Context is per-request only and
        # never enters conversation memory.
        display = message.author.display_name
        context = (
            f"CURRENT USER: {display} — their name on this server. Address "
            "them by name when it fits naturally, and never write '@' "
            "handles, usernames, or mention markup in your replies."
        )
        names = self._name_candidates(message)

        async with message.channel.typing():
            try:
                reply = await self._generate(content, key, context, names)
            except Exception:
                log.exception("Ollama request failed for %s", key)
                await message.channel.send(
                    "⚠️ Ehehe— my brain just went blank for a second: Ollama "
                    "isn't answering. Give me a moment and try again, okay?"
                )
                return

        if not reply:
            return

        for chunk in split_message(reply):
            if mentioned:
                await message.reply(chunk, mention_author=True)
            else:
                await message.channel.send(chunk)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @app_commands.command(
        name="forget",
        description="Clear the bot's memory of this conversation.",
    )
    async def forget(self, interaction: discord.Interaction) -> None:
        key = (interaction.channel_id, interaction.user.id)
        had_memory = key in self.memory
        self.memory.pop(key, None)
        if had_memory:
            await interaction.response.send_message(
                "🧠 Done! I've wiped this conversation from my memory. "
                "Fresh start — what should we talk about?",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "There's nothing to forget here — this is already a fresh "
                "start. Ehehe~",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Personality(bot))
