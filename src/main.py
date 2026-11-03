"""Bot entry point and command registration.

Loads configuration from ``.env``, enables the privileged intents the
bot needs, registers the personality & music cogs, and starts the
Discord client.
"""

from __future__ import annotations

import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

EXTENSIONS = ("cogs.personality", "cogs.music", "cogs.help")

OPTIONAL_VARS = (
    "OLLAMA_HOST",
    "OLLAMA_MODEL",
    "SPOTIPY_CLIENT_ID",
    "SPOTIPY_CLIENT_SECRET",
)


class CharacterBot(commands.Bot):
    """Discord client with slash-command sync and cog bootstrapping."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # personality listener
        intents.voice_states = True      # music voice connection
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,           # slash commands only
        )

    async def setup_hook(self) -> None:
        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
            except Exception:
                log.exception("Failed to load extension %s", extension)
                raise
        try:
            synced = await self.tree.sync()
            log.info("Synced %d global slash command(s)", len(synced))
        except discord.HTTPException:
            log.exception("Slash command sync failed")

    async def on_ready(self) -> None:
        if self.user is None:
            return
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        log.info("Watching %d guild(s)", len(self.guilds))


log = logging.getLogger("bot")


def main() -> None:
    load_dotenv()

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="[%(asctime)s] [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if level_name not in logging.getLevelNamesMapping():
        log.warning("Unknown LOG_LEVEL %r — falling back to INFO", level_name)

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    missing = [name for name in OPTIONAL_VARS if not os.getenv(name)]
    if missing:
        log.warning(
            "Optional environment variables not set (some features may be "
            "limited): %s",
            ", ".join(missing),
        )

    bot = CharacterBot()
    bot.run(token)


if __name__ == "__main__":
    main()
