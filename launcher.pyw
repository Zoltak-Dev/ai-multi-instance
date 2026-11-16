"""Headless launcher for Claude/Codex profiles. No console window.

  launcher.pyw --app=<key> "<profile-name>"  -> open profile in that app

The --app flag is added automatically by desktop shortcuts. If absent, Claude
is assumed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine


def _error(message: str, title: str = "Multi-Instance") -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        pass


def _confirm_codex_restart(name: str) -> bool:
    from ctypes import windll

    ok_cancel, warning, default_cancel, foreground = 0x01, 0x30, 0x100, 0x10000
    answer = windll.user32.MessageBoxW(
        None,
        f"Switch to '{name}'? This will close and reopen the main Codex window.",
        "Codex Multi-Instance",
        ok_cancel | warning | default_cancel | foreground,
    )
    return answer == 1  # IDOK


def _parse_args(argv: list[str]) -> tuple[engine.App, str]:
    app = engine.CLAUDE
    if argv and argv[0].startswith("--app="):
        key = argv[0].split("=", 1)[1].strip().lower()
        if key not in engine.APPS:
            raise ValueError("Choose either --app=claude or --app=codex.")
        app, argv = engine.APPS[key], argv[1:]
    if len(argv) != 1 or not argv[0].strip():
        raise ValueError("Usage: launcher.pyw --app=<claude|codex> <profile-name>")
    return app, argv[0]


def main() -> int:
    try:
        app, name = _parse_args(sys.argv[1:])
    except ValueError as e:
        _error(str(e))
        return 2

    try:
        engine.initialize_runtime()
        engine.set_app(app, persist=False)
        engine.launch_account(name, confirm_restart=lambda _: _confirm_codex_restart(name))
    except (OSError, RuntimeError, ValueError) as e:
        _error(str(e), f"{app.display} Multi-Instance")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
