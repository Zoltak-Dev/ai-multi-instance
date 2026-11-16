"""Claude / Codex Multi-Instance — interactive console UI.

Run with:  python main.py

Profiles are chosen with the arrow keys (never by number); actions are single
digits. So a digit is always an action and never clashes with a profile.
"""
from __future__ import annotations

import msvcrt
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import Structure, byref, windll, wintypes
from math import ceil, isfinite
from pathlib import Path
from queue import SimpleQueue
from typing import Optional

import engine
import usage

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
MINT_GREEN = "\033[38;5;121m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREY = "\033[90m"
ORANGE = "\033[38;5;173m"       # Claude's terracotta — theme accent
CODEX_BLUE = "\033[38;5;33m"    # Codex theme accent — a neutral Word/Discord blue

MAIN_LABEL = "Main instance"
LTC_ADDRESS = "LXFPr2dZj9ymAF7VbggkCXth8nmac36wbE"
KOFI_URL = "https://ko-fi.com/zoltrak"


def _theme(app) -> str:
    """The app's single accent colour, used for the title, the action numbers
    and the selection arrow — so the whole UI matches the app."""
    return ORANGE if app is engine.CLAUDE else CODEX_BLUE


def _title(app) -> str:
    return f"{BOLD}{_theme(app)}◆ {app.display} Multi-Instance{RESET}"


# --- Terminal plumbing ---------------------------------------------------- #
def _enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def _draw(lines: list[str]) -> None:
    """Repaint in place (no full clear → no flicker): home the cursor, rewrite
    each line clearing to end-of-line, then clear everything below."""
    buf = "\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J"
    sys.stdout.write(buf)
    sys.stdout.flush()


def _full_clear() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _hide_cursor() -> None:
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def _show_cursor() -> None:
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


# --- Text-prompt helpers (used by modal actions) -------------------------- #
def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    finally:
        _held_launch_keys.clear()


def pause(msg: str = "  [Enter] ") -> None:
    ask(msg)


def confirm(prompt: str, default: bool = True) -> bool:
    """Yes/no that re-asks on anything unclear, so a mis-hit key never silently
    confirms or cancels. Empty input takes `default`."""
    while True:
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        finally:
            _held_launch_keys.clear()
        if ans == "":
            return default
        if ans in ("y", "yes", "o", "oui"):
            return True
        if ans in ("n", "no", "non"):
            return False
        # anything else: ask again


class _KeyEvent(Structure):
    _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD),
                ("virtual_key", wintypes.WORD), ("scan_code", wintypes.WORD),
                ("char", wintypes.WCHAR), ("modifiers", wintypes.DWORD)]


class _InputRecord(Structure):
    # KEY_EVENT_RECORD is the largest member of INPUT_RECORD's union.
    _fields_ = [("kind", wintypes.WORD), ("key", _KeyEvent)]


_held_launch_keys: set[tuple[int, int]] = set()
_ARROW_KEYS = {0x25: "LEFT", 0x26: "UP", 0x27: "RIGHT", 0x28: "DOWN"}


def _read_console_key() -> _InputRecord | None:
    try:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(0))
    except OSError:
        return None
    record, count = _InputRecord(), wintypes.DWORD()
    if windll.kernel32.ReadConsoleInputW(handle, byref(record), 1, byref(count)) and count.value:
        return record
    return None


def getkey() -> str:
    record = _read_console_key()
    if record is None:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(code, "")
    elif record.kind != 1:  # KEY_EVENT
        if record.kind == 0x10:  # FOCUS_EVENT
            _held_launch_keys.clear()
        return ""
    else:
        event = record.key
        key = event.virtual_key
        identity = (event.scan_code or key, event.modifiers & 0x100)
        if not event.down:
            _held_launch_keys.discard(identity)
            return ""
        if key in _ARROW_KEYS:
            return _ARROW_KEYS[key]
        ch = event.char
        if not event.modifiers & 0x0F:  # Alt and Ctrl, including AltGr.
            if 0x60 <= key <= 0x69:  # VK_NUMPAD0 through VK_NUMPAD9
                ch = str(key - 0x60)
            elif 0x02 <= event.scan_code <= 0x0B:  # Top number row, regardless of layout.
                ch = "1234567890"[event.scan_code - 0x02]
            elif 0x30 <= key <= 0x39:
                ch = chr(key)
        if ch in ("1", "\r"):
            if identity in _held_launch_keys:
                return ""
            _held_launch_keys.add(identity)
        if ch in ("\x00", "\xe0"):
            return ""
    if ch == "\r":
        return "ENTER"
    if ch == "\x1b":
        return "ESC"
    if ch == " ":
        return "SPACE"
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch.lower()


# --- Live "running" probe (background, so the list stays fresh) ----------- #
InstanceSnapshot = tuple[frozenset[str], bool, Optional[str]]
_instances: dict[engine.App, InstanceSnapshot] = {}
_EMPTY_INSTANCES: InstanceSnapshot = (frozenset(), False, None)
_app_versions: dict[engine.App, str] = {}
_menu_usage: dict[tuple[engine.App, str | None], tuple[float, usage.ProfileUsage | None]] = {}
_usage_lock = threading.Lock()
_refresh_instances = threading.Event()
_refresh_usage = {app: threading.Event() for app in engine.APPS.values()}
_close_pool = ThreadPoolExecutor(max_workers=1)


def _scan_instances() -> None:
    global _instances
    try:
        scanned = engine.all_running_instances()
        profile = engine.main_codex_profile(engine.PROJECT_DIR / engine.CODEX.profiles_dirname)
        _instances = {
            app: (frozenset(running), bool(main_pids), profile if app is engine.CODEX else None)
            for app, (running, main_pids) in scanned.items()
        }
    except (OSError, RuntimeError):
        pass


def _running_probe(first_scan: threading.Event) -> None:
    _scan_instances()
    first_scan.set()
    while True:
        _refresh_instances.wait(2.0)
        _refresh_instances.clear()
        _scan_instances()


def _version_probe(app: engine.App) -> None:
    while True:
        try:
            version = engine.app_version(engine.find_app_exe(app=app))
            if version != _app_versions.get(app):
                _app_versions[app] = version
                engine.save_client_version(app, version)
        except OSError:
            pass
        time.sleep(60)


def _usage_probe(app: engine.App, first_scan: threading.Event,
                 first_main_usage: threading.Event | None) -> None:
    first_pass = True
    previous_profile = None
    previous_active: set[str | None] = set()
    next_idle = 0.0
    refresh_event = _refresh_usage[app]
    first_scan.wait()
    while True:
        if (first_pass or app is engine.current_app()) and (snapshot := _instances.get(app)) is not None:
            running, main_running, profile = snapshot
            root = engine.PROJECT_DIR / app.profiles_dirname
            try:
                targets = _usage_targets(app, root, [p.name for p in engine.list_profiles(root)], profile)
            except OSError:
                if first_main_usage is not None:
                    first_main_usage.set()
                time.sleep(2)
                continue
            if first_pass:
                restored = usage.load_quotas(app.key, targets)
                if restored:
                    _publish_usage(app, root, profile, restored, float("-inf"))
                    if first_main_usage is not None:
                        first_main_usage.set()
            first_pass = False
            now = time.monotonic()
            idle_due = now >= next_idle
            if refresh := refresh_event.is_set():
                refresh_event.clear()
            cached = _menu_usage
            active = {name for name in targets if (main_running if name is None else name in running)}
            due = {}
            for name, target in targets.items():
                key = (app, profile if name is None else name)
                if name in active:
                    last = cached[key][0] if key in cached else float("-inf")
                    ready = (name not in previous_active or now - last >= 60
                             or (name is None and profile != previous_profile))
                else:
                    ready = idle_due
                if refresh or ready:
                    due[name] = target
            if idle_due:
                next_idle = now + 180
            previous_profile, previous_active = profile, active
            if first_main_usage is not None and None not in due:
                first_main_usage.set()
            if due:
                names = list(due)
                for i, result in usage.fetch_as_ready(list(due.values()), app.key):
                    name = names[i]
                    if not refresh_event.is_set():
                        _publish_usage(app, root, profile, {name: result}, now)
                    if first_main_usage is not None and name is None:
                        first_main_usage.set()
        elif first_main_usage is not None:
            first_main_usage.set()
        time.sleep(2)


# --- UI state ------------------------------------------------------------- #
# Row 0 is always the always-present "main instance" (the default slot, launched
# without --user-data-dir). Rows 1.. are the real profiles. So cursor 0 = main.
class State:
    def __init__(self) -> None:
        self.profiles: list[str] = []
        self.hidden_profile: str | None = None
        self.main_profile: str | None = None
        self.cursor: int = 0
        self.checked: set[str] = set()
        self.status: str = ""
        self.pending_switch: tuple[str, frozenset[int]] | None = None
        self.closing: tuple[engine.App, Future[str]] | None = None
        self.refreshing: tuple[engine.App, Future[str]] | None = None
        self.sort_by_usage = engine.sort_by_usage()

    def toggle_sort(self) -> None:
        self.sort_by_usage = not self.sort_by_usage
        try:
            engine.set_sort_by_usage(self.sort_by_usage)
        except OSError:
            self.status = f"{YELLOW}Could not save the sort preference.{RESET}"

    def refresh(self) -> None:
        self.order_profiles([p.name for p in engine.list_profiles()])
        self.checked &= set(self.profiles)

    def order_profiles(self, profiles: list[str]) -> None:
        selected = self.current_profile()
        app, cached = engine.current_app(), _menu_usage

        def quota_order(name: str) -> tuple[bool, float, float, str]:
            entry = cached.get((app, name))
            result = entry[1] if entry else None
            five_hours = _used_quota(result, "5h")
            seven_days = _used_quota(result, "7d")
            return (not (result and result.ok),
                    five_hours if five_hours is not None else float("inf"),
                    seven_days if seven_days is not None else float("inf"), name.casefold())

        self.profiles = sorted(profiles, key=quota_order if self.sort_by_usage else str.casefold)
        self.cursor = self.profiles.index(selected) + 1 if selected in self.profiles else 0

    def is_main(self) -> bool:
        return self.cursor == 0

    def current_profile(self) -> str | None:
        return self.profiles[self.cursor - 1] if self.cursor >= 1 else None

    def targets(self) -> list[str]:
        """Profiles an action applies to: the checked ones, else the highlighted
        profile. Empty on the main row (it's handled on its own)."""
        if self.checked:
            return [n for n in self.profiles if n in self.checked]
        cur = self.current_profile()
        return [cur] if cur else []

    def move(self, delta: int) -> None:
        self.cursor = (self.cursor + delta) % (len(self.profiles) + 1)
        if self.hidden_profile is not None and self.current_profile() == self.hidden_profile:
            self.cursor = (self.cursor + delta) % (len(self.profiles) + 1)

    def hide_profile(self, profile: str | None) -> None:
        self.hidden_profile = profile
        if profile is not None:
            self.checked.discard(profile)
            if self.current_profile() == profile:
                self.cursor = 0

    def toggle_check(self) -> None:
        cur = self.current_profile()
        if cur is None:  # the main instance can't be multi-selected
            return
        self.checked.discard(cur) if cur in self.checked else self.checked.add(cur)


# --- Rendering ------------------------------------------------------------ #
_SEP = "─" * 50


def render(state: State) -> list[str]:
    app = engine.current_app()
    is_claude = app is engine.CLAUDE
    shared_history = engine.shared_codex_history()
    snapshot = _instances.get(app)
    running, main_running, main_profile = snapshot or _EMPTY_INSTANCES
    state.main_profile = main_profile
    if state.sort_by_usage:
        state.order_profiles(state.profiles)
    show_named_profile = main_profile is not None and not main_running and not shared_history
    state.hide_profile(main_profile if not show_named_profile and main_profile not in running else None)
    cached = _menu_usage
    th = _theme(app)
    client_version = _app_versions.get(app)

    lines: list[str] = []
    lines.append("")
    lines.append(f"  {_title(app)}   {GREY}v{engine.MANAGER_VERSION}{RESET}")
    other = engine.CODEX if is_claude else engine.CLAUDE
    app_link = f"{th}{BOLD}C{RESET} Switch to {other.display}"
    if not is_claude:
        mode = "Shared history" if shared_history else "Isolated profiles"
        profile_action = "Detach profile" if main_profile else "Attach profile"
        profile_link = f"   ·   {th}{BOLD}D{RESET} {profile_action}" if shared_history else ""
        app_link = f"{BOLD}{mode}{RESET}   ·   {th}{BOLD}H{RESET} Change mode{profile_link}   ·   {app_link}"
    lines.append(f"  {app_link}")
    lines.append("")

    # Row 0 = main instance, then the profiles.
    names = [MAIN_LABEL] + state.profiles
    width = min(max(len(n) for n in names), 22)
    account = f" ({_ellip(main_profile, width)})" if main_profile and not show_named_profile else ""
    status_column = max(width, len(MAIN_LABEL) + len(account))

    def running_tag(on: bool) -> str:
        if snapshot is None:
            return " "
        return f"{GREEN}●{RESET}" if on else f"{GREY}○{RESET}"

    for i, name in enumerate(names):
        if i and name == state.hidden_profile:
            continue
        cur = i == state.cursor
        arrow = f"{th}▸{RESET} " if cur else "  "
        if i == 0:  # main instance
            shown = f"{th}★{RESET} {BOLD}{name}{RESET}" if cur else f"{th}★{RESET} {name}"
            tag = running_tag(main_running)
            if account:
                shown += f"{MINT_GREEN}{account}{RESET}"
            padding = " " * (status_column - len(name) - len(account) + 4)
            result = cached.get((app, main_profile))
            badge = "" if show_named_profile else _usage_badge(result[1] if result else None)
            lines.append(f"  {arrow}{shown}{padding}{tag}{badge}")
        else:
            box = f"{GREEN}◉{RESET} " if name in state.checked else "  "
            shown = name if len(name) <= width else name[: width - 1] + "…"
            label = f"{BOLD}{shown}{RESET}" if cur else shown
            tag = running_tag(name in running)
            result = cached.get((app, name))
            padding = " " * (status_column - len(shown) + 4)
            lines.append(f"  {arrow}{box}{label}{padding}{tag}{_usage_badge(result[1] if result else None)}")
    lines.extend(["", f"  {GREY}{_SEP}{RESET}"])

    # Actions: 3 columns, numbered top-to-bottom down each column, theme-coloured.
    def cell(num: str, label: str, width: int = 9) -> str:
        if not num:
            return " " * (width + 3)
        return f"{th}{BOLD}{num}{RESET}  {label:<{width}}"

    col1 = [cell("1", "Switch" if shared_history and not state.is_main() else "Launch"), cell("2", "Sign in"),
            cell("3", "Close"), cell("4", "Rename")]
    if state.is_main():
        shortcut = "Delete menu link" if engine.manager_shortcut_path().exists() else "Create menu link"
    else:
        existing = {engine.shortcut_exists(name) for name in state.targets()}
        if len(existing) == 1:
            shortcut = "Delete shortcut" if True in existing else "Create shortcut"
        else:
            shortcut = "Manage shortcuts"
    col2 = [cell(num, label, 16)
            for num, label in (("5", shortcut), ("6", "Delete"), ("7", "New"), ("", ""))]
    col3 = [cell("8", "Usage"), cell("9", "Donate"), cell("0", "Quit"), cell("", "")]
    for a, b, c in zip(col1, col2, col3):
        lines.append(f"  {a}  {b}  {c}".rstrip())
    sort_label = "quota" if state.sort_by_usage else "name"
    footer = f"↑↓ Move · Space Select · T Sort ({sort_label}) · R Refresh"
    if client_version is not None:
        client = f"v{client_version}" if client_version else "not found"
        footer += f"   ·   {app.display} client {client}"
    lines.extend(["", f"  {DIM}{footer}{RESET}"])

    # Status line: just the message (no fake input prompt), auto-cleared by loop.
    if state.status:
        lines.extend(["", f"  {state.status}"])
    return lines


_MAIN_ONLY = f"{DIM}not available for the main instance{RESET}"


# --- Actions (operate on names, set a short status line) ------------------ #
def do_launch(state: State) -> None:
    pending, state.pending_switch = state.pending_switch, None
    if state.is_main():
        try:
            engine.launch_main()
            _refresh_instances.set()
            state.status = f"{GREEN}✓ launched the main instance{RESET}"
        except (OSError, RuntimeError, ValueError) as e:
            state.status = f"{RED}{e}{RESET}"
        return
    names = state.targets()
    if not names:
        return
    shared_history = engine.shared_codex_history()
    if shared_history and len(names) > 1:
        state.status = f"{YELLOW}Select one Codex account to switch.{RESET}"
        return

    def confirm_restart(pids: frozenset[int]) -> bool:
        name = names[0]
        if pending == (name, pids):
            return True
        state.pending_switch = (name, pids)
        state.status = (f"{YELLOW}Switching to '{name}' will close Main instance. "
                        f"Press Enter or 1 again · Esc cancel{RESET}")
        return False

    errs = []
    for n in names:
        try:
            if not engine.launch_account(n, confirm_restart=confirm_restart):
                return
            _refresh_instances.set()
        except (OSError, RuntimeError, ValueError) as e:
            errs.append(f"{n}: {e}")
    if errs:
        state.status = f"{RED}{'; '.join(errs)}{RESET}"
    elif shared_history:
        state.status = f"{GREEN}✓ using '{names[0]}' in the main instance{RESET}"
    else:
        state.status = f"{GREEN}✓ launched {', '.join(names)}{RESET}"


def do_attach_or_detach(state: State) -> None:
    try:
        if state.main_profile is not None:
            if not engine.detach_codex_profile(expected_profile=state.main_profile):
                raise RuntimeError("The main Codex profile changed. Try again.")
            message = "Profile removed from the main instance."
        else:
            names = [] if state.is_main() else state.targets()
            if len(names) != 1:
                state.status = f"{YELLOW}Select one Codex profile to attach.{RESET}"
                return
            engine.attach_codex_profile(names[0])
            message = f"Attached '{names[0]}' to the main instance."
    except (OSError, RuntimeError, ValueError) as e:
        _refresh_instances.set()
        state.status = f"{RED}{e}{RESET}"
    else:
        _refresh_instances.set()
        _invalidate_usage(engine.CODEX, None)
        state.status = f"{GREEN}✓ {message}{RESET}"


def do_close(state: State) -> None:
    if state.closing is not None:
        state.status = f"{DIM}Closing in progress…{RESET}"
        return
    app = engine.current_app()
    names = None if state.is_main() else state.targets()
    if names == []:
        return

    def close() -> str:
        try:
            count = engine.close_instances(app, names)
        except (OSError, RuntimeError) as e:
            return f"{RED}{e}{RESET}"
        finally:
            _refresh_instances.set()
        if names is None:
            return (f"{GREEN}✓ closed the main instance{RESET}" if count
                    else f"{DIM}the main instance wasn't closed{RESET}")
        noun = "process" if count == 1 else "processes"
        return (f"{GREEN}✓ closed {count} {noun}{RESET}" if count
                else f"{DIM}no processes closed{RESET}")

    state.status = f"{DIM}Closing {app.display}…{RESET}"
    state.closing = (app, _close_pool.submit(close))


def _poll_close(state: State) -> None:
    if state.closing is None:
        return
    app, result = state.closing
    if result.done():
        state.closing = None
        if app is engine.current_app():
            state.status = result.result()


def do_shortcut(state: State) -> None:
    if state.is_main():
        try:
            link = engine.manager_shortcut_path()
            if link.exists():
                link.unlink()
                action = "Deleted"
            else:
                engine.create_manager_shortcut()
                action = "Created"
        except (OSError, subprocess.SubprocessError) as e:
            state.status = f"{RED}{e}{RESET}"
        else:
            state.status = f"{GREEN}✓ {action} AI Multi-Instance shortcut.{RESET}"
        return
    names = state.targets()
    if not names:
        return
    created, deleted = [], []
    for n in names:
        try:
            if engine.delete_shortcut(n):
                deleted.append(n)
            else:
                engine.create_shortcut(n)
                created.append(n)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            state.status = f"{RED}{n}: {e}{RESET}"
            return
    changes = []
    for action, profiles in (("Created", created), ("Deleted", deleted)):
        if profiles:
            changes.append(f"{action} shortcut for {', '.join(profiles)}")
    state.status = f"{GREEN}✓ {' · '.join(changes)}{RESET}"


def do_new(state: State) -> None:
    _full_clear()
    name = ask("\n  New profile name (empty = cancel): ")
    if not name:
        return
    if not engine.valid_profile_name(name):
        pause(f"  {RED}Invalid name.{RESET} No < > : \" / \\ | ? *   [Enter] ")
        return
    if (engine.PROFILES_DIR / name).exists():
        pause(f"  {YELLOW}'{name}' already exists.{RESET}   [Enter] ")
        return
    try:
        engine.create_profile(name)
    except (OSError, ValueError) as e:
        pause(f"  {RED}Error: {e}{RESET}   [Enter] ")
        return
    _invalidate_usage(engine.current_app(), name)
    state.refresh()
    state.cursor = state.profiles.index(name) + 1
    state.checked.clear()
    state.status = f"{GREEN}✓ created {name}{RESET}"
    try:
        engine.create_shortcut(name)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        state.status = f"{YELLOW}Profile created. Shortcut failed: {e}{RESET}"
        print(f"  {state.status}")
    if engine.current_app() is engine.CODEX or confirm(f"  Sign in '{name}' now? [Y/n] ", default=True):
        sign_in_profile(name, state)


def do_rename(state: State) -> None:
    if state.is_main():
        state.status = _MAIN_ONLY
        return
    name = state.current_profile()
    if not name:
        return
    _full_clear()
    new = ask(f"\n  Rename '{name}' to (empty = cancel): ")
    if not new or new == name:
        return
    try:
        _check_profiles_closed([name])
        engine.rename_profile(name, new)
        _invalidate_usage(engine.current_app(), name, new)
        state.refresh()
        state.cursor = state.profiles.index(new) + 1
        state.status = f"{GREEN}✓ renamed to {new}{RESET}"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        pause(f"  {RED}Rename failed: {e}{RESET}   [Enter] ")


def _check_profiles_closed(names: list[str]) -> None:
    running = {name.casefold() for name in engine.running_profiles()}
    for name in names:
        if name.casefold() in running:
            raise RuntimeError(f"Close '{name}' first.")


def do_delete(state: State) -> None:
    if state.is_main():
        state.status = _MAIN_ONLY
        return
    names = state.targets()
    if not names:
        return
    _full_clear()
    label = f"'{names[0]}'" if len(names) == 1 else f"{len(names)} profiles"
    if ask(f"\n  {YELLOW}Delete {label} and all its data? type 'yes': {RESET}").lower() not in ("yes", "y"):
        return
    try:
        _check_profiles_closed(names)
    except RuntimeError as e:
        state.status = f"{RED}{e}{RESET}"
        return
    failed = []
    deleted = []
    for n in names:
        try:
            engine.delete_profile(n)
        except (OSError, ValueError) as e:
            failed.append(f"{n} ({e})")
        else:
            deleted.append(n)
    if deleted:
        _invalidate_usage(engine.current_app(), *deleted)
    state.checked.clear()
    if failed:
        pause(f"  {RED}Could not delete: {'; '.join(failed)}{RESET}\n  Close it first.  [Enter] ")
    else:
        state.status = f"{GREEN}✓ deleted {label}{RESET}"


def do_login(state: State) -> None:
    if state.is_main():
        state.status = f"{DIM}Highlight a named profile to sign it in.{RESET}"
        return
    name = state.current_profile()
    if name:
        _full_clear()
        sign_in_profile(name, state)


def sign_in_profile(name: str, state: State) -> None:
    if engine.current_app() is engine.CLAUDE:
        if login_flow(name):
            _invalidate_usage(engine.CLAUDE, name)
        _refresh_instances.set()
        return
    _show_cursor()
    try:
        replacing = engine.codex_profile_has_auth(name)
        print(f"\n  {BOLD}Sign in '{name}'{RESET}\n")
        print("  Here's what happens")
        print("    • Choose the ChatGPT account you want in your browser.")
        if replacing:
            print("    • A successful sign-in replaces this profile's saved login.")
        else:
            print("    • The login is saved to this profile once sign-in succeeds.")
        print("    • Your main Codex session and local conversations stay intact.")
        print(f"\n  Press {BOLD}Q{RESET} or {BOLD}Esc{RESET} during sign-in to cancel.")
        choices = "[y/N]" if replacing else "[Y/n]"
        if not confirm(f"\n  Continue? {choices} ", default=not replacing):
            raise KeyboardInterrupt
        print(flush=True)
        engine.sign_in_codex(name, cancelled=lambda: msvcrt.kbhit() and getkey() in ("q", "ESC"))
    except KeyboardInterrupt:
        state.status = f"{DIM}Sign-in cancelled.{RESET}"
    except subprocess.CalledProcessError:
        state.status = f"{RED}Codex sign-in failed. Press 2 to try again.{RESET}"
    except (OSError, RuntimeError, ValueError) as e:
        state.status = f"{RED}{e}{RESET}"
    else:
        state.checked.clear()
        _refresh_instances.set()
        _invalidate_usage(engine.CODEX, name)
        state.status = f"{GREEN}✓ '{name}' signed in. Press 1 to use it.{RESET}"
    finally:
        _hide_cursor()


def do_switch(state: State) -> None:
    other = engine.CODEX if engine.current_app() is engine.CLAUDE else engine.CLAUDE
    engine.set_app(other)
    state.cursor = 0
    state.checked.clear()
    state.status = ""


# --- Sign-in flow (snapshot model) ----------------------------------------- #
# "Signed in" (verified in the app's own code) = a `sessionKey` cookie exists in
# Network/Cookies; the OAuth token in config.json is derived from it. Chromium
# holds a freshly set cookie in memory and only writes it on its periodic flush;
# quitting exits WITHOUT flushing. So we correlate cookie-store writes with the
# moment OAuth appears. Writes from app startup are ignored, while a cookie
# committed just before or after config.json is accepted in either ordering.
def _cookie_store_signature(slot: Path) -> tuple[tuple[bool, int, int], ...]:
    """Cheap live signature for Chromium's cookie database and journal.

    SQLite can commit through ``Cookies-journal`` or ``Cookies-wal`` without
    immediately changing the main database, so all are included. No file is
    opened, which keeps this safe while Claude holds its exclusive database lock.
    """
    signature: list[tuple[bool, int, int]] = []
    for path in (slot / "Network" / "Cookies",
                 slot / "Network" / "Cookies-journal",
                 slot / "Network" / "Cookies-wal"):
        try:
            stat = path.stat()
            signature.append((True, stat.st_size, stat.st_mtime_ns))
        except OSError:
            signature.append((False, 0, 0))
    return tuple(signature)


def _detect_login(slot: Path, timeout: float = 600.0, *, follow_default: bool = False) -> str:
    """Wait, hands-off, for the user to sign in — no Enter needed. Watches
    config.json for the login and the cookie store for the flush that puts the
    sessionKey on disk. Returns "ready", "cancelled" (user pressed q) or
    "timeout". The cookie store is locked while the app runs, so we can't read
    it live; config.json + file metadata are our live signals."""
    cfg = slot / "config.json"
    cookie_ref = _cookie_store_signature(slot)
    cookie_changed_at: float | None = None
    login_at: float | None = None
    # config.json is derived from sessionKey, so the two writes normally happen
    # together. Keep a small lead window for machines where SQLite commits the
    # cookie first, without mistaking older startup writes for the login.
    cookie_lead_window = 2.0
    spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    start = time.monotonic()
    i = 0
    while time.monotonic() - start < timeout:
        if msvcrt.kbhit() and getkey() in ("q", "ESC"):
            sys.stdout.write("\r\033[K")
            return "cancelled"

        now = time.monotonic()
        if follow_default:
            slot = engine.default_slot_dir() or slot
            cfg = slot / "config.json"
        cookie_signature = _cookie_store_signature(slot)
        if cookie_signature != cookie_ref:
            cookie_ref = cookie_signature
            cookie_changed_at = now

        if login_at is None and usage.claude_oauth_saved(cfg):
            login_at = now

        msg = ("waiting for you to sign in…" if login_at is None
               else "login detected — saving the session…")
        # \033[K clears to end of line so a shorter message leaves no leftovers.
        sys.stdout.write(f"\r  {ORANGE}{spin[i % len(spin)]}{RESET} {DIM}{msg}"
                         f"   ({BOLD}q{RESET}{DIM} to cancel){RESET}\033[K")
        sys.stdout.flush()
        i += 1

        if login_at is not None:
            # Accept a cookie commit after OAuth or shortly before it. The
            # rolling reference above prevents unrelated startup writes from
            # satisfying this condition much later when the user signs in.
            if (cookie_changed_at is not None
                    and cookie_changed_at >= login_at - cookie_lead_window):
                time.sleep(1.2)
                sys.stdout.write("\r\033[K")
                return "ready"
        time.sleep(0.4)

    sys.stdout.write("\r\033[K")
    return "timeout"


def login_flow(name: str) -> bool:
    app = engine.current_app()
    if not engine.supports_login_snapshot():
        return False
    slot = engine.default_slot_dir()
    if slot is None:
        # Distinguish "never signed in" from "the app moved its data and we
        # could not follow". The second would otherwise sign the new account
        # into the existing profile while reporting success.
        moved = engine.relocated_slot_dirs()
        if moved:
            pause(f"\n  {RED}Can't sign in safely.{RESET} {app.display} has moved its "
                  f"user data and this tool can't find the new location.\n"
                  f"  Signing in now would land in your existing profile instead of a "
                  f"blank one.\n"
                  f"  {DIM}Moved out of: {moved[0]}{RESET}\n"
                  f"  Update the tool before using this.   [Enter] ")
            return False
        slot = engine.slot_candidates(app)[0]

    _full_clear()
    profile = engine.PROFILES_DIR / name
    try:
        replacing = profile.is_dir() and any(profile.iterdir())
    except OSError as e:
        pause(f"  {RED}Could not read profile '{name}': {e}{RESET}   [Enter] ")
        return False
    print(f"\n  {BOLD}Sign in '{name}'{RESET}\n")
    print("  Here's what happens:")
    print(f"    • Your main {app.display} closes and its session is set aside.")
    print(f"    • A blank {app.display} opens — you sign in with the account you want.")
    print(f"    • The tool grabs that session into '{name}' and puts your main one back.")
    if replacing:
        print(f"\n  {YELLOW}A successful sign-in replaces the saved data in '{name}'.{RESET}")
    print(f"\n  {YELLOW}Don't close this script during the process.{RESET}"
          f" {DIM}To stop, use {RESET}{BOLD}q{RESET}{DIM} — it cancels cleanly and restores your session.{RESET}")
    choices = "[y/N]" if replacing else "[Y/n]"
    if not confirm(f"\n  Continue? {choices} ", default=not replacing):
        return False
    try:
        with engine.claude_snapshot_lock():
            return _sign_in_claude_snapshot(name, slot)
    except (OSError, RuntimeError) as e:
        pause(f"  {RED}Sign-in unavailable. {e}{RESET}   [Enter] ")
        return False


def _sign_in_claude_snapshot(name: str, slot: Path) -> bool:
    app = engine.current_app()
    # A leftover stash would mean a prior interrupted run; refuse rather than
    # risk it (recovery is offered at startup). Better safe with the main session.
    if engine.has_orphan_backup():
        pause(f"  {RED}A previous session is still stashed.{RESET} Restart the tool to "
              "restore it first.   [Enter] ")
        return False

    reopen_main = False
    adopted = False
    stashed = False
    fresh_started = False
    try:
        main_was_running = engine.default_slot_running()
        engine.close_profile(name)
        print(f"\n  {DIM}closing {app.display} and setting your session aside…{RESET}")
        engine.close_default_slot(wait=20)
        reopen_main = main_was_running
        stashed = engine.stash_default_slot()
        engine.launch_default_slot()
        fresh_started = True
        print(f"\n  A blank {app.display} opened — {BOLD}sign in there{RESET} "
              f"{DIM}(email or Google).{RESET}\n")

        outcome = _detect_login(slot, follow_default=True)
        if outcome == "ready":
            engine.close_default_slot(wait=15)
            fresh_started = False
            slot = engine.default_slot_dir() or slot
            snap = usage.session_snapshot(slot)
            if snap["sessionKey"]:
                engine.adopt_default_into(name)
                adopted = True
            else:
                # Never copy a partial session — it would leave a broken profile.
                # Leave the main session untouched and let the user retry.
                pause(f"  {YELLOW}Couldn't capture the full session — nothing was copied.{RESET}\n"
                      f"  Your main session is untouched. Try again.   [Enter] ")
        elif outcome == "cancelled":
            print(f"\n  {DIM}cancelled — putting everything back…{RESET}")
        else:  # timeout
            print(f"\n  {YELLOW}Timed out waiting for sign-in — putting everything back…{RESET}")
    except KeyboardInterrupt:
        print(f"\n  {DIM}cancelled — putting everything back…{RESET}")
    except (OSError, RuntimeError, ValueError) as e:
        pause(f"  {RED}Sign-in failed: {e}{RESET}   [Enter] ")
    finally:
        # Always tear the fresh window down, then restore the main session. This
        # order matters: the slot's files stay locked while the app runs.
        try:
            if fresh_started:
                engine.close_default_slot(wait=15)
            if stashed:
                engine.restore_default_slot(during_signin=True)
        except (OSError, RuntimeError) as e:
            backup = (f"\n  Backup saved at {engine.backup_dir()}. Restart the tool to recover it."
                      if stashed else "")
            pause(f"  {RED}Sign-in cleanup failed: {e}{RESET}{backup}   [Enter] ")
        else:
            if not adopted and reopen_main and not engine.has_orphan_backup():
                print(f"  {DIM}reopening your main {app.display}…{RESET}")
                try:
                    engine.launch_default_slot()
                except (OSError, RuntimeError):
                    pass
    if adopted and not engine.has_orphan_backup():
        pause(f"\n  {GREEN}✓ '{name}' is signed in and ready.{RESET}  "
              f"Highlight it and press {BOLD}1{RESET} to launch.   [Enter] ")
    return adopted


# --- Usage ---------------------------------------------------------------- #
def _usage_remaining(used: float, *, width: int = 4) -> str:
    remaining = round(max(0, min(100, 100 - used)))
    if remaining < 20:
        color = RED
    elif remaining <= 50:
        color = YELLOW
    else:
        color = GREEN
    return f"{color}{usage.format_pct(remaining):>{width}}{RESET}"


def _usage_badge(result: usage.ProfileUsage | None) -> str:
    if result is None:
        return ""
    if not result.ok:
        return f" {GREY}✓{RESET}"
    parts = [f"{GREEN}✓{RESET}"]
    for label in ("5h", "7d"):
        used = _used_quota(result, label)
        if used is not None:
            parts.append(f"{_usage_remaining(used, width=0)} {GREY}({label}){RESET}")
    return " " + " ".join(parts)


def _used_quota(result: usage.ProfileUsage | None, label: str) -> float | None:
    if result is None or not result.ok:
        return None
    used = next((pct for window, pct, _ in result.rows if window == label), None)
    return used if isinstance(used, (int, float)) and isfinite(used) else None


def _ellip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render_usage(results: list[usage.ProfileUsage], app_display: str, *,
                 sort_by_usage: bool = False) -> list[str]:
    n = len(results)
    th = _theme(engine.current_app())
    lines = [
        "",
        f"  {BOLD}{th}◆ Usage{RESET}   {DIM}{app_display} · {n} account{'' if n == 1 else 's'}{RESET}",
        "",
    ]
    # One block per account: header line, then a row per limit with its own
    # reset (5h, 7d/30d, and per-model like Fable) — nothing collapsed.
    for r in results:
        name = r.name
        if not r.ok:
            reason = r.error or "could not verify usage"
            lines.append(f"  {th}{BOLD}{name}{RESET} {GREY}✓   ·{RESET}   {DIM}{reason}{RESET}")
            lines.append("")
            continue
        account = f"   {GREY}·{RESET}   {DIM}{_ellip(r.account, 34)}{RESET}" if r.account else ""
        lines.append(f"  {th}{BOLD}{name}{RESET}{account}")
        if not r.rows:
            lines.append(f"  {DIM}no usage limits reported by the app{RESET}")
        for label, pct, reset in r.rows:
            when = usage.humanize_reset(reset)
            reset_txt = f"   {DIM}resets {when}{RESET}" if when != "—" else ""
            lines.append(f"  {label:<7}  {_usage_remaining(pct)} left{reset_txt}")
        lines.append("")
    sort_label = "quota" if sort_by_usage else "name"
    lines += [f"  {DIM}{BOLD}R{RESET}{DIM} reload   ·   {BOLD}T{RESET}{DIM} Sort ({sort_label})"
              f"   ·   {BOLD}Enter{RESET}{DIM} back{RESET}", ""]
    return lines


def _usage_targets(app: engine.App, root: Path, names: list[str],
                   main_profile: str | None) -> dict[str | None, tuple[str, Path]]:
    targets: dict[str | None, tuple[str, Path]] = {}
    main_dir = engine.codex_main_home().parent if app is engine.CODEX else engine.default_slot_dir(app)
    if main_dir is not None:
        main_name = f"{MAIN_LABEL} · {main_profile}" if main_profile else MAIN_LABEL
        targets[None] = (main_name, main_dir)
    targets.update((name, (name, root / name)) for name in names if name != main_profile)
    return targets


def _invalidate_usage(app: engine.App, *names: str | None) -> None:
    global _menu_usage
    # A timestamp blocks in-flight results from reviving the previous account.
    invalidated = (time.monotonic(), None)
    with _usage_lock:
        _menu_usage = {**_menu_usage, **{(app, name): invalidated for name in names}}
    _refresh_usage[app].set()


def _publish_usage(app: engine.App, root: Path, main_profile: str | None,
                   results: dict[str | None, usage.ProfileUsage], started: float) -> None:
    global _menu_usage
    if None in results and app is engine.CODEX and engine.main_codex_profile(root) != main_profile:
        del results[None]
    with _usage_lock:
        cached = dict(_menu_usage)
        for name, result in results.items():
            key = (app, main_profile if name is None else name)
            previous = cached.get(key)
            if previous is None or started >= previous[0]:
                previous_result = previous[1] if previous is not None else None
                if (result.temporary_error and result.quota_key and previous_result is not None
                        and previous_result.ok and previous_result.quota_key == result.quota_key):
                    result = usage.unexpired_snapshot(previous_result, time.time()) or result
                cached[key] = (started, result)
        _menu_usage = cached


def refresh_selected_usage(state: State) -> None:
    if state.refreshing is not None:
        return
    app, root, name = engine.current_app(), engine.PROFILES_DIR, state.current_profile()
    future: Future[str] = Future()

    def fetch() -> None:
        label = name or MAIN_LABEL
        try:
            profile = engine.main_codex_profile(root) if name is None and app is engine.CODEX else None
            target = _usage_targets(app, root, [], profile).get(None) if name is None else (name, root / name)
            started = time.monotonic()
            result = (usage.fetch_all([target], app.key)[0] if target else
                      usage.ProfileUsage(label, error="not signed in"))
            _publish_usage(app, root, profile, {name: result}, started)
        except (OSError, RuntimeError, ValueError):
            future.set_result(f"{YELLOW}Could not refresh {label}.{RESET}")
            return
        status = (f"{GREEN}✓ Refreshed {result.name}{RESET}" if result.ok else
                  f"{YELLOW}{result.name} · {result.error}{RESET}")
        future.set_result(status)

    state.refreshing = (app, future)
    state.status = f"{DIM}Refreshing {name or MAIN_LABEL}…{RESET}"
    threading.Thread(target=fetch, daemon=True).start()


def _poll_usage_refresh(state: State) -> None:
    if state.refreshing is not None:
        app, future = state.refreshing
        if future.done():
            state.refreshing = None
            if app is engine.current_app() and state.pending_switch is None:
                state.status = future.result()


def _wait_for_live_usage(app: engine.App, targets: list[tuple[str, Path]]) -> list[usage.ProfileUsage] | None:
    future: Future[list[usage.ProfileUsage]] = Future()

    def fetch() -> None:
        try:
            future.set_result(usage.fetch_all(targets, app.key))
        except BaseException as error:
            future.set_exception(error)

    _draw(["", f"  {BOLD}{_theme(app)}◆ Usage{RESET}", "", f"  {DIM}fetching…{RESET}", "",
           f"  {DIM}Enter / Esc back{RESET}"])
    threading.Thread(target=fetch, daemon=True).start()
    while not future.done():
        if msvcrt.kbhit():
            if getkey() in ("ENTER", "ESC"):
                return None
        else:
            time.sleep(0.05)
    return future.result()


def action_usage(state: State) -> None:
    app = engine.current_app()
    by_profile: dict[str | None, usage.ProfileUsage] = {}
    reload = True
    while True:
        if reload:
            profile = engine.main_codex_profile() if app is engine.CODEX else None
            targets = _usage_targets(app, engine.PROFILES_DIR, state.profiles, profile)
            if not targets:
                return
            started = time.monotonic()
            fetched = _wait_for_live_usage(app, list(targets.values()))
            if fetched is None:
                return
            by_profile = dict(zip(targets, fetched))
            _publish_usage(app, engine.PROFILES_DIR, profile, by_profile, started)
        state.order_profiles(state.profiles)
        results = [by_profile[name] for name in (None, *state.profiles) if name in by_profile]
        _draw(render_usage(results, app.display, sort_by_usage=state.sort_by_usage))
        while (key := getkey()) not in ("r", "t", "ENTER", "ESC"):
            pass
        if key in ("ENTER", "ESC"):
            return
        reload = key == "r"
        if key == "t":
            state.toggle_sort()


def _notice_delay(lines: list[str]) -> None:
    deadline = time.monotonic() + 3
    displayed = 0
    while True:
        try:
            remaining = ceil(deadline - time.monotonic())
            if remaining <= 0:
                return
            if remaining != displayed:
                unit = "second" if remaining == 1 else "seconds"
                _draw([*lines, f"  {DIM}Options available in {remaining} {unit}.{RESET}", ""])
                displayed = remaining
            if msvcrt.kbhit():
                getkey()
            time.sleep(0.05)
        except KeyboardInterrupt:
            pass


def action_donate(launches: int | None = None) -> None:
    th = _theme(engine.current_app())
    lines = [
        "",
        f"  {th}{BOLD}◆ Support this project{RESET}",
        "",
    ]
    if launches is not None:
        lines += [f"  You've opened an instance {launches} times with this tool!", ""]
    lines += [
        "  I build and maintain this tool in my spare time as a volunteer.",
        "  If it helped you, even a small donation lets me know it made a difference.",
        "  I'd really appreciate it. Thanks for using the tool!",
        "",
        f"  {BOLD}Litecoin (LTC){RESET}   {LTC_ADDRESS}",
        f"  {BOLD}Ko-fi{RESET}            {KOFI_URL}",
        "",
        f"  {th}{BOLD}L{RESET} Copy LTC address   ·   {th}{BOLD}K{RESET} Open Ko-fi   ·   {BOLD}Enter{RESET} Back",
        "",
    ]
    if launches is not None:
        _notice_delay(lines)
    status = ""
    while True:
        _draw([*lines, status])
        key = getkey()
        if key in ("ENTER", "ESC"):
            return
        try:
            if key == "l":
                subprocess.run(["clip.exe"], input=LTC_ADDRESS, text=True, check=True,
                               creationflags=engine.NO_WINDOW, timeout=5)
                status = f"  {GREEN}LTC address copied.{RESET}"
            elif key == "k":
                os.startfile(KOFI_URL)
                status = f"  {GREEN}Opened Ko-fi in your browser.{RESET}"
        except (OSError, subprocess.SubprocessError) as e:
            status = f"  {RED}{e}{RESET}"


def _show_launch_milestone() -> None:
    launches = engine.take_launch_milestone()
    if launches is not None:
        action_donate(launches)
        _full_clear()


def _show_update(update: str, url: str) -> bool:
    download_url = engine.update_download_url(url)
    auto_update = engine.can_auto_update()
    lines = [
        "",
        f"  {YELLOW}{BOLD}◆ Update available{RESET}",
        "",
        f"  {update} is available. Updating is strongly recommended.",
        f"  {url if auto_update else download_url}",
        "",
        f"  {BOLD}Y{RESET} Update   ·   {BOLD}G{RESET} What's new   ·   {BOLD}N{RESET} Later",
        "",
    ]
    _notice_delay(lines)
    status = ""
    while True:
        _draw([*lines, status])
        key = getkey()
        if key in ("n", "ENTER", "ESC"):
            return True
        if key not in ("g", "y"):
            continue
        if key == "y" and auto_update:
            _draw([*lines, f"  {DIM}Updating from Git…{RESET}"])
            try:
                engine.apply_source_update()
            except RuntimeError as e:
                status = f"  {RED}{e}{RESET}"
                continue
            _draw([*lines, f"  {GREEN}Update installed. Press any key to close the manager, then reopen it.{RESET}"])
            getkey()
            return False
        target_url = url if key == "g" else download_url
        try:
            os.startfile(target_url)
        except OSError:
            status = f"  {RED}Could not open GitHub.{RESET} Copy this link into your browser.\n  {target_url}"
        else:
            if key == "y":
                return True
            status = f"  {DIM}Opened the changes in your browser.{RESET}"


# --- Main loop ------------------------------------------------------------ #
def _dispatch(key: str, state: State) -> bool:
    """Handle one key. Return False to quit. Modal actions repaint themselves."""
    if key and state.pending_switch is not None and key not in ("ENTER", "1"):
        state.pending_switch = None
        state.status = ""
    if not key or key == "ESC":
        return True
    if key == "0":
        return False
    app = engine.current_app()
    if state.closing is not None and state.closing[0] is app and key in ("ENTER", "1", "2", "4", "6", "7", "h", "d"):
        state.status = f"{DIM}Closing {app.display}…{RESET}"
        return True
    if key == "UP":
        state.move(-1)
    elif key == "DOWN":
        state.move(+1)
    elif key == "SPACE":
        state.toggle_check()
    elif key == "t":
        state.toggle_sort()
    elif key == "r":
        refresh_selected_usage(state)
    elif key in ("ENTER", "1"):
        do_launch(state)
    elif key == "2":
        do_login(state); _full_clear()
    elif key == "h" and app is engine.CODEX:
        try:
            shared = engine.toggle_codex_history()
        except (OSError, RuntimeError) as e:
            state.status = f"{RED}{e}{RESET}"
        else:
            _refresh_instances.set()
            _refresh_usage[app].set()
            state.status = (f"{GREEN}✓ Codex accounts use the main instance{RESET}" if shared
                            else f"{GREEN}✓ Codex profiles launch separately{RESET}")
    elif key == "d" and app is engine.CODEX and engine.shared_codex_history():
        do_attach_or_detach(state)
    elif key == "3":
        do_close(state)
    elif key == "4":
        do_rename(state); _full_clear()
    elif key == "5":
        do_shortcut(state)
    elif key == "6":
        do_delete(state); _full_clear()
    elif key == "7":
        do_new(state); _full_clear()
    elif key == "c":
        do_switch(state)
    elif key == "8":
        action_usage(state); _full_clear()
    elif key == "9":
        action_donate(); _full_clear()
    state.refresh()
    return True


def loop() -> None:
    state = State()
    state.refresh()
    updates: SimpleQueue[tuple[str, str]] = SimpleQueue()

    def check_for_update() -> None:
        if update := engine.check_update():
            updates.put(update)

    first_scan, first_main_usage = threading.Event(), threading.Event()
    startup_app = engine.current_app()
    _app_versions.update(engine.saved_client_versions())
    startup_deadline = time.monotonic() + 2.0
    for app in engine.APPS.values():
        threading.Thread(target=_version_probe, args=(app,), daemon=True).start()
        ready = first_main_usage if app is startup_app else None
        threading.Thread(target=_usage_probe, args=(app, first_scan, ready), daemon=True).start()
    threading.Thread(target=_running_probe, args=(first_scan,), daemon=True).start()
    threading.Thread(target=check_for_update, daemon=True).start()
    _full_clear()
    _show_launch_milestone()
    first_main_usage.wait(max(0.0, startup_deadline - time.monotonic()))
    last_sig = None
    prev_status = ""
    status_at = 0.0
    while True:
        if not updates.empty():
            if state.pending_switch is not None:
                state.pending_switch = None
                state.status = ""
            if not _show_update(*updates.get_nowait()):
                return
            _full_clear()
            last_sig = None
        _poll_close(state)
        _poll_usage_refresh(state)
        # Auto-clear the status line ~4 s after it last changed.
        if state.status != prev_status:
            prev_status = state.status
            status_at = time.monotonic()
        now = time.monotonic()
        if state.pending_switch is None and state.status and now - status_at > 4.0:
            state.status = ""
            prev_status = ""

        app = engine.current_app()
        sig = (tuple(state.profiles), state.cursor, frozenset(state.checked), state.sort_by_usage,
               _instances.get(app), _app_versions.get(app), _menu_usage, state.status, app.key)
        if sig != last_sig:
            _draw(render(state))
            last_sig = sig
        if msvcrt.kbhit():
            try:
                key = getkey()
            except KeyboardInterrupt:
                return
            if not key:
                continue
            _hide_cursor()
            if not _dispatch(key, state):
                return
            last_sig = None  # a modal action may have scrolled the screen
        else:
            time.sleep(0.05)


def _recover_interrupted() -> bool:
    if not engine.supports_login_snapshot():
        return True
    try:
        with engine.claude_snapshot_lock():
            return _recover_claude_snapshot()
    except (OSError, RuntimeError) as e:
        pause(f"  {RED}Claude recovery unavailable. {e}{RESET}   [Enter] ")
        return False


def _recover_claude_snapshot() -> bool:
    """If the script was closed mid sign-in, the main session is still stashed.
    Offer to restore it (default) or drop it, before showing the menu."""
    if not engine.has_orphan_backup():
        return True
    app = engine.current_app()
    _full_clear()
    _show_cursor()
    print(f"\n  {YELLOW}⚠ A sign-in was interrupted.{RESET}")
    print(f"  Your main {app.display} session was set aside and never restored.\n")
    print(f"    {BOLD}R{RESET}  restore it   {DIM}(recommended){RESET}")
    print(f"    {BOLD}S{RESET}  delete it permanently   {DIM}(you lose that session){RESET}")
    while True:
        try:
            choice = input("\n  R, S or Q to quit [R]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if choice == "q":
            return False
        if choice in ("", "r", "s"):
            break
    try:
        if choice == "s":
            engine.discard_backup()
        else:
            engine.close_default_slot(wait=15)
            engine.restore_default_slot()
    except (OSError, RuntimeError) as e:
        pause(f"  {RED}Recovery failed: {e}{RESET}   [Enter] ")
        return False
    return True


def main() -> int:
    _enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        engine.initialize_runtime()
    except OSError as e:
        print(f"Could not move the manager's local data: {e}", file=sys.stderr)
        return 1
    try:
        if _recover_interrupted():
            _hide_cursor()
            loop()
    except KeyboardInterrupt:
        pass
    finally:
        _show_cursor()
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
