# Project Specification: Local Discord Character Bot with Ollama & Music Player

## Core Purpose
A locally hosted Python Discord bot configured with a custom persona powered by a local Ollama LLM instance. The bot handles chat interactions in character and features a full-fledged music engine capable of streaming YouTube/Spotify tracks into voice channels with full queue control.

## System Prerequisites (Local Environment)
1. Python 3.11+ installed locally.
2. `FFmpeg` installed and added to the system PATH.
3. Ollama running locally (`ollama serve`), with a target model pulled (e.g., `ollama pull llama3.2`).
4. Discord Developer Account with a created bot token and Privileged Gateway Intents (Message Content Intent, Voice States Intent) enabled.

## Codebase Architecture
```text
discord-bot/
├── .env                  # Bot credentials & configuration
├── requirements.txt      # Dependencies
├── main.py               # Bot entry point and command registration
├── persona.py            # Built-in Monika system prompt (default persona)
├── cogs/
│   ├── personality.py    # Text listener & Ollama LLM interaction
│   ├── help.py           # /help command index + repository link
│   └── music.py          # Voice connection & playback commands
└── utils/
    ├── audio_source.py   # yt-dlp & FFmpeg audio stream handler
    └── spotify_helper.py # Spotify URL parser & metadata extractor
```

## Detailed Specifications

### 1. Environment & Config (`.env`)

* `DISCORD_TOKEN`: Discord Bot Token.
* `OLLAMA_HOST`: Local Ollama URL (default: `http://localhost:11434`).
* `OLLAMA_MODEL`: Target model name (e.g., `llama3.2`).
* `SPOTIPY_CLIENT_ID`: Spotify App Client ID (for Spotify API resolution).
* `SPOTIPY_CLIENT_SECRET`: Spotify App Client Secret.
* `BOT_CHARACTER_PROMPT`: Optional system prompt that *replaces* the built-in Monika persona (`persona.py`) if set; background, tone, and response constraints.

### 2. Personality Cog (`cogs/personality.py`)

* Intercepts messages mentioning the bot or sent in specific chat channels.
* Uses `ollama` (via `asyncio.to_thread` or `aiohttp`) to keep Discord's event loop non-blocking.
* Maintains conversation memory (up to N recent messages) per channel/user to maintain conversational context.
* System prompt prepends every chat request to enforce character integrity — by default the built-in Monika persona from `persona.py`.

### 3. Audio Source Engine (`utils/audio_source.py`)

* Uses `yt-dlp` to extract direct audio streams (`format: bestaudio/best`) without saving files to disk.
* Standard `FFmpegPCMAudio` options for stability:
`-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`
* Handles single YouTube links, YouTube search queries (`ytsearch:`), and YouTube playlist URLs.

### 4. Spotify Resolver (`utils/spotify_helper.py`)

* Resolves Spotify track/album/playlist URLs via `spotipy`.
* Extracts `Artist Name - Track Title` strings and forwards them to `audio_source.py` to search and stream via YouTube.

### 5. Music Cog (`cogs/music.py`)

* **Queue System:** Per-guild `asyncio.Queue` or Python list for upcoming tracks.
* **Commands (Slash or Prefix commands):**
* `/join` / `/leave`: Voice channel management.
* `/play <query_or_url>`: Accepts YouTube URLs, Spotify URLs, or search text. Enqueues item(s) and starts playback if idle.
* `/pause` / `/resume`: Toggles audio state.
* `/skip` / `/next`: Stops current track to trigger the next track in queue.
* `/previous`: Replays the previously finished track from a stored history stack.
* `/queue`: Formats and sends an Embed list of upcoming songs.
* `/shuffle`: Shuffles items in the current queue list.
* `/loop`: Toggles looping mode (Off / Single Song / Queue).
