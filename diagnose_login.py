"""Read-only diagnostics for Claude snapshot sign-in detection.

This script never launches, closes, or modifies Claude. It prints metadata and
key names only: cookie values and config values are never read into the report.
Run it in a second terminal while reproducing a stuck sign-in.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PUBLISHER_HASH = "pzs8sxrjxfjjc"
AUTH_KEY_PARTS = ("account", "auth", "login", "oauth", "session", "token")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[^\s:@]+@[^\s:@]+")
LONG_HEX_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{12,}(?![0-9a-f])", re.IGNORECASE)


def _powershell_json(script: str) -> Any:
    """Run a read-only PowerShell query and decode its JSON output."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=flags,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _package_info() -> list[dict[str, str]]:
    raw = _powershell_json(
        "Get-AppxPackage | Where-Object {$_.Name -like '*Claude*'} | "
        "Select-Object Name,Version,PackageFamilyName | ConvertTo-Json -Compress"
    )
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [
        {
            "name": str(item.get("Name", "")),
            "version": str(item.get("Version", "")),
            "family": str(item.get("PackageFamilyName", "")),
        }
        for item in items
        if isinstance(item, dict)
    ]


def _process_summary() -> dict[str, int]:
    raw = _powershell_json(
        "Get-CimInstance Win32_Process -Filter \"Name = 'claude.exe'\" | "
        "Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    summary = {"packaged_default": 0, "packaged_profile": 0, "other": 0}
    if raw is None:
        return summary
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if not isinstance(item, dict):
            continue
        executable = str(item.get("ExecutablePath") or "").lower()
        command = str(item.get("CommandLine") or "").lower()
        if "windowsapps\\claude_" not in executable:
            summary["other"] += 1
        elif "--user-data-dir=" in command:
            summary["packaged_profile"] += 1
        elif "--type=" not in command:
            summary["packaged_default"] += 1
    return summary


def _default_slot(packages: list[dict[str, str]]) -> tuple[Path, str]:
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    for package in packages:
        family = package.get("family", "")
        if family:
            root = local / "Packages" / family
            if root.is_dir():
                return root / "LocalCache" / "Roaming" / "Claude", "packaged"
    known = local / "Packages" / f"Claude_{PUBLISHER_HASH}"
    if known.is_dir():
        return known / "LocalCache" / "Roaming" / "Claude", "packaged-known-family"
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return roaming / "Claude", "unpackaged-fallback"


def _safe_key(name: str) -> str:
    """Redact identifiers that may be embedded in configuration key names."""
    name = UUID_RE.sub("<uuid>", name)
    name = EMAIL_RE.sub("<email>", name)
    return LONG_HEX_RE.sub("<hex-id>", name)


def _auth_key_paths(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Return auth-related JSON key paths without returning any values."""
    if depth > 6:
        return []
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = _safe_key(str(raw_key))
            path = f"{prefix}.{key}" if prefix else key
            if any(part in key.lower() for part in AUTH_KEY_PARTS):
                found.append(f"{path}<{type(child).__name__}>")
            found.extend(_auth_key_paths(child, path, depth + 1))
    elif isinstance(value, list):
        for child in value[:10]:
            found.extend(_auth_key_paths(child, f"{prefix}[]", depth + 1))
    return sorted(set(found))


def _config_state(slot: Path) -> dict[str, Any]:
    path = slot / "config.json"
    try:
        stat = path.stat()
    except OSError as exc:
        return {
            "signature": (False, 0, 0),
            "parse": f"unavailable:{exc.__class__.__name__}",
            "detector_oauth": False,
            "account_marker": False,
            "top_level_keys": [],
            "auth_key_paths": [],
        }
    state: dict[str, Any] = {
        "signature": (True, stat.st_size, stat.st_mtime_ns),
        "parse": "ok",
        "detector_oauth": False,
        "account_marker": False,
        "top_level_keys": [],
        "auth_key_paths": [],
    }
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            state["parse"] = f"unexpected-root:{type(data).__name__}"
            return state
        state["detector_oauth"] = bool(
            data.get("oauth:tokenCacheV2") or data.get("oauth:tokenCache")
        )
        state["account_marker"] = bool(data.get("lastKnownAccountUuid"))
        state["top_level_keys"] = sorted(_safe_key(str(key)) for key in data)
        state["auth_key_paths"] = _auth_key_paths(data)
    except (OSError, ValueError) as exc:
        state["parse"] = exc.__class__.__name__
    return state


def _file_state(path: Path) -> tuple[bool, int, int]:
    try:
        stat = path.stat()
        return True, stat.st_size, stat.st_mtime_ns
    except OSError:
        return False, 0, 0


def _cookie_state(slot: Path) -> dict[str, tuple[bool, int, int]]:
    network = slot / "Network"
    return {
        name: _file_state(network / name)
        for name in ("Cookies", "Cookies-journal", "Cookies-wal")
    }


def _disk_session_state(slot: Path) -> str:
    """Check only whether sessionKey exists; never select its value."""
    cookies = slot / "Network" / "Cookies"
    if not cookies.is_file():
        return "database-missing"
    try:
        uri = f"file:{cookies.as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=0.1) as connection:
            present = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM cookies "
                "WHERE host_key LIKE '%claude.ai%' AND name = 'sessionKey')"
            ).fetchone()[0]
        return "present" if present else "absent"
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorname", exc.__class__.__name__)
        return f"unreadable:{code}"
    except OSError as exc:
        return f"unreadable:{exc.__class__.__name__}"


def _mtime_text(signature: tuple[bool, int, int]) -> str:
    exists, size, mtime_ns = signature
    if not exists:
        return "missing"
    stamp = dt.datetime.fromtimestamp(
        mtime_ns / 1_000_000_000, tz=dt.timezone.utc
    ).isoformat(timespec="milliseconds")
    return f"size={size}, mtime_utc={stamp}"


def _print_config(label: str, state: dict[str, Any]) -> None:
    print(f"{label}.config={_mtime_text(state['signature'])}")
    print(f"{label}.config_parse={state['parse']}")
    print(f"{label}.detector_oauth={state['detector_oauth']}")
    print(f"{label}.account_marker={state['account_marker']}")
    print(f"{label}.top_level_keys={json.dumps(state['top_level_keys'])}")
    print(f"{label}.auth_key_paths={json.dumps(state['auth_key_paths'])}")


def _print_cookies(label: str, state: dict[str, tuple[bool, int, int]]) -> None:
    for name, signature in state.items():
        print(f"{label}.{name}={_mtime_text(signature)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only monitor for Claude snapshot sign-in signals."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=180.0,
        help="seconds to watch; use 0 for a single snapshot (default: 180)",
    )
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    if args.duration < 0 or args.interval <= 0:
        parser.error("duration must be >= 0 and interval must be > 0")

    packages = _package_info()
    slot, slot_kind = _default_slot(packages)
    config = _config_state(slot)
    cookies = _cookie_state(slot)

    print("=== Claude login diagnostic (read-only, no secret values) ===")
    print(f"timestamp_utc={dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    print(f"python={platform.python_version()}")
    print(f"windows={platform.platform()}")
    print(f"packages={json.dumps(packages, sort_keys=True)}")
    print(f"processes={json.dumps(_process_summary(), sort_keys=True)}")
    print(f"slot_kind={slot_kind}")
    print(f"slot_exists={slot.is_dir()}")
    _print_config("initial", config)
    _print_cookies("initial", cookies)
    print(f"initial.disk_session={_disk_session_state(slot)}")

    if args.duration == 0:
        print("=== End diagnostic ===")
        return 0

    print(f"watching_seconds={args.duration:g} (Ctrl+C stops safely)")
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.duration:
            time.sleep(args.interval)
            elapsed = time.monotonic() - started
            new_config = _config_state(slot)
            new_cookies = _cookie_state(slot)
            if new_config != config:
                _print_config(f"event+{elapsed:.1f}s", new_config)
                config = new_config
            if new_cookies != cookies:
                _print_cookies(f"event+{elapsed:.1f}s", new_cookies)
                print(f"event+{elapsed:.1f}s.disk_session={_disk_session_state(slot)}")
                cookies = new_cookies
    except KeyboardInterrupt:
        print("watch_stopped_by_user=true")

    print(f"final.processes={json.dumps(_process_summary(), sort_keys=True)}")
    final_config = _config_state(slot)
    final_cookies = _cookie_state(slot)
    _print_config("final", final_config)
    _print_cookies("final", final_cookies)
    print(f"final.disk_session={_disk_session_state(slot)}")
    print("=== End diagnostic ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
