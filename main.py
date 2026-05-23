"""Claude Multi-Instance — console menu.

Run with:  python main.py
"""
from __future__ import annotations

import os
import sys

import engine

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREY = "\033[90m"


def _enable_ansi() -> None:
    """Enable ANSI escape processing on legacy Windows cmd."""
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


def _clear_and_write(text: str) -> None:
    """Wipe screen and write the whole frame in one syscall (no per-line flicker)."""
    sys.stdout.write("\033[2J\033[H" + text)
    sys.stdout.flush()


# --- Frame rendering ------------------------------------------------------ #
def render(profiles: list[str]) -> str:
    exe = engine.find_claude_exe()
    version = engine.claude_version(exe) if exe else None
    routing = engine.login_routing_enabled()
    active = engine.active_profile()
    bar = "─" * 52

    out: list[str] = []
    out.append(f"{CYAN}{bar}{RESET}")
    out.append(f"{BOLD}  Claude Multi-Instance{RESET}")
    out.append(f"{CYAN}{bar}{RESET}")
    if version:
        out.append(f"  Claude desktop  {GREY}:{RESET} v{version}")
    else:
        out.append(f"  Claude desktop  {GREY}:{RESET} {RED}not found{RESET}")

    state = f"{GREEN}on{RESET}" if routing else f"{GREY}off{RESET}"
    out.append(f"  Login patch     {GREY}:{RESET} {state}")
    if routing:
        target = active if active else f"{YELLOW}none yet — launch a profile first{RESET}"
        out.append(f"  Google login →  {GREY}:{RESET} {target}")
    else:
        out.append(f"  Google login →  {GREY}:{RESET} {DIM}default Claude (patch off){RESET}")
    out.append("")

    out.append(f"  {BOLD}Profiles ({len(profiles)}){RESET}")
    if not profiles:
        out.append(f"    {DIM}(none){RESET}")
    else:
        width = len(str(len(profiles)))
        for i, name in enumerate(profiles, 1):
            tag = f"  {GREEN}[shortcut]{RESET}" if engine.shortcut_exists(name) else ""
            out.append(f"    {CYAN}{i:>{width}}.{RESET} {name}{tag}")
    out.append("")

    toggle_label = "Disable" if routing else "Enable"
    out.append(f"    {CYAN}1{RESET}  Launch profile(s)")
    out.append(f"    {CYAN}2{RESET}  Close profile(s)")
    out.append(f"    {CYAN}3{RESET}  Create a profile")
    out.append(f"    {CYAN}4{RESET}  Rename a profile")
    out.append(f"    {CYAN}5{RESET}  Toggle desktop shortcut")
    out.append(f"    {CYAN}6{RESET}  Delete profile(s)")
    out.append(f"    {CYAN}7{RESET}  {toggle_label} login patch")
    out.append(f"    {CYAN}0{RESET}  Quit")
    out.append("")
    out.append(f"  {DIM}Tip: multi-select with spaces, e.g. '1 3 5'{RESET}")
    out.append("  > ")
    return "\n".join(out)


# --- Input helpers -------------------------------------------------------- #
def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def pause(msg: str = "  [Enter] ") -> None:
    ask(msg)


def pick_profiles(profiles: list[str], verb: str, allow_multi: bool = True) -> list[str]:
    """Prompt for one or more profile numbers. Returns the matching names, in
    selection order, with duplicates removed. Returns [] on cancel or no match.
    """
    if not profiles:
        ask(f"  {YELLOW}No profile.{RESET} [Enter] ")
        return []
    if allow_multi:
        prompt = f"  Number(s) to {verb} (empty = cancel, e.g. '1 3 5'): "
    else:
        prompt = f"  Number to {verb} (empty = cancel): "
    raw = ask(prompt)
    if not raw:
        return []
    tokens = raw.split()
    if not allow_multi and len(tokens) > 1:
        pause(f"  {YELLOW}One profile at a time for this action.{RESET}  ")
        return []
    picked: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if not tok.isdigit():
            continue
        idx = int(tok)
        if not (1 <= idx <= len(profiles)):
            continue
        name = profiles[idx - 1]
        if name not in seen:
            seen.add(name)
            picked.append(name)
    return picked


# --- Actions -------------------------------------------------------------- #
def action_launch(profiles: list[str]) -> None:
    names = pick_profiles(profiles, "launch")
    if not names:
        return
    errs: list[str] = []
    for name in names:
        try:
            engine.launch_profile(name)
        except RuntimeError as e:
            errs.append(f"{name}: {e}")
    if errs:
        pause(f"  {RED}" + "; ".join(errs) + RESET + " ")


def action_close(profiles: list[str]) -> None:
    names = pick_profiles(profiles, "close")
    if not names:
        return
    total = 0
    not_running: list[str] = []
    for name in names:
        n = engine.close_profile(name)
        if n == 0:
            not_running.append(name)
        else:
            total += n
    parts: list[str] = []
    if total:
        parts.append(f"{GREEN}✓ Killed {total} process(es){RESET}")
    if not_running:
        parts.append(f"{DIM}not running: {', '.join(not_running)}{RESET}")
    if parts:
        pause("  " + "  ".join(parts) + " ")


def action_create() -> None:
    name = ask("  New profile name (empty = cancel): ")
    if not name:
        return
    if not engine.valid_profile_name(name):
        pause(f"  {RED}Invalid name.{RESET} Forbidden chars: < > : \" / \\ | ? *  ")
        return
    if (engine.PROFILES_DIR / name).exists():
        pause(f"  {YELLOW}'{name}' already exists.{RESET}  ")
        return
    try:
        engine.create_profile(name)
        engine.create_shortcut(name)
    except (OSError, ValueError) as e:
        pause(f"  {RED}Error: {e}{RESET}  ")


def action_rename(profiles: list[str]) -> None:
    names = pick_profiles(profiles, "rename", allow_multi=False)
    if not names:
        return
    name = names[0]
    new = ask(f"  New name for '{name}': ")
    if not new or new == name:
        return
    try:
        engine.rename_profile(name, new)
    except (OSError, ValueError, FileExistsError, FileNotFoundError) as e:
        pause(f"  {RED}Rename failed: {e}{RESET}  ")


def action_shortcut(profiles: list[str]) -> None:
    names = pick_profiles(profiles, "toggle shortcut for")
    if not names:
        return
    errs: list[str] = []
    for name in names:
        try:
            if engine.shortcut_exists(name):
                engine.delete_shortcut(name)
            else:
                engine.create_shortcut(name)
        except OSError as e:
            errs.append(f"{name}: {e}")
    if errs:
        pause(f"  {RED}" + "; ".join(errs) + RESET + " ")


def action_delete(profiles: list[str]) -> None:
    names = pick_profiles(profiles, "delete")
    if not names:
        return
    label = f"'{names[0]}'" if len(names) == 1 else f"{len(names)} profiles ({', '.join(names)})"
    confirm = ask(f"  {YELLOW}Delete {label} and all data? Type 'yes': {RESET}")
    if confirm.lower() not in ("yes", "y"):
        return
    failed: list[str] = []
    for name in names:
        try:
            engine.delete_profile(name)
        except OSError as e:
            failed.append(f"{name} ({e})")
    if failed:
        pause(f"  {RED}Could not delete: {'; '.join(failed)}{RESET}\n"
              f"  Close Claude on those profiles first.  ")


def action_toggle_login() -> None:
    try:
        if engine.login_routing_enabled():
            engine.disable_login_routing()
        else:
            engine.enable_login_routing()
    except OSError as e:
        pause(f"  {RED}Registry error: {e}{RESET}  ")


# --- Main loop ------------------------------------------------------------ #
def loop() -> None:
    while True:
        profiles = [p.name for p in engine.list_profiles()]
        _clear_and_write(render(profiles))
        choice = ask("")
        if choice == "1":
            action_launch(profiles)
        elif choice == "2":
            action_close(profiles)
        elif choice == "3":
            action_create()
        elif choice == "4":
            action_rename(profiles)
        elif choice == "5":
            action_shortcut(profiles)
        elif choice == "6":
            action_delete(profiles)
        elif choice == "7":
            action_toggle_login()
        elif choice == "0":
            return
        # Anything else: redraw on next iteration.


def main() -> int:
    _enable_ansi()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        loop()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
