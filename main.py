"""Claude / Codex Multi-Instance — interactive console UI.

Run with:  python main.py

Profiles are chosen with the arrow keys (never by number); actions are single
digits. So a digit is always an action and never clashes with a profile.
"""
from __future__ import annotations

import json
import msvcrt
import os
import sys
import threading
import time
from pathlib import Path

import engine
import usage

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREY = "\033[90m"
ORANGE = "\033[38;5;173m"       # Claude's terracotta — theme accent
CODEX_BLUE = "\033[38;5;33m"    # Codex theme accent — a neutral Word/Discord blue

MAIN_LABEL = "Main instance"


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


def pause(msg: str = "  [Enter] ") -> None:
    ask(msg)


def confirm(prompt: str, default: bool = True) -> bool:
    """Yes/no that re-asks on anything unclear, so a mis-hit key never silently
    confirms or cancels. Empty input takes `default`."""
    while True:
        ans = ask(prompt).lower()
        if ans == "":
            return default
        if ans in ("y", "yes", "o", "oui"):
            return True
        if ans in ("n", "no", "non"):
            return False
        # anything else: ask again


def menu_choice(prompt: str, choices: tuple[str, ...]) -> str:
    """Read a single choice from `choices`, re-asking until one matches (guards
    against mis-hits on decisions that matter, e.g. restore vs delete)."""
    valid = {c.lower() for c in choices}
    while True:
        ans = ask(prompt).lower()
        if ans in valid:
            return ans


def getkey() -> str:
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        code = msvcrt.getwch()
        return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(code, "")
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
# running_profiles() shells out to PowerShell (~300 ms). A daemon thread runs it
# every couple of seconds and swaps the result in atomically, so the UI shows
# launches/closes without the user having to do anything.
_running: dict[str, list[int]] = {}
_main_running: bool = False


def _running_probe() -> None:
    global _running, _main_running
    while True:
        try:
            _running = engine.running_profiles()
            _main_running = engine.default_slot_running()
        except Exception:
            pass
        time.sleep(2.0)


# --- UI state ------------------------------------------------------------- #
# Row 0 is always the always-present "main instance" (the default slot, launched
# without --user-data-dir). Rows 1.. are the real profiles. So cursor 0 = main.
class State:
    def __init__(self) -> None:
        self.profiles: list[str] = []
        self.cursor: int = 0
        self.checked: set[str] = set()
        self.status: str = ""

    def refresh(self) -> None:
        self.profiles = [p.name for p in engine.list_profiles()]
        self.checked &= set(self.profiles)
        self.cursor = max(0, min(self.cursor, len(self.profiles)))

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

    def toggle_check(self) -> None:
        cur = self.current_profile()
        if cur is None:  # the main instance can't be multi-selected
            return
        self.checked.discard(cur) if cur in self.checked else self.checked.add(cur)


# --- Rendering ------------------------------------------------------------ #
_SEP = "─" * 50


def render(state: State) -> list[str]:
    app = engine.current_app()
    other = engine.CODEX if app is engine.CLAUDE else engine.CLAUDE
    is_claude = app is engine.CLAUDE
    th = _theme(app)
    exe = engine.find_app_exe()
    version = engine.app_version(exe) if exe else None

    lines: list[str] = []
    lines.append("")
    ver = f"{GREY}v{version}{RESET}" if version else f"{RED}not found{RESET}"
    lines.append(f"  {_title(app)}   {ver}")
    lines.append(f"  {DIM}↑ ↓ choose   ·   press a number to act{RESET}")
    lines.append("")

    # Row 0 = main instance, then the profiles.
    names = [MAIN_LABEL] + state.profiles
    width = min(max(len(n) for n in names), 22)

    def running_tag(on: bool) -> str:
        return f"{GREEN}● running{RESET}" if on else f"{GREY}○ idle{RESET}"

    for i, name in enumerate(names):
        cur = i == state.cursor
        arrow = f"{th}▸{RESET} " if cur else "  "
        if i == 0:  # main instance
            shown = f"{th}★{RESET} {BOLD}{name}{RESET}" if cur else f"{th}★{RESET} {name}"
            tag = running_tag(_main_running)
            lines.append(f"  {arrow}{shown}{' ' * (width - len(name) + 1)}  {tag}")
        else:
            box = f"{GREEN}◉{RESET} " if name in state.checked else "  "
            shown = name if len(name) <= width else name[: width - 1] + "…"
            label = f"{BOLD}{shown:<{width}}{RESET}" if cur else f"{shown:<{width}}"
            tag = running_tag(name in _running)
            lines.append(f"  {arrow}{box}{label}   {tag}")
    lines.append("")
    lines.append(f"  {GREY}{_SEP}{RESET}")

    # Actions: 3 columns, numbered top-to-bottom down each column, theme-coloured.
    def cell(num: str, label: str, on: bool = True) -> str:
        if not num:
            return " " * 13
        colour = f"{th}{BOLD}" if on else GREY
        return f"{colour}{num}{RESET if on else ''}  {label:<9}{RESET if not on else ''}"

    col1 = [cell("1", "Launch"), cell("2", "Sign in", is_claude),
            cell("3", "Close"), cell("4", "Rename")]
    col2 = [cell("5", "Shortcut"), cell("6", "Delete"), cell("7", "New"), cell("", "")]
    col3 = [cell("8", other.display), cell("9", "Usage"),
            cell("0", "Quit"), cell("", "")]
    for a, b, c in zip(col1, col2, col3):
        lines.append(f"  {a}  {b}  {c}".rstrip())
    lines.append("")
    lines.append(f"  {DIM}↑↓ move   ·   space multi-select   ·   "
                 f"{GREY}github.com/Zoltak-Dev/ai-multi-instance{RESET}")
    lines.append("")

    # Status line: just the message (no fake input prompt), auto-cleared by loop.
    lines.append(f"  {state.status}" if state.status else "")
    lines.append("")
    return lines


_MAIN_ONLY = f"{DIM}not available for the main instance{RESET}"


# --- Actions (operate on names, set a short status line) ------------------ #
def do_launch(state: State) -> None:
    if state.is_main():
        try:
            engine.launch_default_slot()
            state.status = f"{GREEN}✓ launched the main instance{RESET}"
        except RuntimeError as e:
            state.status = f"{RED}{e}{RESET}"
        return
    names = state.targets()
    if not names:
        return
    errs = []
    for n in names:
        try:
            engine.launch_profile(n)
        except RuntimeError as e:
            errs.append(f"{n}: {e}")
    state.status = (f"{RED}{'; '.join(errs)}{RESET}" if errs
                    else f"{GREEN}✓ launched {', '.join(names)}{RESET}")


def do_close(state: State) -> None:
    if state.is_main():
        n = engine.close_default_slot()
        state.status = (f"{GREEN}✓ closed the main instance{RESET}" if n
                        else f"{DIM}the main instance wasn't running{RESET}")
        return
    names = state.targets()
    if not names:
        return
    total = sum(engine.close_profile(n) for n in names)
    state.status = (f"{GREEN}✓ closed {total} process(es){RESET}" if total
                    else f"{DIM}nothing was running{RESET}")


def do_shortcut(state: State) -> None:
    if state.is_main():
        state.status = _MAIN_ONLY
        return
    names = state.targets()
    if not names:
        return
    for n in names:
        try:
            engine.delete_shortcut(n) if engine.shortcut_exists(n) else engine.create_shortcut(n)
        except OSError as e:
            state.status = f"{RED}{n}: {e}{RESET}"
            return
    state.status = f"{GREEN}✓ shortcut toggled{RESET}"


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
        engine.create_shortcut(name)
    except (OSError, ValueError) as e:
        pause(f"  {RED}Error: {e}{RESET}   [Enter] ")
        return
    state.status = f"{GREEN}✓ created {name}{RESET}"
    if engine.supports_login_snapshot() and confirm(f"  Sign in '{name}' now? [Y/n] ", default=True):
        login_flow(name)


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
        engine.rename_profile(name, new)
        state.status = f"{GREEN}✓ renamed to {new}{RESET}"
    except (OSError, ValueError, FileExistsError, FileNotFoundError) as e:
        pause(f"  {RED}Rename failed: {e}{RESET}   [Enter] ")


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
    failed = []
    for n in names:
        try:
            engine.delete_profile(n)
        except OSError as e:
            failed.append(f"{n} ({e})")
    state.checked.clear()
    if failed:
        pause(f"  {RED}Could not delete: {'; '.join(failed)}{RESET}\n  Close it first.  [Enter] ")
    else:
        state.status = f"{GREEN}✓ deleted {label}{RESET}"


def do_login(state: State) -> None:
    if state.is_main():
        state.status = f"{DIM}the main instance is where you sign the others in{RESET}"
        return
    name = state.current_profile()
    if name:
        _full_clear()
        login_flow(name)


def do_switch(state: State) -> None:
    other = engine.CODEX if engine.current_app() is engine.CLAUDE else engine.CLAUDE
    engine.set_app(other)
    state.cursor = 0
    state.checked.clear()
    state.status = ""
    state.refresh()


# --- Sign-in flow (snapshot model) ----------------------------------------- #
# "Signed in" (verified in the app's own code) = a `sessionKey` cookie exists in
# Network/Cookies; the OAuth token in config.json is derived from it. Chromium
# holds a freshly set cookie in memory and only writes it on its periodic flush;
# quitting exits WITHOUT flushing. So we correlate cookie-store writes with the
# moment OAuth appears. Writes from app startup are ignored, while a cookie
# committed just before or after config.json is accepted in either ordering.
def _config_has_oauth(cfg_path: Path) -> bool:
    """True once the fresh window has completed sign-in: the OAuth token appears
    in config.json (a plain file, readable while the app runs — unlike the
    exclusively-locked cookie store). The token is derived from the sessionKey,
    so its presence means the web session now exists in memory."""
    try:
        cfg = json.loads(cfg_path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return bool(cfg.get("oauth:tokenCacheV2") or cfg.get("oauth:tokenCache"))


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


def _detect_login(slot: Path, timeout: float = 600.0) -> str:
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
        cookie_signature = _cookie_store_signature(slot)
        if cookie_signature != cookie_ref:
            cookie_ref = cookie_signature
            cookie_changed_at = now

        if login_at is None and _config_has_oauth(cfg):
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


def login_flow(name: str) -> None:
    app = engine.current_app()
    slot = engine.default_slot_dir()
    if slot is None:
        return

    _full_clear()
    print(f"\n  {BOLD}Sign in '{name}'{RESET}\n")
    print(f"  Here's what happens:")
    print(f"    • Your main {app.display} closes and its session is set aside.")
    print(f"    • A blank {app.display} opens — you sign in with the account you want.")
    print(f"    • The tool grabs that session into '{name}' and puts your main one back.")
    print(f"\n  {YELLOW}Don't close this script during the process.{RESET}"
          f" {DIM}To stop, use {RESET}{BOLD}q{RESET}{DIM} — it cancels cleanly and restores your session.{RESET}")
    if not confirm("\n  Continue? [Y/n] ", default=True):
        return

    # A leftover stash would mean a prior interrupted run; refuse rather than
    # risk it (recovery is offered at startup). Better safe with the main session.
    if engine.has_orphan_backup():
        pause(f"  {RED}A previous session is still stashed.{RESET} Restart the tool to "
              "restore it first.   [Enter] ")
        return

    main_was_running = engine.default_slot_running()  # remember so we can reopen it
    engine.close_profile(name)
    adopted = False
    stashed = False
    try:
        print(f"\n  {DIM}closing {app.display} and setting your session aside…{RESET}")
        engine.close_default_slot(wait=20)
        stashed = engine.stash_default_slot()
        engine.launch_default_slot()
        print(f"\n  A blank {app.display} opened — {BOLD}sign in there{RESET} "
              f"{DIM}(email or Google).{RESET}\n")

        outcome = _detect_login(slot)
        if outcome == "ready":
            engine.close_default_slot(wait=15)
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
    except (OSError, RuntimeError) as e:
        pause(f"  {RED}Sign-in failed: {e}{RESET}   [Enter] ")
    finally:
        # Always tear the fresh window down, then restore the main session. This
        # order matters: the slot's files stay locked while the app runs.
        engine.close_default_slot(wait=15)
        if stashed and engine.has_orphan_backup():
            try:
                engine.restore_default_slot()
            except OSError as e:
                pause(f"  {RED}Could not restore your main session: {e}{RESET}\n"
                      f"  It is safe at {engine.backup_dir()} — restart the tool to recover it.   [Enter] ")
        # Reopen the main app ONLY when we aborted (cancel/timeout/error) and it
        # was open before — restoring the user's state. On a successful sign-in
        # there's no point reopening it.
        if not adopted and main_was_running and not engine.has_orphan_backup():
            print(f"  {DIM}reopening your main {app.display}…{RESET}")
            try:
                engine.launch_default_slot()
            except RuntimeError:
                pass
    if adopted:
        pause(f"\n  {GREEN}✓ '{name}' is signed in and ready.{RESET}  "
              f"Highlight it and press {BOLD}1{RESET} to launch.   [Enter] ")


# --- Usage (Claude only) --------------------------------------------------- #
def _usage_pct(value: float | None) -> str:
    text = usage.format_pct(value)
    if value is None:
        return f"{DIM}{text}{RESET}"
    if value >= 90:
        return f"{RED}{text}{RESET}"
    if value >= 70:
        return f"{YELLOW}{text}{RESET}"
    return f"{GREEN}{text}{RESET}"


def _ellip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def render_usage(results: list[usage.ProfileUsage], app_display: str) -> list[str]:
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
        name = _ellip(r.name, 16)
        if not r.ok:
            lines.append(f"  {th}{BOLD}{name}{RESET}   {DIM}{r.error}{RESET}")
            lines.append("")
            continue
        lines.append(f"  {th}{BOLD}{name}{RESET}   {DIM}{_ellip(r.account, 34)}{RESET}")
        for label, pct, reset in r.rows:
            pct_txt = usage.format_pct(pct)
            pct_col = " " * max(0, 4 - len(pct_txt)) + _usage_pct(pct)
            when = usage.humanize_reset(reset)
            reset_txt = f"   {DIM}resets {when}{RESET}" if when != "—" else ""
            lines.append(f"     {label:<7}{pct_col}{reset_txt}")
        lines.append("")
    lines += [f"  {DIM}{BOLD}R{RESET}{DIM} reload   ·   {BOLD}Enter{RESET}{DIM} back{RESET}", ""]
    return lines


def action_usage(state: State) -> None:
    app = engine.current_app()
    # The main instance is a usage target too, under its own dir: Claude's
    # default slot, or Codex's default CODEX_HOME (~/.codex).
    if app is engine.CLAUDE:
        main_dir = engine.default_slot_dir()
    else:
        main_dir = Path(os.environ.get("USERPROFILE") or Path.home())
    targets: list[tuple[str, Path]] = []
    if main_dir is not None:
        targets.append(("Main instance", main_dir))
    targets += [(n, engine.PROFILES_DIR / n) for n in state.profiles]
    if not targets:
        return
    _show_cursor()
    while True:
        _draw(["", f"  {BOLD}{_theme(app)}◆ Usage{RESET}", "", f"  {DIM}fetching…{RESET}"])
        results = usage.fetch_all(targets, app.key)
        _draw(render_usage(results, app.display))
        if ask("  ").lower() != "r":
            _hide_cursor()
            return


# --- Main loop ------------------------------------------------------------ #
def _dispatch(key: str, state: State) -> bool:
    """Handle one key. Return False to quit. Modal actions repaint themselves."""
    is_claude = engine.current_app() is engine.CLAUDE
    if key in ("q", "0", "ESC"):
        return False
    if key == "UP":
        state.move(-1)
    elif key == "DOWN":
        state.move(+1)
    elif key == "SPACE":
        state.toggle_check()
    elif key in ("ENTER", "1"):
        do_launch(state)
    elif key == "2" and is_claude:
        do_login(state); _full_clear()
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
    elif key == "8":
        do_switch(state)
    elif key == "9":
        action_usage(state); _full_clear()
    state.refresh()
    return True


def loop() -> None:
    state = State()
    state.refresh()
    threading.Thread(target=_running_probe, daemon=True).start()
    _full_clear()
    last_sig = None
    prev_status = ""
    status_at = 0.0
    while True:
        # Auto-clear the status line ~4 s after it last changed.
        if state.status != prev_status:
            prev_status = state.status
            status_at = time.monotonic()
        if state.status and time.monotonic() - status_at > 4.0:
            state.status = ""
            prev_status = ""

        sig = (tuple(state.profiles), state.cursor, frozenset(state.checked),
               frozenset(_running), _main_running, state.status, engine.current_app().key)
        if sig != last_sig:
            _draw(render(state))
            last_sig = sig
        if msvcrt.kbhit():
            try:
                key = getkey()
            except KeyboardInterrupt:
                return
            _hide_cursor()
            if not _dispatch(key, state):
                return
            last_sig = None  # a modal action may have scrolled the screen
        else:
            time.sleep(0.05)


def _recover_interrupted() -> None:
    """If the script was closed mid sign-in, the main session is still stashed.
    Offer to restore it (default) or drop it, before showing the menu."""
    if not engine.has_orphan_backup():
        return
    app = engine.current_app()
    _full_clear()
    _show_cursor()
    print(f"\n  {YELLOW}⚠ A sign-in was interrupted.{RESET}")
    print(f"  Your main {app.display} session was set aside and never restored.\n")
    print(f"    {BOLD}R{RESET}  restore it   {DIM}(recommended){RESET}")
    print(f"    {BOLD}S{RESET}  delete it permanently   {DIM}(you lose that session){RESET}")
    if menu_choice("\n  R or S: ", ("r", "s")) == "s":
        engine.discard_backup()
    else:
        try:
            engine.restore_default_slot()
        except OSError as e:
            pause(f"  {RED}Restore failed: {e}{RESET}   [Enter] ")


def main() -> int:
    _enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    _recover_interrupted()
    _hide_cursor()
    try:
        loop()
    except KeyboardInterrupt:
        pass
    finally:
        _show_cursor()
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
