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
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from ctypes import POINTER, byref, c_char_p, c_ubyte, c_void_p, cast, create_string_buffer, sizeof
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import engine  # single source of truth for PROJECT_DIR (no import cycle)

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
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(
            "SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%claude.ai%'"
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
_CACHE_FILE = engine.PROJECT_DIR / "usage_cache.json"
_CACHE_LOCK = threading.Lock()


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    """Atomic write (temp + os.replace) so a concurrent reader never sees a
    half-written file — fetch_all touches the cache from several threads."""
    tmp = _CACHE_FILE.with_name(_CACHE_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        os.replace(tmp, _CACHE_FILE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _cache_get_session(name: str) -> str | None:
    entry = _load_cache().get(name)
    if not isinstance(entry, dict):
        return None
    try:
        return _dpapi_unprotect(base64.b64decode(entry["session"])).decode("utf-8")
    except (OSError, ValueError, KeyError):
        return None


def _cache_put_session(name: str, session_key: str) -> None:
    try:
        blob = base64.b64encode(_dpapi_protect(session_key.encode("utf-8"))).decode("ascii")
    except OSError:
        return
    with _CACHE_LOCK:
        cache = _load_cache()
        cache[name] = {"session": blob}
        _write_cache(cache)


def _cache_drop(name: str) -> None:
    with _CACHE_LOCK:
        cache = _load_cache()
        if cache.pop(name, None) is not None:
            _write_cache(cache)


def _read_session(profile_dir: Path) -> tuple[str, str | None, str | None]:
    """Resolve the auth for a profile. Returns ``(status, sessionKey, cf_clearance)``:
      * ``"live"``   — read straight from the profile's own cookie store
      * ``"locked"`` — store present but locked (the instance is running)
      * ``"absent"`` — no store / not signed in
    """
    if not (profile_dir / "Network" / "Cookies").is_file() or not (profile_dir / "Local State").is_file():
        return "absent", None, None
    try:
        jar = _read_claude_cookies(profile_dir)
    except (sqlite3.Error, OSError):
        return "locked", None, None
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
        "Cookie": cookie_header,
        "Accept": "*/*",
        "Referer": "https://claude.ai/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _pick_org(orgs: list[dict]) -> dict:
    """Mirror the web client: prefer a Teams org, else the first chat org."""
    chat = [o for o in orgs if "chat" in (o.get("capabilities") or [])]
    pool = chat or orgs
    return next((o for o in pool if o.get("raven_type") == "team"), pool[0])


def _account_label(org_name: str) -> str:
    name = (org_name or "").strip()
    for suffix in ("'s Organization", "’s Organization"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name or "—"


def _utilization(node) -> float | None:
    return node.get("utilization") if isinstance(node, dict) else None


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


def _window_label(seconds) -> str:
    """'5h' / '7d' / '30d' from a window length in seconds."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h"


def _fetch_claude(name: str, profile_dir: Path) -> ProfileUsage:
    res = ProfileUsage(name=name)

    status, session, cf = _read_session(profile_dir)
    if status == "live":
        _cache_put_session(name, session)  # keep the cache warm for when it locks
    else:
        session = _cache_get_session(name)  # fall back while the instance holds the lock
        cf = None
        if not session:
            res.error = "app open — close it once" if status == "locked" else "not signed in"
            return res

    cookie_header = f"sessionKey={session}" + (f"; cf_clearance={cf}" if cf else "")
    try:
        org = _pick_org(_api_get(_ORGS_URL, cookie_header))
        res.account = _account_label(org.get("name", ""))
        org_id = org.get("uuid") or org.get("id")
        data = _api_get(_USAGE_URL.format(org=org_id), cookie_header)
        for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
            node = data.get(key) or {}
            pct = _utilization(node)
            if pct is not None:
                res.rows.append((label, pct, node.get("resets_at") or ""))
        for lim in data.get("limits") or []:  # per-model weekly (Fable, Opus…)
            if lim.get("kind") != "weekly_scoped":
                continue
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            pct = lim.get("percent")
            if model and pct is not None:
                res.rows.append((model, float(pct), lim.get("resets_at") or ""))
        res.ok = True
    except urllib.error.HTTPError as exc:
        res.error = {401: "session expired", 403: "blocked (Cloudflare)",
                     429: "rate-limited"}.get(exc.code, f"HTTP {exc.code}")
        if exc.code == 401:
            _cache_drop(name)  # token rotated — drop it so the next read re-captures
    except urllib.error.URLError:
        res.error = "network unavailable"
    except (OSError, ValueError, KeyError) as exc:
        res.error = exc.__class__.__name__
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
        "Authorization": f"Bearer {access_token}",
        "User-Agent": _UA,
        "Accept": "*/*",
        "chatgpt-account-id": account_id or "",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _epoch_to_iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _fetch_codex(name: str, profile_dir: Path) -> ProfileUsage:
    res = ProfileUsage(name=name)
    auth = profile_dir / ".codex" / "auth.json"
    if not auth.is_file():
        res.error = "not signed in"
        return res
    try:
        tokens = (json.loads(auth.read_text("utf-8")) or {}).get("tokens") or {}
    except (OSError, ValueError):
        res.error = "not signed in"
        return res
    access, account_id = tokens.get("access_token"), tokens.get("account_id")
    if not access:
        res.error = "not signed in"
        return res
    try:
        data = _codex_get(_CODEX_USAGE_URL, access, account_id)
        res.account = data.get("email") or "—"
        rl = data.get("rate_limit") or {}
        for win in (rl.get("primary_window"), rl.get("secondary_window")):
            if not win or win.get("used_percent") is None:
                continue
            label = _window_label(win.get("limit_window_seconds"))
            res.rows.append((label, win["used_percent"], _epoch_to_iso(win.get("reset_at"))))
        res.ok = True
    except urllib.error.HTTPError as exc:
        res.error = {401: "expired — open Codex once", 403: "forbidden",
                     429: "rate-limited"}.get(exc.code, f"HTTP {exc.code}")
    except urllib.error.URLError:
        res.error = "network unavailable"
    except (OSError, ValueError, KeyError) as exc:
        res.error = exc.__class__.__name__
    return res


def session_snapshot(profile_dir: Path) -> dict:
    """Diagnostic for the sign-in flow: which auth material is present and
    readable in a profile. ``{"sessionKey": bool, "oauth": bool}``. The web UI
    needs the ``sessionKey`` cookie; the OAuth token in ``config.json`` is
    written synchronously and survives on its own. The cookie store must be
    unlocked (no running instance) for ``sessionKey`` to read true."""
    has_sk = False
    try:
        has_sk = bool(_read_claude_cookies(profile_dir).get("sessionKey"))
    except (sqlite3.Error, OSError):
        pass
    has_oauth = False
    try:
        cfg = json.loads((profile_dir / "config.json").read_text("utf-8"))
        has_oauth = bool(cfg.get("oauth:tokenCacheV2") or cfg.get("oauth:tokenCache"))
    except (OSError, ValueError):
        pass
    return {"sessionKey": has_sk, "oauth": has_oauth}


def fetch_all(targets: list[tuple[str, Path]], app: str = "claude") -> list[ProfileUsage]:
    """Fetch usage for every ``(display_name, dir)`` target in parallel
    (network-bound), order preserved. ``app`` selects the backend: ``"claude"``
    (cookie + claude.ai) or ``"codex"`` (ChatGPT OAuth token + codex/usage). The
    explicit name lets the main instance show up under a friendly label."""
    if not targets:
        return []
    fetch = _fetch_codex if app == "codex" else _fetch_claude
    workers = min(8, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: fetch(t[0], t[1]), targets))


# --- Formatting helpers (consumed by the UI layer) ------------------------- #
def format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def humanize_reset(iso: str) -> str:
    if not iso:
        return "—"
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return "—"
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    seconds = int((when - now).total_seconds())
    if seconds <= 0:
        return "now"
    days, rem = divmod(seconds, 86_400)
    hours = rem // 3_600
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h"
    return "in <1h"
