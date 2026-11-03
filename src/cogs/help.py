"""``/help`` — list every slash command and feature the bot offers.

The listing is built from the live app-command tree, so commands added
to any cog show up here automatically, each with its registered
description. Ends with a link button to the public repository.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import discord
from discord import app_commands
from discord.ext import commands

REPO_URL = "https://github.com/AlV2201/Monika-Discord-Bot"

# Discord caps embed field values at 1024 characters; stay well under it.
FIELD_LIMIT = 800

# Group label -> (sort order, emoji, feature blurb). Cogs missing from this
# table still get listed, under their own class name (or "General" if the
# command isn't owned by a cog).
CATEGORIES: Dict[str, Tuple[int, str, str]] = {
    "Personality": (
        0,
        "💬",
        "Chat with me: I reply when you mention me, or to every message in "
        "channels listed in `PERSONALITY_CHANNELS`. Conversations are "
        "remembered per channel/user (Ollama-powered, `MEMORY_SIZE` messages).",
    ),
    "Music": (
        1,
        "🎵",
        "Per-guild queue with history, shuffle, and Off / Single / Queue "
        "looping. Accepts YouTube links & playlists, `ytsearch:` queries, "
        "plain text, and Spotify links.",
    ),
    "General": (2, "ℹ️", ""),
}
DEFAULT_CATEGORY = (99, "⚙️", "")


def _category_of(command: app_commands.Command) -> str:
    """Map a command to its group label via the cog that owns it."""
    binding = getattr(command, "binding", None)
    if binding is None:
        return "General"
    cls = binding if isinstance(binding, type) else type(binding)
    name = cls.__name__
    if name in CATEGORIES:
        return name
    return "General" if name == "Help" else name


def _chunk(lines: Sequence[str], limit: int) -> List[str]:
    """Split command lines so no joined chunk exceeds ``limit`` chars."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _build_listing(bot: commands.Bot) -> List[Tuple[str, str]]:
    """Return ``(field name, field value)`` pairs grouped by owning cog."""
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    for command in bot.tree.get_commands():
        if not isinstance(command, app_commands.Command):
            continue  # context menus / groups have nothing useful to list
        grouped.setdefault(_category_of(command), []).append(
            (f"/{command.name}", command.description or "No description.")
        )

    fields: List[Tuple[str, str]] = []
    labels = sorted(
        grouped, key=lambda label: (CATEGORIES.get(label, DEFAULT_CATEGORY)[0], label)
    )
    for label in labels:
        _, emoji, blurb = CATEGORIES.get(label, DEFAULT_CATEGORY)
        body = [
            f"`{name}` — {description}"
            for name, description in sorted(grouped[label])
        ]
        chunks = _chunk(body, FIELD_LIMIT)
        for index, chunk in enumerate(chunks):
            value = f"{blurb}\n\n{chunk}" if blurb and index == 0 else chunk
            suffix = f" ({index + 1})" if len(chunks) > 1 else ""
            fields.append((f"{emoji} {label}{suffix}", value))
    return fields


def _build_embed(bot: commands.Bot) -> discord.Embed:
    """Render the command listing as an embed linking to the repository."""
    embed = discord.Embed(
        title="Monika — Command Help",
        url=REPO_URL,
        description=(
            "Everything I can do, grouped by feature — "
            "click my name for the source code."
        ),
        color=discord.Color.blurple(),
    )
    for name, value in _build_listing(bot):
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(
        text="Mention me in any channel to chat · /help brings this back any time"
    )
    return embed


def _build_view() -> discord.ui.View:
    """Link button pointing at the public repository."""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="View on GitHub",
            emoji="🔗",
            url=REPO_URL,
            style=discord.ButtonStyle.link,
        )
    )
    return view


class Help(commands.Cog):
    """``/help`` — the command & feature index."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="help",
        description="List all available commands and features.",
    )
    async def help_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=_build_embed(self.bot), view=_build_view()
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
