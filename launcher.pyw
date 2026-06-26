"""Headless launcher for Claude/Codex profiles. No console window.

  launcher.pyw --app=<key> "<profile-name>"  -> open profile in that app

The --app flag is added automatically by desktop shortcuts. If absent, Claude
is assumed.
"""
import subprocess
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


def _parse_args(argv: list[str]) -> tuple[engine.App | None, list[str]]:
    """Pop --app=<key> from argv. Returns (app, rest)."""
    app: engine.App | None = None
    rest: list[str] = []
    for a in argv:
        if not a.strip():
            continue
        if a.startswith("--app="):
            app = engine.APPS.get(a.split("=", 1)[1].strip().lower())
        else:
            rest.append(a)
    return app, rest


def main() -> int:
    app, args = _parse_args(sys.argv[1:])
    if app is None:
        app = engine.CLAUDE  # last-resort default

    engine.set_app(app, persist=False)

    exe = engine.find_app_exe()
    if exe is None:
        _error(f"{app.display} not found.\nIs the desktop app installed?",
               f"{app.display} Multi-Instance")
        return 1

    name = args[0] if args else ""
    if not name:
        _error("Usage: launcher.pyw --app=<claude|codex> <profile-name>",
               f"{app.display} Multi-Instance")
        return 2

    try:
        engine.launch_profile(name, exe=exe)
    except RuntimeError as e:
        _error(str(e), f"{app.display} Multi-Instance")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
