"""Profile and process management for Claude/Codex Multi-Instance.

State lives next to this file:
  <App.profiles_dirname>/<name>/  -> --user-data-dir for each profile
  data/state.json                  -> selected app, Codex launch mode, launch counter
"""
from __future__ import annotations

import base64
import errno
import json
import msvcrt
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
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
RUNTIME_DIR = PROJECT_DIR / "data"
LAUNCHER = PROJECT_DIR / "launcher.pyw"
LAUNCHER_EXE = PROJECT_DIR / "launcher.exe"
STATE_FILE = RUNTIME_DIR / "state.json"
MANAGER_VERSION = "2.1.0"
_GITHUB_REPO = "Zoltak-Dev/ai-multi-instance"
_GITHUB_URL = f"https://github.com/{_GITHUB_REPO}"
_SOURCE_ARCHIVE_REVISION = "$Format:%H$"
_SOURCE_UPDATE_FILES = {"main.py", "engine.py", "usage.py", "launcher.pyw"}


# --- Consolidated state (state.json) -------------------------------------- #
def _load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    _write_atomic(STATE_FILE, json.dumps(state, indent=2).encode("utf-8"))


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        yield


def sort_by_usage() -> bool:
    return _load_state().get("sort_by_usage") is True


def set_sort_by_usage(enabled: bool) -> None:
    with file_lock(STATE_FILE.with_suffix(".lock")):
        state = _load_state()
        state["sort_by_usage"] = enabled
        _save_state(state)


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
        try:
            with file_lock(STATE_FILE.with_suffix(".lock")):
                state = _load_state()
                state["selected_app"] = app.key
                _save_state(state)
        except OSError:
            pass


def _restore_selected_app() -> None:
    key = _load_state().get("selected_app")
    if isinstance(key, str) and key in APPS and key != _current.key:
        set_app(APPS[key], persist=False)


def initialize_runtime() -> None:
    """Move the old root-level files before the manager starts using them."""
    names = ("state.json", "usage_cache.json", "launch-count.db")
    if any((PROJECT_DIR / name).is_file() and not (RUNTIME_DIR / name).exists() for name in names):
        with file_lock(STATE_FILE.with_suffix(".lock")):
            for name in names:
                old = PROJECT_DIR / name
                new = RUNTIME_DIR / name
                if old.is_file() and not new.exists():
                    old.rename(new)
            if not (PROJECT_DIR / "state.json").exists():
                try:
                    (PROJECT_DIR / "state.lock").unlink(missing_ok=True)
                except OSError:
                    pass
    _restore_selected_app()


def _load_launch_state() -> dict:
    state = _load_state()
    legacy = STATE_FILE.with_name("launch-count.db")
    if "launch_count" in state or not legacy.is_file():
        return state

    # Read the old counter once. All subsequent launches use state.json only.
    import sqlite3
    from contextlib import closing

    try:
        with closing(sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True, timeout=1)) as db:
            count, shown = db.execute("SELECT count, shown FROM launches WHERE id = 1").fetchone() or (0, 0)
    except sqlite3.Error as exc:
        raise OSError("Could not read the old launch counter.") from exc
    state.update(launch_count=count, donation_milestone=shown)
    _save_state(state)
    try:
        legacy.unlink()
    except OSError:
        pass
    return state


def _counter_value(state: dict, key: str) -> int:
    value = state.get(key)
    return value if type(value) is int and value >= 0 else 0


def _record_launch() -> None:
    try:
        with file_lock(STATE_FILE.with_suffix(".lock")):
            state = _load_launch_state()
            state["launch_count"] = _counter_value(state, "launch_count") + 1
            _save_state(state)
    except OSError:
        pass


def take_launch_milestone() -> int | None:
    try:
        with file_lock(STATE_FILE.with_suffix(".lock")):
            state = _load_launch_state()
            count = _counter_value(state, "launch_count")
            milestone = count // 100
            if milestone > _counter_value(state, "donation_milestone"):
                state["donation_milestone"] = milestone
                _save_state(state)
                return count
    except OSError:
        pass
    return None


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
             "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
             f"(Get-AppxPackage -Name {app.package_filter}).InstallLocation"],
            capture_output=True, encoding="utf-8", timeout=20, creationflags=NO_WINDOW,
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


def saved_client_versions() -> dict[App, str]:
    state = _load_state()
    versions: dict[App, str] = {}
    for app in APPS.values():
        version = state.get(f"{app.key}_client_version")
        if isinstance(version, str) and re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+){3}|\?)?", version):
            versions[app] = version
    return versions


def save_client_version(app: App, version: str) -> None:
    key = f"{app.key}_client_version"
    with file_lock(STATE_FILE.with_suffix(".lock")):
        state = _load_state()
        if state.get(key) != version:
            state[key] = version
            _save_state(state)


def _github_data(endpoint: str) -> dict | list | None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{_GITHUB_REPO}/{endpoint}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"ai-multi-instance/{MANAGER_VERSION}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())
    except (HTTPException, OSError, ValueError):
        return None


def _windows_release_update() -> tuple[str, str] | None:
    releases = _github_data("releases?per_page=30")
    if not isinstance(releases, list):
        return None
    newest = tuple(map(int, MANAGER_VERSION.split(".")))
    newest_tag = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not re.fullmatch(r"v?[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}", tag):
            continue
        version = tuple(map(int, tag.lstrip("v").split(".")))
        assets = release.get("assets")
        zip_name = f"ai-multi-instance-v{tag.lstrip('v')}-windows.zip"
        if version > newest and isinstance(assets, list) and any(
            isinstance(asset, dict) and asset.get("name") == zip_name for asset in assets
        ):
            newest, newest_tag = version, tag
    if newest_tag is None:
        return None
    return f"Version {newest_tag}", f"{_GITHUB_URL}/releases/tag/{newest_tag}"


def _source_file_hashes(revision: str) -> dict[str, str] | None:
    tree = _github_data(f"git/trees/{revision}")
    if not isinstance(tree, dict) or tree.get("truncated"):
        return None
    entries = tree.get("tree")
    if not isinstance(entries, list):
        return None
    files = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        path = entry.get("path")
        if not isinstance(path, str):
            return None
        if path not in _SOURCE_UPDATE_FILES:
            continue
        sha = entry.get("sha")
        if entry.get("type") != "blob" or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None
        files[path] = sha
    return files


def _git(*args: str, timeout: int = 3) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    return subprocess.run(
        ["git", *args], cwd=PROJECT_DIR, check=True, stdin=subprocess.DEVNULL,
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=NO_WINDOW, env=env,
    ).stdout.strip()


def _source_update() -> tuple[str, str] | None:
    head = _SOURCE_ARCHIVE_REVISION
    if (PROJECT_DIR / ".git").exists():
        try:
            head = _git("rev-parse", "--verify", "HEAD")
        except FileNotFoundError:
            pass
        except (OSError, subprocess.SubprocessError):
            return None
        else:
            if not re.fullmatch(r"[0-9a-f]{40}", head):
                return None
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        head = f"v{MANAGER_VERSION}"
    comparison = _github_data(f"compare/main...{head}?per_page=1")
    if not isinstance(comparison, dict) or comparison.get("status") not in ("behind", "diverged"):
        return None
    base = comparison.get("base_commit")
    upstream = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(upstream, str) or not re.fullmatch(r"[0-9a-f]{40}", upstream):
        return None
    upstream_files = _source_file_hashes(upstream)
    if upstream_files is None:
        return None
    installed_files = _source_file_hashes(head)
    if installed_files is None or installed_files == upstream_files:
        return None
    label = f"Source commit {upstream[:7]}"
    if head.startswith("v"):
        label += f" (after {head})"
    return label, f"{_GITHUB_URL}/compare/{head}...main"


def check_update() -> tuple[str, str] | None:
    return _windows_release_update() if getattr(sys, "frozen", False) else _source_update()


def update_download_url(release_url: str) -> str:
    if getattr(sys, "frozen", False):
        return release_url.replace("/releases/tag/", "/releases/expanded_assets/", 1)
    return _GITHUB_URL


def can_auto_update() -> bool:
    return (not getattr(sys, "frozen", False) and (PROJECT_DIR / ".git").exists()
            and shutil.which("git") is not None)


def _require_clean_main() -> None:
    if _git("branch", "--show-current") != "main":
        raise RuntimeError("Switch to the main branch to update automatically.")
    if _git("status", "--porcelain", "--untracked-files=normal"):
        raise RuntimeError("Resolve your local changes before updating.")


def apply_source_update() -> None:
    """Fast-forward the manager's Git checkout after the user chooses Update."""
    if not can_auto_update():
        raise RuntimeError("Automatic updates require a Git checkout and Git.")
    official_remotes = (_GITHUB_URL, f"git@github.com:{_GITHUB_REPO}",
                        f"ssh://git@github.com/{_GITHUB_REPO}")
    try:
        if Path(_git("rev-parse", "--show-toplevel")).resolve() != PROJECT_DIR.resolve():
            raise RuntimeError("Git's working directory does not match this installation.")
        origin = _git("remote", "get-url", "origin").lower().rstrip("/").removesuffix(".git")
        if origin not in {url.lower() for url in official_remotes}:
            raise RuntimeError("Set origin to the official GitHub repository to update automatically.")
        _require_clean_main()
        _git("fetch", "--no-tags", "--no-recurse-submodules", "origin", "refs/heads/main", timeout=45)
        upstream = _git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if not re.fullmatch(r"[0-9a-f]{40}", upstream):
            raise RuntimeError("Git could not verify the update commit.")
        _require_clean_main()
        try:
            _git("merge-base", "--is-ancestor", "HEAD", upstream)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Your local main branch cannot be fast-forwarded to GitHub.") from exc
        _git("merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", upstream, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The Git update timed out. Try again.") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Git could not finish the update. Check the repository and try again.") from exc


# --- Profiles ------------------------------------------------------------- #
def list_profiles(profiles_dir: Path | None = None) -> list[Path]:
    profiles_dir = profiles_dir or PROFILES_DIR
    if not profiles_dir.is_dir():
        return []
    return sorted((d for d in profiles_dir.iterdir() if d.is_dir()),
                  key=lambda p: p.name.lower())


def valid_profile_name(name: str) -> bool:
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{digit}" for prefix in ("COM", "LPT")
                    for digit in "123456789¹²³")
    return (bool(name) and name == name.strip() and not name.endswith(".")
            and not any(c in INVALID_CHARS or ord(c) < 32 for c in name)
            and name.split(".", 1)[0].rstrip().upper() not in reserved)


def _profile_path(name: str, profiles_dir: Path | None = None) -> Path:
    if not valid_profile_name(name):
        raise ValueError(f"Invalid profile name: {name!r}")
    profiles_dir = profiles_dir or PROFILES_DIR
    profile = profiles_dir / name
    if profile.is_symlink() or profile.resolve().parent != profiles_dir.resolve():
        raise ValueError(f"Profile must be a directory inside {profiles_dir}")
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


def _rename_conflicts(src: Path, dst: Path) -> bool:
    # Windows resolves a case-only rename to the existing item.
    return dst.is_symlink() or (dst.exists() and not (src == dst and src.exists() and src.samefile(dst)))


def rename_profile(old: str, new: str) -> None:
    src, dst = _profile_path(old), _profile_path(new)
    if old == new:
        return
    if not src.is_dir():
        raise FileNotFoundError(f"Profile not found: {old}")
    if _rename_conflicts(src, dst):
        raise FileExistsError(f"A profile named {new!r} already exists.")

    desktop = desktop_dir()
    old_link, new_link = desktop / shortcut_filename(old), desktop / shortcut_filename(new)
    if _rename_conflicts(old_link, new_link):
        raise FileExistsError(f"A desktop item named {new_link.name!r} already exists.")
    if old_link.is_symlink() or (old_link.exists() and not old_link.is_file()):
        raise ValueError(f"{old_link.name!r} is not a shortcut file.")
    original = old_link.read_bytes() if old_link.is_file() else None

    src.rename(dst)
    if original is None:
        return
    try:
        _save_shortcut(old_link, *_launcher_invocation(new), replace=True)
        old_link.rename(new_link)
    except (OSError, subprocess.SubprocessError):
        try:
            if old_link.is_file() and old_link.read_bytes() != original:
                old_link.write_bytes(original)
        finally:
            dst.rename(src)
        raise
    _notify_shortcut_rename(old_link, new_link)


# --- Running processes ---------------------------------------------------- #
def _processes(*apps: App) -> list[tuple[App, int, str, str]]:
    """Read process details for the requested apps in one Windows query."""
    by_name = {app.exe_name.lower(): app for app in apps}
    filter_expr = " OR ".join(f"Name='{app.exe_name}'" for app in apps)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
             f'Get-CimInstance Win32_Process -Filter "{filter_expr}" -ErrorAction Stop '
             '| ForEach-Object { "$($_.Name)|$($_.ProcessId)|$($_.ExecutablePath)|$($_.CommandLine)" }'],
            capture_output=True, encoding="utf-8", timeout=10, creationflags=NO_WINDOW, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        names = " or ".join(app.display for app in apps)
        raise RuntimeError(f"Could not check which {names} instances are running. Try again.") from exc
    procs: list[tuple[App, int, str, str]] = []
    for line in out.stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        name, pid_str, exe_path, cmd = parts
        app = by_name.get(name.lower())
        if app is not None and pid_str.strip().isdigit():
            procs.append((app, int(pid_str), exe_path, cmd))
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


InstancePids = tuple[dict[str, list[int]], list[int]]


def _running_instances(roots: dict[App, Path]) -> dict[App, InstancePids]:
    instances: dict[App, InstancePids] = {app: ({}, []) for app in roots}
    for app, pid, exe_path, cmd in _processes(*roots):
        profiles, main = instances[app]
        data = _user_data_dir(cmd)
        if data is None:
            if "--type=" not in cmd.lower() and _is_packaged_exe(exe_path, app):
                main.append(pid)
        elif data.parent == roots[app]:
            profiles.setdefault(data.name, []).append(pid)
    return instances


def running_instances(app: App | None = None) -> InstancePids:
    """Return isolated profile PIDs and main-instance PIDs for one app."""
    if app is None:
        app, profiles_root = _current, PROFILES_DIR
    else:
        profiles_root = PROJECT_DIR / app.profiles_dirname
    return _running_instances({app: profiles_root})[app]


def all_running_instances() -> dict[App, InstancePids]:
    return _running_instances({app: PROJECT_DIR / app.profiles_dirname for app in APPS.values()})


def running_profiles() -> dict[str, list[int]]:
    return running_instances()[0]


def close_profile(name: str) -> int:
    return _kill_pids(running_profiles().get(name, []))


def close_instances(app: App, names: list[str] | None) -> int:
    profiles, main = running_instances(app)
    if names is None:
        return _kill_pids(main)
    pids = [pid for name in names for pid in profiles.get(name, [])]
    if main and app is CODEX and main_codex_profile(PROJECT_DIR / app.profiles_dirname) in names:
        pids.extend(main)
    return _kill_pids(pids)


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


# --- Codex account switching ---------------------------------------------- #

def shared_codex_history() -> bool:
    return _current is CODEX and _load_state().get("codex_shared_history", True) is True


def toggle_codex_history() -> bool:
    with _codex_switch_lock(), file_lock(STATE_FILE.with_suffix(".lock")):
        state = _load_state()
        shared = state.get("codex_shared_history", True) is not True
        state["codex_shared_history"] = shared
        main = codex_main_home() / "auth.json"
        detached = None if shared else _detach_main_codex_profile(main)
        try:
            _save_state(state)
        except OSError:
            if detached is not None:
                data, mtime = detached
                _write_atomic(main, data, mtime_ns=mtime)
            raise
        return shared


def attach_codex_profile(name: str) -> None:
    """Load a saved login into Codex's closed main slot."""
    with _codex_switch_lock():
        if not shared_codex_history():
            raise RuntimeError("Switch to Shared history before attaching a profile.")
        target, data, identity = _read_profile_login(name)
        main = codex_main_home() / "auth.json"
        try:
            previous, active = _codex_auth(main)
        except FileNotFoundError:
            previous, active = None, None
        else:
            if active is None:
                raise RuntimeError("Shared history mode requires a ChatGPT login on the main Codex profile.")
            if _match_main_codex_profile(previous, active) is not None:
                raise RuntimeError("A profile is already loaded in the main Codex instance.")

        running, main_pids = running_instances()
        if main_pids:
            raise RuntimeError("Close the main Codex instance first, then try again.")
        matches = list(_matching_codex_profiles(identity))
        if any(other.casefold() != name.casefold() and saved == data for other, _, saved in matches):
            raise RuntimeError("Another profile has the same Codex login. Sign in again before attaching.")
        _check_codex_profiles_closed(matches, running)
        if previous is not None and active is not None:
            _save_outgoing_codex_auth(previous, active, main, running)
        mtime = target.stat().st_mtime_ns
        latest = main.read_bytes() if main.exists() else None
        if latest != previous or target.read_bytes() != data:
            raise RuntimeError("A Codex login changed while attaching the profile. Try again.")
        _write_atomic(main, data, mtime_ns=mtime)


def detach_codex_profile(*, expected_profile: str | None = None) -> bool:
    """Remove the named account from Codex's main slot in shared-history mode."""
    with _codex_switch_lock():
        return shared_codex_history() and _detach_main_codex_profile(
            codex_main_home() / "auth.json", expected_profile=expected_profile,
        ) is not None


def codex_main_home() -> Path:
    return Path.home() / ".codex"


def _write_atomic(path: Path, data: bytes, *, mtime_ns: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
    pending = Path(temporary.name)
    try:
        with temporary:
            temporary.write(data)
        if mtime_ns is not None:
            os.utime(pending, ns=(mtime_ns, mtime_ns))
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _codex_auth(path: Path) -> tuple[bytes, tuple[str, str] | None]:
    data = path.read_bytes()
    try:
        auth = json.loads(data)
    except ValueError:
        return data, None
    return data, _codex_identity(auth)


def _codex_identity(auth: object) -> tuple[str, str] | None:
    try:
        tokens = auth["tokens"]
        if not all(tokens[key] for key in ("access_token", "refresh_token", "id_token")):
            return None
        payload = tokens["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        # Use signed-in user and workspace claims as a local lookup key only.
        subject = claims.get("sub")
        workspace = tokens.get("account_id") or ""
        if isinstance(subject, str) and subject and isinstance(workspace, str):
            return subject, workspace
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        pass
    return None


@contextmanager
def _try_file_lock(path: Path, busy_message: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError(busy_message) from None
        yield


def _codex_switch_lock():
    return _try_file_lock(codex_main_home() / ".multi-instance.lock",
                          "Another Codex account operation is in progress.")


def claude_snapshot_lock():
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return _try_file_lock(local / ".multi-instance-claude.lock",
                          "A Claude sign-in or recovery is in progress in another window.")


def _codex_default_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("CODEX_HOME", "CODEX_ELECTRON_USER_DATA_PATH", "CODEX_CLI_PATH"):
        env.pop(key, None)
    return env


def _codex_profile_auth(name: str) -> Path:
    profile = _profile_path(name)
    if not profile.is_dir():
        raise RuntimeError(f"Codex profile '{name}' does not exist.")
    return profile / ".codex" / "auth.json"


def codex_profile_has_auth(name: str) -> bool:
    return _codex_profile_auth(name).is_file()


def _matching_codex_profiles(
    identity: tuple[str, str], profiles_dir: Path | None = None,
) -> Iterator[tuple[str, Path, bytes]]:
    for profile in list_profiles(profiles_dir):
        try:
            path = _profile_path(profile.name, profiles_dir) / ".codex" / "auth.json"
            data, saved = _codex_auth(path)
        except (OSError, ValueError):
            continue
        if saved == identity:
            yield profile.name, path, data


def main_codex_profile(profiles_dir: Path | None = None) -> str | None:
    if profiles_dir is None and _current is not CODEX:
        return None
    try:
        data, identity = _codex_auth(codex_main_home() / "auth.json")
    except OSError:
        return None
    if identity is None:
        return None
    profile = _match_main_codex_profile(data, identity, profiles_dir)
    return profile[0] if profile is not None else None


def _match_main_codex_profile(
    data: bytes, identity: tuple[str, str], profiles_dir: Path | None = None,
) -> tuple[str, Path, bytes] | None:
    matches = list(_matching_codex_profiles(identity, profiles_dir))
    if len(matches) == 1:
        return matches[0]
    exact = [match for match in matches if match[2] == data]
    return exact[0] if len(exact) == 1 else None


def _detach_main_codex_profile(main: Path, *, expected_profile: str | None = None) -> tuple[bytes, int] | None:
    try:
        data, identity = _codex_auth(main)
    except FileNotFoundError:
        return None
    if identity is None:
        return None
    match = _match_main_codex_profile(data, identity)
    if match is None:
        return None
    name, target, saved = match
    if expected_profile is not None and name != expected_profile:
        raise RuntimeError("The profile loaded in the main Codex instance changed. Try again.")

    running, main_pids = running_instances()
    if main_pids:
        raise RuntimeError("Close the main Codex instance first, then try again.")
    if target.samefile(main):
        raise RuntimeError(f"Profile '{name}' shares the main auth file. Use a separate profile first.")
    mtime = main.stat().st_mtime_ns
    if main.read_bytes() != data or target.read_bytes() != saved:
        raise RuntimeError("A Codex login changed while detaching the profile. Try again.")
    if data != saved and mtime > target.stat().st_mtime_ns:
        _check_codex_profiles_closed((match,), running)
        _write_atomic(target, data, mtime_ns=mtime)
    main.unlink()
    return data, mtime


def sign_in_codex(name: str, *, cancelled: Callable[[], bool] | None = None) -> None:
    target = _codex_profile_auth(name)
    exe = find_app_exe()
    cli = exe.parent / "resources" / "codex.exe" if exe else None
    if cli is None or not cli.is_file():
        raise RuntimeError("The bundled Codex CLI was not found.")
    with _codex_switch_lock(), tempfile.TemporaryDirectory(prefix=".multi-instance-login-", dir=codex_main_home()) as temp:
        if name in running_profiles():
            raise RuntimeError(f"Close the isolated Codex profile '{name}' before signing in.")
        env = _codex_default_env()
        env["CODEX_HOME"] = temp
        args = [str(cli), "login", "-c", 'cli_auth_credentials_store="file"']
        with subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL) as login:
            try:
                while login.poll() is None:
                    if cancelled is not None and cancelled():
                        raise KeyboardInterrupt
                    time.sleep(0.1)
            finally:
                if login.returncode is None:
                    login.kill()
                    login.wait()
            if login.returncode:
                raise subprocess.CalledProcessError(login.returncode, args)
        data, identity = _codex_auth(Path(temp) / "auth.json")
        if identity is None:
            raise RuntimeError("Codex did not return a ChatGPT account login.")
        _write_atomic(target, data)


def _check_codex_profiles_closed(matches: Iterable[tuple[str, Path, bytes]],
                                 running: dict[str, list[int]]) -> None:
    if not running:
        return
    for name, _, _ in matches:
        if name in running:
            raise RuntimeError(f"Close the isolated Codex profile '{name}' first.")


def _latest_codex_profile(matches: list[tuple[str, Path, bytes]]) -> tuple[str, Path, bytes]:
    return max(matches, key=lambda match: match[1].stat().st_mtime_ns)


def _save_outgoing_codex_auth(data: bytes, identity: tuple[str, str], main: Path,
                              running: dict[str, list[int]]) -> None:
    matches = list(_matching_codex_profiles(identity))
    if matches:
        _, path, saved = _latest_codex_profile(matches)
        if saved != data and main.stat().st_mtime_ns > path.stat().st_mtime_ns:
            _check_codex_profiles_closed(matches, running)
            _write_atomic(path, data)
        return
    name = "Original main account"
    suffix = 2
    while (PROFILES_DIR / name).exists():
        name = f"Original main account {suffix}"
        suffix += 1
    _write_atomic(_profile_path(name) / ".codex" / "auth.json", data)


def _read_profile_login(name: str) -> tuple[Path, bytes, tuple[str, str]]:
    target = _codex_profile_auth(name)
    try:
        data, identity = _codex_auth(target)
    except FileNotFoundError:
        identity = None
    if identity is None:
        raise RuntimeError(f"Select '{name}' in the menu and press 2 to sign in first.")
    return target, data, identity


def launch_account(name: str, exe: Path | None = None, *,
                   confirm_restart: Callable[[frozenset[int]], bool] | None = None) -> bool:
    """Launch a named account using the selected history mode."""
    if not _launch_account(name, exe, confirm_restart):
        return False
    _record_launch()
    return True


def _launch_account(name: str, exe: Path | None,
                    confirm_restart: Callable[[frozenset[int]], bool] | None) -> bool:
    if _current is not CODEX:
        if not _profile_path(name).is_dir():
            raise RuntimeError(f"{_current.display} profile '{name}' does not exist.")
        launch_profile(name, exe=exe)
        return True

    with _codex_switch_lock():
        target, data, identity = _read_profile_login(name)

        main = codex_main_home() / "auth.json"
        if not shared_codex_history():
            _launch_isolated_codex(name, exe, target, identity, main)
            return True

        exe = exe or find_app_exe()
        if exe is None:
            raise RuntimeError("Codex desktop is not installed.")
        running = running_profiles()
        _check_codex_profiles_closed(_matching_codex_profiles(identity), running)
        if main.is_file():
            previous, active = _codex_auth(main)
            if active is None:
                raise RuntimeError("Shared history mode requires a ChatGPT login on the main Codex profile.")
            if active == identity and (previous == data or main.stat().st_mtime_ns >= target.stat().st_mtime_ns):
                launch_default_slot(exe)
                return True
        if close_default_slot(wait=20, confirm_restart=confirm_restart) is None:
            return False
        try:
            previous, active = _codex_auth(main)
            previous_mtime = main.stat().st_mtime_ns
        except FileNotFoundError:
            previous, active, previous_mtime = None, None, None
        if active == identity and (previous == data or previous_mtime >= target.stat().st_mtime_ns):
            launch_default_slot(exe)
            return True
        if previous is not None and active != identity:
            if active is None:
                raise RuntimeError("The main Codex login changed during the switch. Try again.")
            _save_outgoing_codex_auth(previous, active, main, running)
        _write_atomic(main, data)
        try:
            launch_default_slot(exe)
        except OSError:
            if previous is None:
                main.unlink()
            else:
                _write_atomic(main, previous, mtime_ns=previous_mtime)
            raise
        return True


def _launch_isolated_codex(name: str, exe: Path | None, target: Path,
                           identity: tuple[str, str], main: Path) -> None:
    if main.is_file():
        _, active = _codex_auth(main)
        if active == identity:
            if default_slot_running():
                raise RuntimeError("This account is running in the main Codex instance. Close it first.")
            try:
                data, active = _codex_auth(main)
                mtime = main.stat().st_mtime_ns
            except FileNotFoundError:
                active = None
            if active == identity and mtime > target.stat().st_mtime_ns and data != target.read_bytes():
                _write_atomic(target, data, mtime_ns=mtime)
    launch_profile(name, exe=exe)


def launch_main() -> None:
    _launch_main()
    _record_launch()


def _launch_main() -> None:
    if _current is not CODEX:
        launch_default_slot()
        return

    with _codex_switch_lock():
        running, main_pids = running_instances()
        main = codex_main_home() / "auth.json"
        if not main_pids and main.is_file():
            previous, identity = _codex_auth(main)
            if identity is not None:
                matches = list(_matching_codex_profiles(identity))
                _check_codex_profiles_closed(matches, running)
                if matches:
                    _, path, data = _latest_codex_profile(matches)
                    if data != previous and path.stat().st_mtime_ns > main.stat().st_mtime_ns:
                        _write_atomic(main, data)
        launch_default_slot()


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


def claude_profile_userdata(profile: Path) -> Path:
    family = (CLAUDE.package_prefix + CLAUDE.publisher_hash).lower()
    relocated = profile / ".localappdata" / "Claude-Data" / family
    return relocated if _is_live_userdata(relocated) else profile


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
    return running_instances()[1]


def close_default_slot(*, wait: float = 0.0,
                       confirm_restart: Callable[[frozenset[int]], bool] | None = None) -> int | None:
    """Close the default-slot processes, optionally requiring them to exit."""
    pids = default_slot_pids()
    if pids and confirm_restart is not None:
        approved = frozenset(pids)
        if not confirm_restart(approved):
            return None
        pids = default_slot_pids()
        if pids and frozenset(pids) != approved:
            raise RuntimeError("The main Codex window changed. Try switching again.")
    n = _kill_pids(pids)
    if wait > 0:
        deadline = time.monotonic() + wait
        while default_slot_pids():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"The main {_current.display} instance could not be closed.")
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
        env=_codex_default_env() if _current is CODEX else None,
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
    if any(candidate != slot and _is_live_userdata(candidate) for candidate in slot_candidates(_current)):
        raise RuntimeError("Multiple Claude data folders are in use. Cannot start a fresh sign-in safely.")
    _move_retry(slot, backup)
    return True


def restore_default_slot(*, during_signin: bool = False) -> None:
    """Bring the stashed main session back, discarding whatever the sign-in flow
    left in the default location. No-op when there is no stash. The caller MUST
    have killed the fresh instance first, or the slot's files stay locked. The
    backup is kept until the rename succeeds. Recovery after interruption
    preserves any alternate location whose origin is unknown."""
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
        if not during_signin:
            raise RuntimeError(f"Claude data also exists at {live}. Move it aside before restoring {backup}.")
        _rmtree_retry(live)  # The active sign-in may have created a new layout.
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
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    if not profile.exists():
        _move_retry(slot, profile)
        return

    # Stage on the destination volume so the final moves cannot leave a partial copy.
    staging = Path(tempfile.mkdtemp(prefix=".mi-adopt-", dir=PROFILES_DIR.resolve().parent))
    fresh, previous = staging / "fresh", staging / "previous"
    try:
        _move_retry(slot, fresh)
        try:
            _move_retry(profile, previous)
            _move_retry(fresh, profile)
        except (OSError, KeyboardInterrupt):
            if previous.exists():
                try:
                    _move_retry(previous, profile)
                except OSError as exc:
                    raise RuntimeError(f"Could not restore '{name}'. Its previous data is at {previous}.") from exc
            raise
        _rmtree_retry(previous)
    finally:
        if not previous.exists():
            _rmtree_retry(staging)


# --- Desktop shortcuts ---------------------------------------------------- #
_desktop_cache: Path | None = None


def desktop_dir() -> Path:
    import ctypes

    global _desktop_cache
    if _desktop_cache is not None:
        return _desktop_cache
    path = ctypes.create_unicode_buffer(260)
    desktop_directory = 0x0010  # CSIDL_DESKTOPDIRECTORY
    result = ctypes.windll.shell32.SHGetFolderPathW(None, desktop_directory, None, 0, path)
    _desktop_cache = Path(path.value) if result == 0 and path.value else Path.home() / "Desktop"
    return _desktop_cache


def pythonw_path() -> str:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


def shortcut_filename(name: str) -> str:
    """Per-app shortcut filename, prefixed so Claude and Codex profiles don't collide."""
    return f"{_current.display} - {name}.lnk"


def _save_shortcut(lnk: Path, target: str, args: str, *,
                   icon: Path | str | None = None, replace: bool = False) -> Path:
    if not replace and os.path.lexists(lnk):
        raise FileExistsError(f"A desktop item named {lnk.name!r} already exists.")
    env = dict(
        os.environ,
        SC_LNK=str(lnk),
        SC_TARGET=target,
        SC_ARGS=args,
        SC_WORK=str(PROJECT_DIR),
        SC_ICON=str(icon) if icon else "",
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "$w=New-Object -ComObject WScript.Shell;"
        "$s=$w.CreateShortcut($env:SC_LNK);"
        "$s.TargetPath=$env:SC_TARGET;"
        "$s.Arguments=$env:SC_ARGS;"
        "if ($env:SC_ICON) {$s.WorkingDirectory=$env:SC_WORK;$s.IconLocation=$env:SC_ICON};"
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        env=env, capture_output=True, text=True, creationflags=NO_WINDOW,
        check=True, timeout=20,
    )
    return lnk


def _notify_shortcut_rename(old_link: Path, new_link: Path) -> None:
    import ctypes

    # Send a rename event so Explorer can reuse the existing icon position.
    rename_item, path_w, flush_nowait = 0x00000001, 0x0005, 0x3000
    notify = ctypes.windll.shell32.SHChangeNotify
    notify.restype = None
    notify(rename_item, path_w | flush_nowait, ctypes.c_wchar_p(str(old_link)), ctypes.c_wchar_p(str(new_link)))


def create_shortcut(name: str, exe: Path | None = None) -> Path:
    _profile_path(name)
    lnk = desktop_dir() / shortcut_filename(name)
    target, args = _launcher_invocation(name)
    return _save_shortcut(lnk, target, args, icon=exe or find_app_exe() or target)


def manager_shortcut_path() -> Path:
    return desktop_dir() / "AI Multi-Instance.lnk"


def create_manager_shortcut() -> Path:
    target = sys.executable
    args = "" if getattr(sys, "frozen", False) else subprocess.list2cmdline([str(PROJECT_DIR / "main.py")])
    return _save_shortcut(manager_shortcut_path(), target, args, icon=target)


def shortcut_exists(name: str) -> bool:
    return (desktop_dir() / shortcut_filename(name)).exists()


def delete_shortcut(name: str) -> bool:
    lnk = desktop_dir() / shortcut_filename(name)
    try:
        lnk.unlink()
    except FileNotFoundError:
        return False
    return True
