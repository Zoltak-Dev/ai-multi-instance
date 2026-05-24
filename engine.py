"""Profile and process management for Claude Multi-Instance.

State lives next to this file:
  ClaudeProfiles/<name>/  -> Claude's --user-data-dir for each profile
  .active_profile         -> name of the last profile launched (for claude:// routing)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import _userchoice as uc

PUBLISHER_HASH = "pzs8sxrjxfjjc"


def _app_dir() -> Path:
    """Directory holding state files (profiles, .active_profile, launcher).
    Frozen exe: next to the .exe. Source: next to engine.py."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_DIR = _app_dir()
PROFILES_DIR = PROJECT_DIR / "ClaudeProfiles"
LAUNCHER = PROJECT_DIR / "launcher.pyw"           # used in source mode
LAUNCHER_EXE = PROJECT_DIR / "launcher.exe"       # used in frozen mode
ACTIVE_FILE = PROJECT_DIR / ".active_profile"

PROTOCOL = "claude"
PROGID = "ClaudeMultiInstance"
INVALID_CHARS = '<>:"/\\|?*'

NO_WINDOW = 0x08000000                       # CREATE_NO_WINDOW
DETACHED_FLAGS = 0x00000008 | 0x00000200     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _launcher_invocation(arg: str) -> tuple[str, str]:
    """Return (target_executable, args_string) to invoke the launcher with `arg`.
    Frozen: launcher.exe + "<arg>".  Source: pythonw.exe + "launcher.pyw" "<arg>"."""
    if getattr(sys, "frozen", False):
        return str(LAUNCHER_EXE), f'"{arg}"'
    return pythonw_path(), f'"{LAUNCHER}" "{arg}"'


# --- Claude executable ---------------------------------------------------- #
_exe_cache: tuple[bool, Path | None] = (False, None)


def find_claude_exe(refresh: bool = False) -> Path | None:
    """Locate claude.exe. The MSIX install path changes on every Claude update,
    so this is queried via Get-AppxPackage and cached for the session."""
    global _exe_cache
    if not refresh and _exe_cache[0]:
        return _exe_cache[1]

    exe: Path | None = None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name *Claude*).InstallLocation"],
            capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        candidate = Path(line.strip()) / "app" / "claude.exe"
        if candidate.is_file():
            exe = candidate
            break

    if exe is None:
        apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        try:
            for pkg in sorted(apps.glob(f"Claude_*__{PUBLISHER_HASH}"), reverse=True):
                candidate = pkg / "app" / "claude.exe"
                if candidate.is_file():
                    exe = candidate
                    break
        except OSError:
            pass

    _exe_cache = (True, exe)
    return exe


def claude_version(exe: Path | None) -> str:
    if exe is None:
        return ""
    try:
        return exe.parent.parent.name.split("_")[1]
    except IndexError:
        return "?"


# --- Profiles ------------------------------------------------------------- #
def list_profiles() -> list[Path]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted((d for d in PROFILES_DIR.iterdir() if d.is_dir()),
                  key=lambda p: p.name.lower())


def valid_profile_name(name: str) -> bool:
    return bool(name) and not any(c in INVALID_CHARS for c in name)


def create_profile(name: str) -> Path:
    if not valid_profile_name(name):
        raise ValueError(f"Invalid name: {name!r}")
    profile = PROFILES_DIR / name
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def delete_profile(name: str) -> None:
    """Delete the profile directory and its desktop shortcut.

    Raises OSError if Claude is still holding files in the directory.
    """
    profile = PROFILES_DIR / name
    lnk = desktop_dir() / f"{name}.lnk"
    if lnk.exists():
        try:
            lnk.unlink()
        except OSError:
            pass
    if profile.exists():
        shutil.rmtree(profile)
    if active_profile() == name:
        try:
            ACTIVE_FILE.unlink()
        except OSError:
            pass


def rename_profile(old: str, new: str) -> None:
    if not valid_profile_name(new):
        raise ValueError(f"Invalid name: {new!r}")
    if old == new:
        return
    src = PROFILES_DIR / old
    dst = PROFILES_DIR / new
    if not src.is_dir():
        raise FileNotFoundError(f"Profile not found: {old}")
    if dst.exists():
        raise FileExistsError(f"A profile named {new!r} already exists.")
    src.rename(dst)

    if shortcut_exists(old):
        delete_shortcut(old)
        create_shortcut(new)

    if active_profile() == old:
        ACTIVE_FILE.write_text(new, encoding="utf-8")


def active_profile() -> str:
    """Last profile launched, or '' if the file is absent or stale."""
    if not ACTIVE_FILE.exists():
        return ""
    try:
        name = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not name or not (PROFILES_DIR / name).is_dir():
        return ""
    return name


# --- Running processes ---------------------------------------------------- #
def running_profiles() -> dict[str, list[int]]:
    """Return {profile_name: [pids]} for every Claude.exe currently using one
    of our --user-data-dir paths. Includes Electron child processes."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='Claude.exe'\" "
             "| ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    result: dict[str, list[int]] = {}
    flag = "--user-data-dir="
    profiles_root = str(PROFILES_DIR).lower()

    for line in out.stdout.splitlines():
        if "|" not in line:
            continue
        pid_str, _, cmd = line.partition("|")
        pid_str = pid_str.strip()
        if not pid_str.isdigit():
            continue
        idx = cmd.lower().find(flag)
        if idx == -1:
            continue
        rest = cmd[idx + len(flag):]
        if rest.startswith('"'):
            path_str = rest[1:].split('"', 1)[0]
        else:
            path_str = rest.split()[0] if rest else ""
        if not path_str:
            continue
        try:
            parent = str(Path(path_str).parent).lower()
        except OSError:
            continue
        if parent != profiles_root:
            continue
        result.setdefault(Path(path_str).name, []).append(int(pid_str))
    return result


def close_profile(name: str) -> int:
    """Kill the Claude.exe tree for this profile. Returns number of PIDs killed."""
    pids = running_profiles().get(name, [])
    killed = 0
    for pid in pids:
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, creationflags=NO_WINDOW,
            )
            if r.returncode == 0:
                killed += 1
        except (OSError, subprocess.SubprocessError):
            pass
    return killed


def launch_profile(name: str, exe: Path | None = None) -> None:
    exe = exe or find_claude_exe()
    if exe is None:
        raise RuntimeError("Claude not found. Is the desktop app installed?")
    data_dir = PROFILES_DIR / name
    data_dir.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(name, encoding="utf-8")
    # Silence Electron stdout/stderr so node warnings don't bleed into our terminal.
    subprocess.Popen(
        [str(exe), f"--user-data-dir={data_dir}"],
        creationflags=DETACHED_FLAGS, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# --- Desktop shortcuts ---------------------------------------------------- #
_desktop_cache: Path | None = None


def desktop_dir() -> Path:
    global _desktop_cache
    if _desktop_cache is not None:
        return _desktop_cache
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
        ).stdout.strip()
        if out:
            _desktop_cache = Path(out)
            return _desktop_cache
    except (OSError, subprocess.SubprocessError):
        pass
    _desktop_cache = Path.home() / "Desktop"
    return _desktop_cache


def pythonw_path() -> str:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


def create_shortcut(name: str, exe: Path | None = None) -> Path:
    exe = exe or find_claude_exe()
    lnk = desktop_dir() / f"{name}.lnk"
    target, args = _launcher_invocation(name)
    env = dict(
        os.environ,
        SC_LNK=str(lnk),
        SC_TARGET=target,
        SC_ARGS=args,
        SC_WORK=str(PROJECT_DIR),
        SC_ICON=str(exe) if exe else target,
    )
    script = (
        "$w=New-Object -ComObject WScript.Shell;"
        "$s=$w.CreateShortcut($env:SC_LNK);"
        "$s.TargetPath=$env:SC_TARGET;"
        "$s.Arguments=$env:SC_ARGS;"
        "$s.WorkingDirectory=$env:SC_WORK;"
        "$s.IconLocation=$env:SC_ICON;"
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        env=env, capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    return lnk


def shortcut_exists(name: str) -> bool:
    return (desktop_dir() / f"{name}.lnk").exists()


def delete_shortcut(name: str) -> bool:
    lnk = desktop_dir() / f"{name}.lnk"
    if not lnk.exists():
        return False
    try:
        lnk.unlink()
        return True
    except OSError:
        return False


# --- Login routing (claude:// -> active profile) -------------------------- #
def login_routing_enabled() -> bool:
    return uc.current_default(PROTOCOL) == PROGID


def enable_login_routing() -> None:
    target, args = _launcher_invocation("%1")
    command = f'"{target}" {args}'
    uc.register_progid(PROGID, command)
    uc.set_protocol_default(PROTOCOL, PROGID)


def disable_login_routing() -> None:
    uc.clear_protocol_default(PROTOCOL)
