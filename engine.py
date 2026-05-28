"""Profile and process management for Claude/Codex Multi-Instance.

State lives next to this file:
  <App.profiles_dirname>/<name>/  -> --user-data-dir for each profile
  <App.active_filename>           -> name of the last profile launched
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import _userchoice as uc


@dataclass(frozen=True)
class App:
    key: str
    display: str
    package_filter: str    # Get-AppxPackage -Name pattern
    publisher_hash: str    # WindowsApps fallback suffix
    package_prefix: str    # WindowsApps folder prefix, e.g. "Claude_" or "OpenAI.Codex_"
    exe_name: str          # filename inside <pkg>/app/
    protocol: str          # URL scheme for OAuth callbacks
    progid: str            # registry ProgID for protocol routing
    profiles_dirname: str
    active_filename: str


CLAUDE = App(
    key="claude", display="Claude",
    package_filter="*Claude*", publisher_hash="pzs8sxrjxfjjc",
    package_prefix="Claude_", exe_name="claude.exe",
    protocol="claude", progid="ClaudeMultiInstance",
    profiles_dirname="ClaudeProfiles", active_filename=".active_claude",
)
CODEX = App(
    key="codex", display="Codex",
    package_filter="*Codex*", publisher_hash="2p2nqsd0c76g0",
    package_prefix="OpenAI.Codex_", exe_name="Codex.exe",
    protocol="codex", progid="CodexMultiInstance",
    profiles_dirname="CodexProfiles", active_filename=".active_codex",
)
APPS: dict[str, App] = {a.key: a for a in (CLAUDE, CODEX)}

INVALID_CHARS = '<>:"/\\|?*'
NO_WINDOW = 0x08000000
DETACHED_FLAGS = 0x00000008 | 0x00000200


def _app_dir() -> Path:
    """Directory holding state files. Frozen exe: next to .exe. Source: next to engine.py."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_DIR = _app_dir()
LAUNCHER = PROJECT_DIR / "launcher.pyw"
LAUNCHER_EXE = PROJECT_DIR / "launcher.exe"

# Current app state (mutable via set_app). PROFILES_DIR and ACTIVE_FILE are kept
# as module attributes so the rest of the code reads cleanly.
_current: App = CLAUDE
PROFILES_DIR: Path = PROJECT_DIR / CLAUDE.profiles_dirname
ACTIVE_FILE: Path = PROJECT_DIR / CLAUDE.active_filename


def current_app() -> App:
    return _current


def set_app(app: App) -> None:
    global _current, PROFILES_DIR, ACTIVE_FILE
    _current = app
    PROFILES_DIR = PROJECT_DIR / app.profiles_dirname
    ACTIVE_FILE = PROJECT_DIR / app.active_filename
    _exe_cache.clear()


def _launcher_invocation(arg: str, app: App | None = None) -> tuple[str, str]:
    """Return (target_executable, args_string) for launcher with `arg`.
    If `app` is given, prefix with --app=<key> so the launcher knows which app."""
    app = app or _current
    arg_prefix = f"--app={app.key} "
    if getattr(sys, "frozen", False):
        return str(LAUNCHER_EXE), f'{arg_prefix}"{arg}"'
    return pythonw_path(), f'"{LAUNCHER}" {arg_prefix}"{arg}"'


# --- Executable discovery ------------------------------------------------- #
_exe_cache: dict[str, Path | None] = {}


def find_app_exe(refresh: bool = False, app: App | None = None) -> Path | None:
    """Locate the app's main exe. MSIX install path changes on every update,
    so query Get-AppxPackage and cache per session."""
    app = app or _current
    if not refresh and app.key in _exe_cache:
        return _exe_cache[app.key]

    exe: Path | None = None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-AppxPackage -Name {app.package_filter}).InstallLocation"],
            capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        candidate = Path(line.strip()) / "app" / app.exe_name
        if candidate.is_file():
            exe = candidate
            break

    if exe is None:
        apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        try:
            for pkg in sorted(apps.glob(f"{app.package_prefix}*__{app.publisher_hash}"), reverse=True):
                candidate = pkg / "app" / app.exe_name
                if candidate.is_file():
                    exe = candidate
                    break
        except OSError:
            pass

    _exe_cache[app.key] = exe
    return exe


def app_version(exe: Path | None) -> str:
    if exe is None:
        return ""
    try:
        return exe.parent.parent.name.split("_")[1]
    except IndexError:
        return "?"


# Back-compat aliases (in case anything external imports the old names).
find_claude_exe = find_app_exe
claude_version = app_version


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
    profile = PROFILES_DIR / name
    lnk = desktop_dir() / shortcut_filename(name)
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
    """Return {profile_name: [pids]} for processes of the current app using
    one of our --user-data-dir paths."""
    exe_name = _current.exe_name
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" "
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
    exe = exe or find_app_exe()
    if exe is None:
        raise RuntimeError(f"{_current.display} not found. Is the desktop app installed?")
    data_dir = PROFILES_DIR / name
    data_dir.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(name, encoding="utf-8")
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


def shortcut_filename(name: str) -> str:
    """Per-app shortcut filename, prefixed so Claude and Codex profiles don't collide."""
    return f"{_current.display} - {name}.lnk"


def create_shortcut(name: str, exe: Path | None = None) -> Path:
    exe = exe or find_app_exe()
    lnk = desktop_dir() / shortcut_filename(name)
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
    return (desktop_dir() / shortcut_filename(name)).exists()


def delete_shortcut(name: str) -> bool:
    lnk = desktop_dir() / shortcut_filename(name)
    if not lnk.exists():
        return False
    try:
        lnk.unlink()
        return True
    except OSError:
        return False


# --- Login routing (claude://, codex:// -> active profile of that app) ---- #
def login_routing_enabled() -> bool:
    return uc.current_default(_current.protocol) == _current.progid


def enable_login_routing() -> None:
    target, args = _launcher_invocation("%1")
    command = f'"{target}" {args}'
    friendly = f"URL:{_current.display} Multi-Instance"
    uc.register_progid(_current.progid, command, friendly=friendly)
    uc.set_protocol_default(_current.protocol, _current.progid)


def disable_login_routing() -> None:
    uc.clear_protocol_default(_current.protocol)
