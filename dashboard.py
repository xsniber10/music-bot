"""
Bot Core Management System — local control panel for the whole bot fleet.

Run with the project's venv:
    venv\\Scripts\\python.exe dashboard.py
(or venv\\Scripts\\pythonw.exe dashboard.py to launch with no console window)

Every service it controls — which processes to look for, which venv/script
starts them, which Scheduled Task owns them, where their log lives — comes
from bots_config.json via bot_registry.load_registry(). Nothing about a
specific bot is hardcoded here or in service_controller.py; adding a new
service to the fleet means adding one entry to that JSON file.

Also talks to the Music Bot's own Supabase database (via database.py) and
edits its .env file — those two pages remain specific to this project, since
they're this bot's own admin surface rather than fleet-wide process control.
"""

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox
from urllib.parse import urlparse

import customtkinter as ctk

import bot_registry
import database as db
import service_controller
from bot_registry import BotConfig

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "bot_output.log"
COOKIES_PATH = BASE_DIR / "cookies.txt"
AUDIT_LOG_PATH = BASE_DIR / "admin_audit_log.json"
AUDIT_LOG_MAX_ENTRIES = 1000

ACCENT = "#7c5cff"
ACCENT_HOVER = "#6a4ce0"
CARD_BG = "#1c1c2b"
SIDEBAR_BG = "#14141f"
SUCCESS = "#50fa7b"
WARNING = "#ffb86c"
ERROR = "#ff5566"
MUTED = "#9a9ab0"

# Extends the palette above for the Music tab's premium row-card design — same family,
# not a separate theme. ROW_* give each song its own subtly-raised card against
# CARD_BG's section background; BADGE_* are source pills, deliberately kept out of the
# red family since red is reserved for destructive actions (Delete/Wipe) everywhere
# else in this app.
ROW_BG = "#242438"
ROW_BG_HOVER = "#2e2e48"
BORDER = "#2f2f45"
BADGE_YOUTUBE = "#3b82f6"
BADGE_SPOTIFY = "#1db954"
BADGE_UNKNOWN = "#4b4b63"
DANGER_HOVER = "#5c1111"

# Rank-tier accents for the Leaderboard Viewer's top 3 — CTk has no real glow/blur, so
# this is a colored border + a subtly tinted card background per medal, which is the
# honest equivalent of "glowing" achievable in this toolkit.
RANK_GOLD = "#f5c518"
RANK_GOLD_BG = "#2b2410"
RANK_SILVER = "#d5d8dc"
RANK_SILVER_BG = "#24262b"
RANK_BRONZE = "#d98a4a"
RANK_BRONZE_BG = "#2b1f14"

PREMIUM_CARD_RADIUS = 12

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

ERROR_LOG_PATH = BASE_DIR / "dashboard_errors.log"


def _log_background_exception(args) -> None:
    """Every page's data fetch runs on a background thread, and this app is
    normally launched with pythonw.exe (no console) — so an unhandled
    exception there would otherwise vanish with zero visible trace. This
    logs it to a file instead, so a stuck "Loading..." label always has
    something to check."""
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now().isoformat()} | thread {args.thread.name} ---\n")
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=f)
    except OSError:
        pass


threading.excepthook = _log_background_exception


# --------------------------------------------------------------------------------
# .env helpers (Music Bot's own config — not part of the fleet registry)
# --------------------------------------------------------------------------------


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]  # matches python-dotenv's handling of quoted values
            values[key.strip()] = value
    return values


def write_env_values(updates: dict[str, str]) -> None:
    """Rewrites .env, updating only the given keys in place and preserving
    everything else (order, untouched keys, comments). Appends any key that
    doesn't already exist in the file."""
    lines = []
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    remaining = dict(updates)
    new_lines = []
    for line in lines:
        stripped = line.rstrip("\n")
        if "=" in stripped and not stripped.strip().startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}\n")
                continue
        new_lines.append(line if line.endswith("\n") else line + "\n")

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# --------------------------------------------------------------------------------
# Leaderboard Bot's Supabase data (read-only "profiles" table — see the schema
# doc comment at the top of oasis/bot.py). Queried directly over Supabase's
# PostgREST HTTP API with stdlib urllib rather than pulling in the supabase-py
# client as a dependency just for one read-only GET.
# --------------------------------------------------------------------------------


def _leaderboard_supabase_credentials(env_path: Path) -> tuple[str, str]:
    env = read_env_file(env_path)
    url = env.get("SUPABASE_URL")
    key = env.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(f"SUPABASE_URL / SUPABASE_KEY not set in {env_path}")
    return url.rstrip("/"), key


def fetch_leaderboard(env_path: Path) -> list[dict]:
    """Fetches every row of the `profiles` table from the Supabase project
    described by env_path's SUPABASE_URL/SUPABASE_KEY, ordered by total_xp
    descending — the same table and ordering oasis/bot.py's own
    get_sorted_leaderboard() uses. Global across every Discord server the
    bot serves, since `profiles`' primary key is (user_id, guild_id) rather
    than this being scoped to one guild."""
    url, key = _leaderboard_supabase_credentials(env_path)

    endpoint = f"{url}/rest/v1/profiles?select=user_id,guild_id,total_xp,level&order=total_xp.desc"
    request = urllib.request.Request(endpoint, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(_missing_table_hint(body_text) or f"Supabase returned {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Supabase: {exc.reason}") from exc


def compute_level_and_xp(total_xp: int) -> tuple[int, int]:
    """Derives (level, xp) from a total_xp value, replicating oasis/bot.py's leveling
    formula exactly (xp_needed_for_level(level) = level * 100) — so XP edits made here
    stay mathematically consistent with what the live bot displays in Discord. Computed
    fresh from total_xp rather than incrementally, so it's correct for decreases too
    (the bot's own add_xp only ever increments, so it never had to handle that)."""
    total_xp = max(0, total_xp)
    level = 1
    remaining = total_xp
    while remaining >= level * 100:
        remaining -= level * 100
        level += 1
    return level, remaining


def _patch_member_profile(env_path: Path, guild_id: int, user_id: int, fields: dict) -> None:
    url, key = _leaderboard_supabase_credentials(env_path)
    endpoint = f"{url}/rest/v1/profiles?user_id=eq.{user_id}&guild_id=eq.{guild_id}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(fields).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(_missing_table_hint(body_text) or f"Supabase returned {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Supabase: {exc.reason}") from exc


def adjust_member_xp(env_path: Path, guild_id: int, user_id: int, delta: int) -> dict:
    """Applies delta to one member's total_xp (fetched fresh first, not from a
    possibly-stale in-memory row) and recomputes level/xp to match via
    compute_level_and_xp, then writes all three fields back in one PATCH. Positive
    delta is "Add XP", negative is "Remove XP" — same function either way. Returns the
    new {xp, level, total_xp}."""
    url, key = _leaderboard_supabase_credentials(env_path)
    get_endpoint = f"{url}/rest/v1/profiles?user_id=eq.{user_id}&guild_id=eq.{guild_id}&select=total_xp"
    request = urllib.request.Request(get_endpoint, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(_missing_table_hint(body_text) or f"Supabase returned {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Supabase: {exc.reason}") from exc

    if not rows:
        raise RuntimeError("Member profile not found — they may have left the server.")

    new_total_xp = max(0, rows[0]["total_xp"] + delta)
    level, xp = compute_level_and_xp(new_total_xp)
    _patch_member_profile(env_path, guild_id, user_id, {"xp": xp, "level": level, "total_xp": new_total_xp})
    return {"xp": xp, "level": level, "total_xp": new_total_xp}


def reset_member_xp(env_path: Path, guild_id: int, user_id: int) -> None:
    """Resets one member's XP/level/voice-time to 0, mirroring the fields oasis/bot.py's
    own (guild-wide) reset_all_xp() touches — just scoped to a single member here."""
    _patch_member_profile(env_path, guild_id, user_id, {"xp": 0, "level": 1, "total_xp": 0, "voice_minutes": 0})


# --------------------------------------------------------------------------------
# Admin audit log (local JSON, mirroring bot.py's own _load_json/_save_json idiom
# for its leaderboard_panels.json/xp_config.json — this is dashboard-local UI
# state, not member data, so it doesn't belong in Supabase).
# --------------------------------------------------------------------------------


def load_audit_log() -> list[dict]:
    if not AUDIT_LOG_PATH.exists():
        return []
    try:
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def log_admin_action(
    action: str, guild_id: int, user_id: int, display_name: str, amount: int | None, resulting_total_xp: int | None
) -> None:
    """Appends one entry and trims to the most recent AUDIT_LOG_MAX_ENTRIES, so
    the file can't grow unbounded over the life of the dashboard. Called from
    inside the admin-action worker thread only after the Supabase call already
    succeeded, so a failed Add/Remove/Reset never shows up here as if it happened."""
    entries = load_audit_log()
    entries.append(
        {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "guild_id": guild_id,
            "user_id": user_id,
            "display_name": display_name,
            "amount": amount,
            "resulting_total_xp": resulting_total_xp,
        }
    )
    entries = entries[-AUDIT_LOG_MAX_ENTRIES:]
    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def format_audit_timestamp(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    time_part = f"{dt.strftime('%I').lstrip('0') or '12'}:{dt.strftime('%M %p')}"
    if dt.date() == datetime.now().date():
        return time_part
    return f"{dt.strftime('%b %d')}, {time_part}"


def masked_audit_target(entry: dict, streamer_mode: bool) -> str:
    """The stored display_name is only a raw-ID giveaway (e.g. "User 123456")
    when the leaderboard couldn't resolve a real Discord nickname at the time
    of the action — a resolved nickname isn't the "raw ID" the Streamer Mode
    toggle is meant to hide, so only the fallback form gets masked."""
    user_id = entry.get("user_id")
    name = entry.get("display_name") or f"User {user_id}"
    if streamer_mode and name == f"User {user_id}":
        return "User ••••••"
    return name


DISCORD_API_BASE = "https://discord.com/api/v10"


def _fetch_discord_guild_members(token: str, guild_id: int) -> dict[int, str]:
    """Fetches every member of one guild via Discord's REST API — not the
    gateway, so this is a stateless request/response call that can't collide
    with the live bot's own gateway session. Requires the SERVER MEMBERS
    privileged intent to be enabled for the bot application (oasis/bot.py's
    setup docs already require this); if it isn't, Discord returns 403 and
    that propagates up as a RuntimeError.

    Returns {user_id: display_name}, using the same nick > global_name >
    username fallback discord.py's own Member.display_name uses (see
    oasis/bot.py's clean_display_name)."""
    names: dict[int, str] = {}
    after = "0"
    # Discord's API sits behind Cloudflare, which blocks urllib's default
    # "Python-urllib/x.y" User-Agent as a bot-fingerprint match (HTTP 403,
    # Cloudflare error 1010) — a proper one is required, in the format
    # Discord's own API docs recommend for bot clients.
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/discord/discord-api-docs, 1.0) BotCoreManagementSystem",
    }
    for _ in range(10):  # 10 x 1000 = up to 10,000 members; plenty for a personal server
        endpoint = f"{DISCORD_API_BASE}/guilds/{guild_id}/members?limit=1000&after={after}"
        request = urllib.request.Request(endpoint, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                page = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Discord returned {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach Discord: {exc.reason}") from exc

        if not page:
            break
        for entry in page:
            user = entry.get("user") or {}
            user_id = user.get("id")
            if user_id is None:
                continue
            names[int(user_id)] = entry.get("nick") or user.get("global_name") or user.get("username") or user_id
        if len(page) < 1000:
            break
        after = max(entry["user"]["id"] for entry in page if entry.get("user", {}).get("id"))

    return names


def resolve_leaderboard_usernames(env_path: Path, rows: list[dict]) -> dict[tuple[int, int], str]:
    """Best-effort username resolution for fetch_leaderboard()'s rows, keyed
    by (guild_id, user_id) since the same person can have a different
    nickname in each server the bot serves. Fetches each distinct guild's
    member list once (one REST call per guild, not one per row) rather than
    looking up members individually."""
    env = read_env_file(env_path)
    token = env.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(f"DISCORD_TOKEN not set in {env_path}")

    names: dict[tuple[int, int], str] = {}
    guild_ids = {row["guild_id"] for row in rows if row.get("guild_id") is not None}
    for guild_id in guild_ids:
        for user_id, display_name in _fetch_discord_guild_members(token, guild_id).items():
            names[(guild_id, user_id)] = display_name
    return names


def _discord_api_request(env_path: Path, method: str, path: str, json_body: dict | None = None):
    """Generalizes the GET-only pattern above (see _fetch_discord_guild_members)
    to support writes — the first time dashboard.py has ever needed to WRITE
    to Discord's API, needed for ticket-close (DELETE a channel). Same
    Cloudflare-required User-Agent, same error handling."""
    env = read_env_file(env_path)
    token = env.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(f"DISCORD_TOKEN not set in {env_path}")

    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot (https://github.com/discord/discord-api-docs, 1.0) BotCoreManagementSystem",
    }
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(f"{DISCORD_API_BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Discord returned {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Discord: {exc.reason}") from exc


_guild_list_cache: dict[Path, list[dict]] = {}
_guild_list_cache_lock = threading.Lock()


def fetch_bot_guilds(env_path: Path) -> list[dict]:
    """GET /users/@me/guilds — every guild this bot token is currently a
    member of. Used to populate the guild picker on the Shop/Tickets/Welcome/
    Social Feeds tabs.

    Cached per env_path for the life of the process: all four of those tabs
    are constructed together in Dashboard.__init__ and each independently
    calls this on load, which without caching means up to 4 near-simultaneous
    identical Discord API calls on the same bot token every time the
    dashboard launches — confirmed during testing to be enough to trip
    Discord's rate limit (HTTP 429) on its own. Guild membership essentially
    never changes within one dashboard session, so there's nothing to
    invalidate this cache for.

    The lock is held across the network call itself, not just the cache
    read/write — a plain "check then fetch then store" without it still lets
    all 4 background threads see an empty cache and all fire their own
    request before any of them finishes (confirmed: that's exactly what
    still 429'd even with the dict alone). Holding the lock serializes them:
    the first thread in actually fetches, the other three block until it's
    done and then just read the now-populated cache."""
    with _guild_list_cache_lock:
        if env_path in _guild_list_cache:
            return _guild_list_cache[env_path]
        guilds = _discord_api_request(env_path, "GET", "/users/@me/guilds") or []
        _guild_list_cache[env_path] = guilds
        return guilds


# --------------------------------------------------------------------------------
# Shop / Tickets / Welcome / Social Feeds config (Supabase)
#
# All five tables (guild_config, shop_items, shop_purchases, tickets,
# social_feeds) are administered exclusively from the tabs below — bot.py
# only ever reads them (see oasis/bot.py's own matching section) and inserts
# the rows that a live Discord action produces (a purchase, a new ticket).
# --------------------------------------------------------------------------------


def _missing_table_hint(error_body: str) -> str | None:
    """PostgREST's "table not found in schema cache" error (code PGRST205 —
    what you get if a table from bot.py's schema was never actually created
    in this Supabase project, e.g. the setup SQL wasn't run yet) is a raw
    JSON blob that's meaningless to anyone who isn't reading PostgREST's own
    docs. Detects it and pulls the table name out, so callers can show an
    actionable message instead. Returns None for any other error, which the
    caller falls back to showing as-is.

    Checked as a plain substring rather than requiring error_body to parse
    as strict JSON first: PostgREST's real response is valid double-quoted
    JSON, but by the time this string has been read from an HTTPError, wrapped
    in another exception, and possibly re-displayed, there's no guarantee
    something upstream hasn't already normalized the quoting — matching on
    the PGRST205 code and a quote-agnostic table-name pattern is robust to
    that either way, where a strict json.loads(...).get("code") check would
    silently stop matching if the format ever drifts even slightly."""
    if "PGRST205" not in error_body:
        return None
    match = re.search(r"public\.(\w+)", error_body)
    table_name = match.group(1) if match else "one of the"
    return (
        f'The "{table_name}" table doesn\'t exist in this Supabase project yet. This is a one-time setup '
        f"step, not a bug: run the SQL from oasis/bot.py's module docstring (SUPABASE_SCHEMA_SQL near the "
        f"top of the file) once in your Supabase project's SQL Editor, then try again."
    )


def _supabase_request(env_path: Path, method: str, path: str, json_body=None, upsert: bool = False):
    """Generic PostgREST call, generalizing the GET/PATCH pattern
    fetch_leaderboard/_patch_member_profile use above into one place for
    every table's CRUD below, instead of re-deriving the same urllib/error
    boilerplate a dozen more times. `path` is everything after `/rest/v1/`,
    e.g. "guild_config?guild_id=eq.123". Returns the parsed JSON body, or
    None for an empty response."""
    url, key = _leaderboard_supabase_credentials(env_path)
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=representation" + (",resolution=merge-duplicates" if upsert else "")
    request = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        hint = _missing_table_hint(body_text)
        raise RuntimeError(hint or f"Supabase returned {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Supabase: {exc.reason}") from exc


DEFAULT_GUILD_CONFIG = {
    "welcome_channel_id": None,
    "welcome_message": None,
    "verified_role_id": None,
    "ticket_channel_id": None,
    "ticket_staff_role_id": None,
    "ticket_category_id": None,
    "shop_channel_id": None,
}


def fetch_guild_config(env_path: Path, guild_id: int) -> dict:
    rows = _supabase_request(env_path, "GET", f"guild_config?guild_id=eq.{guild_id}")
    if rows:
        return rows[0]
    return {"guild_id": guild_id, **DEFAULT_GUILD_CONFIG}


def save_guild_config(env_path: Path, guild_id: int, fields: dict) -> None:
    """Upserts against the guild_id primary key — works whether or not a row
    already exists for this guild, so callers never need to check first."""
    _supabase_request(env_path, "POST", "guild_config?on_conflict=guild_id", json_body={"guild_id": guild_id, **fields}, upsert=True)


def list_shop_items(env_path: Path, guild_id: int) -> list[dict]:
    return _supabase_request(env_path, "GET", f"shop_items?guild_id=eq.{guild_id}&order=xp_cost.asc") or []


def add_shop_item(env_path: Path, guild_id: int, role_id: int, xp_cost: int, label: str) -> None:
    _supabase_request(
        env_path, "POST", "shop_items",
        json_body={"guild_id": guild_id, "role_id": role_id, "xp_cost": xp_cost, "label": label},
    )


def delete_shop_item(env_path: Path, item_id: int) -> None:
    _supabase_request(env_path, "DELETE", f"shop_items?id=eq.{item_id}")


def list_open_tickets(env_path: Path, guild_id: int) -> list[dict]:
    return _supabase_request(env_path, "GET", f"tickets?guild_id=eq.{guild_id}&status=eq.open&order=created_at.desc") or []


def close_ticket_record(env_path: Path, ticket_id: int) -> None:
    """Marks the Supabase row closed — the caller is responsible for actually
    deleting the Discord channel first via _discord_api_request, so a channel
    that fails to delete doesn't get silently marked closed anyway."""
    _supabase_request(
        env_path, "PATCH", f"tickets?id=eq.{ticket_id}",
        json_body={"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()},
    )


def list_social_feeds(env_path: Path, guild_id: int) -> list[dict]:
    return _supabase_request(env_path, "GET", f"social_feeds?guild_id=eq.{guild_id}&order=created_at.desc") or []


def add_social_feed(env_path: Path, guild_id: int, handle: str, channel_id: int, ping_style: str) -> None:
    _supabase_request(
        env_path, "POST", "social_feeds",
        json_body={
            "guild_id": guild_id,
            "platform": "tiktok",
            "handle": handle,
            "channel_id": channel_id,
            "ping_style": ping_style,
            "enabled": True,
        },
    )


def delete_social_feed(env_path: Path, feed_id: int) -> None:
    _supabase_request(env_path, "DELETE", f"social_feeds?id=eq.{feed_id}")


def set_social_feed_enabled(env_path: Path, feed_id: int, enabled: bool) -> None:
    _supabase_request(env_path, "PATCH", f"social_feeds?id=eq.{feed_id}", json_body={"enabled": enabled})


# --------------------------------------------------------------------------------
# Cache / cookies maintenance (Music Bot specific)
# --------------------------------------------------------------------------------


def clear_ytdlp_cache() -> str:
    python_exe = BASE_DIR / "venv" / "Scripts" / "python.exe"
    result = subprocess.run(
        [str(python_exe), "-m", "yt_dlp", "--rm-cache-dir"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return (result.stdout.strip() or result.stderr.strip() or "Cache cleared.")


def replace_cookies_file(source_path: str) -> None:
    shutil.copyfile(source_path, COOKIES_PATH)


def describe_cookies() -> str:
    if not COOKIES_PATH.exists():
        return "cookies.txt not found."
    data = COOKIES_PATH.read_bytes()
    return f"{len(data)} bytes — sha256={hashlib.sha256(data).hexdigest()[:12]}"


# --------------------------------------------------------------------------------
# Log tailing
# --------------------------------------------------------------------------------


def read_log_tail(log_path: Path, max_bytes: int = 20_000) -> str:
    if not log_path.exists():
        return f"({log_path.name} not found yet)\n"
    size = log_path.stat().st_size
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # drop the partial first line
        return f.read()


class LogTailer(threading.Thread):
    def __init__(self, out_queue: list, log_path: Path):
        super().__init__(daemon=True)
        self.out_queue = out_queue
        self.log_path = log_path
        self._stop_event = threading.Event()
        self._pos = log_path.stat().st_size if log_path.exists() else 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.log_path.exists():
                    size = self.log_path.stat().st_size
                    if size < self._pos:
                        self._pos = 0  # log was rotated/truncated
                    if size > self._pos:
                        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(self._pos)
                            new_text = f.read()
                            self._pos = f.tell()
                        self.out_queue.append(new_text)
            except OSError:
                pass
            self._stop_event.wait(1.0)

    def stop(self) -> None:
        self._stop_event.set()


# --------------------------------------------------------------------------------
# Async bridge (Tkinter's mainloop is synchronous; asyncpg needs an event loop)
# --------------------------------------------------------------------------------


def run_async_task(app: "Dashboard", coro_factory, on_success=None, on_error=None) -> None:
    def worker():
        try:
            result = asyncio.run(coro_factory())
        except Exception as exc:  # noqa: BLE001 - surfacing any DB error to the UI
            # `except ... as exc` unbinds exc at the end of this block, so it must be
            # captured into a plain variable before the deferred lambda can close over it.
            error = exc
            if on_error:
                app.after(0, lambda: on_error(error))
            return
        if on_success:
            app.after(0, lambda: on_success(result))

    threading.Thread(target=worker, daemon=True).start()


async def _connect_db() -> None:
    env = read_env_file()
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in .env")
    await db.init_pool(database_url)
    await db.ensure_schema()


DB_RETRY_ATTEMPTS = 3
DB_RETRY_DELAY_SECONDS = 1.5
DB_OPERATION_TIMEOUT_SECONDS = 12

# database.py keeps its connection pool in one module-level global (_pool), written by
# init_pool() and read by every query/close_pool() call — there's no per-call isolation.
# Every DB-touching page function here runs on its own background thread (see
# run_async_task), each with its own asyncio.run() calling init_pool()/close_pool()
# independently. Two of those overlapping (e.g. MusicPage.__init__ firing
# refresh_counts() and refresh_playlists() at nearly the same moment on startup) is a
# genuine, reproducible race: one thread's init_pool() silently reassigns the global out
# from under a query the other thread already started, and/or one thread's close_pool()
# closes the pool the other is still mid-query on — exactly "connection was closed in
# the middle of operation." Confirmed by force: two concurrent calls to the raw
# operations (bypassing this lock) hung indefinitely rather than failing cleanly. This
# lock serializes every Supabase operation across the whole app so that race can't
# happen at all, regardless of which pages happen to refresh at the same time.
_DB_LOCK = threading.Lock()


async def _with_db_retries(operation, description: str):
    """Runs one Supabase operation (which creates and closes its own connection pool —
    see _connect_db) with a few retries on transient failures: Supabase's Supavisor
    pooler closing a connection mid-operation ("connection was closed in the middle of
    operation"), a query timeout, or a one-off network blip.

    Each attempt is also wrapped in asyncio.wait_for: a half-dead connection can block
    on a read that never completes rather than raising immediately, which is what left
    the UI stuck on "Loading..." forever — nothing had failed yet for run_async_task's
    except block to catch. wait_for's timeout forces that attempt to actually end (by
    cancelling it) so the retry loop below can act on it like any other failure.

    The whole attempt loop runs under _DB_LOCK — see its comment for why that matters
    more than the retries themselves for the "only fails on startup" symptom.

    Every operation this wraps is either a read or an idempotent upsert/truncate, so
    retrying is always safe. Raises the last error once every attempt is exhausted, for
    the caller to surface to the UI."""
    with _DB_LOCK:
        return await _with_db_retries_locked(operation, description)


async def _with_db_retries_locked(operation, description: str):
    last_exc: Exception | None = None
    for attempt in range(1, DB_RETRY_ATTEMPTS + 1):
        try:
            return await asyncio.wait_for(operation(), timeout=DB_OPERATION_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - any connection/timeout flavor should retry
            last_exc = exc
            print(f"[db] {description}: attempt {attempt}/{DB_RETRY_ATTEMPTS} failed: {exc!r}")
            if attempt < DB_RETRY_ATTEMPTS:
                await asyncio.sleep(DB_RETRY_DELAY_SECONDS)
    raise last_exc


async def _fetch_song_counts_once() -> dict[str, int]:
    await _connect_db()
    try:
        return await db.get_song_counts()
    finally:
        await db.close_pool()


async def fetch_song_counts() -> dict[str, int]:
    return await _with_db_retries(_fetch_song_counts_once, "fetch_song_counts")


async def _wipe_all_songs_once() -> None:
    await _connect_db()
    try:
        await db.wipe_music_data()
    finally:
        await db.close_pool()


async def wipe_all_songs() -> None:
    await _with_db_retries(_wipe_all_songs_once, "wipe_all_songs")


# Default playlists always shown in the Music tab's dropdown, auto-created empty if
# they don't exist yet. Lowercase to match bot.py's !saveplaylist/!delplaylist naming
# convention (both .lower() whatever name a user types) and the names configured in
# bot.py's own SPOTIFY_PLAYLIST_SYNCS — a mismatched case would make this dashboard's
# "default" playlist a different row than the one the bot's Spotify-sync pipeline (or a
# user's own !saveplaylist command) actually writes to.
DEFAULT_PLAYLISTS = ["spotify", "general", "lena", "dj"]


async def _fetch_music_data_once() -> dict:
    await _connect_db()
    try:
        data = await db.load_all_data()
        library = data["library"]
        playlists = data["playlists"]
        for name in DEFAULT_PLAYLISTS:
            if name not in playlists:
                await db.upsert_playlist(name, [])
                playlists[name] = []
        return {"library": library, "playlists": playlists}
    finally:
        await db.close_pool()


async def fetch_music_data() -> dict:
    """Returns {"library": {url: {...}}, "playlists": {name: [urls, ...]}} for the
    Music tab's playlist manager. `playlists` is the only Supabase-backed structure
    that has a real, persisted order — the bot's actual live per-guild playback queue
    only exists in bot.py's own process memory and isn't reachable from here.

    Ensures DEFAULT_PLAYLISTS always exist (created empty if missing) so "spotify" and
    "general" are always selectable, even before the bot's Spotify-sync pipeline has run
    or before anything's been manually saved to "general" yet."""
    return await _with_db_retries(_fetch_music_data_once, "fetch_music_data")


async def _save_playlist_order_once(name: str, urls: list[str]) -> None:
    await _connect_db()
    try:
        await db.upsert_playlist(name, urls)
    finally:
        await db.close_pool()


async def save_playlist_order(name: str, urls: list[str]) -> None:
    await _with_db_retries(lambda: _save_playlist_order_once(name, urls), f"save_playlist_order({name!r})")


def format_song_duration(seconds) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def song_source_label(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return "Unknown"
    if "youtube" in netloc or netloc == "youtu.be":
        return "YouTube"
    if "spotify" in netloc:
        return "Spotify"
    return netloc or "Unknown"


def song_source_badge_color(label: str) -> str:
    """Pill background color per source label — kept out of the red family entirely
    (red is reserved for destructive actions throughout this app, e.g. Delete/Wipe)."""
    return {"YouTube": BADGE_YOUTUBE, "Spotify": BADGE_SPOTIFY}.get(label, BADGE_UNKNOWN)


# --------------------------------------------------------------------------------
# Small UI helpers
# --------------------------------------------------------------------------------


class Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _event=None) -> None:
        if self.tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#26263a",
            foreground="#e6e6f0",
            relief="solid",
            borderwidth=1,
            font=("Segoe UI", 9),
            padx=8,
            pady=4,
        ).pack()

    def _hide(self, _event=None) -> None:
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


def add_tooltip(widget, text: str) -> None:
    Tooltip(widget, text)


class LedIndicator(ctk.CTkLabel):
    def __init__(self, master, size: int = 14):
        super().__init__(master, text="", width=size, height=size, corner_radius=size // 2, fg_color="#555566")

    def set_on(self) -> None:
        self.configure(fg_color=SUCCESS)

    def set_off(self) -> None:
        self.configure(fg_color=ERROR)


def bind_hover_card(row: ctk.CTkFrame, widgets: list, normal_color: str, hover_color: str) -> None:
    """Shared hover-highlight behavior for a card-style row: <Enter>/<Leave> on the row
    frame alone only fires when the mouse is over its own bare background, not over any
    child label sitting on top of it, so every widget in the row needs the same binding
    for the whole card to light up as one unit. Used by both the Music and Leaderboard
    tabs' row cards so this logic exists in exactly one place."""

    def on_enter(_event=None):
        row.configure(fg_color=hover_color)

    def on_leave(_event=None):
        row.configure(fg_color=normal_color)

    for widget in [row, *widgets]:
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)


def make_page_header(parent, title: str, subtitle: str | None = None) -> None:
    """Consistent premium header used across every tab: large bold title, optional
    muted subtitle underneath, generous spacing before whatever comes next."""
    header = ctk.CTkFrame(parent, fg_color="transparent")
    header.pack(fill="x", pady=(0, 20))
    ctk.CTkLabel(header, text=title, font=("Segoe UI", 24, "bold")).pack(anchor="w")
    if subtitle:
        ctk.CTkLabel(header, text=subtitle, font=("Segoe UI", 12), text_color=MUTED).pack(anchor="w", pady=(2, 0))


def add_guild_picker(parent, app: "Dashboard", bot_id: str, status_label: ctk.CTkLabel, on_change) -> ctk.CTkOptionMenu:
    """Shared "which server is this tab configuring" dropdown, reused by the
    Shop/Tickets/Welcome/Social Feeds tabs — all four need a guild_id before
    they can read/write anything. Always a real dropdown, even when the bot
    is only in one server (the expected common case): simpler than a
    separate hide/show code path, and it doubles as a "this is the server
    you're configuring" label either way. Fetches the bot's guild list once
    in the background (see fetch_bot_guilds); on_change(env_path, guild_id)
    fires once the list loads, and again on every manual selection change."""
    picker = ctk.CTkOptionMenu(parent, values=["Loading..."], state="disabled", width=240)
    state = {"env_path": None, "guilds_by_name": {}}

    def _select(name: str) -> None:
        guild_id = state["guilds_by_name"].get(name)
        if guild_id is not None and state["env_path"] is not None:
            on_change(state["env_path"], guild_id)

    picker.configure(command=_select)

    def _on_loaded(env_path: Path, guilds: list[dict]) -> None:
        if not guilds:
            status_label.configure(text="⚠️ This bot isn't in any Discord servers.", text_color=ERROR)
            return
        state["env_path"] = env_path
        state["guilds_by_name"] = {g["name"]: int(g["id"]) for g in guilds}
        names = list(state["guilds_by_name"].keys())
        picker.configure(values=names, state="normal" if len(names) > 1 else "disabled")
        picker.set(names[0])
        _select(names[0])

    def worker():
        try:
            bot = next((b for b in app.bots if b.id == bot_id), None)
            if bot is None:
                raise RuntimeError(f'No registered bot with id "{bot_id}" — check bots_config.json.')
            env_path = bot.directory / ".env"
            guilds = fetch_bot_guilds(env_path)
        except Exception as exc:  # noqa: BLE001 - surfacing any fetch error to the UI
            error = exc
            app.after(0, lambda: status_label.configure(text=f"⚠️ {error}", text_color=ERROR))
            return
        app.after(0, lambda: _on_loaded(env_path, guilds))

    # Deferred (same reason as every other page's initial fetch, e.g.
    # LeaderboardViewerPage's self.after(150, self.refresh)): all four of
    # Shop/Tickets/Welcome/Social Feeds are constructed here inside
    # Dashboard.__init__, before Dashboard.mainloop() has started pumping
    # events. A worker thread started immediately can finish and call
    # app.after(...) before that loop exists, which raises "main thread is
    # not in main loop" and silently drops the callback — confirmed during
    # testing. 150ms reliably gives mainloop() time to start first.
    picker.after(150, lambda: threading.Thread(target=worker, daemon=True).start())
    return picker


class ConfirmRemoveBotDialog(ctk.CTkToplevel):
    def __init__(self, master, bot: BotConfig, on_confirm):
        super().__init__(master)
        self.title("Remove Bot")
        self.geometry("420x240")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self,
            text=(
                f"⚠  This stops \"{bot.name}\", deletes its Scheduled Task\n"
                f"(\"{bot.task_name}\"), and removes it from bots_config.json.\n"
                "Its project files and logs are left untouched."
            ),
            text_color=WARNING,
            justify="center",
        ).pack(pady=(24, 16), padx=20)

        self.entry = ctk.CTkEntry(self, placeholder_text="Type REMOVE to confirm", width=220)
        self.entry.pack(pady=5)
        self.entry.bind("<KeyRelease>", self._check)

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=22)
        self.confirm_btn = ctk.CTkButton(
            button_row,
            text="Remove Bot",
            state="disabled",
            fg_color="#8b1a1a",
            hover_color="#5c1111",
            command=self._confirm,
        )
        self.confirm_btn.pack(side="left", padx=10)
        ctk.CTkButton(button_row, text="Cancel", fg_color="gray30", hover_color="gray20", command=self.destroy).pack(
            side="left", padx=10
        )

        self._on_confirm = on_confirm

    def _check(self, _event=None) -> None:
        self.confirm_btn.configure(state="normal" if self.entry.get().strip() == "REMOVE" else "disabled")

    def _confirm(self) -> None:
        self.destroy()
        self._on_confirm()


# --------------------------------------------------------------------------------
# Overview page — one Service Control Card per registered bot, generated from
# bot_registry.load_registry() rather than hardcoded per bot.
# --------------------------------------------------------------------------------


class ServiceCard(ctk.CTkFrame):
    """A complete per-bot command center: status LED + text, Start/Stop/
    Restart, live CPU/RAM, and shortcuts to that bot's logs and removal.
    Every action re-polls real status afterward rather than optimistically
    guessing the new state, so what the card shows is always what
    service_controller actually observed."""

    CARDS_PER_ROW = 3

    def __init__(self, master, app: "Dashboard", bot: BotConfig, on_view_logs=None, on_remove=None):
        super().__init__(master, corner_radius=14, fg_color=CARD_BG)
        self.app = app
        self.bot = bot
        self._on_view_logs = on_view_logs
        self._on_remove = on_remove

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 6))

        self.led = LedIndicator(header)
        self.led.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(header, text=f"{bot.icon}  {bot.name}", font=("Segoe UI", 15, "bold")).pack(side="left")

        self.status_label = ctk.CTkLabel(header, text="…", text_color=MUTED, font=("Segoe UI", 11))
        self.status_label.pack(side="right")

        action_row = ctk.CTkFrame(self, fg_color="transparent")
        action_row.pack(fill="x", padx=16, pady=(0, 8))
        self.start_btn = ctk.CTkButton(action_row, text="▶ Start", width=68, command=self._on_start)
        self.start_btn.pack(side="left", padx=(0, 4))
        self.stop_btn = ctk.CTkButton(
            action_row, text="⏹ Stop", width=68, fg_color="#8b1a1a", hover_color="#5c1111", command=self._on_stop
        )
        self.stop_btn.pack(side="left", padx=4)
        self.restart_btn = ctk.CTkButton(
            action_row, text="🔄 Restart", width=84, fg_color="gray30", hover_color="gray20", command=self._on_restart
        )
        self.restart_btn.pack(side="left", padx=4)
        add_tooltip(self.start_btn, "Launch this bot hidden via its Scheduled Task")
        add_tooltip(self.stop_btn, "Instantly kill this bot's process family")
        add_tooltip(self.restart_btn, "Stop then Start in one click")

        self.stats_label = ctk.CTkLabel(self, text="CPU: --%   RAM: -- MB", text_color=MUTED)
        self.stats_label.pack(anchor="w", padx=16, pady=(0, 8))

        secondary_row = ctk.CTkFrame(self, fg_color="transparent")
        secondary_row.pack(fill="x", padx=16, pady=(0, 14))
        if bot.log_file is not None and self._on_view_logs is not None:
            logs_btn = ctk.CTkButton(
                secondary_row,
                text="📜 Logs",
                width=70,
                fg_color="transparent",
                border_width=1,
                command=lambda: self._on_view_logs(bot),
            )
            logs_btn.pack(side="left")
            add_tooltip(logs_btn, "Jump to this bot's live log tail")
        if self._on_remove is not None:
            remove_btn = ctk.CTkButton(
                secondary_row,
                text="🗑 Remove",
                width=84,
                fg_color="transparent",
                border_width=1,
                border_color=ERROR,
                text_color=ERROR,
                hover_color="#3a1a1a",
                command=lambda: self._on_remove(bot),
            )
            remove_btn.pack(side="right")
            add_tooltip(remove_btn, "Stops it, deletes its Scheduled Task, and removes it from the registry")

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.start_btn.configure(state=state)
        self.stop_btn.configure(state=state)
        self.restart_btn.configure(state=state)

    def _run_action(self, action, busy_label: str) -> None:
        self._set_actions_enabled(False)
        self.status_label.configure(text=busy_label, text_color=MUTED)

        def worker():
            action(self.bot)
            status = service_controller.query_status(self.bot)
            stats = service_controller.get_process_stats(self.bot)
            self.app.after(0, lambda: self._apply(status, stats))

        threading.Thread(target=worker, daemon=True).start()

    def _on_start(self) -> None:
        self._run_action(service_controller.start, "Starting…")

    def _on_stop(self) -> None:
        self._run_action(service_controller.stop, "Stopping…")

    def _on_restart(self) -> None:
        self._run_action(service_controller.restart, "Restarting…")

    def refresh(self) -> None:
        def worker():
            status = service_controller.query_status(self.bot)
            stats = service_controller.get_process_stats(self.bot)
            self.app.after(0, lambda: self._apply(status, stats))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, status: str, stats: dict | None) -> None:
        running = status.startswith("Running")
        self.led.set_on() if running else self.led.set_off()
        self.status_label.configure(text=status, text_color=SUCCESS if running else MUTED)
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.restart_btn.configure(state="normal")
        if stats:
            self.stats_label.configure(text=f"CPU: {stats['cpu']:.1f}%   RAM: {stats['ram_mb']:.0f} MB")
        else:
            self.stats_label.configure(text="CPU: --%   RAM: -- MB")


class OverviewPage(ctk.CTkFrame):
    AUTO_REFRESH_MS = 4000  # live CPU/RAM + status polling cadence (spec: every 3-5s)

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        make_page_header(self, "🏠  System Overview", "Your bot fleet at a glance")

        if self.app.registry_errors:
            warning_text = "⚠  Some registry entries were skipped: " + "; ".join(self.app.registry_errors)
            ctk.CTkLabel(self, text=warning_text, text_color=WARNING, wraplength=900, justify="left").pack(
                anchor="w", pady=(0, 12)
            )

        cards_grid = ctk.CTkFrame(self, fg_color="transparent")
        cards_grid.pack(fill="x")
        for col in range(ServiceCard.CARDS_PER_ROW):
            cards_grid.grid_columnconfigure(col, weight=1, uniform="cards")

        self.cards: dict[str, ServiceCard] = {}
        for i, bot in enumerate(self.app.bots):
            row, col = divmod(i, ServiceCard.CARDS_PER_ROW)
            card = ServiceCard(cards_grid, app, bot, on_view_logs=self._view_logs, on_remove=self._confirm_remove)
            card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
            self.cards[bot.id] = card

        ctk.CTkLabel(self, text="Quick Actions", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(24, 8))

        actions_row = ctk.CTkFrame(self, fg_color="transparent")
        actions_row.pack(fill="x")

        pot_provider = next((b for b in self.app.bots if b.id == "pot_provider"), None)
        if pot_provider is not None:
            refresh_pot_btn = ctk.CTkButton(
                actions_row, text="🔄  Refresh PO Token", command=lambda: self._refresh_pot_token(pot_provider)
            )
            refresh_pot_btn.pack(side="left", padx=(0, 10))
            add_tooltip(refresh_pot_btn, "Restarts the PO Token provider to force a fresh token")

        clear_cache_btn = ctk.CTkButton(actions_row, text="🧹  Clear yt-dlp Cache", command=self._clear_cache)
        clear_cache_btn.pack(side="left", padx=10)
        add_tooltip(clear_cache_btn, "Clears yt-dlp's local extractor cache")

        self.action_status = ctk.CTkLabel(self, text="", text_color=MUTED)
        self.action_status.pack(anchor="w", pady=(12, 0))

        # Deferred (see MusicPage's refresh_counts() for why): guarantees
        # mainloop() is already running before any card's background refresh
        # thread calls back into self.app.after(...).
        self.after(150, self._auto_refresh)

    def _refresh_pot_token(self, pot_provider: BotConfig) -> None:
        self.action_status.configure(text="Restarting PO Token provider...", text_color=MUTED)

        def worker():
            service_controller.restart(pot_provider)
            self.app.after(
                0, lambda: self.action_status.configure(text="✅ PO Token provider restarted.", text_color=SUCCESS)
            )

        threading.Thread(target=worker, daemon=True).start()

    def _clear_cache(self) -> None:
        self.action_status.configure(text="Clearing yt-dlp cache...", text_color=MUTED)

        def worker():
            result = clear_ytdlp_cache()
            self.app.after(0, lambda: self.action_status.configure(text=f"✅ {result}", text_color=SUCCESS))

        threading.Thread(target=worker, daemon=True).start()

    def _auto_refresh(self) -> None:
        for card in self.cards.values():
            card.refresh()
        self.app.after(self.AUTO_REFRESH_MS, self._auto_refresh)

    def _view_logs(self, bot: BotConfig) -> None:
        self.app.show_page("Logs")
        select_bot = getattr(self.app.pages["Logs"], "select_bot", None)
        if callable(select_bot):
            select_bot(bot.id)

    def _confirm_remove(self, bot: BotConfig) -> None:
        ConfirmRemoveBotDialog(self.app, bot, on_confirm=lambda: self._do_remove(bot))

    def _do_remove(self, bot: BotConfig) -> None:
        def worker():
            result = service_controller.remove_bot(bot)

            def done():
                self.app.reload_registry()
                if result.startswith("Removed") and "could not" not in result:
                    messagebox.showinfo("Bot removed", result)
                else:
                    messagebox.showwarning("Bot removed with a warning", result)

            self.app.after(0, done)

        threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------------
# Music page (Music Bot's own admin surface — not part of the fleet registry)
# --------------------------------------------------------------------------------


class ConfirmWipeDialog(ctk.CTkToplevel):
    def __init__(self, master, on_confirm):
        super().__init__(master)
        self.title("Confirm Wipe")
        self.geometry("400x230")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self,
            text="⚠  This permanently deletes ALL saved songs,\nplaylists, and favorites. This cannot be undone.",
            text_color=WARNING,
            justify="center",
        ).pack(pady=(24, 16), padx=20)

        self.entry = ctk.CTkEntry(self, placeholder_text="Type WIPE to confirm", width=220)
        self.entry.pack(pady=5)
        self.entry.bind("<KeyRelease>", self._check)

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=22)
        self.confirm_btn = ctk.CTkButton(
            button_row,
            text="Wipe Everything",
            state="disabled",
            fg_color="#8b1a1a",
            hover_color="#5c1111",
            command=self._confirm,
        )
        self.confirm_btn.pack(side="left", padx=10)
        ctk.CTkButton(button_row, text="Cancel", fg_color="gray30", hover_color="gray20", command=self.destroy).pack(
            side="left", padx=10
        )

        self._on_confirm = on_confirm

    def _check(self, _event=None) -> None:
        self.confirm_btn.configure(state="normal" if self.entry.get().strip() == "WIPE" else "disabled")

    def _confirm(self) -> None:
        self.destroy()
        self._on_confirm()


class MusicPage(ctk.CTkFrame):
    """Library summary/wipe plus a Playlist Manager: a real, Supabase-backed, ordered
    song list with working reorder/delete, styled as a premium track-list UI. This
    manages saved playlists, not the bot's live per-guild playback queue — that only
    exists in bot.py's own process memory and has no channel to this dashboard (a
    separate process) today."""

    COLUMN_WIDTHS = {"badge": 34, "title": 300, "duration": 90, "source": 110}
    ROW_HEIGHT = 52

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.library: dict[str, dict] = {}
        self.playlists: dict[str, list[str]] = {}
        self.current_playlist: str | None = None
        self.stat_values: dict[str, ctk.CTkLabel] = {}

        make_page_header(self, "🎵  Music", "Library overview and playlist management")

        # --- Library summary ---
        summary_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        summary_card.pack(fill="x", pady=(0, 16))

        stats_row = ctk.CTkFrame(summary_card, fg_color="transparent")
        stats_row.pack(fill="x", padx=20, pady=(20, 8))
        for key, icon, label in [
            ("library", "📚", "Library"),
            ("playlists", "📃", "Playlists"),
            ("favorites", "⭐", "Favorites"),
        ]:
            self.stat_values[key] = self._make_stat_tile(stats_row, icon, label)

        summary_button_row = ctk.CTkFrame(summary_card, fg_color="transparent")
        summary_button_row.pack(pady=(4, 20))
        ctk.CTkButton(
            summary_button_row,
            text="🔄  Refresh Counts",
            corner_radius=10,
            height=34,
            fg_color="#2a2a40",
            hover_color=ROW_BG_HOVER,
            command=self.refresh_counts,
        ).pack(side="left", padx=6)
        wipe_btn = ctk.CTkButton(
            summary_button_row,
            text="🗑  Wipe All Songs",
            corner_radius=10,
            height=34,
            fg_color="transparent",
            border_width=1,
            border_color=ERROR,
            text_color=ERROR,
            hover_color=DANGER_HOVER,
            command=self._open_confirm_dialog,
        )
        wipe_btn.pack(side="left", padx=6)
        add_tooltip(wipe_btn, "Permanently deletes every saved song, playlist, and favorite")

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(pady=(0, 16))

        # --- Playlist manager ---
        ctk.CTkLabel(self, text="Playlist Manager", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 10))

        control_bar = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        control_bar.pack(fill="x", pady=(0, 12))
        control_row = ctk.CTkFrame(control_bar, fg_color="transparent")
        control_row.pack(fill="x", padx=16, pady=14)
        self.playlist_selector = ctk.CTkOptionMenu(
            control_row,
            values=["(loading...)"],
            command=self._on_playlist_selected,
            width=240,
            height=36,
            corner_radius=10,
            fg_color="#2a2a40",
            button_color=ACCENT,
            button_hover_color=ACCENT_HOVER,
            dropdown_fg_color=CARD_BG,
            dropdown_hover_color=ROW_BG_HOVER,
            font=("Segoe UI", 13),
            dropdown_font=("Segoe UI", 13),
        )
        self.playlist_selector.pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            control_row,
            text="🔄  Refresh",
            width=110,
            height=36,
            corner_radius=10,
            fg_color="#2a2a40",
            hover_color=ROW_BG_HOVER,
            command=self.refresh_playlists,
        ).pack(side="left")
        self.playlist_status = ctk.CTkLabel(control_row, text="", text_color=MUTED, font=("Segoe UI", 12))
        self.playlist_status.pack(side="left", padx=(14, 0))

        table_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        table_card.pack(fill="both", expand=True)

        col_header = ctk.CTkFrame(table_card, fg_color="transparent")
        col_header.pack(fill="x", padx=24, pady=(16, 6))
        ctk.CTkLabel(col_header, text="", width=self.COLUMN_WIDTHS["badge"]).pack(side="left")
        for key, label in [("title", "TITLE"), ("duration", "DURATION"), ("source", "SOURCE")]:
            ctk.CTkLabel(
                col_header,
                text=label,
                width=self.COLUMN_WIDTHS[key],
                anchor="w",
                font=("Segoe UI", 11, "bold"),
                text_color=MUTED,
            ).pack(side="left")

        divider = ctk.CTkFrame(table_card, fg_color=BORDER, height=1)
        divider.pack(fill="x", padx=24, pady=(0, 8))

        self.rows_frame = ctk.CTkScrollableFrame(
            table_card,
            fg_color="transparent",
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT,
        )
        self.rows_frame.pack(fill="both", expand=True, padx=16, pady=(0, 18))

        # Deferred rather than called directly: this kicks off a background
        # thread that calls back into self.app.after(...), which raises
        # RuntimeError("main thread is not in main loop") if it fires before
        # Dashboard.mainloop() has actually started pumping events — a real
        # race, since other pages built during the same __init__ (e.g.
        # Settings' synchronous PowerShell self-heal checks) can keep the
        # main thread busy long enough for a fast query to finish first.
        # self.after() only ever fires once the event loop is live, so
        # routing the kickoff through it guarantees the later cross-thread
        # callback is safe.
        #
        # The two delays are deliberately different (not both 150ms): _DB_LOCK
        # above already guarantees these two can't corrupt each other's Supabase
        # pool no matter how they're timed, but starting them apart means the
        # second one doesn't sit blocked on the lock for the first's whole
        # round-trip before its own "Loading…" placeholder even appears.
        self.after(150, self.refresh_counts)
        self.after(400, self.refresh_playlists)

    @staticmethod
    def _make_stat_tile(parent, icon: str, label: str) -> ctk.CTkLabel:
        tile = ctk.CTkFrame(parent, corner_radius=12, fg_color=ROW_BG)
        tile.pack(side="left", padx=(0, 12), fill="x", expand=True)
        ctk.CTkLabel(tile, text=f"{icon}  {label}", font=("Segoe UI", 11), text_color=MUTED).pack(
            anchor="w", padx=16, pady=(12, 0)
        )
        value_label = ctk.CTkLabel(tile, text="—", font=("Segoe UI", 22, "bold"))
        value_label.pack(anchor="w", padx=16, pady=(0, 12))
        return value_label

    @staticmethod
    def _make_icon_button(parent, text: str, *, danger: bool = False):
        return ctk.CTkButton(
            parent,
            text=text,
            width=30,
            height=30,
            corner_radius=15,
            font=("Segoe UI", 12),
            fg_color="transparent",
            border_width=1,
            border_color=BORDER,
            text_color=ERROR if danger else MUTED,
            hover_color=DANGER_HOVER if danger else ACCENT,
        )

    # --- Library summary / wipe ---

    def refresh_counts(self) -> None:
        self.status_label.configure(text="Loading...", text_color=MUTED)
        run_async_task(self.app, fetch_song_counts, self._on_counts_loaded, self._on_error)

    def _on_counts_loaded(self, counts: dict[str, int]) -> None:
        for key, value_label in self.stat_values.items():
            value_label.configure(text=str(counts.get(key, 0)))
        self.status_label.configure(text="")

    def _open_confirm_dialog(self) -> None:
        ConfirmWipeDialog(self.app, on_confirm=self._do_wipe)

    def _do_wipe(self) -> None:
        self.status_label.configure(text="Wiping...", text_color=MUTED)
        run_async_task(self.app, wipe_all_songs, self._on_wipe_done, self._on_error)

    def _on_wipe_done(self, _result) -> None:
        self.status_label.configure(text="✅ All songs wiped.", text_color=SUCCESS)
        self.refresh_counts()
        self.refresh_playlists()

    def _on_error(self, exc: Exception) -> None:
        self.status_label.configure(text=f"Error: {exc}", text_color=ERROR)

    # --- Playlist manager ---

    def refresh_playlists(self) -> None:
        self.playlist_status.configure(text="Loading...", text_color=MUTED)
        self.playlist_selector.configure(state="disabled")
        # Show a loading placeholder right in the table area rather than leaving
        # whatever was there before (or nothing, on first load) — fetch_music_data
        # retries transient Supabase failures on its own now, but if every attempt
        # exhausts, _on_playlists_error below replaces this with a real error message
        # and a retry button instead of leaving this "Loading…" text stuck forever.
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._render_placeholder("Loading playlists…")

        run_async_task(self.app, fetch_music_data, self._on_playlists_loaded, self._on_playlists_error)

    def _on_playlists_loaded(self, data: dict) -> None:
        self.library = data["library"]
        self.playlists = data["playlists"]
        names = sorted(self.playlists.keys())

        if names:
            if self.current_playlist not in self.playlists:
                self.current_playlist = names[0]
            self.playlist_selector.configure(values=names, state="normal")
            self.playlist_selector.set(self.current_playlist)
        else:
            self.current_playlist = None
            self.playlist_selector.configure(values=["(no playlists saved)"], state="disabled")
            self.playlist_selector.set("(no playlists saved)")

        self._render_songs()
        self.playlist_status.configure(text=f"✅ {len(names)} playlist(s) loaded.", text_color=SUCCESS)

    def _on_playlists_error(self, exc: Exception) -> None:
        """fetch_music_data already retried this internally (DB_RETRY_ATTEMPTS times)
        before giving up — this only runs once that's genuinely exhausted, so the UI
        lock releases here for good: a real error plus a working retry button, not a
        permanent "Loading..." with no way out."""
        self.playlist_selector.configure(state="normal" if self.playlists else "disabled")
        self.playlist_status.configure(text=f"❌ {exc}", text_color=ERROR)

        for child in self.rows_frame.winfo_children():
            child.destroy()
        error_card = ctk.CTkFrame(self.rows_frame, corner_radius=12, fg_color=ROW_BG)
        error_card.pack(fill="x", pady=6)
        ctk.CTkLabel(
            error_card,
            text=f"⚠️  Could not load playlists\n{exc}",
            text_color=ERROR,
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(16, 10))
        ctk.CTkButton(
            error_card,
            text="🔄  Click to Retry",
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self.refresh_playlists,
        ).pack(anchor="w", padx=18, pady=(0, 16))

    def _on_playlist_selected(self, name: str) -> None:
        if name not in self.playlists:
            return
        self.current_playlist = name
        self._render_songs()

    def _render_placeholder(self, message: str, *, muted: bool = True) -> None:
        placeholder = ctk.CTkFrame(self.rows_frame, corner_radius=12, fg_color=ROW_BG)
        placeholder.pack(fill="x", pady=6)
        ctk.CTkLabel(
            placeholder, text=message, text_color=MUTED if muted else ERROR, font=("Segoe UI", 13)
        ).pack(padx=18, pady=18)

    def _render_songs(self) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()

        urls = self.playlists.get(self.current_playlist, []) if self.current_playlist else []
        if not urls:
            message = "🎧  This playlist is empty." if self.current_playlist else "No playlists saved yet."
            self._render_placeholder(message)
            return

        for i, url in enumerate(urls):
            info = self.library.get(url, {})
            title = info.get("title") or url
            source = song_source_label(url)

            row = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG, height=self.ROW_HEIGHT)
            row.pack(fill="x", pady=5)
            row.pack_propagate(False)

            hoverable: list = []

            badge = ctk.CTkLabel(
                row,
                text=str(i + 1),
                width=self.COLUMN_WIDTHS["badge"],
                text_color=MUTED,
                font=("Segoe UI", 11, "bold"),
            )
            badge.pack(side="left", padx=(14, 0))
            hoverable.append(badge)

            title_label = ctk.CTkLabel(
                row,
                text=f"🎵  {title}",
                width=self.COLUMN_WIDTHS["title"],
                anchor="w",
                font=("Segoe UI", 13),
            )
            title_label.pack(side="left", padx=(6, 0))
            hoverable.append(title_label)

            duration_label = ctk.CTkLabel(
                row,
                text=format_song_duration(info.get("duration")),
                width=self.COLUMN_WIDTHS["duration"],
                anchor="w",
                text_color=MUTED,
                font=("Segoe UI", 12),
            )
            duration_label.pack(side="left")
            hoverable.append(duration_label)

            badge_wrap = ctk.CTkFrame(row, fg_color="transparent", width=self.COLUMN_WIDTHS["source"], height=28)
            badge_wrap.pack(side="left")
            badge_wrap.pack_propagate(False)
            hoverable.append(badge_wrap)
            source_pill = ctk.CTkLabel(
                badge_wrap,
                text=f"  {source}  ",
                corner_radius=8,
                fg_color=song_source_badge_color(source),
                text_color="#0e0e16",
                font=("Segoe UI", 10, "bold"),
            )
            source_pill.pack(anchor="w")
            hoverable.append(source_pill)

            actions = ctk.CTkFrame(row, fg_color="transparent")
            actions.pack(side="right", padx=(0, 12))

            up_btn = self._make_icon_button(actions, "▲")
            up_btn.configure(command=lambda idx=i: self._move(idx, -1))
            up_btn.pack(side="left", padx=3)
            if i == 0:
                up_btn.configure(state="disabled", text_color=BORDER)

            down_btn = self._make_icon_button(actions, "▼")
            down_btn.configure(command=lambda idx=i: self._move(idx, 1))
            down_btn.pack(side="left", padx=3)
            if i == len(urls) - 1:
                down_btn.configure(state="disabled", text_color=BORDER)

            del_btn = self._make_icon_button(actions, "✕", danger=True)
            del_btn.configure(command=lambda idx=i: self._delete(idx))
            del_btn.pack(side="left", padx=(8, 0))
            add_tooltip(del_btn, "Remove this song from the playlist")

            bind_hover_card(row, hoverable, ROW_BG, ROW_BG_HOVER)

    def _move(self, index: int, delta: int) -> None:
        if self.current_playlist is None:
            return
        urls = list(self.playlists.get(self.current_playlist, []))
        new_index = index + delta
        if not (0 <= index < len(urls)) or not (0 <= new_index < len(urls)):
            return
        urls[index], urls[new_index] = urls[new_index], urls[index]
        self._persist_and_refresh(urls)

    def _delete(self, index: int) -> None:
        if self.current_playlist is None:
            return
        urls = list(self.playlists.get(self.current_playlist, []))
        if not (0 <= index < len(urls)):
            return
        del urls[index]
        self._persist_and_refresh(urls)

    def _persist_and_refresh(self, urls: list[str]) -> None:
        name = self.current_playlist
        # Optimistic local update: the reorder/delete shows immediately instead of
        # waiting on the Supabase round-trip, then refresh_playlists() below re-fetches
        # from source of truth once the write actually lands, so the two can never
        # drift out of sync.
        self.playlists[name] = urls
        self._render_songs()
        self.playlist_status.configure(text="Saving...", text_color=MUTED)
        run_async_task(
            self.app,
            lambda: save_playlist_order(name, urls),
            on_success=lambda _: self._on_save_success(),
            on_error=self._on_save_error,
        )

    def _on_save_success(self) -> None:
        self.playlist_status.configure(text="✅ Saved.", text_color=SUCCESS)
        self.refresh_playlists()

    def _on_save_error(self, exc: Exception) -> None:
        # Unlike _on_playlists_error, there's still a perfectly good (if possibly
        # stale) playlist on screen here — no need for the big empty-state retry UI.
        # save_playlist_order already retried internally; once it's truly failed, the
        # optimistic edit above may not match what's actually in Supabase, so re-fetch
        # from source of truth rather than leaving an unconfirmed local state on screen.
        self.playlist_status.configure(text=f"❌ Could not save: {exc}", text_color=ERROR)
        self.refresh_playlists()


# --------------------------------------------------------------------------------
# Logs page — one tab per registered bot that declares a log_file.
# --------------------------------------------------------------------------------


class LogsPage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._queue: list[str] = []
        self._tailer: LogTailer | None = None

        self.log_sources: dict[str, Path] = {
            f"{bot.icon} {bot.name}": bot.log_file for bot in app.bots if bot.log_file is not None
        }
        self._source_by_bot_id: dict[str, str] = {
            bot.id: f"{bot.icon} {bot.name}" for bot in app.bots if bot.log_file is not None
        }

        make_page_header(self, "📜  Live Logs", "Real-time output from your registered bots")

        if not self.log_sources:
            ctk.CTkLabel(self, text="No registered bot declares a log_file.", text_color=MUTED).pack(anchor="w")
            return

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(fill="x", pady=(0, 8))

        self.source_selector = ctk.CTkSegmentedButton(
            button_row, values=list(self.log_sources), command=self._on_source_changed
        )
        self.source_selector.set(next(iter(self.log_sources)))
        self.source_selector.pack(side="left", padx=(0, 10))

        ctk.CTkButton(button_row, text="Clear view", width=100, command=self._clear).pack(side="left")

        self.textbox = ctk.CTkTextbox(
            self, wrap="word", state="disabled", fg_color="#0e0e16", font=("Cascadia Code", 12)
        )
        self.textbox.pack(fill="both", expand=True)
        self.textbox.tag_config("error", foreground=ERROR)
        self.textbox.tag_config("warning", foreground=WARNING)
        self.textbox.tag_config("success", foreground=SUCCESS)
        self.textbox.tag_config("default", foreground="#d0d0e0")

        self._switch_source(next(iter(self.log_sources)))
        self._poll_queue()

    def _on_source_changed(self, selected: str) -> None:
        self._switch_source(selected)

    def _switch_source(self, source_name: str) -> None:
        if self._tailer is not None:
            self._tailer.stop()
        self._queue.clear()

        log_path = self.log_sources[source_name]
        self._clear()
        self._append(read_log_tail(log_path))

        self._tailer = LogTailer(self._queue, log_path)
        self._tailer.start()

    @staticmethod
    def _classify(line: str) -> str:
        lowered = line.lower()
        if any(k in lowered for k in ("error", "traceback", "forbidden", "failed", "403", "429")):
            return "error"
        if "warning" in lowered:
            return "warning"
        if "✅" in line or any(k in lowered for k in ("online and ready", "logged in as", "verified")):
            return "success"
        return "default"

    def _append(self, text: str) -> None:
        if not text:
            return
        self.textbox.configure(state="normal")
        for line in text.splitlines(keepends=True):
            self.textbox.insert("end", line, self._classify(line))
        self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def _clear(self) -> None:
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")

    def _poll_queue(self) -> None:
        while self._queue:
            self._append(self._queue.pop(0))
        self.app.after(500, self._poll_queue)

    def stop(self) -> None:
        if self._tailer is not None:
            self._tailer.stop()

    def select_bot(self, bot_id: str) -> None:
        """Switches the viewer to a specific bot's log, e.g. from that bot's
        "📜 Logs" shortcut on its Overview card. No-op if the bot has no
        log_file (its shortcut isn't shown in the first place)."""
        source_name = self._source_by_bot_id.get(bot_id)
        if source_name is None:
            return
        self.source_selector.set(source_name)
        self._switch_source(source_name)


# --------------------------------------------------------------------------------
# Leaderboard Viewer page — reads the Leaderboard Bot's Supabase `profiles`
# table. Looks up that bot's own directory from the registry (rather than a
# second hardcoded path) to find its .env, so this keeps working even if the
# "leaderboard_bot" entry's location in bots_config.json ever changes.
# --------------------------------------------------------------------------------


class LeaderboardViewerPage(ctk.CTkFrame):
    """Card-based rankings: the top 3 get gold/silver/bronze accent treatment (a
    colored border + tinted background + medal glyph — CTk has no real glow/blur, this
    is the honest equivalent), everyone else gets the same plain premium row card the
    Music tab uses. Raw Discord user IDs are demoted to small muted subtext under the
    display name rather than their own column, per the "hide ugly IDs" request."""

    RANK_BADGE_WIDTH = 64
    NAME_COLUMN_WIDTH = 340

    # Smart-polling cadence for near-live XP sync (see _poll() below). Deliberately
    # NOT a Supabase Realtime websocket subscription — this app has zero websocket/
    # realtime dependencies today (fetch_leaderboard etc. are all one-shot stdlib
    # urllib calls), and bridging a persistent asyncio event loop into tkinter's
    # mainloop for sub-second push updates isn't worth it for a leaderboard, where
    # ~5s is already indistinguishable from "live" to a human watching it.
    LEADERBOARD_POLL_MS = 5000

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 20))
        title_col = ctk.CTkFrame(header_row, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="🏆  Leaderboard", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_col, text="Live XP rankings, synced from Supabase and Discord", font=("Segoe UI", 12), text_color=MUTED
        ).pack(anchor="w", pady=(2, 0))
        self.refresh_btn = ctk.CTkButton(
            header_row,
            text="🔄  Refresh Data",
            height=36,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=("Segoe UI", 13, "bold"),
            command=self.refresh,
        )
        self.refresh_btn.pack(side="right", anchor="e")
        add_tooltip(self.refresh_btn, "Re-fetches the latest XP rankings from Supabase and usernames from Discord")

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(anchor="w", pady=(0, 14))

        table_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        table_card.pack(fill="both", expand=True)

        col_header = ctk.CTkFrame(table_card, fg_color="transparent")
        col_header.pack(fill="x", padx=24, pady=(18, 6))
        ctk.CTkLabel(
            col_header, text="RANK", width=self.RANK_BADGE_WIDTH, anchor="w", font=("Segoe UI", 11, "bold"), text_color=MUTED
        ).pack(side="left")
        ctk.CTkLabel(
            col_header, text="MEMBER", width=self.NAME_COLUMN_WIDTH, anchor="w", font=("Segoe UI", 11, "bold"), text_color=MUTED
        ).pack(side="left", padx=(12, 0))
        ctk.CTkLabel(col_header, text="XP", anchor="e", font=("Segoe UI", 11, "bold"), text_color=MUTED).pack(
            side="right", padx=(0, 4)
        )

        divider = ctk.CTkFrame(table_card, fg_color=BORDER, height=1)
        divider.pack(fill="x", padx=24, pady=(0, 8))

        self.rows_frame = ctk.CTkScrollableFrame(
            table_card, fg_color="transparent", scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT
        )
        self.rows_frame.pack(fill="both", expand=True, padx=16, pady=(0, 20))

        # Deferred (see refresh_counts() below for why): a Supabase
        # round-trip can finish before Dashboard.mainloop() has actually
        # started pumping events, which made this get permanently stuck on
        # "Loading..." — the background thread's callback into
        # self.app.after(...) was raising RuntimeError and being silently
        # dropped (invisible under pythonw.exe's no-console launch).
        self._last_rows: list[dict] | None = None
        self._last_names: dict[tuple[int, int], str] = {}
        self._expanded_user_id: int | None = None
        self._action_busy = False

        self.after(150, self.refresh)
        self.after(self.LEADERBOARD_POLL_MS, self._poll)

    def refresh(self) -> None:
        self.refresh_btn.configure(state="disabled")
        self.status_label.configure(text="Loading...", text_color=MUTED)

        def worker():
            try:
                bot = next((b for b in self.app.bots if b.id == "leaderboard_bot"), None)
                if bot is None:
                    raise RuntimeError('No registered bot with id "leaderboard_bot" — check bots_config.json.')
                env_path = bot.directory / ".env"
                rows = fetch_leaderboard(env_path)
            except Exception as exc:  # noqa: BLE001 - surfacing any fetch error to the UI
                error = exc
                self.app.after(0, lambda: self._on_error(error))
                return

            # Username resolution is best-effort and separate from the XP
            # fetch above: if Discord's API is unreachable, the token lacks
            # the SERVER MEMBERS intent, or anything else goes wrong here,
            # the leaderboard should still render with raw user IDs rather
            # than the whole page failing over a secondary lookup.
            name_error: Exception | None = None
            try:
                names = resolve_leaderboard_usernames(env_path, rows)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully, don't fail the whole page
                names = {}
                name_error = exc

            self.app.after(0, lambda: self._on_loaded(rows, names, name_error))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        """Self-rescheduling smart-poll tick: cheap Supabase-only re-fetch (no
        Discord username resolution — see LEADERBOARD_POLL_MS's docstring) so a
        member's XP/rank updates on screen within a few seconds of earning it in
        Discord, without a manual Refresh click. Skips the actual fetch (but still
        reschedules) whenever this tab isn't the visible one or an admin XP action
        is in flight, so it's nearly free the rest of the time."""
        if self.app.current_page_name != "Leaderboard" or self._action_busy:
            self.app.after(self.LEADERBOARD_POLL_MS, self._poll)
            return

        def worker():
            try:
                bot = next((b for b in self.app.bots if b.id == "leaderboard_bot"), None)
                if bot is None:
                    raise RuntimeError("no leaderboard_bot registered")
                rows = fetch_leaderboard(bot.directory / ".env")
            except Exception:  # noqa: BLE001 - a quiet poll tick skips on error; refresh() already surfaces real errors to the user
                self.app.after(0, lambda: self.app.after(self.LEADERBOARD_POLL_MS, self._poll))
                return
            self.app.after(0, lambda: self._on_polled(rows))

        threading.Thread(target=worker, daemon=True).start()

    def _on_polled(self, rows: list[dict]) -> None:
        new_ids = {(r.get("guild_id"), r.get("user_id")) for r in rows}
        if not new_ids.issubset(self._last_names.keys()):
            # A member not in our resolved-names cache appeared (new member, or
            # this is the very first poll before the initial refresh() finished)
            # — fall back to a full refresh() so their username gets resolved too.
            self.refresh()
        elif rows != self._last_rows:
            self._last_rows = rows
            self._render_rows(rows, self._last_names, name_error=None)
        self.app.after(self.LEADERBOARD_POLL_MS, self._poll)

    def apply_streamer_mode(self, _enabled: bool) -> None:
        """Called by Dashboard._toggle_streamer_mode(). Re-renders from the
        already-cached rows/names (no new fetch needed) purely to flip every
        id_label's text — _render_rows reads self.app.streamer_mode directly."""
        if self._last_rows is not None:
            self._render_rows(self._last_rows, self._last_names, name_error=None)

    # (medal, border/accent color, tinted card background, rank-badge text color)
    RANK_TIERS = {
        1: ("🥇", RANK_GOLD, RANK_GOLD_BG, RANK_GOLD),
        2: ("🥈", RANK_SILVER, RANK_SILVER_BG, RANK_SILVER),
        3: ("🥉", RANK_BRONZE, RANK_BRONZE_BG, RANK_BRONZE),
    }

    def _on_loaded(self, rows: list[dict], names: dict[tuple[int, int], str], name_error: Exception | None) -> None:
        self._last_rows = rows
        self._last_names = names
        self._render_rows(rows, names, name_error)
        self.refresh_btn.configure(state="normal")

    def _render_rows(self, rows: list[dict], names: dict[tuple[int, int], str], name_error: Exception | None) -> None:
        """Rebuilds every row card from (rows, names). Used by the initial/manual
        full refresh() and by the lightweight poll path alike, so both stay
        pixel-identical. Preserves scroll position and re-opens whichever member's
        Manage-XP drawer was expanded, so a poll-triggered rebuild reads as a smooth
        update rather than the view jumping back to the top."""
        expanded_user_id = self._expanded_user_id
        try:
            scroll_fraction = self.rows_frame._parent_canvas.yview()[0]
        except Exception:  # noqa: BLE001 - best-effort; a missed scroll restore is harmless
            scroll_fraction = None

        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._expanded_user_id = None

        for rank, row in enumerate(rows, start=1):
            user_id = row.get("user_id")
            guild_id = row.get("guild_id")
            resolved_name = names.get((guild_id, user_id))
            display_name = resolved_name or f"User {user_id}"
            xp = row.get("total_xp", 0)
            tier = self.RANK_TIERS.get(rank)

            # A wrapper (not the row card itself) holds both the row and its admin
            # panel stacked vertically, so expanding one row's Manage drawer can't
            # shove it in between two unrelated rows.
            wrapper = ctk.CTkFrame(self.rows_frame, fg_color="transparent")
            wrapper.pack(fill="x", pady=5)

            card_bg = tier[2] if tier else ROW_BG  # top-3 keep their medal tint permanently, no hover swap
            row_widget = ctk.CTkFrame(
                wrapper,
                corner_radius=PREMIUM_CARD_RADIUS,
                fg_color=card_bg,
                border_width=2 if tier else 0,
                border_color=tier[1] if tier else None,
            )
            row_widget.pack(fill="x", ipady=4)
            hoverable: list = []

            rank_text = f"{tier[0]}" if tier else f"#{rank}"
            rank_label = ctk.CTkLabel(
                row_widget,
                text=rank_text,
                width=self.RANK_BADGE_WIDTH,
                font=("Segoe UI", 20 if tier else 14, "bold"),
                text_color=tier[3] if tier else MUTED,
            )
            rank_label.pack(side="left", padx=(16, 4), pady=10)
            hoverable.append(rank_label)

            name_col = ctk.CTkFrame(row_widget, fg_color="transparent", width=self.NAME_COLUMN_WIDTH, height=52)
            name_col.pack(side="left", padx=(8, 0), pady=10)
            name_col.pack_propagate(False)
            hoverable.append(name_col)
            ctk.CTkLabel(
                name_col,
                text=display_name,
                anchor="w",
                font=("Segoe UI", 16 if tier else 13, "bold"),
                text_color=None if resolved_name else MUTED,
            ).pack(anchor="w")
            id_label = ctk.CTkLabel(
                name_col,
                text="ID ••••••" if self.app.streamer_mode else f"ID {user_id}",
                anchor="w",
                font=("Segoe UI", 10),
                text_color=MUTED,
            )
            id_label.pack(anchor="w")
            hoverable.append(id_label)

            xp_label = ctk.CTkLabel(
                row_widget,
                text=f"{xp:,} XP",
                font=("Segoe UI", 16 if tier else 13, "bold"),
                text_color=tier[1] if tier else ACCENT,
            )
            xp_label.pack(side="right", padx=(0, 20), pady=10)
            hoverable.append(xp_label)

            manage_btn = self._make_manage_button(row_widget)
            manage_btn.pack(side="right", padx=(0, 4), pady=10)
            hoverable.append(manage_btn)
            add_tooltip(manage_btn, "Manage this member's XP")

            admin_holder: dict = {"panel": None}
            manage_btn.configure(
                command=lambda w=wrapper, h=admin_holder, gid=guild_id, uid=user_id, dn=display_name: self._toggle_admin_panel(
                    w, h, gid, uid, dn
                )
            )

            if user_id == expanded_user_id:
                # This member's Manage-XP drawer was open before this rebuild
                # (e.g. a poll tick just landed) — reopen it in place instead of
                # silently collapsing it out from under whoever's using it.
                admin_holder["panel"] = self._build_admin_panel(wrapper, guild_id, user_id, display_name)
                admin_holder["panel"].pack(fill="x", pady=(6, 0))
                self._expanded_user_id = user_id

            if not tier:
                bind_hover_card(row_widget, hoverable, ROW_BG, ROW_BG_HOVER)

        if rows:
            status_text = f"✅ {len(rows)} member(s) loaded."
            status_color = SUCCESS
            if name_error is not None:
                status_text += f"  (Usernames unavailable: {name_error})"
                status_color = WARNING
            self.status_label.configure(text=status_text, text_color=status_color)
        else:
            placeholder = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
            placeholder.pack(fill="x", pady=6)
            ctk.CTkLabel(placeholder, text="No members found.", text_color=MUTED).pack(padx=18, pady=18)
            self.status_label.configure(text="No members found.", text_color=MUTED)

        if scroll_fraction is not None:
            # Deferred a tick: the canvas' scrollregion isn't recomputed until
            # after these newly-packed widgets go through layout, so restoring
            # yview immediately would snap back to whatever the old scrollregion
            # last was.
            self.after(10, lambda f=scroll_fraction: self.rows_frame._parent_canvas.yview_moveto(f))

    def _on_error(self, exc: Exception) -> None:
        self.status_label.configure(text="", text_color=MUTED)
        for child in self.rows_frame.winfo_children():
            child.destroy()
        error_card = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
        error_card.pack(fill="x", pady=6)
        ctk.CTkLabel(
            error_card, text=f"⚠️  {exc}", text_color=ERROR, wraplength=560, justify="left"
        ).pack(anchor="w", padx=18, pady=(16, 10))
        ctk.CTkButton(
            error_card, text="🔄  Click to Retry", corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.refresh
        ).pack(anchor="w", padx=18, pady=(0, 16))
        self.refresh_btn.configure(state="normal")

    # --- Admin XP controls ---

    @staticmethod
    def _make_manage_button(parent) -> ctk.CTkButton:
        return ctk.CTkButton(
            parent,
            text="⚙",
            width=30,
            height=30,
            corner_radius=15,
            font=("Segoe UI", 13),
            fg_color="transparent",
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            hover_color=ACCENT,
        )

    def _toggle_admin_panel(self, wrapper: ctk.CTkFrame, holder: dict, guild_id: int, user_id: int, display_name: str) -> None:
        if holder["panel"] is None:
            # Built lazily on first expand, not eagerly for all ~200+ rows up front —
            # constructing an entry + 3 buttons + status label for every row regardless
            # of whether it's ever opened would meaningfully slow down every refresh.
            holder["panel"] = self._build_admin_panel(wrapper, guild_id, user_id, display_name)

        panel = holder["panel"]
        if panel.winfo_ismapped():
            panel.pack_forget()
            if self._expanded_user_id == user_id:
                self._expanded_user_id = None
        else:
            panel.pack(fill="x", pady=(6, 0))
            self._expanded_user_id = user_id

    def _build_admin_panel(self, wrapper: ctk.CTkFrame, guild_id: int, user_id: int, display_name: str) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(wrapper, corner_radius=10, fg_color=ROW_BG_HOVER)

        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(controls, text="Manage XP", font=("Segoe UI", 11, "bold"), text_color=MUTED).pack(
            side="left", padx=(0, 12)
        )
        amount_entry = ctk.CTkEntry(controls, placeholder_text="Amount", width=90, height=30, corner_radius=8)
        amount_entry.pack(side="left", padx=(0, 8))

        add_btn = ctk.CTkButton(
            controls,
            text="+ Add XP",
            width=92,
            height=30,
            corner_radius=8,
            fg_color=SUCCESS,
            hover_color="#3ecf68",
            text_color="#0e0e16",
            font=("Segoe UI", 11, "bold"),
        )
        add_btn.pack(side="left", padx=3)
        remove_btn = ctk.CTkButton(
            controls,
            text="− Remove XP",
            width=108,
            height=30,
            corner_radius=8,
            fg_color=WARNING,
            hover_color="#e0a050",
            text_color="#0e0e16",
            font=("Segoe UI", 11, "bold"),
        )
        remove_btn.pack(side="left", padx=3)
        reset_btn = ctk.CTkButton(
            controls,
            text="🗑  Reset",
            width=92,
            height=30,
            corner_radius=8,
            fg_color="transparent",
            border_width=1,
            border_color=ERROR,
            text_color=ERROR,
            hover_color=DANGER_HOVER,
            font=("Segoe UI", 11, "bold"),
        )
        reset_btn.pack(side="left", padx=3)

        status_label = ctk.CTkLabel(panel, text="", font=("Segoe UI", 11))
        status_label.pack(anchor="w", padx=16, pady=(0, 12))

        def resolve_env_path() -> Path | None:
            bot = next((b for b in self.app.bots if b.id == "leaderboard_bot"), None)
            if bot is None:
                status_label.configure(
                    text='No registered bot with id "leaderboard_bot" — check bots_config.json.', text_color=ERROR
                )
                return None
            return bot.directory / ".env"

        def parse_amount() -> int | None:
            raw = amount_entry.get().strip()
            if not raw.isdigit() or int(raw) <= 0:
                status_label.configure(text="Enter a positive whole number first.", text_color=ERROR)
                return None
            return int(raw)

        def run_action(action, busy_text: str) -> None:
            status_label.configure(text=busy_text, text_color=MUTED)
            self._action_busy = True  # pauses the poll tick (_poll) until this finishes, see below

            def worker():
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 - surfacing any Supabase/network error to the UI
                    error = exc
                    self.app.after(0, lambda: self._on_admin_action_error(status_label, error))
                    return
                self.app.after(0, self._on_admin_action_done)

            threading.Thread(target=worker, daemon=True).start()

        def on_add() -> None:
            env_path = resolve_env_path()
            amount = parse_amount()
            if env_path is None or amount is None:
                return

            def do():
                result = adjust_member_xp(env_path, guild_id, user_id, amount)
                log_admin_action("add", guild_id, user_id, display_name, amount, result["total_xp"])

            run_action(do, "Adding XP...")

        def on_remove() -> None:
            env_path = resolve_env_path()
            amount = parse_amount()
            if env_path is None or amount is None:
                return

            def do():
                result = adjust_member_xp(env_path, guild_id, user_id, -amount)
                log_admin_action("remove", guild_id, user_id, display_name, amount, result["total_xp"])

            run_action(do, "Removing XP...")

        def on_reset() -> None:
            env_path = resolve_env_path()
            if env_path is None:
                return
            if not messagebox.askyesno("Reset XP?", f"Reset {display_name}'s XP to 0? This cannot be undone."):
                return

            def do():
                reset_member_xp(env_path, guild_id, user_id)
                log_admin_action("reset", guild_id, user_id, display_name, None, 0)

            run_action(do, "Resetting...")

        add_btn.configure(command=on_add)
        remove_btn.configure(command=on_remove)
        reset_btn.configure(command=on_reset)

        return panel

    def _on_admin_action_error(self, status_label: ctk.CTkLabel, error: Exception) -> None:
        self._action_busy = False
        status_label.configure(text=f"❌ {error}", text_color=ERROR)

    def _on_admin_action_done(self) -> None:
        self._action_busy = False
        self.status_label.configure(text="✅ Updated — refreshing leaderboard...", text_color=SUCCESS)
        self.refresh()


# --------------------------------------------------------------------------------
# Admin audit log page
# --------------------------------------------------------------------------------


class AuditLogPage(ctk.CTkFrame):
    """Read-only history of every Add/Remove/Reset XP action taken from the
    Leaderboard tab's admin controls — written by log_admin_action(), called
    from inside LeaderboardViewerPage._build_admin_panel's on_add/on_remove/
    on_reset. Deliberately separate from LogsPage, which tails bot process
    stdout files and has no notion of admin actions."""

    ACTION_STYLE = {  # action -> (icon, accent color)
        "add": ("➕", SUCCESS),
        "remove": ("➖", WARNING),
        "reset": ("🗑", ERROR),
    }

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 20))
        title_col = ctk.CTkFrame(header_row, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="🧾  Admin Audit Log", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_col,
            text="Every Add / Remove / Reset XP action taken from this dashboard",
            font=("Segoe UI", 12),
            text_color=MUTED,
        ).pack(anchor="w", pady=(2, 0))
        refresh_btn = ctk.CTkButton(
            header_row,
            text="🔄  Refresh",
            height=36,
            corner_radius=10,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=("Segoe UI", 13, "bold"),
            command=self.refresh,
        )
        refresh_btn.pack(side="right", anchor="e")
        add_tooltip(refresh_btn, "Re-reads admin_audit_log.json from disk")

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(anchor="w", pady=(0, 14))

        table_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        table_card.pack(fill="both", expand=True)

        self.rows_frame = ctk.CTkScrollableFrame(
            table_card, fg_color="transparent", scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT
        )
        self.rows_frame.pack(fill="both", expand=True, padx=16, pady=20)

        self.refresh()

    def refresh(self) -> None:
        """Reads admin_audit_log.json fresh every call — this is a small local
        file with at most AUDIT_LOG_MAX_ENTRIES rows, so a plain synchronous
        read (unlike every Supabase-backed page's threaded fetch) is fine here."""
        entries = list(reversed(load_audit_log()))  # newest first

        for child in self.rows_frame.winfo_children():
            child.destroy()

        if not entries:
            placeholder = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
            placeholder.pack(fill="x", pady=6)
            ctk.CTkLabel(placeholder, text="No admin actions logged yet.", text_color=MUTED).pack(padx=18, pady=18)
            self.status_label.configure(text="No admin actions logged yet.", text_color=MUTED)
            return

        for entry in entries:
            action = entry.get("action")
            icon, color = self.ACTION_STYLE.get(action, ("•", MUTED))
            target = masked_audit_target(entry, self.app.streamer_mode)
            amount = entry.get("amount")

            if action == "add":
                desc = f"Admin added {amount} XP to {target}"
            elif action == "remove":
                desc = f"Admin removed {amount} XP from {target}"
            else:
                desc = f"Admin reset {target}'s XP to 0"
            resulting = entry.get("resulting_total_xp")
            if resulting is not None and action != "reset":
                desc += f"  (total: {resulting:,} XP)"

            row = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
            row.pack(fill="x", pady=5, ipady=2)
            ctk.CTkLabel(row, text=icon, font=("Segoe UI", 15), text_color=color, width=32).pack(
                side="left", padx=(14, 4), pady=10
            )
            text_col = ctk.CTkFrame(row, fg_color="transparent")
            text_col.pack(side="left", fill="x", expand=True, padx=(4, 10), pady=8)
            ctk.CTkLabel(text_col, text=desc, anchor="w", font=("Segoe UI", 13)).pack(anchor="w")
            ctk.CTkLabel(
                text_col,
                text=format_audit_timestamp(entry.get("timestamp", "")),
                anchor="w",
                font=("Segoe UI", 10),
                text_color=MUTED,
            ).pack(anchor="w")

        self.status_label.configure(text=f"✅ {len(entries)} action(s) logged.", text_color=SUCCESS)

    def apply_streamer_mode(self, _enabled: bool) -> None:
        self.refresh()


# --------------------------------------------------------------------------------
# Shop Management page
# --------------------------------------------------------------------------------


class ShopManagementPage(ctk.CTkFrame):
    """Create/edit/delete XP-Shop items (a Discord role + an XP price) and
    set which channel hosts the buy panel. oasis/bot.py's ShopView reads
    shop_items/guild_config directly and re-syncs its live panel every
    CONFIG_REFRESH_INTERVAL_MINUTES, so a save here shows up in Discord
    automatically without a bot restart."""

    BOT_ID = "leaderboard_bot"

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.env_path: Path | None = None
        self.guild_id: int | None = None

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 20))
        title_col = ctk.CTkFrame(header_row, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="🛒  Shop Management", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_col, text="Roles members can buy with XP", font=("Segoe UI", 12), text_color=MUTED
        ).pack(anchor="w", pady=(2, 0))

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(anchor="w", pady=(0, 14))

        self.guild_picker = add_guild_picker(header_row, app, self.BOT_ID, self.status_label, self._on_guild_changed)
        self.guild_picker.pack(side="right", anchor="e")

        # --- Panel Settings ---
        settings_card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        settings_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(settings_card, text="Panel Channel", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=16, pady=(14, 6)
        )
        settings_row = ctk.CTkFrame(settings_card, fg_color="transparent")
        settings_row.pack(fill="x", padx=16, pady=(0, 14))
        self.channel_entry = ctk.CTkEntry(settings_row, placeholder_text="Shop Channel ID", width=220)
        self.channel_entry.pack(side="left", padx=(0, 8))
        self.save_channel_btn = ctk.CTkButton(
            settings_row, text="💾  Save", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._save_channel
        )
        self.save_channel_btn.pack(side="left")
        add_tooltip(self.channel_entry, "The bot posts/updates the Buy panel in this channel")

        # --- Add Item ---
        add_card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        add_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(add_card, text="Add Item", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        add_row = ctk.CTkFrame(add_card, fg_color="transparent")
        add_row.pack(fill="x", padx=16, pady=(0, 14))
        self.role_entry = ctk.CTkEntry(add_row, placeholder_text="Role ID", width=140)
        self.role_entry.pack(side="left", padx=(0, 8))
        self.label_entry = ctk.CTkEntry(add_row, placeholder_text="Label (e.g. VIP)", width=160)
        self.label_entry.pack(side="left", padx=(0, 8))
        self.cost_entry = ctk.CTkEntry(add_row, placeholder_text="XP Cost", width=100)
        self.cost_entry.pack(side="left", padx=(0, 8))
        self.add_item_btn = ctk.CTkButton(
            add_row, text="+  Add Item", fg_color=SUCCESS, hover_color="#3ecf68", text_color="#0e0e16",
            command=self._add_item,
        )
        self.add_item_btn.pack(side="left")

        # --- Item list ---
        list_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        list_card.pack(fill="both", expand=True)
        self.rows_frame = ctk.CTkScrollableFrame(
            list_card, fg_color="transparent", scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT
        )
        self.rows_frame.pack(fill="both", expand=True, padx=16, pady=16)

    def _on_guild_changed(self, env_path: Path, guild_id: int) -> None:
        self.env_path = env_path
        self.guild_id = guild_id
        self.refresh()

    def refresh(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        env_path, guild_id = self.env_path, self.guild_id
        self.status_label.configure(text="Loading...", text_color=MUTED)

        def worker():
            try:
                config = fetch_guild_config(env_path, guild_id)
                items = list_shop_items(env_path, guild_id)
            except Exception as exc:  # noqa: BLE001 - surfacing any fetch error to the UI
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, lambda: self._on_loaded(config, items))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: dict, items: list[dict]) -> None:
        self.channel_entry.delete(0, "end")
        if config.get("shop_channel_id"):
            self.channel_entry.insert(0, str(config["shop_channel_id"]))

        for child in self.rows_frame.winfo_children():
            child.destroy()

        if not items:
            placeholder = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
            placeholder.pack(fill="x", pady=6)
            ctk.CTkLabel(placeholder, text="No items yet — add one above.", text_color=MUTED).pack(padx=18, pady=18)
        else:
            for item in items:
                row = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
                row.pack(fill="x", pady=5, ipady=4)
                text_col = ctk.CTkFrame(row, fg_color="transparent")
                text_col.pack(side="left", fill="x", expand=True, padx=(16, 8), pady=8)
                ctk.CTkLabel(text_col, text=item["label"], anchor="w", font=("Segoe UI", 13, "bold")).pack(anchor="w")
                role_text = "Role ID ••••••" if self.app.streamer_mode else f"Role ID {item['role_id']}"
                ctk.CTkLabel(text_col, text=role_text, anchor="w", font=("Segoe UI", 10), text_color=MUTED).pack(
                    anchor="w"
                )
                ctk.CTkLabel(
                    row, text=f"{item['xp_cost']:,} XP", font=("Segoe UI", 13, "bold"), text_color=ACCENT
                ).pack(side="right", padx=(0, 12))
                ctk.CTkButton(
                    row, text="🗑", width=32, height=28, corner_radius=8, fg_color="transparent", border_width=1,
                    border_color=ERROR, text_color=ERROR, hover_color=DANGER_HOVER,
                    command=lambda i=item: self._delete_item(i),
                ).pack(side="right", padx=(0, 8))

        self.status_label.configure(text=f"✅ {len(items)} item(s).", text_color=SUCCESS)

    def _save_channel(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        raw = self.channel_entry.get().strip()
        if raw and not raw.isdigit():
            self.status_label.configure(text="Channel ID must be a number.", text_color=ERROR)
            return
        env_path, guild_id = self.env_path, self.guild_id
        channel_id = int(raw) if raw else None
        self.status_label.configure(text="Saving...", text_color=MUTED)

        def worker():
            try:
                save_guild_config(env_path, guild_id, {"shop_channel_id": channel_id})
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(
                0, lambda: self.status_label.configure(text="✅ Saved — the bot will pick this up within a couple minutes.", text_color=SUCCESS)
            )

        threading.Thread(target=worker, daemon=True).start()

    def _add_item(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        role_raw = self.role_entry.get().strip()
        label = self.label_entry.get().strip()
        cost_raw = self.cost_entry.get().strip()
        if not role_raw.isdigit():
            self.status_label.configure(text="Enter a valid Role ID.", text_color=ERROR)
            return
        if not label:
            self.status_label.configure(text="Enter a label for the item.", text_color=ERROR)
            return
        if not cost_raw.isdigit() or int(cost_raw) <= 0:
            self.status_label.configure(text="XP Cost must be a positive whole number.", text_color=ERROR)
            return

        env_path, guild_id = self.env_path, self.guild_id
        role_id, xp_cost = int(role_raw), int(cost_raw)
        self.status_label.configure(text="Adding...", text_color=MUTED)

        def worker():
            try:
                add_shop_item(env_path, guild_id, role_id, xp_cost, label)
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, self._on_item_added)

        threading.Thread(target=worker, daemon=True).start()

    def _on_item_added(self) -> None:
        self.role_entry.delete(0, "end")
        self.label_entry.delete(0, "end")
        self.cost_entry.delete(0, "end")
        self.refresh()

    def _delete_item(self, item: dict) -> None:
        if self.env_path is None:
            return
        if not messagebox.askyesno("Delete Item?", f"Delete \"{item['label']}\" from the shop?"):
            return
        env_path = self.env_path
        self.status_label.configure(text="Deleting...", text_color=MUTED)

        def worker():
            try:
                delete_shop_item(env_path, item["id"])
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def apply_streamer_mode(self, _enabled: bool) -> None:
        self.refresh()


# --------------------------------------------------------------------------------
# Support Tickets page
# --------------------------------------------------------------------------------


class TicketsPage(ctk.CTkFrame):
    """Configure the ticket-open panel's channel + staff role, and close open
    tickets — closing here DELETEs the real Discord channel via the REST API
    (see _discord_api_request) and marks the Supabase row closed; there's no
    in-Discord close button by design, this is the only place to close one."""

    BOT_ID = "leaderboard_bot"

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.env_path: Path | None = None
        self.guild_id: int | None = None

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 20))
        title_col = ctk.CTkFrame(header_row, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="🎫  Support Tickets", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_col, text="Open private-channel tickets, closed from here", font=("Segoe UI", 12), text_color=MUTED
        ).pack(anchor="w", pady=(2, 0))

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(anchor="w", pady=(0, 14))

        self.guild_picker = add_guild_picker(header_row, app, self.BOT_ID, self.status_label, self._on_guild_changed)
        self.guild_picker.pack(side="right", anchor="e")

        # --- Panel Settings ---
        settings_card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        settings_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(settings_card, text="Panel Settings", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=16, pady=(14, 6)
        )
        settings_row = ctk.CTkFrame(settings_card, fg_color="transparent")
        settings_row.pack(fill="x", padx=16, pady=(0, 14))
        self.channel_entry = ctk.CTkEntry(settings_row, placeholder_text="Ticket Panel Channel ID", width=200)
        self.channel_entry.pack(side="left", padx=(0, 8))
        self.staff_role_entry = ctk.CTkEntry(settings_row, placeholder_text="Staff Role ID", width=160)
        self.staff_role_entry.pack(side="left", padx=(0, 8))
        self.save_settings_btn = ctk.CTkButton(
            settings_row, text="💾  Save", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._save_settings
        )
        self.save_settings_btn.pack(side="left")
        add_tooltip(self.staff_role_entry, "Every ticket channel is created private to just the opener + this role")

        # --- Open ticket list ---
        list_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        list_card.pack(fill="both", expand=True)
        self.rows_frame = ctk.CTkScrollableFrame(
            list_card, fg_color="transparent", scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT
        )
        self.rows_frame.pack(fill="both", expand=True, padx=16, pady=16)

    def _on_guild_changed(self, env_path: Path, guild_id: int) -> None:
        self.env_path = env_path
        self.guild_id = guild_id
        self.refresh()

    def refresh(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        env_path, guild_id = self.env_path, self.guild_id
        self.status_label.configure(text="Loading...", text_color=MUTED)

        def worker():
            try:
                config = fetch_guild_config(env_path, guild_id)
                tickets = list_open_tickets(env_path, guild_id)
            except Exception as exc:  # noqa: BLE001 - surfacing any fetch error to the UI
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return

            # Best-effort username resolution — same "still render with raw
            # IDs rather than fail the whole page" approach as the Leaderboard
            # tab, since it's a secondary lookup, not the primary data.
            names: dict[int, str] = {}
            try:
                token = read_env_file(env_path).get("DISCORD_TOKEN")
                if token:
                    names = _fetch_discord_guild_members(token, guild_id)
            except Exception:  # noqa: BLE001
                pass

            self.app.after(0, lambda: self._on_loaded(config, tickets, names))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: dict, tickets: list[dict], names: dict[int, str]) -> None:
        self.channel_entry.delete(0, "end")
        if config.get("ticket_channel_id"):
            self.channel_entry.insert(0, str(config["ticket_channel_id"]))
        self.staff_role_entry.delete(0, "end")
        if config.get("ticket_staff_role_id"):
            self.staff_role_entry.insert(0, str(config["ticket_staff_role_id"]))

        for child in self.rows_frame.winfo_children():
            child.destroy()

        if not tickets:
            placeholder = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
            placeholder.pack(fill="x", pady=6)
            ctk.CTkLabel(placeholder, text="No open tickets.", text_color=MUTED).pack(padx=18, pady=18)
        else:
            for ticket in tickets:
                fallback_name = f"User {ticket['opener_user_id']}"
                opener_name = names.get(ticket["opener_user_id"], fallback_name)
                if self.app.streamer_mode and opener_name == fallback_name:
                    opener_name = "User ••••••"
                channel_text = "#••••••" if self.app.streamer_mode else f"#{ticket['channel_id']}"
                row = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
                row.pack(fill="x", pady=5, ipady=4)
                text_col = ctk.CTkFrame(row, fg_color="transparent")
                text_col.pack(side="left", fill="x", expand=True, padx=(16, 8), pady=8)
                ctk.CTkLabel(text_col, text=channel_text, anchor="w", font=("Segoe UI", 13, "bold")).pack(anchor="w")
                ctk.CTkLabel(
                    text_col, text=f"Opened by {opener_name}", anchor="w", font=("Segoe UI", 10), text_color=MUTED
                ).pack(anchor="w")
                ctk.CTkButton(
                    row, text="Close", width=80, height=28, corner_radius=8, fg_color="transparent", border_width=1,
                    border_color=ERROR, text_color=ERROR, hover_color=DANGER_HOVER,
                    command=lambda t=ticket: self._close_ticket(t),
                ).pack(side="right", padx=(0, 12))

        self.status_label.configure(text=f"✅ {len(tickets)} open ticket(s).", text_color=SUCCESS)

    def _save_settings(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        channel_raw = self.channel_entry.get().strip()
        role_raw = self.staff_role_entry.get().strip()
        if (channel_raw and not channel_raw.isdigit()) or (role_raw and not role_raw.isdigit()):
            self.status_label.configure(text="Channel/Role IDs must be numbers.", text_color=ERROR)
            return
        env_path, guild_id = self.env_path, self.guild_id
        fields = {
            "ticket_channel_id": int(channel_raw) if channel_raw else None,
            "ticket_staff_role_id": int(role_raw) if role_raw else None,
        }
        self.status_label.configure(text="Saving...", text_color=MUTED)

        def worker():
            try:
                save_guild_config(env_path, guild_id, fields)
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(
                0, lambda: self.status_label.configure(text="✅ Saved — the bot will pick this up within a couple minutes.", text_color=SUCCESS)
            )

        threading.Thread(target=worker, daemon=True).start()

    def _close_ticket(self, ticket: dict) -> None:
        if self.env_path is None:
            return
        if not messagebox.askyesno("Close Ticket?", "This permanently deletes the Discord channel. Continue?"):
            return
        env_path = self.env_path
        self.status_label.configure(text="Closing...", text_color=MUTED)

        def worker():
            try:
                try:
                    _discord_api_request(env_path, "DELETE", f"/channels/{ticket['channel_id']}")
                except RuntimeError as exc:
                    if "404" not in str(exc):  # channel already gone is fine, anything else isn't
                        raise
                close_ticket_record(env_path, ticket["id"])
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def apply_streamer_mode(self, _enabled: bool) -> None:
        self.refresh()


# --------------------------------------------------------------------------------
# Welcome & Verify page
# --------------------------------------------------------------------------------


class WelcomeVerifyPage(ctk.CTkFrame):
    """Configure the per-join welcome message and the persistent Verify
    panel's role grant — oasis/bot.py's on_member_join and WelcomeXPView both
    read guild_config directly, so a save here takes effect on the very next
    join / panel re-sync, no bot restart needed."""

    BOT_ID = "leaderboard_bot"
    DEFAULT_MESSAGE = "Welcome to the server, {member}! 🎉"

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.env_path: Path | None = None
        self.guild_id: int | None = None

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 20))
        title_col = ctk.CTkFrame(header_row, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="👋  Welcome & Verify", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_col, text="Per-join welcome message and the Verify panel's role grant", font=("Segoe UI", 12),
            text_color=MUTED,
        ).pack(anchor="w", pady=(2, 0))

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(anchor="w", pady=(0, 14))

        self.guild_picker = add_guild_picker(header_row, app, self.BOT_ID, self.status_label, self._on_guild_changed)
        self.guild_picker.pack(side="right", anchor="e")

        card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card.pack(fill="both", expand=True)

        ctk.CTkLabel(card, text="Welcome Channel ID", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 4)
        )
        self.channel_entry = ctk.CTkEntry(card, placeholder_text="e.g. 123456789012345678", width=260)
        self.channel_entry.pack(anchor="w", padx=16)
        add_tooltip(self.channel_entry, "Hosts both the per-join welcome message and the persistent Verify panel")

        ctk.CTkLabel(card, text="Welcome Message", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 2)
        )
        ctk.CTkLabel(
            card, text="Placeholders: {member} mentions the new member, {server} is this server's name",
            font=("Segoe UI", 10), text_color=MUTED,
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self.message_box = ctk.CTkTextbox(card, height=90, wrap="word", fg_color="#0e0e16", font=("Segoe UI", 12))
        self.message_box.pack(fill="x", padx=16)

        ctk.CTkLabel(card, text="Verified Role ID", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 4)
        )
        self.role_entry = ctk.CTkEntry(card, placeholder_text="e.g. 123456789012345678 (optional)", width=260)
        self.role_entry.pack(anchor="w", padx=16)
        add_tooltip(self.role_entry, "Granted instantly when a member clicks Verify — leave blank for XP-only verification")

        ctk.CTkButton(
            card, text="💾  Save", fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._save
        ).pack(anchor="w", padx=16, pady=16)

    def _on_guild_changed(self, env_path: Path, guild_id: int) -> None:
        self.env_path = env_path
        self.guild_id = guild_id
        self.refresh()

    def refresh(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        env_path, guild_id = self.env_path, self.guild_id
        self.status_label.configure(text="Loading...", text_color=MUTED)

        def worker():
            try:
                config = fetch_guild_config(env_path, guild_id)
            except Exception as exc:  # noqa: BLE001 - surfacing any fetch error to the UI
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, lambda: self._on_loaded(config))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, config: dict) -> None:
        self.channel_entry.delete(0, "end")
        if config.get("welcome_channel_id"):
            self.channel_entry.insert(0, str(config["welcome_channel_id"]))
        self.message_box.delete("1.0", "end")
        self.message_box.insert("1.0", config.get("welcome_message") or self.DEFAULT_MESSAGE)
        self.role_entry.delete(0, "end")
        if config.get("verified_role_id"):
            self.role_entry.insert(0, str(config["verified_role_id"]))
        self.status_label.configure(text="✅ Loaded.", text_color=SUCCESS)

    def _save(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        channel_raw = self.channel_entry.get().strip()
        role_raw = self.role_entry.get().strip()
        if (channel_raw and not channel_raw.isdigit()) or (role_raw and not role_raw.isdigit()):
            self.status_label.configure(text="Channel/Role IDs must be numbers.", text_color=ERROR)
            return
        message = self.message_box.get("1.0", "end").strip() or self.DEFAULT_MESSAGE

        env_path, guild_id = self.env_path, self.guild_id
        fields = {
            "welcome_channel_id": int(channel_raw) if channel_raw else None,
            "welcome_message": message,
            "verified_role_id": int(role_raw) if role_raw else None,
        }
        self.status_label.configure(text="Saving...", text_color=MUTED)

        def worker():
            try:
                save_guild_config(env_path, guild_id, fields)
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(
                0, lambda: self.status_label.configure(text="✅ Saved — the bot will pick this up within a couple minutes.", text_color=SUCCESS)
            )

        threading.Thread(target=worker, daemon=True).start()

    def apply_streamer_mode(self, enabled: bool) -> None:
        """No re-fetch needed — .get() on a masked CTkEntry still returns the
        real underlying value, `show` only changes how it renders, so Save
        keeps working correctly while masked."""
        show = "*" if enabled else ""
        self.channel_entry.configure(show=show)
        self.role_entry.configure(show=show)


# --------------------------------------------------------------------------------
# Social Feeds page
# --------------------------------------------------------------------------------


class SocialFeedsPage(ctk.CTkFrame):
    """Configure TikTok feeds to auto-announce. oasis/bot.py's
    social_feed_checker polls every enabled row here every 10 minutes — see
    that function's docstring for why TikTok specifically is best-effort/
    unofficial (no public API for watching an arbitrary creator's uploads),
    a caveat repeated in this tab's own copy below so it's visible wherever
    someone's actually configuring one."""

    BOT_ID = "leaderboard_bot"
    PING_LABELS = {"everyone": "@everyone", "here": "@here", "none": "No ping"}
    PING_VALUES = {label: value for value, label in PING_LABELS.items()}

    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.env_path: Path | None = None
        self.guild_id: int | None = None

        header_row = ctk.CTkFrame(self, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 20))
        title_col = ctk.CTkFrame(header_row, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="📢  Social Feeds", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            title_col, text="Auto-announce new TikTok posts to a channel", font=("Segoe UI", 12), text_color=MUTED
        ).pack(anchor="w", pady=(2, 0))

        # Static caveat, distinct from self.status_label below (which is
        # reused for transient load/save messages and would otherwise
        # overwrite this on the very first refresh) — this stays visible the
        # whole time someone's looking at this tab.
        ctk.CTkLabel(
            self,
            text="⚠️ TikTok has no public API for this — detection is best-effort (page scraping) and can "
            "occasionally miss a post if TikTok changes their page.",
            font=("Segoe UI", 11),
            text_color=WARNING,
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        self.status_label = ctk.CTkLabel(self, text="", font=("Segoe UI", 12))
        self.status_label.pack(anchor="w", pady=(0, 14))

        self.guild_picker = add_guild_picker(header_row, app, self.BOT_ID, self.status_label, self._on_guild_changed)
        self.guild_picker.pack(side="right", anchor="e")

        # --- Add Feed ---
        add_card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        add_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(add_card, text="Add TikTok Feed", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=16, pady=(14, 6)
        )
        add_row = ctk.CTkFrame(add_card, fg_color="transparent")
        add_row.pack(fill="x", padx=16, pady=(0, 14))
        self.handle_entry = ctk.CTkEntry(add_row, placeholder_text="TikTok handle (no @)", width=180)
        self.handle_entry.pack(side="left", padx=(0, 8))
        self.channel_entry = ctk.CTkEntry(add_row, placeholder_text="Announce Channel ID", width=180)
        self.channel_entry.pack(side="left", padx=(0, 8))
        self.ping_menu = ctk.CTkOptionMenu(add_row, values=list(self.PING_LABELS.values()), width=120)
        self.ping_menu.set(self.PING_LABELS["none"])
        self.ping_menu.pack(side="left", padx=(0, 8))
        self.add_feed_btn = ctk.CTkButton(
            add_row, text="+  Add Feed", fg_color=SUCCESS, hover_color="#3ecf68", text_color="#0e0e16",
            command=self._add_feed,
        )
        self.add_feed_btn.pack(side="left")

        # --- Feed list ---
        list_card = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        list_card.pack(fill="both", expand=True)
        self.rows_frame = ctk.CTkScrollableFrame(
            list_card, fg_color="transparent", scrollbar_button_color=BORDER, scrollbar_button_hover_color=ACCENT
        )
        self.rows_frame.pack(fill="both", expand=True, padx=16, pady=16)

    def _on_guild_changed(self, env_path: Path, guild_id: int) -> None:
        self.env_path = env_path
        self.guild_id = guild_id
        self.refresh()

    def refresh(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        env_path, guild_id = self.env_path, self.guild_id

        def worker():
            try:
                feeds = list_social_feeds(env_path, guild_id)
            except Exception as exc:  # noqa: BLE001 - surfacing any fetch error to the UI
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, lambda: self._on_loaded(feeds))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, feeds: list[dict]) -> None:
        for child in self.rows_frame.winfo_children():
            child.destroy()

        if not feeds:
            placeholder = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
            placeholder.pack(fill="x", pady=6)
            ctk.CTkLabel(placeholder, text="No feeds configured yet — add one above.", text_color=MUTED).pack(
                padx=18, pady=18
            )
        else:
            for feed in feeds:
                row = ctk.CTkFrame(self.rows_frame, corner_radius=PREMIUM_CARD_RADIUS, fg_color=ROW_BG)
                row.pack(fill="x", pady=5, ipady=4)
                text_col = ctk.CTkFrame(row, fg_color="transparent")
                text_col.pack(side="left", fill="x", expand=True, padx=(16, 8), pady=8)
                ctk.CTkLabel(text_col, text=f"@{feed['handle']}", anchor="w", font=("Segoe UI", 13, "bold")).pack(
                    anchor="w"
                )
                ping_label = self.PING_LABELS.get(feed.get("ping_style"), "No ping")
                channel_text = "••••••" if self.app.streamer_mode else str(feed["channel_id"])
                ctk.CTkLabel(
                    text_col, text=f"→ #{channel_text} • {ping_label}", anchor="w", font=("Segoe UI", 10),
                    text_color=MUTED,
                ).pack(anchor="w")

                ctk.CTkButton(
                    row, text="🗑", width=32, height=28, corner_radius=8, fg_color="transparent", border_width=1,
                    border_color=ERROR, text_color=ERROR, hover_color=DANGER_HOVER,
                    command=lambda f=feed: self._delete_feed(f),
                ).pack(side="right", padx=(0, 12))

                enabled_switch = ctk.CTkSwitch(row, text="Enabled", progress_color=ACCENT)
                if feed.get("enabled"):
                    enabled_switch.select()
                enabled_switch.configure(command=lambda f=feed, s=enabled_switch: self._toggle_feed(f, s))
                enabled_switch.pack(side="right", padx=(0, 12))

        self.status_label.configure(text=f"✅ {len(feeds)} feed(s).", text_color=SUCCESS)

    def _add_feed(self) -> None:
        if self.env_path is None or self.guild_id is None:
            return
        handle = self.handle_entry.get().strip().lstrip("@")
        channel_raw = self.channel_entry.get().strip()
        if not handle:
            self.status_label.configure(text="Enter a TikTok handle.", text_color=ERROR)
            return
        if not channel_raw.isdigit():
            self.status_label.configure(text="Enter a valid Announce Channel ID.", text_color=ERROR)
            return

        env_path, guild_id = self.env_path, self.guild_id
        channel_id = int(channel_raw)
        ping_style = self.PING_VALUES.get(self.ping_menu.get(), "none")
        self.status_label.configure(text="Adding...", text_color=MUTED)

        def worker():
            try:
                add_social_feed(env_path, guild_id, handle, channel_id, ping_style)
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, self._on_feed_added)

        threading.Thread(target=worker, daemon=True).start()

    def _on_feed_added(self) -> None:
        self.handle_entry.delete(0, "end")
        self.channel_entry.delete(0, "end")
        self.refresh()

    def _delete_feed(self, feed: dict) -> None:
        if self.env_path is None:
            return
        if not messagebox.askyesno("Delete Feed?", f"Stop watching @{feed['handle']}?"):
            return
        env_path = self.env_path

        def worker():
            try:
                delete_social_feed(env_path, feed["id"])
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: self.status_label.configure(text=f"❌ {error}", text_color=ERROR))
                return
            self.app.after(0, self.refresh)

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_feed(self, feed: dict, switch: ctk.CTkSwitch) -> None:
        if self.env_path is None:
            return
        env_path = self.env_path
        enabled = bool(switch.get())

        def _revert_switch(error: Exception) -> None:
            # The switch already flipped to the new (optimistic) position the
            # instant the user clicked it, before this worker even started —
            # if the save failed, flip it back so it doesn't sit there
            # showing a state that was never actually persisted.
            (switch.deselect if enabled else switch.select)()
            self.status_label.configure(text=f"❌ {error}", text_color=ERROR)

        def worker():
            try:
                set_social_feed_enabled(env_path, feed["id"], enabled)
            except Exception as exc:  # noqa: BLE001
                error = exc
                self.app.after(0, lambda: _revert_switch(error))
                return

        threading.Thread(target=worker, daemon=True).start()

    def apply_streamer_mode(self, _enabled: bool) -> None:
        self.refresh()


# --------------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------------


class SettingsPage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        env = read_env_file()

        make_page_header(self, "⚙️  Settings", "Bot credentials, cookies, and reliability")

        # --- Bot password / verification ---
        card1 = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card1.pack(fill="x", pady=8)
        ctk.CTkLabel(card1, text="Bot Password", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        row1 = ctk.CTkFrame(card1, fg_color="transparent")
        row1.pack(fill="x", padx=16, pady=(0, 6))
        self.password_entry = ctk.CTkEntry(row1, width=220, show="*")
        self.password_entry.insert(0, env.get("BOT_PASSWORD", ""))
        self.password_entry.pack(side="left", padx=(0, 8))
        self.show_password = ctk.CTkCheckBox(row1, text="Show", width=10, command=self._toggle_password_visibility)
        self.show_password.pack(side="left")

        self.verification_switch = ctk.CTkSwitch(
            card1, text="Require !verify <password> before commands work", progress_color=ACCENT
        )
        if env.get("VERIFICATION_ENABLED", "false").strip().lower() == "true":
            self.verification_switch.select()
        self.verification_switch.pack(anchor="w", padx=16, pady=(10, 14))
        add_tooltip(
            self.verification_switch,
            "When on, users must run !verify <password> once before using any other command",
        )

        # --- Cookies ---
        card2 = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card2.pack(fill="x", pady=8)
        ctk.CTkLabel(card2, text="YouTube Cookies", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=16, pady=(14, 6)
        )
        self.cookies_status = ctk.CTkLabel(card2, text=describe_cookies(), text_color=MUTED)
        self.cookies_status.pack(anchor="w", padx=16)
        browse_btn = ctk.CTkButton(card2, text="📁  Update cookies.txt...", command=self._browse_cookies)
        browse_btn.pack(anchor="w", padx=16, pady=(10, 14))
        add_tooltip(browse_btn, "Pick a freshly exported cookies.txt to replace the current one")

        # --- Reliability: one self-heal switch per registered bot ---
        card3 = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card3.pack(fill="x", pady=8)
        ctk.CTkLabel(card3, text="Reliability", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 6))

        self.autoheal_bots = [bot for bot in app.bots if bot.self_heal]
        self.autoheal_switches: dict[str, ctk.CTkSwitch] = {}
        for bot in self.autoheal_bots:
            switch = ctk.CTkSwitch(
                card3, text=f"Auto-restart {bot.name} every 5 minutes if it stops", progress_color=ACCENT
            )
            if service_controller.get_self_heal_enabled(bot):
                switch.select()
            switch.pack(anchor="w", padx=16, pady=(0, 6))
            add_tooltip(switch, "Keeps this service alive across sleep/crash without needing a fresh Windows login")
            self.autoheal_switches[bot.id] = switch
        if not self.autoheal_bots:
            ctk.CTkLabel(card3, text="No registered bot has self_heal enabled.", text_color=MUTED).pack(
                anchor="w", padx=16, pady=(0, 14)
            )

        ctk.CTkFrame(card3, fg_color="transparent", height=8).pack()

        ctk.CTkButton(self, text="💾  Save Settings", command=self._on_save, fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(
            pady=18
        )

        self.status_label = ctk.CTkLabel(self, text="")
        self.status_label.pack()

    def _toggle_password_visibility(self) -> None:
        if self.app.streamer_mode:
            return  # Streamer Mode overrides the Show checkbox — see apply_streamer_mode
        self.password_entry.configure(show="" if self.show_password.get() else "*")

    def apply_streamer_mode(self, enabled: bool) -> None:
        """The Bot Password field is the only other raw secret in this app
        (leaderboard user IDs are the other half, handled by
        LeaderboardViewerPage.apply_streamer_mode) — force it hidden and lock
        the Show checkbox out while streaming, regardless of what the admin
        had it set to beforehand."""
        if enabled:
            self.password_entry.configure(show="*")
            self.show_password.deselect()
            self.show_password.configure(state="disabled")
        else:
            self.show_password.configure(state="normal")

    def _browse_cookies(self) -> None:
        path = filedialog.askopenfilename(
            title="Select cookies.txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            replace_cookies_file(path)
        except OSError as exc:
            self.status_label.configure(text=f"Error: {exc}", text_color=ERROR)
            return
        self.cookies_status.configure(text=describe_cookies())
        self.status_label.configure(text="✅ cookies.txt updated. Restart the bot to use it.", text_color=SUCCESS)

    def _on_save(self) -> None:
        write_env_values(
            {
                "BOT_PASSWORD": self.password_entry.get(),
                "VERIFICATION_ENABLED": "true" if self.verification_switch.get() else "false",
            }
        )
        desired_state = {bot_id: bool(switch.get()) for bot_id, switch in self.autoheal_switches.items()}
        bots_by_id = {bot.id: bot for bot in self.autoheal_bots}

        def worker():
            for bot_id, enabled in desired_state.items():
                service_controller.set_self_heal_enabled(bots_by_id[bot_id], enabled)

        threading.Thread(target=worker, daemon=True).start()
        self.status_label.configure(
            text="✅ Saved. Restart a bot (Overview tab) for password/verification changes to take effect.",
            text_color=SUCCESS,
        )


# --------------------------------------------------------------------------------
# Add Bot page — appends a new entry to bots_config.json via bot_registry.add_bot()
# and asks the main window to reload the registry, so the new service's card
# shows up on Overview immediately with no restart.
# --------------------------------------------------------------------------------


class AddBotPage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        ctk.CTkLabel(self, text="➕  Add Bot", font=("Segoe UI", 24, "bold")).pack(anchor="w", pady=(0, 8))
        ctk.CTkLabel(
            self,
            text=(
                "Registers a new Python/venv-based bot. It appears on the Overview tab "
                "immediately — flip its switch there to start it for the first time, which "
                "auto-creates its Scheduled Task."
            ),
            text_color=MUTED,
            font=("Segoe UI", 12),
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(0, 20))

        card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card.pack(fill="x", pady=8)

        self.name_entry = self._add_text_field(card, "Bot Name")
        self.directory_entry = self._add_path_field(card, "Project Directory Path", self._browse_directory)
        self.main_script_entry = self._add_text_field(card, "Main Script Name", placeholder="bot.py")
        self.venv_entry = self._add_path_field(card, "Virtual Environment Path", self._browse_venv)

        ctk.CTkFrame(card, fg_color="transparent", height=6).pack()

        ctk.CTkButton(self, text="➕  Add Bot", command=self._on_add, fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(
            pady=(18, 6)
        )

        self.status_label = ctk.CTkLabel(self, text="", wraplength=820, justify="left")
        self.status_label.pack(anchor="w")

        ctk.CTkLabel(self, text="Currently registered", font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(24, 6))
        self.registered_label = ctk.CTkLabel(self, text="", text_color=MUTED, justify="left")
        self.registered_label.pack(anchor="w")
        self._refresh_registered_list()

    def _add_text_field(self, card, label: str, placeholder: str = "") -> ctk.CTkEntry:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(row, text=label, width=190, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row, placeholder_text=placeholder)
        entry.pack(side="left", fill="x", expand=True)
        return entry

    def _add_path_field(self, card, label: str, browse_command) -> ctk.CTkEntry:
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(row, text=label, width=190, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Browse...", width=90, command=lambda: browse_command(entry)).pack(side="left")
        return entry

    def _browse_directory(self, entry: ctk.CTkEntry) -> None:
        path = filedialog.askdirectory(title="Select the bot's project directory")
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _browse_venv(self, entry: ctk.CTkEntry) -> None:
        path = filedialog.askdirectory(title="Select the bot's venv folder (e.g. project\\venv)")
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _on_add(self) -> None:
        name = self.name_entry.get().strip()
        directory = self.directory_entry.get().strip()
        main_script = self.main_script_entry.get().strip() or "bot.py"
        venv_path = self.venv_entry.get().strip()

        try:
            bot = bot_registry.add_bot(
                name=name,
                directory=Path(directory),
                main_script=main_script,
                venv_path=Path(venv_path),
            )
        except ValueError as exc:
            self.status_label.configure(text=f"❌ {exc}", text_color=ERROR)
            return
        except OSError as exc:
            self.status_label.configure(text=f"❌ Could not save bots_config.json: {exc}", text_color=ERROR)
            return

        self.app.reload_registry()
        self._refresh_registered_list()
        self.status_label.configure(
            text=(
                f"✅ Added \"{bot.name}\" (task \"{bot.task_name}\"). "
                "Go to Overview and flip its switch to start it for the first time."
            ),
            text_color=SUCCESS,
        )
        for entry in (self.name_entry, self.directory_entry, self.main_script_entry, self.venv_entry):
            entry.delete(0, "end")

    def _refresh_registered_list(self) -> None:
        if not self.app.bots:
            self.registered_label.configure(text="(none yet)")
            return
        self.registered_label.configure(
            text="\n".join(f"{bot.icon}  {bot.name}   —   id: {bot.id}   —   task: {bot.task_name}" for bot in self.app.bots)
        )


# --------------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------------


class Dashboard(ctk.CTk):
    PAGES = [
        ("Overview", "🏠"),
        ("Music", "🎵"),
        ("Logs", "📜"),
        ("Leaderboard", "📊"),
        ("Audit Log", "🧾"),
        ("Shop", "🛒"),
        ("Tickets", "🎫"),
        ("Welcome & Verify", "👋"),
        ("Social Feeds", "📢"),
        ("Settings", "⚙️"),
        ("Add Bot", "➕"),
    ]

    # Pages that are rebuilt from scratch by reload_registry() because their
    # layout is generated from the bot list (new cards, log tabs, self-heal
    # switches). Database and Add Bot don't depend on the bot list shape, so
    # they're left alone.
    REGISTRY_DEPENDENT_PAGES = {
        "Overview": OverviewPage,
        "Logs": LogsPage,
        "Settings": SettingsPage,
    }

    def __init__(self):
        super().__init__()
        self.bots, self.registry_errors = bot_registry.load_registry()
        self.current_page_name = "Overview"
        self.streamer_mode = False  # session-only by design — always starts OFF, never silently on from a forgotten prior run

        self.title("Bot Core Management System")
        self.geometry("1000x680")
        self.minsize(860, 600)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=222, corner_radius=0, fg_color=SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky="nswe")
        sidebar.grid_propagate(False)

        ctk.CTkLabel(sidebar, text="🧠 Bot Core", font=("Segoe UI", 18, "bold")).pack(pady=(32, 2), padx=22)
        ctk.CTkLabel(
            sidebar, text="Ctrl+1-9 tabs · Ctrl+R refresh", font=("Segoe UI", 10), text_color=MUTED
        ).pack(pady=(0, 26), padx=22)

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nswe", padx=24, pady=24)

        # Each nav item is an indicator bar + a pill button, so the active tab reads as
        # a glowing accent strip alongside a highlighted label — not just a flat
        # fg_color swap.
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.nav_indicators: dict[str, ctk.CTkFrame] = {}
        for name, icon in self.PAGES:
            item = ctk.CTkFrame(sidebar, fg_color="transparent", height=46)
            item.pack(fill="x", padx=(0, 14), pady=4)
            item.pack_propagate(False)

            indicator = ctk.CTkFrame(item, width=4, corner_radius=2, fg_color="transparent")
            indicator.pack(side="left", fill="y", padx=(10, 6), pady=6)
            self.nav_indicators[name] = indicator

            btn = ctk.CTkButton(
                item,
                text=f"{icon}   {name}",
                anchor="w",
                corner_radius=10,
                fg_color="transparent",
                hover_color=ROW_BG_HOVER,
                height=42,
                font=("Segoe UI", 13),
                command=lambda n=name: self.show_page(n),
            )
            btn.pack(side="left", fill="both", expand=True)
            self.nav_buttons[name] = btn

        # Pinned to the bottom of the sidebar (not buried in Settings) so it's a
        # one-click "make this safe to show on stream" switch reachable from
        # every tab — the sidebar itself is never torn down or swapped, unlike
        # the page content area to its right.
        streamer_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        streamer_frame.pack(side="bottom", fill="x", padx=18, pady=(0, 22))
        self.streamer_switch = ctk.CTkSwitch(
            streamer_frame,
            text="🔒 Streamer Mode",
            font=("Segoe UI", 12, "bold"),
            progress_color=ACCENT,
            command=self._toggle_streamer_mode,
        )
        self.streamer_switch.pack(anchor="w")
        add_tooltip(
            self.streamer_switch,
            "Masks raw Discord user IDs and the bot password — safe to leave on while screen-sharing this dashboard",
        )

        self.pages: dict[str, ctk.CTkFrame] = {
            "Overview": OverviewPage(self.content, self),
            "Music": MusicPage(self.content, self),
            "Logs": LogsPage(self.content, self),
            "Leaderboard": LeaderboardViewerPage(self.content, self),
            "Audit Log": AuditLogPage(self.content, self),
            "Shop": ShopManagementPage(self.content, self),
            "Tickets": TicketsPage(self.content, self),
            "Welcome & Verify": WelcomeVerifyPage(self.content, self),
            "Social Feeds": SocialFeedsPage(self.content, self),
            "Settings": SettingsPage(self.content, self),
            "Add Bot": AddBotPage(self.content, self),
        }
        for page in self.pages.values():
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.show_page("Overview")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._hotkey_last_fired: dict[str, float] = {}
        self._setup_hotkeys()

    def show_page(self, name: str) -> None:
        self.current_page_name = name
        for n, btn in self.nav_buttons.items():
            active = n == name
            btn.configure(fg_color=ACCENT if active else "transparent", font=("Segoe UI", 13, "bold" if active else "normal"))
            self.nav_indicators[n].configure(fg_color=ACCENT if active else "transparent")
        self.pages[name].tkraise()

    def _toggle_streamer_mode(self) -> None:
        """Fans the new state out to every page via a duck-typed hook — same
        getattr-if-callable idiom already used for reload_registry()'s stop()
        check and _hotkey_refresh_current_page()'s refresh() check — so pages
        with nothing sensitive to mask (Overview, Music, Logs, Add Bot) simply
        don't implement apply_streamer_mode and are skipped."""
        self.streamer_mode = bool(self.streamer_switch.get())
        for page in self.pages.values():
            apply_mode = getattr(page, "apply_streamer_mode", None)
            if callable(apply_mode):
                apply_mode(self.streamer_mode)

    # --- Keyboard shortcuts ---
    #
    # Bound via Tkinter's own bind_all — native, event-driven key handling, not a
    # `while` polling loop and not an external keyboard/pynput listener. Tk only
    # dispatches these callbacks when a real key event occurs, so there's zero ongoing
    # CPU cost while idle. Every handler goes through _fire_hotkey, which debounces
    # (holding a key down generates OS key-repeat events, which would otherwise fire
    # this repeatedly) and only ever calls into the SAME background-thread-based
    # refresh methods the on-screen buttons already use (see MusicPage.refresh_playlists
    # / LeaderboardViewerPage.refresh) — so a hotkey can never block the GUI thread with
    # a Supabase call the way a naive direct call would.

    HOTKEY_DEBOUNCE_SECONDS = 0.5

    def _setup_hotkeys(self) -> None:
        for i, (name, _icon) in enumerate(self.PAGES, start=1):
            if i > 9:
                break
            self.bind_all(f"<Control-Key-{i}>", lambda _event, n=name: self._fire_hotkey(f"goto:{n}", lambda: self.show_page(n)))

        self.bind_all("<Control-r>", lambda _event: self._fire_hotkey("refresh", self._hotkey_refresh_current_page))
        self.bind_all("<Control-R>", lambda _event: self._fire_hotkey("refresh", self._hotkey_refresh_current_page))
        self.bind_all("<F5>", lambda _event: self._fire_hotkey("refresh", self._hotkey_refresh_current_page))

    def _fire_hotkey(self, key: str, action) -> None:
        now = time.monotonic()
        if now - self._hotkey_last_fired.get(key, 0.0) < self.HOTKEY_DEBOUNCE_SECONDS:
            return
        self._hotkey_last_fired[key] = now
        action()

    def _hotkey_refresh_current_page(self) -> None:
        """Refreshes whichever page is currently visible, if it has a meaningful
        "refresh" concept — Overview already auto-refreshes on its own timer, and Logs/
        Settings/Add Bot don't have a fetch to re-trigger, so this is a deliberate no-op
        for those rather than forcing something on pages that don't need it."""
        page = self.pages.get(self.current_page_name)
        if page is None:
            return
        if hasattr(page, "refresh_playlists"):
            page.refresh_counts()
            page.refresh_playlists()
        elif hasattr(page, "refresh"):
            page.refresh()

    def reload_registry(self) -> None:
        """Re-reads bots_config.json and rebuilds the pages whose layout
        depends on the bot list, so a bot added via the Add Bot page shows up
        on Overview (and Logs/Settings) immediately, with no restart."""
        self.bots, self.registry_errors = bot_registry.load_registry()
        for name, page_cls in self.REGISTRY_DEPENDENT_PAGES.items():
            old_page = self.pages[name]
            stop = getattr(old_page, "stop", None)
            if callable(stop):
                stop()
            old_page.destroy()
            new_page = page_cls(self.content, self)
            new_page.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.pages[name] = new_page
        self.show_page(self.current_page_name)

    def _on_close(self) -> None:
        self.pages["Logs"].stop()
        self.destroy()


if __name__ == "__main__":
    Dashboard().mainloop()
