"""
Local control panel for the Discord music bot.

Run with the project's venv:
    venv\\Scripts\\python.exe dashboard.py

Talks to the same Supabase database as bot.py (via database.py), edits the same
.env file, and controls the same two Windows Scheduled Tasks (MusicDiscordBot,
BgutilPotProvider) set up for running the bot locally. This is a separate process
from the bot — it opens its own short-lived database connections rather than
sharing the bot's.
"""

import asyncio
import csv
import io
import subprocess
import threading
from pathlib import Path

import customtkinter as ctk

import database as db

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "bot_output.log"

BOT_TASK_NAME = "MusicDiscordBot"
POTPROVIDER_TASK_NAME = "BgutilPotProvider"

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
# Scheduled Task helpers
# --------------------------------------------------------------------------------


def run_schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def query_task_status(task_name: str) -> str:
    result = run_schtasks("/query", "/tn", task_name, "/fo", "CSV", "/nh")
    if result.returncode != 0:
        return "Not found"
    try:
        row = next(csv.reader(io.StringIO(result.stdout)))
        return row[-1]
    except (StopIteration, IndexError):
        return "Unknown"


def start_task(task_name: str) -> str:
    result = run_schtasks("/run", "/tn", task_name)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()


def stop_task(task_name: str) -> str:
    result = run_schtasks("/end", "/tn", task_name)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()


def restart_task(task_name: str) -> str:
    stop_task(task_name)
    return start_task(task_name)


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
    def __init__(self, out_queue: "asyncio.Queue | list"):
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
# UI
# --------------------------------------------------------------------------------


class DatabaseTab(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        self.counts_label = ctk.CTkLabel(self, text="Song counts: (not loaded)", font=("", 14))
        self.counts_label.pack(pady=(20, 10))

        ctk.CTkButton(self, text="Refresh counts", command=self.refresh_counts).pack(pady=5)

        warning = ctk.CTkLabel(
            self,
            text=(
                "⚠ This permanently deletes every saved song, playlist, and favorite\n"
                "from Supabase. This cannot be undone."
            ),
            text_color="#e07b39",
            justify="center",
        )
        warning.pack(pady=(30, 10))

        self.confirm_entry = ctk.CTkEntry(self, placeholder_text="Type WIPE to confirm", width=250)
        self.confirm_entry.pack(pady=5)
        self.confirm_entry.bind("<KeyRelease>", self._on_confirm_text_changed)

        self.wipe_button = ctk.CTkButton(
            self,
            text="Wipe All Songs",
            fg_color="#8b1a1a",
            hover_color="#5c1111",
            state="disabled",
            command=self._on_wipe_clicked,
        )
        self.wipe_button.pack(pady=10)

        self.status_label = ctk.CTkLabel(self, text="", text_color="gray70")
        self.status_label.pack(pady=5)

        self.refresh_counts()

    def _on_confirm_text_changed(self, _event=None) -> None:
        self.wipe_button.configure(
            state="normal" if self.confirm_entry.get().strip() == "WIPE" else "disabled"
        )

    def refresh_counts(self) -> None:
        self.status_label.configure(text="Loading...")
        run_async_task(self.app, fetch_song_counts, self._on_counts_loaded, self._on_error)

    def _on_counts_loaded(self, counts: dict[str, int]) -> None:
        self.counts_label.configure(
            text=(
                f"Library: {counts['library']}    "
                f"Playlists: {counts['playlists']}    "
                f"Favorites: {counts['favorites']}"
            )
        )
        self.status_label.configure(text="")

    def _on_wipe_clicked(self) -> None:
        self.wipe_button.configure(state="disabled")
        self.status_label.configure(text="Wiping...")
        run_async_task(self.app, wipe_all_songs, self._on_wipe_done, self._on_error)

    def _on_wipe_done(self, _result) -> None:
        self.status_label.configure(text="✅ All songs wiped.", text_color="#4caf50")
        self.confirm_entry.delete(0, "end")
        self.refresh_counts()

    def _on_error(self, exc: Exception) -> None:
        self.status_label.configure(text=f"Error: {exc}", text_color="#e05c5c")
        self.wipe_button.configure(state="normal")


class ConfigTab(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        env = read_env_file()

        ctk.CTkLabel(self, text="Bot Password", font=("", 14)).pack(pady=(30, 5))
        password_row = ctk.CTkFrame(self, fg_color="transparent")
        password_row.pack(pady=5)
        self.password_entry = ctk.CTkEntry(password_row, width=220, show="*")
        self.password_entry.insert(0, env.get("BOT_PASSWORD", ""))
        self.password_entry.pack(side="left", padx=(0, 8))
        self.show_password = ctk.CTkCheckBox(
            password_row, text="Show", width=10, command=self._toggle_password_visibility
        )
        self.show_password.pack(side="left")

        self.verification_switch = ctk.CTkSwitch(
            self, text="Require verification (!verify <password>) before commands work"
        )
        if env.get("VERIFICATION_ENABLED", "false").strip().lower() == "true":
            self.verification_switch.select()
        self.verification_switch.pack(pady=25)

        ctk.CTkButton(self, text="Save", command=self._on_save).pack(pady=10)

        note = ctk.CTkLabel(
            self,
            text="Restart the bot (Services tab) for changes to take effect.",
            text_color="gray70",
        )
        note.pack(pady=5)

        self.status_label = ctk.CTkLabel(self, text="")
        self.status_label.pack(pady=5)

    def _toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "*")

    def _on_save(self) -> None:
        write_env_values(
            {
                "BOT_PASSWORD": self.password_entry.get(),
                "VERIFICATION_ENABLED": "true" if self.verification_switch.get() else "false",
            }
        )
        self.status_label.configure(text="✅ Saved.", text_color="#4caf50")


class ServicesTab(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app

        self.bot_row = self._build_service_row("Music Bot", BOT_TASK_NAME)
        self.pot_row = self._build_service_row("PO Token Provider", POTPROVIDER_TASK_NAME)

        ctk.CTkButton(self, text="Refresh status", command=self.refresh_all).pack(pady=15)

        self.refresh_all()
        self._schedule_auto_refresh()

    def _build_service_row(self, label: str, task_name: str) -> dict:
        frame = ctk.CTkFrame(self)
        frame.pack(pady=15, padx=20, fill="x")

        ctk.CTkLabel(frame, text=label, font=("", 16, "bold")).grid(
            row=0, column=0, columnspan=4, pady=(10, 5), padx=10, sticky="w"
        )
        status_label = ctk.CTkLabel(frame, text="Status: (checking...)")
        status_label.grid(row=1, column=0, columnspan=4, pady=(0, 10), padx=10, sticky="w")

        start_btn = ctk.CTkButton(
            frame, text="Start", width=90, command=lambda: self._run_action(task_name, start_task)
        )
        stop_btn = ctk.CTkButton(
            frame, text="Stop", width=90, fg_color="#8b1a1a", hover_color="#5c1111",
            command=lambda: self._run_action(task_name, stop_task),
        )
        restart_btn = ctk.CTkButton(
            frame, text="Restart", width=90, command=lambda: self._run_action(task_name, restart_task)
        )
        start_btn.grid(row=2, column=0, padx=10, pady=(0, 10))
        stop_btn.grid(row=2, column=1, padx=10, pady=(0, 10))
        restart_btn.grid(row=2, column=2, padx=10, pady=(0, 10))

        return {"task_name": task_name, "status_label": status_label}

    def _run_action(self, task_name: str, action) -> None:
        def worker():
            action(task_name)
            self.app.after(300, self.refresh_all)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_all(self) -> None:
        for row in (self.bot_row, self.pot_row):
            self._refresh_row(row)

    def _refresh_row(self, row: dict) -> None:
        def worker():
            status = query_task_status(row["task_name"])
            self.app.after(0, lambda: row["status_label"].configure(text=f"Status: {status}"))

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_auto_refresh(self) -> None:
        self.refresh_all()
        self.app.after(5000, self._schedule_auto_refresh)


class LogsTab(ctk.CTkFrame):
    def __init__(self, master, app: "Dashboard"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._queue: list[str] = []

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(pady=10, fill="x")
        ctk.CTkButton(button_row, text="Clear view", width=100, command=self._clear).pack(
            side="left", padx=10
        )

        self.textbox = ctk.CTkTextbox(self, wrap="word", state="disabled")
        self.textbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self._append(read_log_tail())

        self._tailer = LogTailer(self._queue)
        self._tailer.start()
        self._poll_queue()

    def _append(self, text: str) -> None:
        if not text:
            return
        self.textbox.configure(state="normal")
        self.textbox.insert("end", text)
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


class Dashboard(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Music Bot Control Panel")
        self.geometry("640x560")

        tabview = ctk.CTkTabview(self)
        tabview.pack(fill="both", expand=True, padx=10, pady=10)

        db_tab = tabview.add("Database")
        config_tab = tabview.add("Configuration")
        services_tab = tabview.add("Services")
        logs_tab = tabview.add("Logs")

        DatabaseTab(db_tab, self).pack(fill="both", expand=True)
        ConfigTab(config_tab, self).pack(fill="both", expand=True)
        ServicesTab(services_tab, self).pack(fill="both", expand=True)
        self.logs_tab = LogsTab(logs_tab, self)
        self.logs_tab.pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.logs_tab.stop()
        self.destroy()


if __name__ == "__main__":
    Dashboard().mainloop()
