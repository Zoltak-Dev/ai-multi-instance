"""Profile and process management for Claude/Codex Multi-Instance.

State lives next to this file:
  <App.profiles_dirname>/<name>/  -> --user-data-dir for each profile
  state.json                       -> selected app + active profile per app
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class App:
    key: str
    display: str
    package_filter: str    # Get-AppxPackage -Name pattern
    publisher_hash: str    # WindowsApps fallback suffix
    package_prefix: str    # WindowsApps folder prefix, e.g. "Claude_" or "OpenAI.Codex_"
    exe_name: str          # filename inside <pkg>/app/
    profiles_dirname: str
    # Optional per-profile env overrides. Used when the app stores state
    # outside the Chromium --user-data-dir (e.g. Codex's ~/.codex/auth.json)
    # or when it explicitly ignores --user-data-dir and reads a custom env
    # var (e.g. CODEX_ELECTRON_USER_DATA_PATH, which Codex consults BEFORE
    # its singleton check so different values yield separate instances).
    # Format: tuple of (env_var_name, relative_subpath_under_profile_dir).
    env_overrides: tuple[tuple[str, str], ...] = ()
    # Logical userData folder name of the app's DEFAULT instance (no
    # --user-data-dir). That default slot is the only place the app's
    # protocol sign-in callback (claude://…) can ever land — see the
    # "Default-slot sign-in" section for where it REALLY lives on disk.
    # Empty = snapshot sign-in unsupported.
    userdata_dirname: str = ""


CLAUDE = App(
    key="claude", display="Claude",
    package_filter="*Claude*", publisher_hash="pzs8sxrjxfjjc",
    package_prefix="Claude_", exe_name="claude.exe",
    profiles_dirname="ClaudeProfiles",
    # Claude 2.x+ can redirect userData before its singleton check. Keep its
    # relocation target separate as well as passing --user-data-dir.
    env_overrides=(("LOCALAPPDATA", ".localappdata"),),
    userdata_dirname="Claude",
)
CODEX = App(
    key="codex", display="Codex",
    package_filter="*Codex*", publisher_hash="2p2nqsd0c76g0",
    # The package's entry point is app/ChatGPT.exe (see the AppxManifest
    # <Application Executable="app/ChatGPT.exe">), NOT app/Codex.exe. Codex.exe
    # is the updater trampoline (windows_update_trampoline) and carries a
    # process trust label (SID S-1-19-512-4096) that strips execute rights from
    # ordinary processes: CreateProcess on it fails with WinError 5 even though
    # the file is readable and the ACL grants Users ReadAndExecute.
    package_prefix="OpenAI.Codex_", exe_name="ChatGPT.exe",
    profiles_dirname="CodexProfiles",
    env_overrides=(
        # CLI auth (~/.codex/auth.json) — keeps OpenAI sign-in per profile.
        ("CODEX_HOME", ".codex"),
        # Electron userData path — Codex reads this BEFORE its singleton
        # check, so each profile gets its own Electron lock scope and runs
        # truly in parallel. See app.asar bootstrap.js.
        ("CODEX_ELECTRON_USER_DATA_PATH", "electron"),
    ),
)
APPS: dict[str, App] = {a.key: a for a in (CLAUDE, CODEX)}

INVALID_CHARS = '<>:"/\\|?*'
NO_WINDOW = 0x08000000
DETACHED_FLAGS = 0x00000008 | 0x00000200

_exe_cache: dict[str, Path | None] = {}


def _app_dir() -> Path:
    """Directory holding state files. Frozen exe: next to .exe. Source: next to engine.py."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


PROJECT_DIR = _app_dir()
LAUNCHER = PROJECT_DIR / "launcher.pyw"
LAUNCHER_EXE = PROJECT_DIR / "launcher.exe"
STATE_FILE = PROJECT_DIR / "state.json"


# --- Consolidated state (state.json) -------------------------------------- #
# Schema:  { "selected_app": "claude"|"codex" }
def _load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


# Current app (mutable via set_app). PROFILES_DIR mirrors it for cleaner reads.
_current: App = CLAUDE
PROFILES_DIR: Path = PROJECT_DIR / CLAUDE.profiles_dirname


def current_app() -> App:
    return _current


def set_app(app: App, *, persist: bool = True) -> None:
    global _current, PROFILES_DIR
    _current = app
    PROFILES_DIR = PROJECT_DIR / app.profiles_dirname
    if persist:
        state = _load_state()
        state["selected_app"] = app.key
        _save_state(state)


def _restore_selected_app() -> None:
    key = _load_state().get("selected_app")
    if isinstance(key, str) and key in APPS and key != _current.key:
        set_app(APPS[key], persist=False)


_restore_selected_app()


def _launcher_invocation(arg: str, app: App | None = None) -> tuple[str, str]:
    """Return (target_executable, args_string) for launcher with `arg`. The
    args string always carries --app=<key> so the launcher routes to the right
    app — defaults to the current one when `app` is omitted."""
    app = app or _current
    arg_prefix = f"--app={app.key} "
    if getattr(sys, "frozen", False):
        return str(LAUNCHER_EXE), f'{arg_prefix}"{arg}"'
    return pythonw_path(), f'"{LAUNCHER}" {arg_prefix}"{arg}"'


# --- Executable discovery ------------------------------------------------- #
def find_app_exe(refresh: bool = False, app: App | None = None) -> Path | None:
    """Locate the app's main exe. MSIX install path changes on every update,
    so query Get-AppxPackage and cache per session."""
    app = app or _current
    cached = _exe_cache.get(app.key)
    if not refresh and cached is not None and cached.is_file():
        return cached

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


# --- Profiles ------------------------------------------------------------- #
def list_profiles() -> list[Path]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted((d for d in PROFILES_DIR.iterdir() if d.is_dir()),
                  key=lambda p: p.name.lower())


def valid_profile_name(name: str) -> bool:
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{digit}" for prefix in ("COM", "LPT")
                    for digit in "123456789¹²³")
    return (bool(name) and name == name.strip() and not name.endswith(".")
            and not any(c in INVALID_CHARS or ord(c) < 32 for c in name)
            and name.split(".", 1)[0].rstrip().upper() not in reserved)


def _profile_path(name: str) -> Path:
    if not valid_profile_name(name):
        raise ValueError(f"Invalid profile name: {name!r}")
    profile = PROFILES_DIR / name
    if profile.is_symlink() or profile.resolve().parent != PROFILES_DIR.resolve():
        raise ValueError(f"Profile must be a directory inside {PROFILES_DIR}")
    return profile


def create_profile(name: str) -> Path:
    profile = _profile_path(name)
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _force_rw(func, path, _exc):
    """rmtree error handler: clear read-only bit (Git pack .idx files set it) and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: Path) -> None:
    # onexc replaces onerror in 3.12+; pass both for portability.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_rw)
    else:
        shutil.rmtree(path, onerror=_force_rw)


def delete_profile(name: str) -> None:
    profile = _profile_path(name)
    if profile.exists():
        _rmtree(profile)
    lnk = desktop_dir() / shortcut_filename(name)
    if lnk.exists():
        try:
            lnk.unlink()
        except OSError:
            pass


def rename_profile(old: str, new: str) -> None:
    src, dst = _profile_path(old), _profile_path(new)
    if old == new:
        return
    if not src.is_dir():
        raise FileNotFoundError(f"Profile not found: {old}")
    if dst.exists():
        raise FileExistsError(f"A profile named {new!r} already exists.")
    src.rename(dst)

    if shortcut_exists(old):
        delete_shortcut(old)
        create_shortcut(new)


# --- Running processes ---------------------------------------------------- #
def _processes(exe_name: str) -> list[tuple[int, str, str]]:
    """Return (pid, executable path, command line) for every running process
    named `exe_name`."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" "
             "| ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)|$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    procs: list[tuple[int, str, str]] = []
    for line in out.stdout.splitlines():
        pid_str, sep, rest = line.partition("|")
        pid_str = pid_str.strip()
        if not sep or not pid_str.isdigit():
            continue
        exe_path, _, cmd = rest.partition("|")
        procs.append((int(pid_str), exe_path, cmd))
    return procs


def _kill_pids(pids: list[int]) -> int:
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


def _user_data_dir(command: str) -> Path | None:
    # Windows may quote the entire argument or just its value. Splitting on
    # whitespace truncates profile names like "Work account" to "Work".
    match = re.search(
        r'(?:^|\s)(?:"--user-data-dir=([^"]*)"|--user-data-dir=(?:"([^"]*)"|(\S+))'
        r'|--user-data-dir\s+(?:"([^"]*)"|(\S+)))', command, re.IGNORECASE)
    value = next((part for part in match.groups() if part), None) if match else None
    return Path(value) if value else None


def running_profiles() -> dict[str, list[int]]:
    """Return {profile_name: [pids]} for processes of the current app using
    one of our --user-data-dir paths."""
    result: dict[str, list[int]] = {}
    profiles_root = str(PROFILES_DIR).lower()

    for pid, _exe, cmd in _processes(_current.exe_name):
        data = _user_data_dir(cmd)
        if data is None:
            continue
        try:
            parent = str(data.parent).lower()
        except OSError:
            continue
        if parent != profiles_root:
            continue
        result.setdefault(data.name, []).append(pid)
    return result


def close_profile(name: str) -> int:
    return _kill_pids(running_profiles().get(name, []))


def profile_env(name: str) -> dict[str, str]:
    """Return the environment Popen should use for `name` under the current app.
    Adds every per-profile env override defined on the app (e.g. CODEX_HOME,
    CODEX_ELECTRON_USER_DATA_PATH)."""
    env = dict(os.environ)
    profile = _profile_path(name)
    for var, sub in _current.env_overrides:
        target = profile / sub
        target.mkdir(parents=True, exist_ok=True)
        env[var] = str(target)
    return env


def launch_profile(name: str, exe: Path | None = None) -> None:
    exe = exe or find_app_exe()
    if exe is None:
        raise RuntimeError(f"{_current.display} not found. Is the desktop app installed?")
    data_dir = _profile_path(name)
    data_dir.mkdir(parents=True, exist_ok=True)
    env = profile_env(name)
    if _current is CODEX:
        cli = exe.parent / "resources" / "codex.exe"
        if cli.is_file():
            env["CODEX_CLI_PATH"] = str(cli)
    subprocess.Popen(
        [str(exe), f"--user-data-dir={data_dir}"],
        env=env,
        creationflags=DETACHED_FLAGS, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# --- Default-slot sign-in (snapshot model) --------------------------------- #
# Browser callbacks normally activate the default instance. Stash its data,
# sign in there, adopt the fresh session into a profile, then restore the main
# session. DPAPI data stays readable under the same Windows user.
#
# Claude has used three layouts: Local/Claude-Data/<family>, the MSIX
# LocalCache/Roaming/Claude directory, and plain Roaming/Claude. The old MSIX
# directory may remain as an empty .user-data-left.<family> marker. Probe the
# contents rather than trusting that a directory exists at the expected path.

BACKUP_SUFFIX = ".mi-backup"

# Left behind in the OLD location when the app relocates its userData.
RELOCATION_MARKER_PREFIX = ".user-data-left"

# Entries that exist only inside a userData dir the app has actually used.
# Any one is sufficient; requiring all of them would be brittle across versions.
_USERDATA_MARKERS = ("Local State", "Preferences", "Network")


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _has_relocation_marker(path: Path) -> bool:
    """True when `path` is a directory the app has MOVED OUT of."""
    return path.is_dir() and any(
        child.name.startswith(RELOCATION_MARKER_PREFIX) for child in _safe_iterdir(path)
    )


def _is_live_userdata(path: Path) -> bool:
    """True only when `path` holds real userData. Existence is NOT enough —
    see the layout note above."""
    if not path.is_dir() or _has_relocation_marker(path):
        return False
    return any((path / marker).exists() for marker in _USERDATA_MARKERS)


def slot_candidates(app: App) -> list[Path]:
    """Every layout the default slot could use, most recent first. Built
    unconditionally: `backup_dir()` has to resolve the same way before and
    after a stash, when the live directory is temporarily absent."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    family = app.package_prefix + app.publisher_hash
    return [
        local / f"{app.userdata_dirname}-Data" / family.lower(),
        local / "Packages" / family / "LocalCache" / "Roaming" / app.userdata_dirname,
        appdata / app.userdata_dirname,
    ]


def default_slot_dir(app: App | None = None) -> Path | None:
    """Real on-disk userData dir of the app's DEFAULT instance — the only dir
    that can receive the sign-in callback. None when the app has no snapshot
    support, or when no candidate layout actually holds a profile."""
    app = app or _current
    if not app.userdata_dirname:
        return None
    for candidate in slot_candidates(app):
        if _is_live_userdata(candidate):
            return candidate
    return None


def relocated_slot_dirs(app: App | None = None) -> list[Path]:
    """Candidate layouts the app has moved out of. A non-empty result alongside
    `default_slot_dir() is None` means the layout changed and this tool failed
    to follow it — which is the one state where continuing is unsafe."""
    app = app or _current
    if not app.userdata_dirname:
        return []
    return [c for c in slot_candidates(app) if _has_relocation_marker(c)]


def supports_login_snapshot(app: App | None = None) -> bool:
    """Whether this app has a userData dir the snapshot flow understands.
    Deliberately independent of whether a profile is currently signed in —
    an unused install still supports the flow."""
    app = app or _current
    return bool(app.userdata_dirname)


def backup_dir(app: App | None = None) -> Path | None:
    """Where the stash lives. An EXISTING stash wins wherever it sits, so a
    stash taken under one layout survives an app relocation and can still be
    restored."""
    app = app or _current
    if not app.userdata_dirname:
        return None
    candidates = slot_candidates(app)
    for candidate in candidates:
        backup = candidate.with_name(candidate.name + BACKUP_SUFFIX)
        if backup.is_dir():
            return backup
    live = default_slot_dir(app)
    anchor = live if live is not None else candidates[0]
    return anchor.with_name(anchor.name + BACKUP_SUFFIX)


def _is_packaged_exe(exe_path: str, app: App) -> bool:
    """True when `exe_path` is the MSIX desktop app's own binary. Other
    programs share the exe name — Claude Code's CLI is also claude.exe — so
    matching on the name alone would kill unrelated processes. The packaged
    binary always sits at <pkg>\\app\\<exe> under a <package_prefix>* folder,
    whatever the version."""
    p = Path(exe_path.strip())
    try:
        return (p.name.lower() == app.exe_name.lower()
                and p.parent.name.lower() == "app"
                and p.parent.parent.name.startswith(app.package_prefix))
    except OSError:
        return False


def default_slot_pids() -> list[int]:
    """Main processes of the desktop app running on the default slot: the
    packaged exe itself, with no --user-data-dir (so %APPDATA% state) and no
    --type= (Chromium children)."""
    pids: list[int] = []
    for pid, exe_path, cmd in _processes(_current.exe_name):
        if not _is_packaged_exe(exe_path, _current):
            continue
        low = cmd.lower()
        if _user_data_dir(cmd) is None and "--type=" not in low:
            pids.append(pid)
    return pids


def close_default_slot(*, wait: float = 0.0) -> int:
    """Force-kill the default-slot processes. When `wait` > 0, block until they
    are actually gone (or the timeout elapses) so the folder can be moved."""
    n = _kill_pids(default_slot_pids())
    if wait > 0:
        deadline = time.monotonic() + wait
        while default_slot_pids() and time.monotonic() < deadline:
            time.sleep(0.5)
    return n


def default_slot_running() -> bool:
    return bool(default_slot_pids())


def launch_default_slot(exe: Path | None = None) -> None:
    """Launch the app on its default slot (no --user-data-dir)."""
    exe = exe or find_app_exe()
    if exe is None:
        raise RuntimeError(f"{_current.display} not found. Is the desktop app installed?")
    subprocess.Popen(
        [str(exe)],
        creationflags=DETACHED_FLAGS, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _move_retry(src: Path, dst: Path, timeout: float = 15.0) -> None:
    """Rename with retries — file locks linger a few seconds after taskkill.
    Falls back to copy+delete when src and dst sit on different volumes."""
    if dst.exists():
        raise FileExistsError(f"Destination already exists: {dst}")
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                shutil.move(str(src), str(dst))
                return
            raise


def _rmtree_retry(path: Path, timeout: float = 15.0) -> None:
    """Delete a tree, retrying while file handles from a just-killed process
    are still being released. Raises only if the tree survives the timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            _rmtree(path)
        except OSError:
            pass
        if not path.exists():
            return
        if time.monotonic() >= deadline:
            raise OSError(f"Could not remove {path} — files still in use.")
        time.sleep(0.5)


def stash_default_slot() -> bool:
    """Move the default slot aside before a fresh sign-in. False = nothing
    to stash (the user had no default-slot state). Refuses to clobber an
    existing backup — that would destroy a previously stashed main session."""
    slot, backup = default_slot_dir(), backup_dir()
    if backup is not None and backup.is_dir():
        raise RuntimeError("A stashed session already exists; refusing to overwrite it.")
    if slot is None:
        # No candidate layout holds a profile. Usually that just means the user
        # has never signed in to the default instance — safe, nothing to stash.
        # But if a candidate carries a relocation marker, the app has moved its
        # userData somewhere this tool does not know about, and the "fresh"
        # sign-in would land in the real profile instead. Refuse: a silent
        # mis-isolation is worse than a failed command.
        moved = relocated_slot_dirs()
        if moved:
            raise RuntimeError(
                "The app has relocated its user data and this tool cannot find the "
                "new location, so a sign-in would land in your existing profile. "
                "Relocated: " + "; ".join(str(p) for p in moved)
            )
        return False
    _move_retry(slot, backup)
    return True


def restore_default_slot() -> None:
    """Bring the stashed main session back, discarding whatever the sign-in flow
    left in the default location. No-op when there is no stash. The caller MUST
    have killed the fresh instance first, or the slot's files stay locked. The
    backup is never removed until it is safely renamed into place, so a failure
    here leaves the main session intact on disk for recovery."""
    backup = backup_dir()
    if backup is None or not backup.is_dir():
        return
    if not _is_live_userdata(backup):
        raise RuntimeError(f"Backup {backup} has no recognized profile data. Nothing was removed.")
    # Restore to where the stash was TAKEN FROM, by stripping the suffix —
    # not to whatever default_slot_dir() resolves now. After a cancelled
    # sign-in nothing is live, so probing would return None here; and this way
    # the restore is an exact inverse of the move that created the stash.
    slot = backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])
    live = default_slot_dir()
    if live is not None and live != slot:
        _rmtree_retry(live)  # a fresh sign-in may have migrated to a newer layout
    if slot.is_dir():
        _rmtree_retry(slot)  # the throwaway fresh session; raises if still locked
    _move_retry(backup, slot)


def has_orphan_backup() -> bool:
    """A leftover stash means a previous sign-in flow was interrupted."""
    backup = backup_dir()
    return backup is not None and backup.is_dir()


def discard_backup() -> None:
    backup = backup_dir()
    if backup is not None and backup.is_dir():
        _rmtree(backup)


def adopt_default_into(name: str) -> None:
    """Move the default slot's signed-in state into profile `name`,
    replacing whatever the profile held before."""
    slot = default_slot_dir()
    if slot is None or not slot.is_dir():
        raise RuntimeError("No default-slot state to adopt.")
    profile = _profile_path(name)
    if profile.exists():
        _rmtree_retry(profile)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _move_retry(slot, profile)


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
    _profile_path(name)
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
        check=True, timeout=20,
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
