"""Per-account usage tracking for Claude and Codex — zero third-party deps.

For each Claude profile we read the session cookie straight from that profile's
Chromium cookie store (``ClaudeProfiles/<name>/Network/Cookies``), decrypt it,
and query the official claude.ai usage endpoint. Because every profile is an
isolated ``--user-data-dir``, each account's session lives in its own store, so
usage is reported per account with no cross-talk.

Codex is simpler: each profile stores its ChatGPT OAuth tokens in a plain file
(``CodexProfiles/<name>/.codex/auth.json``), and the ``codex/usage`` endpoint
returns the rolling-window percentages directly — see the "Codex API" section.
``fetch_all(dirs, app)`` dispatches to the right backend.

Decryption uses Windows DPAPI (``crypt32``) for the master key and AES-256-GCM
via CNG/BCrypt (``bcrypt.dll``) for the cookie values — all through ``ctypes``,
keeping the project on the standard library only. The ``sessionKey`` is a bearer
token, so a plain HTTPS request carrying it plus a Chrome User-Agent clears
Cloudflare and returns usage — no browser to spin up.

A running instance holds an *exclusive* lock on its own cookie store, so the
profile in active use is the one we cannot read. We therefore cache the
sessionKey (DPAPI-encrypted) whenever the store is readable and fall back to it
while locked — see the "Session resolution + cache" section.

Notes on the on-disk format (verified empirically against live profiles):
  * ``Local State`` -> ``os_crypt.encrypted_key`` is base64, prefixed with the
    literal ``DPAPI`` then DPAPI-protected; once unprotected it is the 32-byte
    AES key.
  * Cookie values are ``v10``/``v11`` blobs: 3-byte version prefix, 12-byte
    nonce, ciphertext, 16-byte GCM tag. Recent Chromium prepends
    ``SHA256(host_key)`` (32 bytes) to the plaintext to bind a cookie to its
    domain; we verify and strip it.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from ctypes import POINTER, byref, c_char_p, c_ubyte, c_void_p, cast, create_string_buffer, sizeof
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from http.client import HTTPException
from math import isfinite
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread

import engine

# --- CNG / DPAPI bindings -------------------------------------------------- #
_bcrypt = ctypes.WinDLL("bcrypt")
_crypt32 = ctypes.WinDLL("crypt32")
_kernel32 = ctypes.WinDLL("kernel32")

_ULONG = wintypes.ULONG
_NTSTATUS = wintypes.LONG


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", _ULONG), ("pbData", POINTER(c_ubyte))]


class _AuthInfo(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO."""
    _fields_ = [
        ("cbSize", _ULONG),
        ("dwInfoVersion", _ULONG),
        ("pbNonce", POINTER(c_ubyte)),
        ("cbNonce", _ULONG),
        ("pbAuthData", POINTER(c_ubyte)),
        ("cbAuthData", _ULONG),
        ("pbTag", POINTER(c_ubyte)),
        ("cbTag", _ULONG),
        ("pbMacContext", POINTER(c_ubyte)),
        ("cbMacContext", _ULONG),
        ("cbAAD", _ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", _ULONG),
    ]


_crypt32.CryptUnprotectData.argtypes = [
    POINTER(_DataBlob), c_void_p, POINTER(_DataBlob), c_void_p, c_void_p, _ULONG, POINTER(_DataBlob)]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_crypt32.CryptProtectData.argtypes = [
    POINTER(_DataBlob), wintypes.LPCWSTR, c_void_p, c_void_p, c_void_p, _ULONG, POINTER(_DataBlob)]
_crypt32.CryptProtectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [c_void_p]

_bcrypt.BCryptOpenAlgorithmProvider.argtypes = [POINTER(c_void_p), wintypes.LPCWSTR, wintypes.LPCWSTR, _ULONG]
_bcrypt.BCryptOpenAlgorithmProvider.restype = _NTSTATUS
_bcrypt.BCryptCloseAlgorithmProvider.argtypes = [c_void_p, _ULONG]
_bcrypt.BCryptCloseAlgorithmProvider.restype = _NTSTATUS
_bcrypt.BCryptSetProperty.argtypes = [c_void_p, wintypes.LPCWSTR, c_char_p, _ULONG, _ULONG]
_bcrypt.BCryptSetProperty.restype = _NTSTATUS
_bcrypt.BCryptGetProperty.argtypes = [c_void_p, wintypes.LPCWSTR, c_char_p, _ULONG, POINTER(_ULONG), _ULONG]
_bcrypt.BCryptGetProperty.restype = _NTSTATUS
_bcrypt.BCryptGenerateSymmetricKey.argtypes = [c_void_p, POINTER(c_void_p), c_char_p, _ULONG, c_char_p, _ULONG, _ULONG]
_bcrypt.BCryptGenerateSymmetricKey.restype = _NTSTATUS
_bcrypt.BCryptDestroyKey.argtypes = [c_void_p]
_bcrypt.BCryptDestroyKey.restype = _NTSTATUS
_bcrypt.BCryptDecrypt.argtypes = [
    c_void_p, c_char_p, _ULONG, c_void_p, c_char_p, _ULONG, c_char_p, _ULONG, POINTER(_ULONG), _ULONG]
_bcrypt.BCryptDecrypt.restype = _NTSTATUS


def _check(status: int) -> None:
    if status != 0:
        raise OSError(f"CNG call failed: 0x{status & 0xFFFFFFFF:08X}")


def _dpapi_unprotect(blob: bytes) -> bytes:
    src = _DataBlob(len(blob), cast(c_char_p(blob), POINTER(c_ubyte)))
    out = _DataBlob()
    if not _crypt32.CryptUnprotectData(byref(src), None, None, None, None, 0, byref(out)):
        raise OSError("DPAPI CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def _dpapi_protect(data: bytes) -> bytes:
    src = _DataBlob(len(data), cast(c_char_p(data), POINTER(c_ubyte)))
    out = _DataBlob()
    if not _crypt32.CryptProtectData(byref(src), None, None, None, None, 0, byref(out)):
        raise OSError("DPAPI CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


class _AesGcm:
    """A reusable AES-256-GCM key — decrypt many cookie blobs with one key."""

    def __init__(self, key: bytes):
        self._alg = c_void_p()
        self._key = c_void_p()
        self._key_obj: bytes | None = None
        _check(_bcrypt.BCryptOpenAlgorithmProvider(byref(self._alg), "AES", None, 0))
        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        _check(_bcrypt.BCryptSetProperty(self._alg, "ChainingMode", mode, len(mode), 0))
        obj_len = _ULONG(0)
        got = _ULONG(0)
        _check(_bcrypt.BCryptGetProperty(self._alg, "ObjectLength", cast(byref(obj_len), c_char_p), 4, byref(got), 0))
        self._key_obj = create_string_buffer(obj_len.value)
        _check(_bcrypt.BCryptGenerateSymmetricKey(
            self._alg, byref(self._key), self._key_obj, obj_len.value, key, len(key), 0))

    def decrypt(self, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        info = _AuthInfo()
        info.cbSize = sizeof(info)
        info.dwInfoVersion = 1
        nonce_buf = create_string_buffer(nonce, len(nonce))
        tag_buf = create_string_buffer(tag, len(tag))
        info.pbNonce = cast(nonce_buf, POINTER(c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = cast(tag_buf, POINTER(c_ubyte))
        info.cbTag = len(tag)
        out = create_string_buffer(len(ciphertext))
        written = _ULONG(0)
        _check(_bcrypt.BCryptDecrypt(
            self._key, ciphertext, len(ciphertext), byref(info), None, 0, out, len(ciphertext), byref(written), 0))
        return out.raw[:written.value]

    def close(self) -> None:
        if self._key:
            _bcrypt.BCryptDestroyKey(self._key)
            self._key = c_void_p()
        if self._alg:
            _bcrypt.BCryptCloseAlgorithmProvider(self._alg, 0)
            self._alg = c_void_p()

    def __enter__(self) -> "_AesGcm":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# --- Cookie store reading -------------------------------------------------- #
def _master_key(profile_dir: Path) -> bytes:
    state = json.loads((profile_dir / "Local State").read_text(encoding="utf-8"))
    enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if enc[:5] == b"DPAPI":
        enc = enc[5:]
    return _dpapi_unprotect(enc)


def _decrypt_value(blob: bytes, host_key: str, gcm: _AesGcm) -> str:
    if blob[:3] in (b"v10", b"v11"):
        nonce, ciphertext, tag = blob[3:15], blob[15:-16], blob[-16:]
        raw = gcm.decrypt(nonce, ciphertext, tag)
        # Recent Chromium binds the value to its domain with a SHA256(host_key)
        # prefix. Verify before stripping so we never silently mangle a value.
        if raw[:32] == hashlib.sha256(host_key.encode()).digest():
            raw = raw[32:]
        return raw.decode("utf-8", "replace")
    # Legacy (pre-v10) values are raw DPAPI blobs.
    try:
        return _dpapi_unprotect(blob).decode("utf-8", "replace")
    except OSError:
        return blob.decode("utf-8", "replace")


def _read_claude_cookies(profile_dir: Path) -> dict[str, str]:
    """Decrypt every claude.ai cookie for a profile. Opened read-only/immutable;
    raises sqlite3.OperationalError when a running instance holds the exclusive
    lock — _read_session catches that and falls back to the cached session."""
    db = profile_dir / "Network" / "Cookies"
    if not db.is_file():
        return {}
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(
            "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key IN ('claude.ai', '.claude.ai')"
        ).fetchall()
    finally:
        con.close()
    jar: dict[str, str] = {}
    with _AesGcm(_master_key(profile_dir)) as gcm:
        for host_key, name, blob in rows:
            if not blob:
                continue
            try:
                jar[name] = _decrypt_value(bytes(blob), host_key, gcm)
            except OSError:
                pass
    return jar


# --- Session resolution + cache -------------------------------------------- #
# A running desktop instance holds an *exclusive* lock on its own cookie store,
# so the profile in active use is exactly the one we cannot read. To make the
# tracker work whether or not the instance is running, we cache the sessionKey
# (DPAPI-encrypted, user-scoped — same protection level the browser uses) every
# time the store is readable, and fall back to that cache while it is locked.
# The sessionKey is a bearer token, so a cached one keeps working live.
_CACHE_FILE = engine.RUNTIME_DIR / "usage_cache.json"


def _load_cache() -> dict:
    try:
        cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    engine._write_atomic(_CACHE_FILE, json.dumps(cache, indent=2).encode("utf-8"))


def _cookie_file_id(profile: str) -> list[int] | None:
    try:
        info = (Path(profile) / "Network" / "Cookies").stat()
    except OSError:
        return None
    return [info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", info.st_ctime_ns)]


def _cache_get_session(name: str) -> str | None:
    entry = _load_cache().get(name)
    cookie_file = _cookie_file_id(name)
    if not isinstance(entry, dict) or cookie_file is None or entry.get("cookie_file") != cookie_file:
        return None
    try:
        return _dpapi_unprotect(base64.b64decode(entry["session"])).decode("utf-8")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_put_session(name: str, session_key: str, cookie_file: list[int]) -> None:
    try:
        blob = base64.b64encode(_dpapi_protect(session_key.encode("utf-8"))).decode("ascii")
        with engine.file_lock(_CACHE_FILE.with_suffix(".lock")):
            cache = _load_cache()
            cache[name] = {"session": blob, "cookie_file": cookie_file}
            _write_cache(cache)
    except OSError:
        pass


def _cache_drop(name: str) -> None:
    try:
        with engine.file_lock(_CACHE_FILE.with_suffix(".lock")):
            cache = _load_cache()
            if name in cache:
                del cache[name]
                _write_cache(cache)
    except OSError:
        pass


def _read_session(profile_dir: Path) -> tuple[str, str | None, str | None]:
    """Resolve the auth for a profile. Returns ``(status, sessionKey, cf_clearance)``:
      * ``"live"``   — read straight from the profile's own cookie store
      * ``"locked"`` — store present but locked (the instance is running)
      * ``"unreadable"`` — invalid cookie or encryption data
      * ``"absent"`` — no store / not signed in
    """
    if not (profile_dir / "Network" / "Cookies").is_file() or not (profile_dir / "Local State").is_file():
        return "absent", None, None
    try:
        jar = _read_claude_cookies(profile_dir)
    except (sqlite3.Error, OSError):
        return "locked", None, None
    except (ValueError, KeyError, TypeError):
        return "unreadable", None, None
    session = jar.get("sessionKey")
    if not session:
        return "absent", None, None
    return "live", session, jar.get("cf_clearance")


# --- Claude API ------------------------------------------------------------ #
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
_ORGS_URL = "https://claude.ai/api/organizations"
_USAGE_URL = "https://claude.ai/api/organizations/{org}/usage"


def _api_get(url: str, cookie_header: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "*/*",
        "Referer": "https://claude.ai/",
    })
    req.add_unredirected_header("Cookie", cookie_header)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _pick_org(orgs: list[dict]) -> dict:
    """Mirror the web client: prefer a Teams org, else the first chat org."""
    chat = [o for o in orgs if "chat" in (o.get("capabilities") or [])]
    pool = chat or orgs
    return next((o for o in pool if o.get("raven_type") == "team"), pool[0])


def _account_label(org_name: str) -> str:
    name = org_name.strip() if isinstance(org_name, str) else ""
    for suffix in ("'s Organization", "’s Organization"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name or "—"


def _percent(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return percent if isfinite(percent) else None


@dataclass
class ProfileUsage:
    """Usage snapshot for one profile. ``ok`` False means ``error`` explains why.
    ``rows`` is the ordered list of limits to display — rolling windows first
    (labelled by length: '5h', '7d', or Codex Free's '30d'), then per-model
    weekly breakdowns (Claude's 'Fable', etc.). Each row carries its OWN reset
    time. Only what the account actually reports is listed."""
    name: str
    ok: bool = False
    error: str = ""
    account: str = ""
    rows: list[tuple[str, float, str]] = field(default_factory=list)  # (label, pct, reset_iso)
    quota_key: str = field(default="", repr=False, compare=False)
    temporary_error: bool = False
    checked_at: float = field(default=0.0, repr=False, compare=False)


_QUOTA_CACHE_TTL = 7 * 24 * 3600


def _quota_key(app: str, identity: str | tuple[str, str]) -> str:
    return hashlib.sha256(json.dumps((app, identity)).encode("utf-8")).hexdigest()


def _codex_quota_key(saved: object) -> str:
    tokens = saved.get("tokens") if isinstance(saved, dict) else None
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    return _quota_key("codex", engine._codex_identity(saved) or access) if isinstance(access, str) and access else ""


def _quota_entry_is_current(entry: object, now: float) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("at"), (int, float))
            and now - _QUOTA_CACHE_TTL < entry["at"] <= now)


def unexpired_snapshot(result: ProfileUsage, now: float) -> ProfileUsage | None:
    """Keep verified windows that have yet to reset."""
    age = now - result.checked_at
    if not 0 <= age < _QUOTA_CACHE_TTL:
        return None
    rows = []
    for row in result.rows:
        window, _, reset = row
        seconds = _reset_seconds(reset, now=now)
        max_age = 5 * 3600 if window == "5h" else _QUOTA_CACHE_TTL
        unexpired = age < max_age if seconds is None else seconds > 0
        if unexpired:
            rows.append(row)
    return replace(result, rows=rows) if rows else None


def load_quotas(app: str, targets: dict[str | None, tuple[str, Path]]) -> dict[str | None, ProfileUsage]:
    saved = _load_cache().get("_quotas")
    if not isinstance(saved, dict) or not saved:
        return {}
    now = time.time()
    results = {}
    for name, (label, path) in targets.items():
        try:
            if app == "codex":
                key = _codex_quota_key(json.loads((path / ".codex" / "auth.json").read_text("utf-8")))
            else:
                path = engine.claude_profile_userdata(path)
                status, session, _ = _read_session(path)
                if status == "locked":
                    session = _cache_get_session(str(path.resolve()))
                key = _quota_key("claude", session) if session else ""
        except (OSError, ValueError):
            continue
        if not key:
            continue
        entry = saved.get(key)
        if not _quota_entry_is_current(entry, now) or not isinstance(entry.get("rows"), list):
            continue
        rows = []
        for row in entry["rows"]:
            if not isinstance(row, list) or len(row) != 3:
                continue
            window, used, reset = row
            pct = _percent(used)
            if not isinstance(window, str) or not isinstance(reset, str) or pct is None:
                continue
            rows.append((window, pct, reset))
        restored = ProfileUsage(label, ok=True, rows=rows, quota_key=key, checked_at=entry["at"])
        if (current := unexpired_snapshot(restored, now)) is not None:
            results[name] = current
    return results


def _save_quota(result: ProfileUsage, started: float) -> None:
    if not result.quota_key or result.temporary_error:
        return
    try:
        with engine.file_lock(_CACHE_FILE.with_suffix(".lock")):
            cache = _load_cache()
            saved = cache.get("_quotas")
            now = time.time()
            quotas = ({key: entry for key, entry in saved.items() if _quota_entry_is_current(entry, now)}
                      if isinstance(saved, dict) else {})
            previous = quotas.get(result.quota_key)
            if previous is not None and previous["at"] > started:
                return
            quotas[result.quota_key] = {"at": started, "rows": result.rows if result.ok else []}
            cache["_quotas"] = quotas
            _write_cache(cache)
    except OSError:
        pass


def _usage_request_error(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return {
            401: "session expired or invalid — sign in again",
            403: "access denied",
            429: "too many requests — try again later",
        }.get(error.code, f"usage request failed (HTTP {error.code})")
    if isinstance(error, (OSError, HTTPException)):
        return "network unavailable"
    return "unreadable usage response"


def _set_usage_error(result: ProfileUsage, error: Exception) -> None:
    result.error = _usage_request_error(error)
    result.temporary_error = not isinstance(error, urllib.error.HTTPError) or error.code not in (401, 403)
    if isinstance(error, urllib.error.HTTPError) and error.fp is not None:
        error.close()


def _window_label(seconds) -> str:
    """'5h' / '7d' / '30d' from a window length in seconds."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h"


def _fetch_claude(name: str, profile_dir: Path) -> ProfileUsage:
    res = ProfileUsage(name=name)
    profile_dir = engine.claude_profile_userdata(profile_dir)
    cache_key = str(profile_dir.resolve())
    cookie_file = _cookie_file_id(cache_key)

    status, session, cf = _read_session(profile_dir)
    if status == "live":
        if cookie_file is not None:
            _cache_put_session(cache_key, session, cookie_file)
    else:
        session = _cache_get_session(cache_key) if status == "locked" else None
        cf = None
        if not session:
            res.error = {
                "locked": "login data locked — close the instance and reload",
                "unreadable": "login data unreadable",
            }.get(status, "not signed in")
            return res

    res.quota_key = _quota_key("claude", session)
    cookie_header = f"sessionKey={session}" + (f"; cf_clearance={cf}" if cf else "")
    try:
        orgs = _api_get(_ORGS_URL, cookie_header)
        if not orgs:
            res.error = "no organizations found"
            return res
        org = _pick_org(orgs)
        res.account = _account_label(org.get("name", ""))
        org_id = org.get("uuid") or org.get("id")
        data = _api_get(_USAGE_URL.format(org=org_id), cookie_header)
        for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
            node = data.get(key)
            if not isinstance(node, dict):
                continue
            pct = _percent(node.get("utilization"))
            if pct is not None:
                reset = node.get("resets_at")
                res.rows.append((label, pct, reset if isinstance(reset, str) else ""))
        for lim in data.get("limits") or []:  # per-model weekly (Fable, Opus…)
            if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
                continue
            scope = lim.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            label = model.get("display_name") if isinstance(model, dict) else None
            pct = _percent(lim.get("percent"))
            if isinstance(label, str) and label and pct is not None:
                reset = lim.get("resets_at")
                res.rows.append((label, pct, reset if isinstance(reset, str) else ""))
        res.ok = True
    except (HTTPException, OSError, ValueError, KeyError) as exc:
        _set_usage_error(res, exc)
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
            _cache_drop(cache_key)  # token rotated — drop it so the next read re-captures
    return res


# --- Codex API ------------------------------------------------------------- #
# Codex signs in with ChatGPT (OAuth). Each profile keeps its own tokens in
# <profile>/.codex/auth.json (CODEX_HOME). The backend exposes a clean usage
# endpoint — no quota spent, per-account — with a primary and (on paid plans) a
# secondary rolling window, each as used-percent. Window lengths vary by plan
# (Plus: 5h + 7d; Free: a single 30-day window), so we label them by duration.
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"


def _codex_get(url: str, access_token: str, account_id: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "*/*",
    })
    req.add_unredirected_header("Authorization", f"Bearer {access_token}")
    req.add_unredirected_header("Chatgpt-account-id", account_id or "")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _epoch_to_iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _fetch_codex(name: str, profile_dir: Path) -> ProfileUsage:
    res = ProfileUsage(name=name)
    auth = profile_dir / ".codex" / "auth.json"
    try:
        saved = json.loads(auth.read_text("utf-8"))
    except FileNotFoundError:
        res.error = "not signed in"
        return res
    except (OSError, ValueError):
        res.error = "login data unreadable"
        return res
    tokens = saved.get("tokens", {}) if isinstance(saved, dict) else None
    if not isinstance(tokens, dict):
        res.error = "login data unreadable"
        return res
    access, account_id = tokens.get("access_token"), tokens.get("account_id")
    if not access:
        res.error = "not signed in"
        return res
    res.quota_key = _codex_quota_key(saved)
    try:
        data = _codex_get(_CODEX_USAGE_URL, access, account_id)
        email = data.get("email")
        res.account = email if isinstance(email, str) and email else "—"
        rl = data.get("rate_limit") or {}
        for win in (rl.get("primary_window"), rl.get("secondary_window")):
            if not isinstance(win, dict):
                continue
            pct = _percent(win.get("used_percent"))
            if pct is not None:
                label = _window_label(win.get("limit_window_seconds"))
                res.rows.append((label, pct, _epoch_to_iso(win.get("reset_at"))))
        res.ok = True
    except (HTTPException, OSError, ValueError, KeyError) as exc:
        _set_usage_error(res, exc)
    return res


def claude_oauth_saved(config: Path) -> bool:
    try:
        cfg = json.loads(config.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(cfg, dict) and bool(cfg.get("oauth:tokenCacheV2") or cfg.get("oauth:tokenCache"))


def session_snapshot(profile_dir: Path) -> dict:
    """Diagnostic for the sign-in flow: which auth material is present and
    readable in a profile. ``{"sessionKey": bool, "oauth": bool}``. The web UI
    needs the ``sessionKey`` cookie; the OAuth token in ``config.json`` is
    written synchronously and survives on its own. The cookie store must be
    unlocked (no running instance) for ``sessionKey`` to read true."""
    has_sk = False
    try:
        has_sk = bool(_read_claude_cookies(profile_dir).get("sessionKey"))
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        pass
    return {"sessionKey": has_sk, "oauth": claude_oauth_saved(profile_dir / "config.json")}


def fetch_as_ready(targets: list[tuple[str, Path]], app: str) -> Iterator[tuple[int, ProfileUsage]]:
    """Fetch accounts in parallel and yield each result with its target index."""
    if not targets:
        return
    fetch = _fetch_codex if app == "codex" else _fetch_claude

    def fetch_target(target: tuple[str, Path]) -> ProfileUsage:
        started = time.time()
        try:
            result = fetch(*target)
        except (HTTPException, OSError, sqlite3.Error, ValueError, KeyError, TypeError,
                AttributeError, IndexError, OverflowError):
            return ProfileUsage(name=target[0], error="unreadable account data")
        if result.ok:
            result.checked_at = started
        _save_quota(result, started)
        return result

    pending = SimpleQueue()
    completed = SimpleQueue()
    for job in enumerate(targets):
        pending.put(job)

    def worker() -> None:
        while True:
            try:
                index, target = pending.get_nowait()
            except Empty:
                return
            try:
                result = fetch_target(target)
            except BaseException as error:
                completed.put((index, error))
                return
            completed.put((index, result))

    # Network requests in the background must not hold the manager open.
    for _ in range(min(8, len(targets))):
        Thread(target=worker, daemon=True).start()
    for _ in targets:
        index, result = completed.get()
        if isinstance(result, BaseException):
            raise result
        yield index, result


def fetch_all(targets: list[tuple[str, Path]], app: str = "claude") -> list[ProfileUsage]:
    """Fetch usage for each (display name, directory) in target order."""
    return [result for _, result in sorted(fetch_as_ready(targets, app))]


# --- Formatting helpers (consumed by the UI layer) ------------------------- #
def format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def _reset_seconds(iso: str, *, now: float | None = None) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = datetime.now(when.tzinfo) if now is None else datetime.fromtimestamp(now, when.tzinfo)
    return (when - current).total_seconds()


def humanize_reset(iso: str) -> str:
    seconds = _reset_seconds(iso)
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"in {max(1, int(seconds))}s"
    hours, minutes = divmod(int(seconds) // 60, 60)
    days, hours = divmod(hours, 24)
    units = [f"{value}{unit}" for value, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if value]
    return "in " + " ".join(units)
