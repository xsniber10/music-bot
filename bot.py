import asyncio
import html as html_module
import json
import os
import re
import sys
import time
import urllib.request

import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

import database as db

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Paste your Spotify playlist link here (e.g. https://open.spotify.com/playlist/xxxxxxxxxxxx)
SPOTIFY_PLAYLIST_URL = os.getenv(
    "SPOTIFY_PLAYLIST_URL", "https://open.spotify.com/playlist/0IalEO1MniD8cDpAfj39jC?si=7114a6a5582d40ea"
)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YTDL_FLAT_OPTIONS = {
    "quiet": True,
    "extract_flat": "in_playlist",
    "skip_download": True,
}

AUTO_DISCONNECT_SECONDS = 120
PROGRESS_UPDATE_SECONDS = 40
PROGRESS_BAR_LENGTH = 15
MAX_PLAYLIST_SONGS = 100

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_flat = yt_dlp.YoutubeDL(YTDL_FLAT_OPTIONS)


# Populated from Postgres by initialize_data() before the bot logs in.
playlist_data: dict = {"playlists": {}, "favorites": {}, "library": {}}

# Put your new songs here! Format: {"title": "Song Title", "url": "https://www.youtube.com/watch?v=..."}
SONG_LIBRARY = [
    # {"title": "My New Song 1", "url": "https://www.youtube.com/watch?v=..."},
    # I will add the rest of my new songs here
]


SPOTIFY_PLAYLIST_ID_RE = re.compile(r"/playlist/([A-Za-z0-9]+)")


def _extract_json_array(text: str, array_start: int) -> str:
    depth = 0
    in_string = False
    escape = False
    i = array_start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[array_start : i + 1]
        i += 1
    return text[array_start:i]


def fetch_spotify_playlist_tracks(playlist_url: str) -> list[dict]:
    """Credential-free: reads the public embed page's inline track list, no API key needed."""
    id_match = SPOTIFY_PLAYLIST_ID_RE.search(playlist_url)
    if not id_match:
        return []
    playlist_id = id_match.group(1)

    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    request = urllib.request.Request(
        embed_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(5_000_000)
    except Exception as exc:
        print(f"Could not fetch Spotify playlist '{playlist_url}': {exc}")
        return []

    text = raw.decode("utf-8", errors="ignore")
    marker = '"trackList":['
    marker_index = text.find(marker)
    if marker_index == -1:
        print(f"Could not find track list in Spotify playlist '{playlist_url}'.")
        return []

    array_text = _extract_json_array(text, marker_index + len(marker) - 1)
    try:
        raw_tracks = json.loads(array_text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"Could not parse Spotify playlist tracks '{playlist_url}': {exc}")
        return []

    tracks = []
    for item in raw_tracks:
        title = html_module.unescape((item.get("title") or "").strip())
        artist = html_module.unescape((item.get("subtitle") or "").strip())
        if not title:
            continue
        full_title = f"{title} - {artist}" if artist else title
        duration_ms = item.get("duration")
        tracks.append(
            {
                "title": full_title,
                "url": f"ytsearch1:{full_title}",
                "duration": round(duration_ms / 1000) if duration_ms else None,
            }
        )
    return tracks


async def fetch_spotify_playlist_tracks_async(playlist_url: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_spotify_playlist_tracks, playlist_url)


async def seed_song_library() -> None:
    new_entries: list[tuple[str, str, int | None, str | None]] = []

    for entry in SONG_LIBRARY:
        url = entry.get("url")
        title = entry.get("title")
        if not url or not title:
            continue
        if url in playlist_data["library"]:
            continue
        playlist_data["library"][url] = {"title": title, "duration": None, "thumbnail": None}
        new_entries.append((url, title, None, None))

    if SPOTIFY_PLAYLIST_URL:
        for track in fetch_spotify_playlist_tracks(SPOTIFY_PLAYLIST_URL):
            url = track["url"]
            if url in playlist_data["library"]:
                continue
            duration = track.get("duration")
            playlist_data["library"][url] = {
                "title": track["title"],
                "duration": duration,
                "thumbnail": None,
            }
            new_entries.append((url, track["title"], duration, None))

    for url, title, duration, thumbnail in new_entries:
        await db.upsert_library_entry(url, title, duration, thumbnail)


class Song:
    def __init__(
        self,
        source_url: str,
        title: str,
        webpage_url: str,
        duration: int | None,
        thumbnail: str | None,
        requester: discord.Member,
    ):
        self.source_url = source_url
        self.title = title
        self.webpage_url = webpage_url
        self.duration = duration
        self.thumbnail = thumbnail
        self.requester = requester


class GuildState:
    def __init__(self):
        self.session_songs: list[Song] = []
        self.current_index: int = -1
        self.panel_message: discord.Message | None = None
        self.panel_view: "MusicControlView | None" = None
        self.text_channel: discord.abc.Messageable | None = None
        self.volume: float = 1.0
        self.loop: bool = False
        self.manual_transition: bool = False
        self.disconnect_task: asyncio.Task | None = None
        self.progress_task: asyncio.Task | None = None
        self.elapsed_offset: float = 0.0
        self.play_resumed_at: float | None = None
        self.spotify_view: bool = False
        self.spotify_tracks_cache: list[dict] | None = None


guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    return guild_states.setdefault(guild_id, GuildState())


def get_now_playing_song(state: GuildState) -> Song | None:
    if 0 <= state.current_index < len(state.session_songs):
        return state.session_songs[state.current_index]
    return None


def get_upcoming_songs(state: GuildState) -> list[Song]:
    return state.session_songs[state.current_index + 1 :]


async def save_song_to_library(song: Song) -> None:
    playlist_data["library"][song.webpage_url] = {
        "title": song.title,
        "duration": song.duration,
        "thumbnail": song.thumbnail,
    }
    await db.upsert_library_entry(song.webpage_url, song.title, song.duration, song.thumbnail)


def load_library_into_session(state: GuildState, requester: discord.Member) -> int:
    existing_urls = {s.webpage_url for s in state.session_songs}
    added = 0
    for url, info in playlist_data["library"].items():
        if url in existing_urls:
            continue
        state.session_songs.append(
            Song(
                source_url="",
                title=info.get("title", url),
                webpage_url=url,
                duration=info.get("duration"),
                thumbnail=info.get("thumbnail"),
                requester=requester,
            )
        )
        existing_urls.add(url)
        added += 1
    return added


def get_recent_library_entries(limit: int = 10) -> list[tuple[str, dict]]:
    items = list(playlist_data["library"].items())
    recent = items[-limit:]
    recent.reverse()
    return recent


def mark_paused(state: GuildState) -> None:
    if state.play_resumed_at is not None:
        state.elapsed_offset += time.monotonic() - state.play_resumed_at
        state.play_resumed_at = None


def mark_resumed(state: GuildState) -> None:
    if state.play_resumed_at is None:
        state.play_resumed_at = time.monotonic()


def get_elapsed_seconds(state: GuildState) -> float:
    elapsed = state.elapsed_offset
    if state.play_resumed_at is not None:
        elapsed += time.monotonic() - state.play_resumed_at
    return elapsed


def stop_progress_task(state: GuildState) -> None:
    if state.progress_task is not None and not state.progress_task.done():
        state.progress_task.cancel()
    state.progress_task = None


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_progress_bar(elapsed: float, duration: int | None) -> str:
    if not duration:
        return f"`{format_duration(elapsed)} {'▬' * PROGRESS_BAR_LENGTH} 🔴 LIVE`"

    elapsed = max(0.0, min(elapsed, duration))
    ratio = elapsed / duration
    filled = min(int(ratio * PROGRESS_BAR_LENGTH), PROGRESS_BAR_LENGTH - 1)
    bar = "▬" * filled + "🔘" + "▬" * (PROGRESS_BAR_LENGTH - filled - 1)
    return f"`{format_duration(elapsed)} {bar} {format_duration(duration)}`"


async def extract_song(query: str, requester: discord.Member) -> Song:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, lambda: ytdl.extract_info(query, download=False)
    )

    if "entries" in data:
        data = data["entries"][0]

    return Song(
        source_url=data["url"],
        title=data.get("title", "Unknown title"),
        webpage_url=data.get("webpage_url", query),
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        requester=requester,
    )


async def ensure_voice_connected(guild: discord.Guild, member: discord.Member) -> str | None:
    if guild.voice_client is not None:
        return None
    if member.voice is None or member.voice.channel is None:
        return "You need to be in a voice channel first."
    await member.voice.channel.connect()
    return None


def is_playlist_url(query: str) -> bool:
    return query.startswith(("http://", "https://")) and "list=" in query


async def extract_playlist_urls(url: str) -> list[str]:
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, lambda: ytdl_flat.extract_info(url, download=False))

    entries = list(data.get("entries") or [])
    urls = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id")
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            video_url = entry.get("url") or entry.get("webpage_url")
        if video_url:
            urls.append(video_url)
    return urls


def is_spotify_track_url(query: str) -> bool:
    return "open.spotify.com/track/" in query.lower()


SPOTIFY_TRACK_ID_RE = re.compile(r"/track/([A-Za-z0-9]+)")
SPOTIFY_EMBED_TITLE_ARTIST_RE = re.compile(
    r'"title":"((?:[^"\\]|\\.)*)"\s*,\s*"artists":\[\{"name":"((?:[^"\\]|\\.)*)"'
)


def _fetch_spotify_track_query(url: str) -> str | None:
    id_match = SPOTIFY_TRACK_ID_RE.search(url)
    if not id_match:
        return None
    track_id = id_match.group(1)

    # Spotify's normal track page is a JS app shell with no per-track title in the
    # initial HTML, but the embed page ships the track's title/artist as inline JSON —
    # no API key or login required, just a plain page fetch.
    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    request = urllib.request.Request(
        embed_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(300000)
    except Exception as exc:
        print(f"Could not fetch Spotify embed page '{embed_url}': {exc}")
        return None

    text = raw.decode("utf-8", errors="ignore")
    match = SPOTIFY_EMBED_TITLE_ARTIST_RE.search(text)
    if not match:
        return None

    title = html_module.unescape(match.group(1)).strip()
    artist = html_module.unescape(match.group(2)).strip()
    if not title:
        return None
    return f"{title} - {artist}" if artist else title


async def resolve_spotify_track_query(url: str) -> str | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_spotify_track_query, url)


def build_now_playing_embed(state: GuildState, song: Song) -> discord.Embed:
    embed = discord.Embed(
        title=song.title,
        url=song.webpage_url,
        description="🎶 Now Playing",
        color=discord.Color.blurple(),
    )

    elapsed = get_elapsed_seconds(state)

    if state.play_resumed_at is None:
        embed.add_field(name="⏱️ Ends", value="⏸️ Paused", inline=False)
    elif song.duration:
        remaining = max(song.duration - elapsed, 0)
        end_epoch = int(time.time() + remaining)
        embed.add_field(name="⏱️ Ends", value=f"<t:{end_epoch}:R>", inline=False)

    embed.add_field(name="📊 Progress", value=build_progress_bar(elapsed, song.duration), inline=False)

    if song.thumbnail:
        embed.set_image(url=song.thumbnail)
    embed.set_footer(
        text=f"🙋 Requested by {song.requester.display_name}  •  "
        f"🎵 Track {state.current_index + 1} of {len(state.session_songs)}",
        icon_url=song.requester.display_avatar.url,
    )
    return embed


def build_standby_embed() -> discord.Embed:
    return discord.Embed(
        title="🎵 Music Bot - Standby Mode",
        description="Ready and waiting! Type `!play [song]` or select a song from the dropdown below.",
        color=discord.Color.dark_grey(),
    )


def build_panel_content(state: GuildState, guild: discord.Guild) -> tuple[discord.Embed, "MusicControlView"]:
    song = get_now_playing_song(state)
    vc = guild.voice_client
    active = song is not None and vc is not None and (vc.is_playing() or vc.is_paused())

    embed = build_now_playing_embed(state, song) if active else build_standby_embed()
    view = MusicControlView(guild.id, active=active)
    return embed, view


def build_queue_embed(guild_id: int) -> discord.Embed:
    state = get_state(guild_id)
    now_playing = get_now_playing_song(state)
    upcoming = get_upcoming_songs(state)

    embed = discord.Embed(title="📜 Music Queue", color=discord.Color.blurple())

    if now_playing is None and not upcoming:
        embed.description = "The queue is empty and nothing is playing."
        return embed

    if now_playing:
        embed.add_field(
            name="🎧 Now Playing",
            value=f"[{now_playing.title}]({now_playing.webpage_url}) "
            f"`{format_duration(now_playing.duration)}`",
            inline=False,
        )

    if upcoming:
        lines = "\n".join(
            f"**{i}.** [{s.title}]({s.webpage_url}) `{format_duration(s.duration)}`"
            for i, s in enumerate(upcoming, start=1)
        )
        embed.add_field(name="📜 Up Next", value=lines[:1024], inline=False)
    else:
        embed.add_field(name="📜 Up Next", value="No songs queued.", inline=False)

    return embed


PANEL_PURGE_LIMIT = 100


async def purge_bot_messages(channel: discord.abc.Messageable) -> None:
    """Deletes every message the bot has sent in this channel (never touches other users' messages)."""
    purge = getattr(channel, "purge", None)
    if purge is None:
        return

    def is_bot_message(message: discord.Message) -> bool:
        return bot.user is not None and message.author.id == bot.user.id

    try:
        await purge(limit=PANEL_PURGE_LIMIT, check=is_bot_message)
    except discord.HTTPException:
        pass


async def render_panel(guild: discord.Guild, channel: discord.abc.Messageable | None = None) -> None:
    state = get_state(guild.id)
    embed, view = build_panel_content(state, guild)
    state.panel_view = view

    target_channel = channel or state.text_channel
    if target_channel is None and state.panel_message is not None:
        target_channel = state.panel_message.channel

    if state.panel_message is not None:
        try:
            await state.panel_message.delete()
        except discord.HTTPException:
            pass
        state.panel_message = None

    if target_channel is None:
        return

    await purge_bot_messages(target_channel)

    state.panel_message = await target_channel.send(embed=embed, view=view)


async def send_temp(
    ctx: commands.Context,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    delay: float = 5.0,
) -> discord.Message:
    message = await ctx.send(content=content, embed=embed)
    await message.delete(delay=delay)
    return message


async def progress_updater(guild: discord.Guild) -> None:
    try:
        while True:
            await asyncio.sleep(PROGRESS_UPDATE_SECONDS)

            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                return
            if not (vc.is_playing() or vc.is_paused()):
                return

            await render_panel(guild)
    except asyncio.CancelledError:
        return


async def perform_skip(guild: discord.Guild, channel: discord.abc.Messageable) -> str:
    state = get_state(guild.id)
    vc = guild.voice_client

    if vc is None or not (vc.is_playing() or vc.is_paused()):
        return "Nothing is playing right now."

    if not state.session_songs:
        return "There are no songs to skip to."

    next_index = state.current_index + 1
    if next_index >= len(state.session_songs):
        next_index = 0

    state.manual_transition = True
    vc.stop()

    await start_track(guild, channel, next_index)
    return f"Skipped to **{state.session_songs[next_index].title}**."


async def perform_previous(guild: discord.Guild, channel: discord.abc.Messageable) -> str:
    state = get_state(guild.id)
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return "I'm not connected to a voice channel."

    prev_index = state.current_index - 1
    if prev_index < 0:
        return "❌ لا توجد أغنية سابقة للرجوع إليها! | No previous song in history."

    state.manual_transition = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await start_track(guild, channel, prev_index)
    return f"⏮️ Back to **{state.session_songs[prev_index].title}**."


async def perform_stop(guild: discord.Guild) -> str:
    vc = guild.voice_client
    if vc is None:
        return "I'm not connected to a voice channel."

    state = get_state(guild.id)
    state.manual_transition = True

    if state.disconnect_task is not None and not state.disconnect_task.done():
        state.disconnect_task.cancel()
        state.disconnect_task = None

    stop_progress_task(state)

    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await vc.disconnect()

    state.session_songs.clear()
    state.current_index = -1
    state.loop = False
    state.elapsed_offset = 0.0
    state.play_resumed_at = None

    await render_panel(guild)

    return "Stopped and returned to standby."


async def perform_jump(guild: discord.Guild, channel: discord.abc.Messageable, index: int) -> str:
    state = get_state(guild.id)

    if index < 0 or index >= len(state.session_songs):
        return "That song is no longer available."

    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return "I'm not connected to a voice channel."

    state.manual_transition = True
    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await start_track(guild, channel, index)
    return f"Now playing: **{state.session_songs[index].title}**."


async def play_library_selection(
    guild: discord.Guild, channel: discord.abc.Messageable, member: discord.Member, url: str
) -> str:
    try:
        song = await extract_song(url, member)
    except Exception as exc:
        print(f"Failed to load selected song '{url}': {exc}")
        return f"Could not load that song: {exc}"

    await save_song_to_library(song)
    state = get_state(guild.id)
    state.session_songs.append(song)
    new_index = len(state.session_songs) - 1

    load_library_into_session(state, member)

    return await perform_jump(guild, channel, new_index)


async def start_track(guild: discord.Guild, channel: discord.abc.Messageable, index: int) -> None:
    state = get_state(guild.id)
    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return

    song = state.session_songs[index]
    if not song.source_url:
        placeholder_key = song.webpage_url
        try:
            resolved = await extract_song(song.webpage_url, song.requester)
        except Exception as exc:
            print(f"Failed to resolve queued song '{song.webpage_url}': {exc}")
            del state.session_songs[index]
            if not state.session_songs:
                state.current_index = -1
                stop_progress_task(state)
                await render_panel(guild, channel)
                return
            next_index = index if index < len(state.session_songs) else 0
            await start_track(guild, channel, next_index)
            return
        state.session_songs[index] = resolved
        song = resolved
        if resolved.webpage_url != placeholder_key and placeholder_key in playlist_data["library"]:
            del playlist_data["library"][placeholder_key]
            await db.delete_library_entry(placeholder_key)
        await save_song_to_library(song)

    stop_progress_task(state)
    state.current_index = index
    state.elapsed_offset = 0.0
    state.play_resumed_at = time.monotonic()

    audio = discord.FFmpegPCMAudio(song.source_url, **FFMPEG_OPTIONS)
    source = discord.PCMVolumeTransformer(audio, volume=state.volume)

    def after_playing(error: Exception | None):
        if error:
            print(f"Player error: {error}")
        asyncio.run_coroutine_threadsafe(track_finished(guild, channel), bot.loop)

    vc.play(source, after=after_playing)

    await render_panel(guild, channel)
    state.progress_task = bot.loop.create_task(progress_updater(guild))


async def track_finished(guild: discord.Guild, channel: discord.abc.Messageable) -> None:
    state = get_state(guild.id)

    if state.manual_transition:
        state.manual_transition = False
        return

    vc = guild.voice_client
    if vc is None or not vc.is_connected():
        return

    if state.loop and get_now_playing_song(state) is not None:
        await start_track(guild, channel, state.current_index)
        return

    if not state.session_songs:
        stop_progress_task(state)
        await render_panel(guild, channel)
        return

    next_index = state.current_index + 1
    if next_index >= len(state.session_songs):
        next_index = 0

    await start_track(guild, channel, next_index)


async def auto_disconnect(guild: discord.Guild) -> None:
    try:
        await asyncio.sleep(AUTO_DISCONNECT_SECONDS)
    except asyncio.CancelledError:
        return

    vc = guild.voice_client
    if vc is None:
        return

    human_members = [m for m in vc.channel.members if not m.bot]
    if human_members:
        return

    state = get_state(guild.id)
    text_channel = state.text_channel

    await perform_stop(guild)

    if text_channel is not None:
        try:
            await text_channel.send(
                "Left the voice channel after being alone for 2 minutes.", delete_after=5
            )
        except discord.HTTPException:
            pass


STATUS_META = {
    "now_playing": ("🎧", "[Now Playing]"),
    "saved": ("⭐", "[Saved Song]"),
    "standby": ("💤", ""),
}

DROPDOWN_TITLE_MAX_LENGTH = 40

TITLE_CLUTTER_PATTERNS = [
    r"\(official video\)",
    r"\(official music video\)",
    r"\[official video\]",
    r"\[mv\]",
    r"\(فيديو كليب حصري\)",
    r"\(فيديو كليب\)",
    r"\(حصرياً\)",
    r"\|\s*حصرياً",
    r"\(lyrics\)",
    r"\(lyrical\)",
    r"\(official audio\)",
    r"\(audio\)",
    r"\(remix\)",
]
TITLE_CLUTTER_RE = re.compile("|".join(TITLE_CLUTTER_PATTERNS), re.IGNORECASE)


def clean_title_for_dropdown(title: str) -> str:
    cleaned = TITLE_CLUTTER_RE.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|.")

    if not cleaned:
        cleaned = title.strip()

    if len(cleaned) > DROPDOWN_TITLE_MAX_LENGTH:
        cleaned = cleaned[:DROPDOWN_TITLE_MAX_LENGTH] + "..."

    return cleaned


def _normalized_title(title: str) -> str:
    return clean_title_for_dropdown(title).strip().lower()


def find_duplicate_library_entry(title: str) -> str | None:
    normalized = _normalized_title(title)
    for url, info in playlist_data["library"].items():
        if _normalized_title(info.get("title", url)) == normalized:
            return url
    return None


async def dedupe_song_library() -> None:
    seen: dict[str, str] = {}
    duplicate_urls = []

    for url, info in playlist_data["library"].items():
        normalized = _normalized_title(info.get("title", url))
        if normalized in seen:
            duplicate_urls.append(url)
        else:
            seen[normalized] = url

    if not duplicate_urls:
        return

    for url in duplicate_urls:
        del playlist_data["library"][url]
        await db.delete_library_entry(url)

    print(f"Removed {len(duplicate_urls)} duplicate song(s) from the library.")


async def initialize_data() -> None:
    global playlist_data
    playlist_data = await db.load_all_data()
    await seed_song_library()
    await dedupe_song_library()


class QueueSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        state = get_state(guild_id)
        entries = self.build_entries(state)
        empty = not entries

        options = self.entries_to_options(entries) if entries else [
            discord.SelectOption(
                label="No songs in session yet. Type !play to add one!",
                value="placeholder_empty",
                emoji="🎵",
            )
        ]

        super().__init__(
            placeholder="No songs yet — try !play" if empty else "🎼 Jump to a song...",
            options=options,
            disabled=empty,
            min_values=1,
            max_values=1,
            row=3,
        )

    @staticmethod
    def build_entries(state: GuildState) -> list[dict]:
        now_entry = None
        standby_entries = []
        played_entries = []

        for i, song in enumerate(state.session_songs):
            entry = {"title": song.title, "duration": song.duration, "value": f"s:{i}"}
            if i == state.current_index:
                entry["status"] = "now_playing"
                now_entry = entry
            elif i > state.current_index:
                entry["status"] = "standby"
                standby_entries.append(entry)
            else:
                entry["status"] = "saved"
                played_entries.append(entry)

        session_urls = {s.webpage_url for s in state.session_songs}
        library_entries = []
        for url, info in playlist_data["library"].items():
            if url in session_urls:
                continue
            library_entries.append(
                {
                    "status": "saved",
                    "title": info.get("title", url),
                    "duration": info.get("duration"),
                    "value": f"l:{url}",
                }
            )
        library_entries.reverse()

        ordered = []
        if now_entry:
            ordered.append(now_entry)
        ordered.extend(standby_entries)
        ordered.extend(played_entries[::-1])
        ordered.extend(library_entries)

        return ordered[:25]

    @staticmethod
    def entries_to_options(entries: list[dict]) -> list[discord.SelectOption]:
        options = []
        for entry in entries:
            emoji, tag = STATUS_META[entry["status"]]
            title = clean_title_for_dropdown(entry["title"])
            label = f"{tag} {title}".strip() if tag else title

            if entry["status"] == "now_playing":
                description = f"▶️ NOW PLAYING • {format_duration(entry['duration'])} • ⚡ ACTIVE"
            else:
                description = f"⏱️ {format_duration(entry['duration'])}"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=entry["value"],
                    description=description[:100],
                    emoji=emoji,
                    default=(entry["status"] == "now_playing"),
                )
            )
        return options

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        value = self.values[0]

        if value == "placeholder_empty":
            await interaction.response.defer()
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.defer()
            return

        if guild.voice_client is None:
            if member.voice is None or member.voice.channel is None:
                await interaction.response.defer()
                return
            await member.voice.channel.connect()

        state = get_state(guild.id)
        channel = state.text_channel or interaction.channel
        state.text_channel = channel

        if value.startswith("s:"):
            try:
                index = int(value[2:])
            except ValueError:
                await interaction.response.defer()
                return

            load_library_into_session(state, member)

            await interaction.response.defer()
            await perform_jump(guild, channel, index)
            return

        if value.startswith("l:"):
            url = value[2:]
            await interaction.response.defer()
            await play_library_selection(guild, channel, member, url)
            return

        await interaction.response.defer()


class RecentSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        state = get_state(guild_id)
        entries = self.build_entries(state)
        empty = not entries

        options = self.entries_to_options(entries) if entries else [
            discord.SelectOption(
                label="No songs saved yet. Type !play to add one!",
                value="placeholder_empty",
                emoji="✨",
            )
        ]

        super().__init__(
            placeholder="No recent songs yet" if empty else "✨ أحدث 10 أغاني | Recently Added",
            options=options,
            disabled=empty,
            min_values=1,
            max_values=1,
            row=2,
        )

    @staticmethod
    def build_entries(state: GuildState) -> list[dict]:
        now_playing = get_now_playing_song(state)
        now_playing_url = now_playing.webpage_url if now_playing else None

        entries = []
        for url, info in get_recent_library_entries(10):
            status = "now_playing" if url == now_playing_url else "saved"
            entries.append(
                {
                    "status": status,
                    "title": info.get("title", url),
                    "duration": info.get("duration"),
                    "value": f"l:{url}",
                }
            )
        return entries

    @staticmethod
    def entries_to_options(entries: list[dict]) -> list[discord.SelectOption]:
        options = []
        for i, entry in enumerate(entries, start=1):
            emoji, _tag = STATUS_META[entry["status"]]
            title = clean_title_for_dropdown(entry["title"])
            label = f"{i}. {title}"

            if entry["status"] == "now_playing":
                description = f"▶️ NOW PLAYING • {format_duration(entry['duration'])} • ⚡ ACTIVE"
            else:
                description = f"⏱️ {format_duration(entry['duration'])}"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=entry["value"],
                    description=description[:100],
                    emoji=emoji,
                )
            )
        return options

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        value = self.values[0]

        if value == "placeholder_empty":
            await interaction.response.defer()
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.defer()
            return

        if guild.voice_client is None:
            if member.voice is None or member.voice.channel is None:
                await interaction.response.defer()
                return
            await member.voice.channel.connect()

        state = get_state(guild.id)
        channel = state.text_channel or interaction.channel
        state.text_channel = channel

        if not value.startswith("l:"):
            await interaction.response.defer()
            return

        url = value[2:]
        await interaction.response.defer()
        await play_library_selection(guild, channel, member, url)


class SpotifySelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        state = get_state(guild_id)
        entries = (state.spotify_tracks_cache or [])[:25]

        options = self.entries_to_options(entries) if entries else [
            discord.SelectOption(label="No Spotify tracks found", value="none", emoji="🟢")
        ]

        super().__init__(
            placeholder="🟢 Spotify Playlist" if entries else "No Spotify tracks found",
            options=options,
            disabled=not entries,
            min_values=1,
            max_values=1,
            row=2,
        )

    @staticmethod
    def entries_to_options(entries: list[dict]) -> list[discord.SelectOption]:
        options = []
        for i, track in enumerate(entries, start=1):
            title = clean_title_for_dropdown(track["title"])
            label = f"{i}. {title}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"l:{track['url']}",
                    description=format_duration(track.get("duration")),
                    emoji="🟢",
                )
            )
        return options

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        value = self.values[0]

        if value == "none":
            await interaction.response.defer()
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.defer()
            return

        if guild.voice_client is None:
            if member.voice is None or member.voice.channel is None:
                await interaction.response.defer()
                return
            await member.voice.channel.connect()

        state = get_state(guild.id)
        channel = state.text_channel or interaction.channel
        state.text_channel = channel

        if not value.startswith("l:"):
            await interaction.response.defer()
            return

        url = value[2:]
        await interaction.response.defer()
        await play_library_selection(guild, channel, member, url)


class SongSearchModal(discord.ui.Modal, title="🔍 البحث عن أغنية | Search Song"):
    query = discord.ui.TextInput(
        label="اسم الأغنية أو الرابط | Song Name or Link",
        placeholder="e.g., Amr Diab - Tamally Maak or YouTube URL...",
        style=discord.TextStyle.short,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.response.defer()
            return

        if guild.voice_client is None:
            if member.voice is None or member.voice.channel is None:
                await interaction.response.defer()
                return
            await member.voice.channel.connect()

        state = get_state(guild.id)
        channel = state.text_channel or interaction.channel
        state.text_channel = channel

        await interaction.response.defer()
        await play_query(guild, channel, member, self.query.value, priority=True)


class RemoveSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        state = get_state(guild_id)
        entries = self.build_entries(state)

        options = self.entries_to_options(entries) if entries else [
            discord.SelectOption(label="No songs to remove yet", value="none")
        ]

        super().__init__(
            placeholder="Select a song to remove..." if entries else "No songs to remove yet",
            options=options,
            disabled=not entries,
            min_values=1,
            max_values=1,
        )

    @staticmethod
    def build_entries(state: GuildState) -> list[dict]:
        entries = []

        for i, song in enumerate(state.session_songs):
            if i <= state.current_index:
                continue
            entries.append({"title": song.title, "duration": song.duration, "value": f"s:{i}"})

        session_urls = {s.webpage_url for s in state.session_songs}
        for url, info in playlist_data["library"].items():
            if url in session_urls:
                continue
            entries.append(
                {"title": info.get("title", url), "duration": info.get("duration"), "value": f"l:{url}"}
            )

        return entries[:25]

    @staticmethod
    def entries_to_options(entries: list[dict]) -> list[discord.SelectOption]:
        options = []
        for entry in entries:
            title = clean_title_for_dropdown(entry["title"])
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    value=entry["value"],
                    description=format_duration(entry["duration"]),
                    emoji="🗑️",
                )
            )
        return options

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "none":
            await interaction.response.defer()
            return

        guild = interaction.guild
        state = get_state(guild.id)
        channel = state.text_channel or interaction.channel
        state.text_channel = channel

        removed_title = None

        if value.startswith("s:"):
            try:
                index = int(value[2:])
            except ValueError:
                await interaction.response.defer()
                return

            if index <= state.current_index or index >= len(state.session_songs):
                await interaction.response.edit_message(content="That song is no longer in the queue.", view=None)
                return

            removed_song = state.session_songs.pop(index)
            removed_title = removed_song.title

            if removed_song.webpage_url in playlist_data["library"]:
                del playlist_data["library"][removed_song.webpage_url]
                await db.delete_library_entry(removed_song.webpage_url)

        elif value.startswith("l:"):
            url = value[2:]
            info = playlist_data["library"].pop(url, None)
            if info is None:
                await interaction.response.edit_message(content="That song is no longer saved.", view=None)
                return
            removed_title = info.get("title", url)
            await db.delete_library_entry(url)

        else:
            await interaction.response.defer()
            return

        await interaction.response.edit_message(
            content=f"🗑️ Removed **{removed_title}** from the playlist!", view=None
        )

        await render_panel(guild)


class MusicControlView(discord.ui.View):
    def __init__(self, guild_id: int, active: bool):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.active = active
        self.sync_pause_button()
        self.sync_loop_button()
        self.sync_spotify_button()
        self.prev_button.disabled = not active
        self.pause_button.disabled = not active
        self.skip_button.disabled = not active

        state = get_state(guild_id)

        if state.spotify_view:
            self.remove_item(self.search_button)
            self.remove_item(self.remove_button)

        self.queue_select = QueueSelect(guild_id)
        self.add_item(self.queue_select)

        self.secondary_select = SpotifySelect(guild_id) if state.spotify_view else RecentSelect(guild_id)
        self.add_item(self.secondary_select)

    def sync_pause_button(self) -> None:
        guild = bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        if vc is not None and vc.is_paused():
            self.pause_button.label = "Resume"
            self.pause_button.emoji = "▶️"
        else:
            self.pause_button.label = "Pause"
            self.pause_button.emoji = "⏸️"

    def sync_loop_button(self) -> None:
        state = get_state(self.guild_id)
        if state.loop:
            self.loop_button.label = "Loop: ON"
            self.loop_button.style = discord.ButtonStyle.blurple
        else:
            self.loop_button.label = "Loop: OFF"
            self.loop_button.style = discord.ButtonStyle.grey

    def sync_spotify_button(self) -> None:
        state = get_state(self.guild_id)
        if state.spotify_view:
            self.spotify_toggle_button.label = "Back to Normal"
            self.spotify_toggle_button.emoji = "⚪"
            self.spotify_toggle_button.style = discord.ButtonStyle.grey
        else:
            self.spotify_toggle_button.label = "Switch to Spotify"
            self.spotify_toggle_button.emoji = "🟢"
            self.spotify_toggle_button.style = discord.ButtonStyle.success

    @discord.ui.button(label="Switch to Spotify", emoji="🟢", style=discord.ButtonStyle.success, row=0)
    async def spotify_toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        state = get_state(guild.id)
        channel = state.text_channel or interaction.channel
        state.text_channel = channel

        await interaction.response.defer()

        if state.spotify_view:
            state.spotify_view = False
            state.spotify_tracks_cache = None
            await render_panel(guild)
            message = await channel.send("🔄 Switched back to normal view.")
            await message.delete(delay=5)
            return

        tracks = await fetch_spotify_playlist_tracks_async(SPOTIFY_PLAYLIST_URL) if SPOTIFY_PLAYLIST_URL else []
        state.spotify_view = True
        state.spotify_tracks_cache = tracks
        await render_panel(guild)

    @discord.ui.button(label="Prev", emoji="⏮️", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)

        if state.current_index <= 0:
            await interaction.response.send_message(
                "❌ لا توجد أغنية سابقة للرجوع إليها! | No previous song in history.",
                ephemeral=True,
            )
            return

        channel = state.text_channel or interaction.channel
        await interaction.response.defer()
        await perform_previous(interaction.guild, channel)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=1)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)

        if vc is None:
            await interaction.response.defer()
            return

        if vc.is_playing():
            vc.pause()
            mark_paused(state)
            button.label = "Resume"
            button.emoji = "▶️"
        elif vc.is_paused():
            vc.resume()
            mark_resumed(state)
            button.label = "Pause"
            button.emoji = "⏸️"
        else:
            await interaction.response.defer()
            return

        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            # The panel message can be deleted/reposted (auto-repost cycle) between the click
            # and this response; the pause/resume itself already succeeded above regardless.
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            await render_panel(interaction.guild)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=1)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        channel = state.text_channel or interaction.channel
        await interaction.response.defer()
        await perform_skip(interaction.guild, channel)

    @discord.ui.button(label="Search", emoji="🔍", style=discord.ButtonStyle.grey, row=0)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SongSearchModal())

    @discord.ui.button(label="Loop: OFF", emoji="🔂", style=discord.ButtonStyle.grey, row=4)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        state.loop = not state.loop
        await interaction.response.defer()
        await render_panel(interaction.guild)

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.grey, row=4)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)

        if state.spotify_view:
            await interaction.response.send_message(
                "🔒 Spotify songs can only be added or removed directly from your Spotify app!",
                ephemeral=True,
            )
            return

        view = discord.ui.View(timeout=60)
        view.add_item(RemoveSelect(interaction.guild.id))
        await interaction.response.send_message(
            "Select a song to remove from the queue:", view=view, ephemeral=True
        )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot is online and ready.")


@bot.after_invoke
async def cleanup_command_message(ctx: commands.Context) -> None:
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    if member.bot:
        return

    guild = member.guild
    vc = guild.voice_client
    if vc is None:
        return

    state = get_state(guild.id)
    human_members = [m for m in vc.channel.members if not m.bot]

    if human_members:
        if state.disconnect_task is not None and not state.disconnect_task.done():
            state.disconnect_task.cancel()
            state.disconnect_task = None
    else:
        if state.disconnect_task is None or state.disconnect_task.done():
            state.disconnect_task = bot.loop.create_task(auto_disconnect(guild))


@bot.command(name="join")
async def join(ctx: commands.Context):
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await send_temp(ctx, "You need to be in a voice channel first.")
        return

    channel = ctx.author.voice.channel

    if ctx.voice_client is None:
        await channel.connect()
    else:
        await ctx.voice_client.move_to(channel)

    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    added = load_library_into_session(state, ctx.author)

    message = f"Joined **{channel.name}**."
    if added:
        message += f" Loaded **{added}** song(s) from your library into the queue."
    await send_temp(ctx, message)
    await render_panel(ctx.guild, ctx.channel)


@bot.command(name="play")
async def play(ctx: commands.Context, *, query: str):
    if ctx.voice_client is None:
        if ctx.author.voice is None:
            await send_temp(ctx, "You need to be in a voice channel first.")
            return
        await ctx.author.voice.channel.connect()

    await play_query(ctx.guild, ctx.channel, ctx.author, query)


async def play_query(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    member: discord.Member,
    query: str,
    *,
    priority: bool = False,
) -> None:
    state = get_state(guild.id)
    state.text_channel = channel

    if is_spotify_track_url(query):
        resolved_query = await resolve_spotify_track_query(query)
        if not resolved_query:
            message = await channel.send("Could not read that Spotify link. Try pasting the song name instead.")
            await message.delete(delay=5)
            return
        query = resolved_query

    if is_playlist_url(query):
        await handle_playlist_play(guild, channel, member, query)
        return

    voice_client = guild.voice_client
    shortcut_url = playlist_data["favorites"].get(query.strip().lower())
    lookup = shortcut_url if shortcut_url else query

    async with channel.typing():
        try:
            song = await extract_song(lookup, member)
        except Exception as exc:
            message = await channel.send(f"Could not find or load that track: {exc}")
            await message.delete(delay=5)
            return

    duplicate_url = find_duplicate_library_entry(song.title)

    if duplicate_url:
        message = await channel.send(
            f"🔁 Song already in playlist! Jumping directly to playing: **{song.title}**"
        )
        await message.delete(delay=5)

        existing_index = next(
            (i for i, s in enumerate(state.session_songs) if s.webpage_url == duplicate_url), None
        )
        if existing_index is not None:
            await perform_jump(guild, channel, existing_index)
            return

        state.session_songs.append(song)
        new_index = len(state.session_songs) - 1
        if voice_client.is_playing() or voice_client.is_paused():
            state.manual_transition = True
            voice_client.stop()
        await start_track(guild, channel, new_index)
        return

    await save_song_to_library(song)
    is_active = voice_client.is_playing() or voice_client.is_paused()

    if priority and is_active:
        state.session_songs.insert(state.current_index + 1, song)
        message = await channel.send(f"🔍 **{song.title}** will play next!")
        await message.delete(delay=5)
        await render_panel(guild)
        return

    state.session_songs.append(song)
    new_index = len(state.session_songs) - 1

    if is_active:
        message = await channel.send(f"Added **{song.title}** to the queue!")
        await message.delete(delay=5)
        await render_panel(guild)
        return

    await start_track(guild, channel, new_index)


async def handle_playlist_play(
    guild: discord.Guild, channel: discord.abc.Messageable, member: discord.Member, url: str
) -> None:
    voice_client = guild.voice_client
    state = get_state(guild.id)

    status_message = await channel.send("🔎 Reading YouTube playlist...")

    try:
        urls = await extract_playlist_urls(url)
    except Exception as exc:
        await status_message.edit(content=f"Could not read that playlist: {exc}")
        await status_message.delete(delay=5)
        return

    if not urls:
        await status_message.edit(content="That playlist appears to be empty or unavailable.")
        await status_message.delete(delay=5)
        return

    truncated = len(urls) > MAX_PLAYLIST_SONGS
    if truncated:
        urls = urls[:MAX_PLAYLIST_SONGS]

    await status_message.edit(content=f"🎶 Loading **{len(urls)}** songs from your YouTube playlist...")

    added = 0
    failed = 0
    first_new_index = len(state.session_songs)

    async with channel.typing():
        for video_url in urls:
            try:
                song = await extract_song(video_url, member)
            except Exception as exc:
                failed += 1
                print(f"Failed to load '{video_url}' from playlist: {exc}")
                continue
            await save_song_to_library(song)
            state.session_songs.append(song)
            added += 1

    if added == 0:
        await status_message.edit(content="Could not load any songs from that playlist.")
        await status_message.delete(delay=5)
        return

    summary = f"✅ Added **{added}** song(s) from the playlist to the queue!"
    if failed:
        summary += f" ({failed} failed to load.)"
    if truncated:
        summary += f" (playlist truncated to the first {MAX_PLAYLIST_SONGS} songs.)"
    await status_message.edit(content=summary)
    await status_message.delete(delay=5)

    if voice_client.is_playing() or voice_client.is_paused():
        await render_panel(guild)
    else:
        await start_track(guild, channel, first_new_index)


@bot.command(name="skip")
async def skip(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    channel = state.text_channel or ctx.channel
    await send_temp(ctx, await perform_skip(ctx.guild, channel))


@bot.command(name="queue", aliases=["q"])
async def queue_cmd(ctx: commands.Context):
    await send_temp(ctx, embed=build_queue_embed(ctx.guild.id))


@bot.command(name="clear")
async def clear(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    state.session_songs = state.session_songs[: state.current_index + 1]
    await render_panel(ctx.guild)
    await send_temp(ctx, "Queue cleared.")


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    voice_client = ctx.voice_client

    if voice_client is None or not voice_client.is_playing():
        await send_temp(ctx, "Nothing is playing right now.")
        return

    voice_client.pause()
    mark_paused(get_state(ctx.guild.id))
    await render_panel(ctx.guild)
    await send_temp(ctx, "Paused.")


@bot.command(name="resume")
async def resume(ctx: commands.Context):
    voice_client = ctx.voice_client

    if voice_client is None or not voice_client.is_paused():
        await send_temp(ctx, "Nothing is paused right now.")
        return

    voice_client.resume()
    mark_resumed(get_state(ctx.guild.id))
    await render_panel(ctx.guild)
    await send_temp(ctx, "Resumed.")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    await send_temp(ctx, await perform_stop(ctx.guild))


@bot.command(name="volume")
async def volume(ctx: commands.Context, level: int):
    if not 1 <= level <= 100:
        await send_temp(ctx, "Volume must be a number between 1 and 100.")
        return

    state = get_state(ctx.guild.id)
    state.volume = level / 100

    vc = ctx.voice_client
    if vc is not None and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = state.volume

    await send_temp(ctx, f"Volume set to {level}%.")


@bot.command(name="loop")
async def loop_cmd(ctx: commands.Context):
    state = get_state(ctx.guild.id)
    state.loop = not state.loop
    await render_panel(ctx.guild)
    await send_temp(ctx, f"Loop is now {'enabled 🔁' if state.loop else 'disabled'} for the current track.")


@bot.command(name="saveplaylist", aliases=["sp"])
async def saveplaylist(ctx: commands.Context, *, name: str):
    name = name.strip().lower()
    if not name:
        await send_temp(ctx, "Please provide a playlist name: `!saveplaylist <name>`.")
        return

    state = get_state(ctx.guild.id)
    upcoming = get_upcoming_songs(state)
    if not upcoming:
        await send_temp(ctx, "The queue is empty — there's nothing to save.")
        return

    urls = [song.webpage_url for song in upcoming]
    playlist_data["playlists"][name] = urls
    await db.upsert_playlist(name, urls)

    await send_temp(ctx, f"Saved **{len(urls)}** song(s) to playlist **{name}**.")


@bot.command(name="loadplaylist", aliases=["lp"])
async def loadplaylist(ctx: commands.Context, *, name: str):
    name = name.strip().lower()
    urls = playlist_data["playlists"].get(name)

    if not urls:
        await send_temp(ctx, f"No playlist named **{name}** was found. Use `!playlists` to see saved playlists.")
        return

    if ctx.voice_client is None:
        if ctx.author.voice is None:
            await send_temp(ctx, "You need to be in a voice channel first.")
            return
        await ctx.author.voice.channel.connect()

    voice_client = ctx.voice_client
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel

    status_message = await ctx.send(f"Loading **{len(urls)}** song(s) from playlist **{name}**...")

    added = 0
    failed = 0
    first_new_index = len(state.session_songs)
    async with ctx.typing():
        for url in urls:
            try:
                song = await extract_song(url, ctx.author)
            except Exception as exc:
                failed += 1
                print(f"Failed to load '{url}' from playlist '{name}': {exc}")
                continue
            await save_song_to_library(song)
            state.session_songs.append(song)
            added += 1

    if added == 0:
        await status_message.edit(content="Could not load any songs from that playlist.")
        await status_message.delete(delay=5)
        return

    summary = f"Added **{added}** song(s) from **{name}** to the queue."
    if failed:
        summary += f" ({failed} failed to load.)"
    await status_message.edit(content=summary)
    await status_message.delete(delay=5)

    if voice_client.is_playing() or voice_client.is_paused():
        await render_panel(ctx.guild)
    else:
        await start_track(ctx.guild, ctx.channel, first_new_index)


@bot.command(name="fav")
async def fav(ctx: commands.Context, url: str, *, shortcut_name: str):
    shortcut_name = shortcut_name.strip().lower()

    if not shortcut_name:
        await send_temp(ctx, "Please provide a shortcut name: `!fav <YouTube_URL> <shortcut_name>`.")
        return

    if not (url.startswith("http://") or url.startswith("https://")):
        await send_temp(ctx, "Please provide a valid YouTube URL as the first argument.")
        return

    playlist_data["favorites"][shortcut_name] = url
    await db.upsert_favorite(shortcut_name, url)

    await send_temp(ctx, f"Saved shortcut **{shortcut_name}** → {url}")


@bot.command(name="playlists")
async def playlists_cmd(ctx: commands.Context):
    playlists = playlist_data["playlists"]
    favorites = playlist_data["favorites"]

    if not playlists and not favorites:
        await send_temp(ctx, "No playlists or shortcuts have been saved yet.")
        return

    embed = discord.Embed(title="Saved Playlists & Shortcuts", color=discord.Color.blurple())

    if playlists:
        lines = [f"**{name}** — {len(urls)} song(s)" for name, urls in playlists.items()]
        embed.add_field(name="Playlists", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Playlists", value="None saved.", inline=False)

    if favorites:
        lines = [f"**{name}** — {url}" for name, url in favorites.items()]
        embed.add_field(name="Song Shortcuts", value="\n".join(lines)[:1024], inline=False)
    else:
        embed.add_field(name="Song Shortcuts", value="None saved.", inline=False)

    await send_temp(ctx, embed=embed)


@bot.command(name="delplaylist")
async def delplaylist(ctx: commands.Context, *, name: str):
    name = name.strip().lower()
    removed = False

    if name in playlist_data["playlists"]:
        del playlist_data["playlists"][name]
        await db.delete_playlist(name)
        removed = True

    if name in playlist_data["favorites"]:
        del playlist_data["favorites"][name]
        await db.delete_favorite(name)
        removed = True

    if not removed:
        await send_temp(ctx, f"No playlist or shortcut named **{name}** was found.")
        return

    await send_temp(ctx, f"Deleted **{name}**.")


async def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN not found. Set it in the .env file next to bot.py."
        )
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL not found. Set it in the .env file (or your host's environment "
            "variables) to your Supabase Postgres connection string."
        )

    await db.init_pool(DATABASE_URL)
    await db.ensure_schema()
    await initialize_data()

    try:
        async with bot:
            await bot.start(TOKEN)
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
