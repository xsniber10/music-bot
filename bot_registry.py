"""
Bot fleet registry — loads bots_config.json into validated BotConfig objects.

Adding a new service to the dashboard should only require adding one entry
here; nothing in dashboard.py or service_controller.py should need to change.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = BASE_DIR / "bots_config.json"

VALID_MATCH_MODES = {"venv_project", "cmdline"}


@dataclass(frozen=True)
class BotConfig:
    id: str
    name: str
    icon: str
    directory: Path
    interpreter: str
    interpreter_args: list[str]
    main_script: str
    process_name: str
    match_mode: str  # "venv_project" | "cmdline"
    task_name: str
    log_file: Path | None = field(default=None)
    self_heal: bool = True

    @property
    def project_dir_fragment(self) -> str:
        """The project's own folder name, used to match its venv's python.exe
        by ExecutablePath (see service_controller._find_pids_by_project)."""
        return self.directory.name

    @property
    def cmdline_fragment(self) -> str:
        """The main script's filename, used as a command-line substring match
        for services with no per-project venv (see _find_pids_by_cmdline)."""
        return Path(self.main_script).name

    @property
    def venv_dir_name(self) -> str:
        """The venv's own folder name (e.g. "venv", ".venv"), derived from the
        interpreter path's grandparent directory (<venv>\\Scripts\\python.exe)
        rather than assumed — so a bot isn't required to name its venv folder
        literally "venv" for venv_project PID matching to work."""
        return Path(self.interpreter).parent.parent.name or "venv"

    def build_launch_command(self) -> str:
        """The command line a Scheduled Task action runs to start this bot."""
        args = " ".join(self.interpreter_args)
        base = f"{self.interpreter} {args} {self.main_script}".replace("  ", " ").strip()
        if self.log_file is not None:
            return f"{base} >> {self.log_file.name} 2>&1"
        return base


def _parse_entry(raw: dict) -> BotConfig:
    missing = [
        key
        for key in ("id", "name", "icon", "directory", "interpreter", "main_script", "process_name", "match_mode", "task_name")
        if key not in raw
    ]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")

    if raw["match_mode"] not in VALID_MATCH_MODES:
        raise ValueError(f"match_mode must be one of {sorted(VALID_MATCH_MODES)}, got {raw['match_mode']!r}")

    directory = Path(raw["directory"])
    if not directory.is_dir():
        raise ValueError(f"directory does not exist: {directory}")

    if not (directory / raw["main_script"]).exists():
        raise ValueError(f"main_script not found: {directory / raw['main_script']}")

    interpreter = raw["interpreter"]
    # A bare command like "node" is resolved on PATH at launch time; anything
    # that looks like a path (contains a separator) must actually exist,
    # since that's almost always a venv interpreter local to `directory`.
    if ("\\" in interpreter or "/" in interpreter) and not (directory / interpreter).exists():
        raise ValueError(f"interpreter not found: {directory / interpreter}")

    log_file_name = raw.get("log_file")
    log_file = (directory / log_file_name) if log_file_name else None

    return BotConfig(
        id=raw["id"],
        name=raw["name"],
        icon=raw["icon"],
        directory=directory,
        interpreter=interpreter,
        interpreter_args=list(raw.get("interpreter_args", [])),
        main_script=raw["main_script"],
        process_name=raw["process_name"],
        match_mode=raw["match_mode"],
        task_name=raw["task_name"],
        log_file=log_file,
        self_heal=bool(raw.get("self_heal", True)),
    )


def load_registry(path: Path = REGISTRY_PATH) -> tuple[list[BotConfig], list[str]]:
    """Returns (bots, load_errors). A malformed entry is skipped and reported
    in load_errors rather than raising — one bad row in the JSON shouldn't
    take down the whole fleet view."""
    if not path.exists():
        return [], [f"{path.name} not found"]

    try:
        raw_entries = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [f"{path.name} is not valid JSON: {exc}"]

    bots: list[BotConfig] = []
    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_entries):
        label = raw.get("id") or raw.get("name") or f"entry #{i}"
        try:
            bot = _parse_entry(raw)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        if bot.id in seen_ids:
            errors.append(f"{label}: duplicate id {bot.id!r}, skipped")
            continue
        seen_ids.add(bot.id)
        bots.append(bot)

    return bots, errors


def remove_bot_entry(bot_id: str, path: Path = REGISTRY_PATH) -> None:
    """Removes one entry from bots_config.json by id, leaving every other
    entry untouched. Raises ValueError if no entry with that id exists."""
    existing_raw: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    remaining = [entry for entry in existing_raw if entry.get("id") != bot_id]
    if len(remaining) == len(existing_raw):
        raise ValueError(f"no registered bot with id {bot_id!r}")
    path.write_text(json.dumps(remaining, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------
# Adding a bot from the GUI (dashboard.py's "Add Bot" page)
# --------------------------------------------------------------------------------


def _slugify_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "bot"


def _make_unique(candidate: str, taken: set[str], joiner: str = "") -> str:
    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}{joiner}{n}" in taken:
        n += 1
    return f"{candidate}{joiner}{n}"


def add_bot(
    *,
    name: str,
    directory: Path,
    main_script: str,
    venv_path: Path,
    icon: str = "🤖",
    log_file: str | None = "bot_output.log",
    self_heal: bool = True,
    path: Path = REGISTRY_PATH,
) -> BotConfig:
    """Validates and appends one new Python/venv-based bot to bots_config.json,
    then returns the resulting BotConfig. Raises ValueError for any bad input
    — nothing is written to disk unless the whole entry checks out, so a bad
    form submission can't corrupt the registry. id and task_name are derived
    from `name` and de-duplicated against whatever's already registered."""
    if not name.strip():
        raise ValueError("Bot name is required.")
    if not str(directory).strip():
        raise ValueError("Project directory path is required.")
    if not directory.is_dir():
        raise ValueError(f"Project directory does not exist: {directory}")
    if not main_script.strip():
        raise ValueError("Main script name is required.")
    if not (directory / main_script).exists():
        raise ValueError(f"Main script not found: {directory / main_script}")
    if not str(venv_path).strip():
        raise ValueError("Virtual environment path is required.")

    python_exe = venv_path / "Scripts" / "python.exe"
    if not python_exe.exists():
        raise ValueError(f"Not a valid virtual environment (missing {python_exe}).")

    existing_raw: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing_ids = {e.get("id") for e in existing_raw}
    existing_task_names = {e.get("task_name") for e in existing_raw}

    bot_id = _make_unique(_slugify_id(name), existing_ids, joiner="_")
    task_base = "".join(word.capitalize() for word in bot_id.split("_")) or "Bot"
    task_name = _make_unique(task_base, existing_task_names)

    try:
        interpreter = str(python_exe.relative_to(directory))
    except ValueError:
        interpreter = str(python_exe)  # venv lives outside the project directory

    raw_entry = {
        "id": bot_id,
        "name": name.strip(),
        "icon": icon,
        "directory": str(directory),
        "interpreter": interpreter,
        "interpreter_args": ["-u"],
        "main_script": main_script,
        "log_file": log_file,
        "process_name": "python.exe",
        "match_mode": "venv_project",
        "task_name": task_name,
        "self_heal": self_heal,
    }

    bot = _parse_entry(raw_entry)  # re-validates the fully-assembled entry before writing anything

    existing_raw.append(raw_entry)
    path.write_text(json.dumps(existing_raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return bot
