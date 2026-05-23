"""Headless launcher for Claude profiles. No console window.

  launcher.pyw "<profile-name>"  -> open the profile, record it as active
  launcher.pyw "claude://..."    -> OAuth callback, send URL to the active profile
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine


def _error(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, "Claude Multi-Instance", 0x10)
    except Exception:
        pass


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    url = next((a for a in args if a.startswith("claude://")), None)

    exe = engine.find_claude_exe()
    if exe is None:
        _error("Claude not found.\nIs the desktop app installed?")
        return 1

    if url:
        name = engine.active_profile()
        data_dir = engine.PROFILES_DIR / name if name else None
        cmd = [str(exe)]
        if data_dir is not None:
            cmd.append(f"--user-data-dir={data_dir}")
        cmd.append(url)
        subprocess.Popen(
            cmd, creationflags=engine.DETACHED_FLAGS, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return 0

    name = args[0] if args else ""
    if not name:
        _error("Usage: launcher.pyw <profile-name>")
        return 2

    try:
        engine.launch_profile(name, exe=exe)
    except RuntimeError as e:
        _error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
