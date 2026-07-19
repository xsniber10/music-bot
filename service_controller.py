"""
Abstract Service Controller — generic process/Scheduled-Task management for
any BotConfig from bot_registry. Nothing in here is specific to any one bot;
all per-service behavior comes from the registry entry passed in.
"""

import json
import subprocess
import time

import psutil

import bot_registry
from bot_registry import BASE_DIR, BotConfig

HIDDEN_LAUNCHER_PATH = BASE_DIR / "hidden_launcher.vbs"


# --------------------------------------------------------------------------------
# Low-level command runners
# --------------------------------------------------------------------------------


def run_schtasks(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=["schtasks", *args], returncode=1, stdout="", stderr=str(exc))


def run_powershell(command: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=["powershell"], returncode=1, stdout="", stderr=str(exc))


def _ps_quote(value: str) -> str:
    """Escapes a value for embedding in a single-quoted PowerShell string literal."""
    return value.replace("'", "''")


# --------------------------------------------------------------------------------
# PID matching
#
# Both python-based bots run an identically-named, identically-invoked
# "venv\Scripts\python.exe -u <script>" — since they're separate projects, a
# plain command-line substring match on the script name can't tell them
# apart. "venv_project" mode instead matches by the venv's own path (e.g.
# "...\music-bot\venv\..."), then the match is extended to any child process
# (the venv launcher re-execs into the real interpreter, which reports a
# different, non-project-specific ExecutablePath) via the parent/child PID
# chain. Services with no per-project venv (e.g. a Node service) use
# "cmdline" mode: a plain command-line substring match, which is fine as long
# as no two registered services share both a process name and a script name.
# --------------------------------------------------------------------------------


def _find_pids_by_cmdline(process_name: str, cmdline_substring: str) -> list[int]:
    ps_command = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{cmdline_substring}*' }} | "
        f"Select-Object -ExpandProperty ProcessId"
    )
    result = run_powershell(ps_command)
    return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]


def _find_pids_by_project(
    process_name: str, project_dir_fragment: str, venv_dir_name: str, script_fragment: str
) -> list[int]:
    """Finds every PID in one project's process family: the venv's python.exe
    (whose ExecutablePath contains the project's own folder name, AND whose
    command line runs the registered main script) plus any child process it
    re-execs into, matched via the parent/child PID chain.

    The command-line check matters because the dashboard may run from the
    exact same venv as a bot it's monitoring — matching on venv path alone
    could also catch the dashboard's own process.

    Uses ConvertTo-Json rather than hand-rolled delimited text: a process's
    own CommandLine can itself contain embedded newlines, which breaks any
    parsing that assumes one process per line. JSON properly escapes that
    instead of silently corrupting the row split."""
    ps_command = (
        f"Get-CimInstance Win32_Process -Filter \"Name='{process_name}'\" | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    result = run_powershell(ps_command)

    try:
        parsed = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):  # PowerShell unwraps single-element results to one object
        parsed = [parsed]

    rows: list[tuple[int, int, str, str]] = [
        (
            entry["ProcessId"],
            entry["ParentProcessId"],
            entry.get("ExecutablePath") or "",
            entry.get("CommandLine") or "",
        )
        for entry in parsed
    ]

    fragment = f"\\{project_dir_fragment}\\{venv_dir_name}\\".lower()
    script = script_fragment.lower()
    matched = {
        pid
        for pid, _ppid, exe, cmdline in rows
        if exe and fragment in exe.lower() and script in cmdline.lower()
    }

    changed = True
    while changed:
        changed = False
        for pid, ppid, _exe, _cmdline in rows:
            if ppid in matched and pid not in matched:
                matched.add(pid)
                changed = True

    return sorted(matched)


def find_pids(bot: BotConfig) -> list[int]:
    if bot.match_mode == "venv_project":
        return _find_pids_by_project(
            bot.process_name, bot.project_dir_fragment, bot.venv_dir_name, bot.cmdline_fragment
        )
    return _find_pids_by_cmdline(bot.process_name, bot.cmdline_fragment)


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


# --------------------------------------------------------------------------------
# Status / start / stop / restart
# --------------------------------------------------------------------------------


def query_status(bot: BotConfig) -> str:
    pids = find_pids(bot)
    return f"Running (PID {pids[0]})" if pids else "Stopped"


def task_exists(task_name: str) -> bool:
    return run_schtasks("/query", "/tn", task_name).returncode == 0


def ensure_scheduled_task(bot: BotConfig) -> str | None:
    """Creates bot's Scheduled Task if it doesn't already exist yet. Returns
    None on success (including when the task already existed and was left
    untouched), or an error string. This never modifies an existing task —
    it only fills the gap for a bot that was just added to the registry."""
    if task_exists(bot.task_name):
        return None

    launcher = _ps_quote(str(HIDDEN_LAUNCHER_PATH))
    workdir = _ps_quote(str(bot.directory))
    cmdline = _ps_quote(bot.build_launch_command())
    task_name = _ps_quote(bot.task_name)

    ps_command = (
        "$action = New-ScheduledTaskAction -Execute 'wscript.exe' "
        f"-Argument '//B \"{launcher}\" \"{workdir}\" \"{cmdline}\"'; "
        "$logon = New-ScheduledTaskTrigger -AtLogOn; "
        "$heal = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        "-RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650); "
        "$heal.Enabled = $false; "
        f"Register-ScheduledTask -TaskName '{task_name}' -Action $action "
        "-Trigger @($logon,$heal) -Description 'Auto-created by Bot Core Management System' -Force | Out-Null"
    )
    result = run_powershell(ps_command)
    if result.returncode != 0:
        return result.stderr.strip() or "failed to create scheduled task"
    return None


def start(bot: BotConfig) -> str:
    if find_pids(bot):
        return "Already running."
    error = ensure_scheduled_task(bot)
    if error:
        return f"Could not prepare scheduled task: {error}"
    result = run_schtasks("/run", "/tn", bot.task_name)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()


def stop(bot: BotConfig) -> str:
    pids = find_pids(bot)
    if not pids:
        return "Already stopped."
    _kill_pids(pids)
    return f"Stopped (killed PID {', '.join(map(str, pids))})."


def restart(bot: BotConfig) -> str:
    stop(bot)
    time.sleep(1)
    return start(bot)


def delete_scheduled_task(task_name: str) -> str | None:
    """Deletes task_name if it exists. Returns None on success (including
    when it never existed), or an error string."""
    if not task_exists(task_name):
        return None
    result = run_schtasks("/delete", "/tn", task_name, "/f")
    if result.returncode != 0:
        return result.stderr.strip() or f'failed to delete scheduled task "{task_name}"'
    return None


def remove_bot(bot: BotConfig) -> str:
    """Fully unregisters a bot: stops its process, deletes its Scheduled
    Task, and removes its entry from bots_config.json. Stopping and task
    deletion are best-effort (a bot that's already stopped, or has no task
    yet, isn't an error) — but a failure to remove the registry entry is
    surfaced, since that's the step that actually unregisters it."""
    stop(bot)
    task_error = delete_scheduled_task(bot.task_name)
    try:
        bot_registry.remove_bot_entry(bot.id)
    except (ValueError, OSError) as exc:
        return f'Stopped "{bot.name}" but could not remove it from the registry: {exc}'
    if task_error:
        return f'Removed "{bot.name}" from the registry, but could not delete its Scheduled Task: {task_error}'
    return f'Removed "{bot.name}".'


# --------------------------------------------------------------------------------
# Self-heal trigger toggle (the 5-minute repeating trigger alongside "At logon")
# --------------------------------------------------------------------------------


def get_self_heal_enabled(bot: BotConfig) -> bool:
    ps_command = (
        f"$t = Get-ScheduledTask -TaskName '{_ps_quote(bot.task_name)}'; "
        f"($t.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }}).Enabled"
    )
    result = run_powershell(ps_command)
    return result.stdout.strip().lower() == "true"


def set_self_heal_enabled(bot: BotConfig, enabled: bool) -> None:
    value = "$true" if enabled else "$false"
    ps_command = (
        f"$t = Get-ScheduledTask -TaskName '{_ps_quote(bot.task_name)}'; "
        f"($t.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }}).Enabled = {value}; "
        f"Set-ScheduledTask -TaskName '{_ps_quote(bot.task_name)}' -Trigger $t.Triggers | Out-Null"
    )
    run_powershell(ps_command)


# --------------------------------------------------------------------------------
# Process resource stats
# --------------------------------------------------------------------------------

_psutil_procs: dict[int, psutil.Process] = {}


def get_process_stats(bot: BotConfig) -> dict | None:
    """Aggregates CPU/RAM across every matching PID (the venv's python.exe
    launcher plus the real interpreter it spawns, both belonging to the same
    project's process family), rather than trying to guess which single PID
    is the "real" one."""
    pids = find_pids(bot)
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
