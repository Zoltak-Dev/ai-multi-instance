"""Set a per-user default handler for a URL protocol (e.g. ``claude://`` or ``codex://``).

Windows protects protocol/file defaults with a per-user "UserChoice" hash so
that only a real user choice is honored. This module computes that hash in pure
Python (the documented algorithm, same one used by SetUserFTA and Mozilla's
WindowsUserChoice), which lets us point a custom scheme at our own handler and
route OAuth login callbacks to the right profile.

Note: the UCPD driver only locks http/https/.pdf; custom protocols like
``claude://`` or ``codex://`` can still be set this way.
"""
import base64
import hashlib
import os
import subprocess
import winreg
from datetime import datetime

_HKCU = winreg.HKEY_CURRENT_USER
_EXPERIENCE_FALLBACK = ("User Choice set via Windows User Experience "
                        "{D18B6DD5-6124-4341-9318-804003BAFA0B}")
_NO_WINDOW = 0x08000000


def _user_sid() -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value"],
        capture_output=True, text=True, creationflags=_NO_WINDOW,
    ).stdout.strip()
    return out.lower()


def _user_experience() -> str:
    try:
        path = os.path.join(os.environ["SystemRoot"], "SysWOW64", "Shell32.dll")
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-16-le", errors="ignore")
        key = "User Choice set via Windows User Experience"
        start = text.index(key)
        end = text.index("}", start)
        return text[start:end + 1]
    except Exception:
        return _EXPERIENCE_FALLBACK


def _hex_datetime() -> str:
    now = datetime.now()
    dt = datetime(now.year, now.month, now.day, now.hour, now.minute, 0)
    epoch_as_filetime = 116444736000000000  # 1601-01-01 -> 1970-01-01, in 100ns
    filetime = int(dt.timestamp() * 10_000_000) + epoch_as_filetime
    hi = (filetime >> 32) & 0xFFFFFFFF
    lo = filetime & 0xFFFFFFFF
    return ("%08X%08X" % (hi, lo)).lower()


def _compute_hash(base_info: str) -> str:
    data = base_info.encode("utf-16-le") + b"\x00\x00"
    md5 = hashlib.md5(data).digest()
    base_len = len(base_info) * 2 + 2
    length = (1 if (base_len & 4) == 0 else 0) + (base_len >> 2) - 1
    if length <= 1:
        return ""

    M = 0xFFFFFFFF
    md5_0 = int.from_bytes(md5[0:4], "little")
    md5_1 = int.from_bytes(md5[4:8], "little")

    def gl(i):
        return int.from_bytes(data[i:i + 4], "little")

    def sr(v, c):
        return (v & M) >> c

    out = bytearray(16)

    # round 1
    md51 = ((md5_0 | 1) + 0x69FB0000) & M
    md52 = ((md5_1 | 1) + 0x13DB0000) & M
    counter = sr(length - 2, 1) + 1
    cache = outhash1 = pdata = outhash2 = 0
    while counter:
        r0 = (gl(pdata) + outhash1) & M
        r1 = gl(pdata + 4) & M
        pdata += 8
        r2 = ((r0 * md51) - (0x10FA9605 * sr(r0, 16))) & M
        r2 = ((0x79F8A395 * r2) + (0x689B6B9F * sr(r2, 16))) & M
        r3 = ((0xEA970001 * r2) - (0x3C101569 * sr(r2, 16))) & M
        r4 = (r3 + r1) & M
        r5 = (cache + r3) & M
        r6 = ((r4 * md52) - (0x3CE8EC25 * sr(r4, 16))) & M
        r6 = ((0x59C3AF2D * r6) - (0x2232E0F1 * sr(r6, 16))) & M
        outhash1 = ((0x1EC90001 * r6) + (0x35BD1EC9 * sr(r6, 16))) & M
        outhash2 = (r5 + outhash1) & M
        cache = outhash2
        counter -= 1
    out[0:4] = outhash1.to_bytes(4, "little")
    out[4:8] = outhash2.to_bytes(4, "little")

    # round 2
    md51 = (md5_0 | 1) & M
    md52 = (md5_1 | 1) & M
    counter = sr(length - 2, 1) + 1
    cache = outhash1 = pdata = outhash2 = 0
    while counter:
        r0 = (gl(pdata) + outhash1) & M
        pdata += 8
        r1 = (r0 * md51) & M
        r1 = ((0xB1110000 * r1) - (0x30674EEF * sr(r1, 16))) & M
        r2 = ((0x5B9F0000 * r1) - (0x78F7A461 * sr(r1, 16))) & M
        r2 = ((0x12CEB96D * sr(r2, 16)) - (0x46930000 * r2)) & M
        r3 = ((0x1D830000 * r2) + (0x257E1D83 * sr(r2, 16))) & M
        r4 = (md52 * ((r3 + gl(pdata - 4)) & M)) & M
        r4 = ((0x16F50000 * r4) - (0x5D8BE90B * sr(r4, 16))) & M
        r5 = ((0x96FF0000 * r4) - (0x2C7C6901 * sr(r4, 16))) & M
        r5 = ((0x2B890000 * r5) + (0x7C932B89 * sr(r5, 16))) & M
        outhash1 = ((0x9F690000 * r5) - (0x405B6097 * sr(r5, 16))) & M
        outhash2 = (outhash1 + cache + r3) & M
        cache = outhash2
        counter -= 1
    out[8:12] = outhash1.to_bytes(4, "little")
    out[12:16] = outhash2.to_bytes(4, "little")

    hv1 = (int.from_bytes(out[8:12], "little") ^ int.from_bytes(out[0:4], "little")) & M
    hv2 = (int.from_bytes(out[12:16], "little") ^ int.from_bytes(out[4:8], "little")) & M
    return base64.b64encode(hv1.to_bytes(4, "little") + hv2.to_bytes(4, "little")).decode()


def _proto_key(protocol: str) -> str:
    return r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\%s\UserChoice" % protocol


def register_progid(progid: str, command: str, friendly: str = "URL:Multi-Instance") -> None:
    with winreg.CreateKey(_HKCU, r"Software\Classes\%s" % progid) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, friendly)
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(_HKCU, r"Software\Classes\%s\shell\open\command" % progid) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)


def set_protocol_default(protocol: str, progid: str) -> str:
    """Make ``progid`` the default handler for ``protocol``. Returns the hash."""
    base_info = (protocol + _user_sid() + progid + _hex_datetime() + _user_experience()).lower()
    proghash = _compute_hash(base_info)
    key = _proto_key(protocol)
    try:
        winreg.DeleteKey(_HKCU, key)
    except OSError:
        pass
    with winreg.CreateKey(_HKCU, key) as k:
        winreg.SetValueEx(k, "ProgId", 0, winreg.REG_SZ, progid)
        winreg.SetValueEx(k, "Hash", 0, winreg.REG_SZ, proghash)
    return proghash


def clear_protocol_default(protocol: str) -> None:
    try:
        winreg.DeleteKey(_HKCU, _proto_key(protocol))
    except OSError:
        pass


def current_default(protocol: str) -> str | None:
    try:
        with winreg.OpenKey(_HKCU, _proto_key(protocol)) as k:
            return winreg.QueryValueEx(k, "ProgId")[0]
    except OSError:
        return None
