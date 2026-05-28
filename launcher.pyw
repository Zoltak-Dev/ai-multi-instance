"""Headless launcher for Claude/Codex profiles. No console window.

  launcher.pyw --app=<key> "<profile-name>"  -> open profile in that app
  launcher.pyw --app=<key> "<scheme>://..."  -> OAuth callback to active profile

The --app flag is added automatically by shortcuts and by the protocol
registry handler. If absent, the URL scheme is used as a fallback.
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
    """Pop --app=<key> from argv (or default by URL scheme later). Returns (app, rest)."""
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

    # Fallback: detect app from a URL scheme.
    if app is None:
        for a in args:
            scheme = a.split(":", 1)[0].lower() if ":" in a else ""
            for candidate in engine.APPS.values():
                if scheme == candidate.protocol:
                    app = candidate
                    break
            if app is not None:
                break
    if app is None:
        app = engine.CLAUDE  # last-resort default

    engine.set_app(app, persist=False)

    exe = engine.find_app_exe()
    if exe is None:
        _error(f"{app.display} not found.\nIs the desktop app installed?",
               f"{app.display} Multi-Instance")
        return 1

    url = next((a for a in args if a.startswith(f"{app.protocol}://")), None)
    if url:
        name = engine.active_profile()
        data_dir = engine.PROFILES_DIR / name if name else None
        cmd = [str(exe)]
        if data_dir is not None:
            cmd.append(f"--user-data-dir={data_dir}")
        cmd.append(url)
        env = engine.profile_env(name) if name else None
        subprocess.Popen(
            cmd, env=env, creationflags=engine.DETACHED_FLAGS, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return 0

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
