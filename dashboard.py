"""
Music Bot Command Center — local control panel for the Discord music bot.

Run with the project's venv:
    venv\\Scripts\\python.exe dashboard.py
(or venv\\Scripts\\pythonw.exe dashboard.py to launch with no console window)

Talks to the same Supabase database as bot.py (via database.py), edits the same
.env file, and controls the same two Windows Scheduled Tasks (MusicDiscordBot,
BgutilPotProvider) set up for running the bot locally. This is a separate process
from the bot — it opens its own short-lived database connections rather than
sharing the bot's.
"""

import asyncio
import hashlib
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
import psutil

import database as db

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "bot_output.log"
COOKIES_PATH = BASE_DIR / "cookies.txt"

BOT_TASK_NAME = "MusicDiscordBot"
POTPROVIDER_TASK_NAME = "BgutilPotProvider"

# (process name, command-line substring) used to find each service's real process,
# since Task Scheduler no longer tracks it directly — see the comment above
# query_service_status.
_PROCESS_MATCH = {
    BOT_TASK_NAME: ("python.exe", "bot.py"),
    POTPROVIDER_TASK_NAME: ("node.exe", "main.js"),
}

ACCENT = "#7c5cff"
ACCENT_HOVER = "#6a4ce0"
CARD_BG = "#1c1c2b"
SIDEBAR_BG = "#14141f"
SUCCESS = "#50fa7b"
WARNING = "#ffb86c"
ERROR = "#ff5566"
MUTED = "#9a9ab0"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# --------------------------------------------------------------------------------
# .env helpers
# --------------------------------------------------------------------------------


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
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
# Scheduled Task / process helpers
# --------------------------------------------------------------------------------


def run_schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def run_powershell(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def start_task(task_name: str) -> str:
    result = run_schtasks("/run", "/tn", task_name)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()


# Both tasks launch their real process via a VBScript wrapper (WScript.Shell.Run with
# window style 0) so no console window appears — but that means Task Scheduler only
# tracks the short-lived wscript.exe launcher, not the detached process it starts, so
# "schtasks /query" and "schtasks /end" don't reflect or control the actual bot/
# provider process. Status and stop/restart below check and kill the real process
# directly instead.
def _find_pids(process_name: str, cmdline_substring: str) -> list[int]:
    ps_command = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{cmdline_substring}*' }} | "
        f"Select-Object -ExpandProperty ProcessId"
    )
    result = run_powershell(ps_command)
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def query_service_status(task_name: str) -> str:
    pids = _find_pids(*_PROCESS_MATCH[task_name])
    return f"Running (PID {pids[0]})" if pids else "Stopped"


def stop_task(task_name: str) -> str:
    pids = _find_pids(*_PROCESS_MATCH[task_name])
    if not pids:
        return "Already stopped."
    _kill_pids(pids)
    return f"Stopped (killed PID {', '.join(map(str, pids))})."


def restart_task(task_name: str) -> str:
    stop_task(task_name)
    time.sleep(1)
    return start_task(task_name)


# --------------------------------------------------------------------------------
# Self-heal trigger toggle (the 5-minute repeating trigger added alongside "At logon")
# --------------------------------------------------------------------------------


def get_self_heal_enabled(task_name: str) -> bool:
    ps_command = (
        f"$t = Get-ScheduledTask -TaskName '{task_name}'; "
        f"($t.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }}).Enabled"
    )
    result = run_powershell(ps_command)
    return result.stdout.strip().lower() == "true"


def set_self_heal_enabled(task_name: str, enabled: bool) -> None:
    value = "$true" if enabled else "$false"
    ps_command = (
        f"$t = Get-ScheduledTask -TaskName '{task_name}'; "
        f"($t.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }}).Enabled = {value}; "
        f"Set-ScheduledTask -TaskName '{task_name}' -Trigger $t.Triggers | Out-Null"
    )
    run_powershell(ps_command)


# --------------------------------------------------------------------------------
# Process resource stats
# --------------------------------------------------------------------------------

_psutil_procs: dict[int, psutil.Process] = {}


def get_process_stats(task_name: str) -> dict | None:
    """Aggregates CPU/RAM across every matching PID (the venv's python.exe launcher
    plus the real interpreter it spawns both match "bot.py" in their command line),
    rather than trying to guess which single PID is the "real" one."""
    pids = _find_pids(*_PROCESS_MATCH[task_name])
    if not pids:
        return None

    total_cpu = 0.0
    total_ram_mb = 0.0
    live_pids = []
    for pid in pids:
        proc = _psutil_procs.get(pid)
        if proc is None:
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(interval=None)  # prime the baseline for the next call
                _psutil_procs[pid] = proc
            except psutil.NoSuchProcess:
                continue
        try:
            total_cpu += proc.cpu_percent(interval=None)
            total_ram_mb += proc.memory_info().rss / (1024 * 1024)
            live_pids.append(pid)
        except psutil.NoSuchProcess:
            _psutil_procs.pop(pid, None)

    if not live_pids:
        return None
    return {"pids": live_pids, "cpu": total_cpu, "ram_mb": total_ram_mb}


# --------------------------------------------------------------------------------
# Cache / cookies maintenance
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


def read_log_tail(max_bytes: int = 20_000) -> str:
    if not LOG_PATH.exists():
        return "(bot_output.log not found yet)\n"
    size = LOG_PATH.stat().st_size
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # drop the partial first line
        return f.read()


class LogTailer(threading.Thread):
    def __init__(self, out_queue: list):
        super().__init__(daemon=True)
        self.out_queue = out_queue
        self._stop_event = threading.Event()
        self._pos = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if LOG_PATH.exists():
                    size = LOG_PATH.stat().st_size
                    if size < self._pos:
                        self._pos = 0  # log was rotated/truncated
                    if size > self._pos:
                        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
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


async def fetch_song_counts() -> dict[str, int]:
    await _connect_db()
    try:
        return await db.get_song_counts()
    finally:
        await db.close_pool()


async def wipe_all_songs() -> None:
    await _connect_db()
    try:
        await db.wipe_music_data()
    finally:
        await db.close_pool()


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


# --------------------------------------------------------------------------------
# Overview page
# --------------------------------------------------------------------------------


class ServiceCard(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard", label: str, task_name: str):
        super().__init__(master, corner_radius=14, fg_color=CARD_BG)
        self.app = app
        self.task_name = task_name
        self._suspend_sync_until = 0.0

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 6))

        self.led = LedIndicator(header)
        self.led.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(header, text=label, font=("Segoe UI", 15, "bold")).pack(side="left")

        self.switch = ctk.CTkSwitch(header, text="", command=self._on_toggle, progress_color=ACCENT)
        self.switch.pack(side="right")
        add_tooltip(self.switch, "Start / stop this service")

        self.stats_label = ctk.CTkLabel(self, text="CPU: --%   RAM: -- MB", text_color=MUTED)
        self.stats_label.pack(anchor="w", padx=16, pady=(0, 14))

    def _on_toggle(self) -> None:
        want_running = bool(self.switch.get())
        self._suspend_sync_until = time.monotonic() + 4  # give the action time to land

        def worker():
            if want_running:
                start_task(self.task_name)
            else:
                stop_task(self.task_name)

        threading.Thread(target=worker, daemon=True).start()

    def refresh(self) -> None:
        def worker():
            status = query_service_status(self.task_name)
            stats = get_process_stats(self.task_name)
            self.app.after(0, lambda: self._apply(status, stats))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, status: str, stats: dict | None) -> None:
        running = status.startswith("Running")
        self.led.set_on() if running else self.led.set_off()
        if time.monotonic() >= self._suspend_sync_until:
            self.switch.select() if running else self.switch.deselect()
        if stats:
            self.stats_label.configure(text=f"CPU: {stats['cpu']:.1f}%   RAM: {stats['ram_mb']:.0f} MB")
        else:
            self.stats_label.configure(text="CPU: --%   RAM: -- MB")


class OverviewPage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        ctk.CTkLabel(self, text="System Overview", font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(0, 16))

        cards_row = ctk.CTkFrame(self, fg_color="transparent")
        cards_row.pack(fill="x")
        self.bot_card = ServiceCard(cards_row, app, "🎵  Music Bot", BOT_TASK_NAME)
        self.bot_card.pack(side="left", expand=True, fill="both", padx=(0, 8))
        self.pot_card = ServiceCard(cards_row, app, "🔑  PO Token Provider", POTPROVIDER_TASK_NAME)
        self.pot_card.pack(side="left", expand=True, fill="both", padx=(8, 0))

        ctk.CTkLabel(self, text="Quick Actions", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(24, 8))

        actions_row = ctk.CTkFrame(self, fg_color="transparent")
        actions_row.pack(fill="x")

        refresh_pot_btn = ctk.CTkButton(actions_row, text="🔄  Refresh PO Token", command=self._refresh_pot_token)
        refresh_pot_btn.pack(side="left", padx=(0, 10))
        add_tooltip(refresh_pot_btn, "Restarts the PO Token provider to force a fresh token")

        clear_cache_btn = ctk.CTkButton(actions_row, text="🧹  Clear yt-dlp Cache", command=self._clear_cache)
        clear_cache_btn.pack(side="left", padx=10)
        add_tooltip(clear_cache_btn, "Clears yt-dlp's local extractor cache")

        self.action_status = ctk.CTkLabel(self, text="", text_color=MUTED)
        self.action_status.pack(anchor="w", pady=(12, 0))

        self._auto_refresh()

    def _refresh_pot_token(self) -> None:
        self.action_status.configure(text="Restarting PO Token provider...", text_color=MUTED)

        def worker():
            restart_task(POTPROVIDER_TASK_NAME)
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
        self.bot_card.refresh()
        self.pot_card.refresh()
        self.app.after(2500, self._auto_refresh)


# --------------------------------------------------------------------------------
# Database page
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


class DatabasePage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        ctk.CTkLabel(self, text="Database", font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(0, 16))

        card = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card.pack(fill="x", pady=10)
        self.counts_label = ctk.CTkLabel(card, text="Loading...", font=("Segoe UI", 14))
        self.counts_label.pack(pady=24, padx=20)

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=10)
        ctk.CTkButton(button_row, text="Refresh Counts", command=self.refresh_counts).pack(side="left", padx=10)
        wipe_btn = ctk.CTkButton(
            button_row,
            text="🗑  Wipe All Songs",
            fg_color="#8b1a1a",
            hover_color="#5c1111",
            command=self._open_confirm_dialog,
        )
        wipe_btn.pack(side="left", padx=10)
        add_tooltip(wipe_btn, "Permanently deletes every saved song, playlist, and favorite")

        self.status_label = ctk.CTkLabel(self, text="")
        self.status_label.pack(pady=10)

        self.refresh_counts()

    def refresh_counts(self) -> None:
        self.status_label.configure(text="Loading...", text_color=MUTED)
        run_async_task(self.app, fetch_song_counts, self._on_counts_loaded, self._on_error)

    def _on_counts_loaded(self, counts: dict[str, int]) -> None:
        self.counts_label.configure(
            text=(
                f"📚  Library: {counts['library']}     "
                f"📃  Playlists: {counts['playlists']}     "
                f"⭐  Favorites: {counts['favorites']}"
            )
        )
        self.status_label.configure(text="")

    def _open_confirm_dialog(self) -> None:
        ConfirmWipeDialog(self.app, on_confirm=self._do_wipe)

    def _do_wipe(self) -> None:
        self.status_label.configure(text="Wiping...", text_color=MUTED)
        run_async_task(self.app, wipe_all_songs, self._on_wipe_done, self._on_error)

    def _on_wipe_done(self, _result) -> None:
        self.status_label.configure(text="✅ All songs wiped.", text_color=SUCCESS)
        self.refresh_counts()

    def _on_error(self, exc: Exception) -> None:
        self.status_label.configure(text=f"Error: {exc}", text_color=ERROR)


# --------------------------------------------------------------------------------
# Logs page
# --------------------------------------------------------------------------------


class LogsPage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._queue: list[str] = []

        ctk.CTkLabel(self, text="Live Logs", font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(0, 12))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(button_row, text="Clear view", width=100, command=self._clear).pack(side="left")

        self.textbox = ctk.CTkTextbox(
            self, wrap="word", state="disabled", fg_color="#0e0e16", font=("Cascadia Code", 12)
        )
        self.textbox.pack(fill="both", expand=True)
        self.textbox.tag_config("error", foreground=ERROR)
        self.textbox.tag_config("warning", foreground=WARNING)
        self.textbox.tag_config("success", foreground=SUCCESS)
        self.textbox.tag_config("default", foreground="#d0d0e0")

        self._append(read_log_tail())

        self._tailer = LogTailer(self._queue)
        self._tailer.start()
        self._poll_queue()

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
        self._tailer.stop()


# --------------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------------


class SettingsPage(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        env = read_env_file()

        ctk.CTkLabel(self, text="Settings", font=("Segoe UI", 22, "bold")).pack(anchor="w", pady=(0, 16))

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

        # --- Reliability ---
        card3 = ctk.CTkFrame(self, corner_radius=14, fg_color=CARD_BG)
        card3.pack(fill="x", pady=8)
        ctk.CTkLabel(card3, text="Reliability", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 6))
        self.autoheal_switch = ctk.CTkSwitch(
            card3, text="Auto-restart services every 5 minutes if they stop", progress_color=ACCENT
        )
        if get_self_heal_enabled(BOT_TASK_NAME):
            self.autoheal_switch.select()
        self.autoheal_switch.pack(anchor="w", padx=16, pady=(0, 14))
        add_tooltip(
            self.autoheal_switch,
            "Keeps both services alive across sleep/crash without needing a fresh Windows login",
        )

        ctk.CTkButton(self, text="💾  Save Settings", command=self._on_save, fg_color=ACCENT, hover_color=ACCENT_HOVER).pack(
            pady=18
        )

        self.status_label = ctk.CTkLabel(self, text="")
        self.status_label.pack()

    def _toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "*")

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
        autoheal = bool(self.autoheal_switch.get())

        def worker():
            set_self_heal_enabled(BOT_TASK_NAME, autoheal)
            set_self_heal_enabled(POTPROVIDER_TASK_NAME, autoheal)

        threading.Thread(target=worker, daemon=True).start()
        self.status_label.configure(
            text="✅ Saved. Restart the bot (Overview tab) for password/verification changes to take effect.",
            text_color=SUCCESS,
        )


# --------------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------------


class Dashboard(ctk.CTk):
    PAGES = [
        ("Overview", "🏠"),
        ("Database", "🗄️"),
        ("Logs", "📜"),
        ("Settings", "⚙️"),
    ]

    def __init__(self):
        super().__init__()
        self.title("Music Bot Command Center")
        self.geometry("1000x680")
        self.minsize(860, 600)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=210, corner_radius=0, fg_color=SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky="nswe")
        sidebar.grid_propagate(False)

        ctk.CTkLabel(sidebar, text="🎧 Command Center", font=("Segoe UI", 17, "bold")).pack(pady=(28, 24), padx=20)

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nswe", padx=16, pady=16)

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for name, icon in self.PAGES:
            btn = ctk.CTkButton(
                sidebar,
                text=f"{icon}   {name}",
                anchor="w",
                fg_color="transparent",
                hover_color="#26263a",
                height=42,
                font=("Segoe UI", 13),
                command=lambda n=name: self.show_page(n),
            )
            btn.pack(fill="x", padx=14, pady=4)
            self.nav_buttons[name] = btn

        self.pages: dict[str, ctk.CTkFrame] = {
            "Overview": OverviewPage(content, self),
            "Database": DatabasePage(content, self),
            "Logs": LogsPage(content, self),
            "Settings": SettingsPage(content, self),
        }
        for page in self.pages.values():
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.show_page("Overview")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def show_page(self, name: str) -> None:
        for n, btn in self.nav_buttons.items():
            btn.configure(fg_color=ACCENT if n == name else "transparent")
        self.pages[name].tkraise()

    def _on_close(self) -> None:
        self.pages["Logs"].stop()
        self.destroy()


if __name__ == "__main__":
    Dashboard().mainloop()
