"""Profile and process management for Claude/Codex Multi-Instance.

State lives next to this file:
  <App.profiles_dirname>/<name>/  -> --user-data-dir for each profile
  state.json                       -> selected app + active profile per app
"""
from __future__ import annotations

import errno
import json
import os
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
    userdata_dirname="Claude",
)
CODEX = App(
    key="codex", display="Codex",
    package_filter="*Codex*", publisher_hash="2p2nqsd0c76g0",
    package_prefix="OpenAI.Codex_", exe_name="Codex.exe",
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
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
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


def _force_rw(func, path, _exc):
    """rmtree error handler: clear read-only bit (Git pack .idx files set it) and retry."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    # onexc replaces onerror in 3.12+; pass both for portability.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_rw)
    else:
        shutil.rmtree(path, onerror=_force_rw)


def delete_profile(name: str) -> None:
    profile = PROFILES_DIR / name
    lnk = desktop_dir() / shortcut_filename(name)
    if lnk.exists():
        try:
            lnk.unlink()
        except OSError:
            pass
    if profile.exists():
        _rmtree(profile)


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


def running_profiles() -> dict[str, list[int]]:
    """Return {profile_name: [pids]} for processes of the current app using
    one of our --user-data-dir paths."""
    result: dict[str, list[int]] = {}
    flag = "--user-data-dir="
    profiles_root = str(PROFILES_DIR).lower()

    for pid, _exe, cmd in _processes(_current.exe_name):
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
        result.setdefault(Path(path_str).name, []).append(pid)
    return result


def close_profile(name: str) -> int:
    return _kill_pids(running_profiles().get(name, []))


def profile_env(name: str) -> dict[str, str]:
    """Return the environment Popen should use for `name` under the current app.
    Adds every per-profile env override defined on the app (e.g. CODEX_HOME,
    CODEX_ELECTRON_USER_DATA_PATH)."""
    env = dict(os.environ)
    for var, sub in _current.env_overrides:
        target = PROFILES_DIR / name / sub
        target.mkdir(parents=True, exist_ok=True)
        env[var] = str(target)
    return env


def launch_profile(name: str, exe: Path | None = None) -> None:
    exe = exe or find_app_exe()
    if exe is None:
        raise RuntimeError(f"{_current.display} not found. Is the desktop app installed?")
    data_dir = PROFILES_DIR / name
    data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(exe), f"--user-data-dir={data_dir}"],
        env=profile_env(name),
        creationflags=DETACHED_FLAGS, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# --- Default-slot sign-in (snapshot model) --------------------------------- #
# The MSIX package owns the app's protocol (claude://…): every sign-in
# callback — email magic link or OAuth — lands in the instance launched
# WITHOUT --user-data-dir (the "default slot"). Redirecting the protocol per
# profile is impossible on current Windows: the UCPD driver ignores
# programmatic UserChoice writes and MSIX protocol activation bypasses the
# registry entirely. So profiles get signed in by snapshot instead: stash the
# default slot aside, let the user sign in there (the callback works, it's
# the real default app), move the freshly signed-in state into the profile,
# restore the stash. DPAPI blobs are scoped to the Windows user, not to the
# folder, so the moved state stays fully readable — usage.py already relies
# on exactly that.
#
# WHERE the default slot really lives: the packaged app THINKS its userData
# is %APPDATA%\<userdata_dirname>, but MSIX filesystem virtualization
# redirects every access to the package's LocalCache. Verified empirically:
# the plain %APPDATA%\Claude does not even exist outside the container —
# the real signed-in state sits in
#   %LOCALAPPDATA%\Packages\<family>\LocalCache\Roaming\<userdata_dirname>
# and that path is what this tool (running OUTSIDE the container) must move.

BACKUP_SUFFIX = ".mi-backup"


def default_slot_dir(app: App | None = None) -> Path | None:
    """Real on-disk userData dir of the app's DEFAULT instance — the only dir
    that can receive the sign-in callback. None when the app has no snapshot
    support."""
    app = app or _current
    if not app.userdata_dirname:
        return None
    # MSIX install: state is virtualized into the package's LocalCache.
    # PackageFamilyName is <Name>_<publisher hash> = package_prefix + hash.
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    pkg_root = Path(local) / "Packages" / (app.package_prefix + app.publisher_hash)
    if pkg_root.is_dir():
        return pkg_root / "LocalCache" / "Roaming" / app.userdata_dirname
    # Unpackaged install: the plain %APPDATA% path is the real one.
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / app.userdata_dirname


def supports_login_snapshot(app: App | None = None) -> bool:
    return default_slot_dir(app) is not None


def backup_dir(app: App | None = None) -> Path | None:
    slot = default_slot_dir(app)
    return slot.with_name(slot.name + BACKUP_SUFFIX) if slot else None


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
        if "--user-data-dir=" not in low and "--type=" not in low:
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
    if slot is None or not slot.is_dir():
        return False
    _move_retry(slot, backup)
    return True


def restore_default_slot() -> None:
    """Bring the stashed main session back, discarding whatever the sign-in flow
    left in the default location. No-op when there is no stash. The caller MUST
    have killed the fresh instance first, or the slot's files stay locked. The
    backup is never removed until it is safely renamed into place, so a failure
    here leaves the main session intact on disk for recovery."""
    slot, backup = default_slot_dir(), backup_dir()
    if backup is None or not backup.is_dir():
        return
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
    profile = PROFILES_DIR / name
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
