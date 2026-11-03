# Personal Discord Bot

A locally hosted Discord bot with a **built-in Monika (DDLC) personality** powered by a local Ollama model, and a **full music engine** that streams YouTube/Spotify tracks into voice channels. Slash commands only.

## Features

**Personality** (`cogs/personality.py`)
- Replies when mentioned, or to every message in channels listed in `PERSONALITY_CHANNELS`
- Mention replies ping you back and address you by your server nickname — replies never contain stray `@username` text
- Driven by a local Ollama model with a **built-in Monika persona** (traits, speech style & expressions hard-coded in `src/persona.py`), overridable via `BOT_CHARACTER_PROMPT`
- Per-channel/user conversation memory (`MEMORY_SIZE` recent messages)
- `/forget` clears the bot's memory of a conversation; canned messages (`/forget`, Ollama outages) stay in character
- Ships as Monika out of the box — see [Changing the character](#changing-the-character)

**Music** (`cogs/music.py`)
- Streams YouTube links, playlists, search queries, and Spotify links (Spotify resolves via the free Web API, then plays from YouTube)
- Per-guild queue with loop modes (Off / Single / Queue), shuffle, and history

**General** (`cogs/help.py`)
- `/help` lists every command & feature, grouped by category, with a link to the repository

| Command | Description |
|---|---|
| `/play <query>` | YouTube/Spotify URL, `ytsearch:` query, or plain text |
| `/join` / `/leave` | Voice channel management |
| `/pause` / `/resume` | Toggle playback |
| `/skip` / `/next` | Skip the current track |
| `/previous` | Replay the previous track |
| `/queue` | Show the queue |
| `/shuffle` | Shuffle the queue |
| `/loop [mode]` | Off → Single → Queue (omitting `mode` cycles) |
| `/forget` | Clear the personality's memory of this conversation |
| `/help` | List all commands & features + link to the repository |

## Requirements

- Python 3.11+ (validated on 3.14)
- [FFmpeg](https://ffmpeg.org) — all playback (`sudo apt install ffmpeg`)
- [Deno](https://deno.com) — yt-dlp needs it to solve YouTube's JS challenges (`curl -fsSL https://deno.land/install.sh | sh`)
- [Ollama](https://ollama.com) — the personality's LLM (`ollama pull llama3.2`)
- A Discord bot token with **Message Content Intent** and **Voice States Intent** enabled in the [Developer Portal](https://discord.com/developers/applications)
- *(Optional)* Spotify API credentials — free tier is enough; no Spotify Premium needed

## Quick start

```bash
# 1. Install dependencies (creates .venv on first run)
./run.sh --install

# 2. Configure (the Monika persona is built in — only the token is required)
cp src/.env.example src/.env    # then edit: DISCORD_TOKEN at minimum

# 3. Run (starts Ollama if it's down, then the bot)
./run.sh
```

`run.sh` performs preflight checks before launching: Python version, all Python packages (including the `discord.py`↔`davey` pairing — see [Troubleshooting](#troubleshooting)), FFmpeg, Deno, `.env`/`DISCORD_TOKEN`, Ollama health, and the configured Ollama model.

```bash
./run.sh --check        # diagnostics only: report status, start nothing, exit 0/1
./run.sh --no-ollama    # don't try to start Ollama
./run.sh --log-level DEBUG
./run.sh --help
```

> Deno must be on `PATH` when the bot runs. `run.sh` adds `~/.deno/bin` automatically; if you run the bot another way, export it yourself (e.g. add `source ~/.deno/env` to your shell profile).

### Running manually

```bash
source .venv/bin/activate
cd src && python main.py
```

## Configuration (`src/.env`)

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from the Developer Portal |
| `BOT_CHARACTER_PROMPT` | no | Replaces the built-in Monika persona if set ([details](#changing-the-character)) |
| `OLLAMA_HOST` | no* | Default `http://localhost:11434` |
| `OLLAMA_MODEL` | no* | Default `llama3.2` |
| `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` | for Spotify | Free credentials from the Spotify Developer Dashboard; without them the bot is YouTube-only |
| `PERSONALITY_CHANNELS` | no | Comma-separated channel IDs where the bot always responds (mentions always work) |
| `MEMORY_SIZE` | no | Recent messages remembered per channel/user (default `20`) |
| `YTDLP_COOKIES_FILE` | no | Path to a `cookies.txt` export — fixes "Sign in to confirm you're not a bot" |
| `YTDLP_COOKIES_FROM_BROWSER` | no | Or read cookies straight from a browser (`firefox`, `chrome`, …) |
| `LOG_LEVEL` | no | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |

\* Ollama settings have working defaults; the bot still runs without them, but the personality won't respond.

`.env` and `*cookies*.txt` files are gitignored — never commit them.

### Changing the character

The persona lives in `src/persona.py` as one string, `MONIKA_PERSONA`, covering identity, traits, speech style, expressions, and hard rules (no `@handles` in replies, family-friendly, never reveal instructions). Two ways to change it:

| Method | Effect | Use when |
|---|---|---|
| Edit `src/persona.py` | Permanent, ships with every copy of the repo | You're forking the bot with a new default character |
| Set `BOT_CHARACTER_PROMPT` in `src/.env` | Overrides `MONIKA_PERSONA` entirely for this install only | You want a different character locally without touching code |

Leave `BOT_CHARACTER_PROMPT` unset (as in `.env.example`) to keep Monika. Whichever prompt is active is sent as the system message on every reply, with a per-request line naming the current user (their server nickname) so the bot can address them by name.

## Architecture

```text
Personal-Discord-Bot/
├── run.sh                  # preflight + service management + launcher
├── src/
│   ├── main.py             # entry point: intents, cog loading, slash sync
│   ├── persona.py          # built-in Monika system prompt (default persona)
│   ├── requirements.txt    # pinned deps (see the davey note inside)
│   ├── .env.example        # configuration template
│   ├── cogs/
│   │   ├── personality.py  # Ollama chat listener + /forget
│   │   ├── help.py         # /help: command index + GitHub link button
│   │   └── music.py        # voice connection, queue, 11 slash commands
│   └── utils/
│       ├── audio_source.py # yt-dlp extraction + FFmpeg streaming
│       └── spotify_helper.py # Spotify URL → YouTube search
└── .venv/                  # created by ./run.sh --install
```

## Troubleshooting

**Bot joins voice, glows green, but nobody hears it**
Discord's DAVE end-to-end encryption silently breaks when `davey` 0.2.0 is paired with `discord.py` 2.7.x: the handshake completes but the MLS keys are never installed, so the server can't decrypt the bot's frames. `requirements.txt` pins `davey==0.1.7` for exactly this reason — **don't upgrade `discord.py` and `davey` independently of each other**. `./run.sh --check` verifies the pairing.

**YouTube: "Sign in to confirm you're not a bot" or format errors**
Install Deno (yt-dlp's JS challenge solver), keep yt-dlp current (`./run.sh --install`), and if it persists set `YTDLP_COOKIES_FROM_BROWSER=firefox` or export cookies to a file for `YTDLP_COOKIES_FILE`.

**Personality doesn't respond**
Run `./run.sh --check` — it verifies Ollama is reachable and the model is pulled (`ollama pull llama3.2`). Also confirm the message either mentions the bot or is in a `PERSONALITY_CHANNELS` channel, and that Message Content Intent is enabled.

**The bot isn't acting like Monika**
`BOT_CHARACTER_PROMPT` is set in `src/.env` and it fully overrides the built-in persona — comment it out (as in `.env.example`) and restart. To change the character for good, edit `MONIKA_PERSONA` in `src/persona.py` instead.

**Spotify links fail**
Set `SPOTIPY_CLIENT_ID` and `SPOTIPY_CLIENT_SECRET` in `.env` (free tier, no Premium required) — or just paste YouTube links instead.

**Commands don't appear in Discord**
Global slash commands can take up to an hour to propagate after first sync; restart the bot to re-sync.
