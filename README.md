# Claude / Codex Multi-Instance

[![Download](https://img.shields.io/badge/Download-Windows%20ZIP-28A745?style=for-the-badge&logo=windows11&logoColor=white)](https://github.com/Zoltak-Dev/ai-multi-instance/releases/latest/download/ai-multi-instance-v2.0.1-windows.zip)
[![Support on Ko-fi](https://img.shields.io/badge/Support%20me-Ko--fi-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white)](https://ko-fi.com/zoltrak)

Run multiple accounts of the [Claude desktop app](https://claude.ai/download) **and** the [Codex desktop app](https://chatgpt.com/codex) on Windows. The official clients only support one account at a time. This tool launches each app with a dedicated profile directory per account, allowing several accounts to run side by side and be switched instantly.

![Claude / Codex Multi-Instance preview](https://github.com/user-attachments/assets/3c8f5415-9795-490f-a177-153f42b21f44)

The interface is arrow-key driven: move the highlight with `↑`/`↓`, then press a digit for an action (`1` launch, `2` sign-in, `3` close, `4` rename, `5` shortcut, `6` delete, `7` new, `8` switch app, `9` usage, `0` quit). Because rows are picked with the arrows and never by number, a digit is always an action — no ambiguity. Press `Space` to tick several profiles and act on them at once. The list's first row is always the **main instance** (your normal Claude, launched without a profile) so you can launch or close it from here too; the running/idle state refreshes on its own. The accent colour follows the app — Claude's orange, Codex's blue.

## Install

Windows 10+ and the Claude and/or Codex desktop app installed.

**Compatibility checked on 30 October 2026:** Claude Desktop `3.1623.1.0`, Codex `26.1027.3384.0`, Windows 11 build `26200`. Two temporary profiles of each app were launched side by side. Snapshot adoption, cancellation and recovery are covered by automated tests using temporary directories; a complete browser sign-in still needs to be checked with your account.

**From source (recommended)** — requires Python 3.9+:

```powershell
git clone https://github.com/Zoltak-Dev/ai-multi-instance.git
cd ai-multi-instance
python main.py
```

No `pip install` — the project uses only the Python standard library.

**Pre-built binary (no Python required)** — download `ai-multi-instance-vX.Y.Z-windows.zip` from [Releases](https://github.com/Zoltak-Dev/ai-multi-instance/releases), unzip both `.exe` files in the same folder, and run `ai-multi-instance.exe`.

The binaries are unsigned. Windows may show a SmartScreen warning, and [Smart App Control](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/overview) may block unknown unsigned apps. If Windows blocks the download, use the source install above.

**Updating:** replace the two executables together, or run `git pull --ff-only` for a source install. Keep your `ClaudeProfiles`, `CodexProfiles`, `state.json` and `usage_cache.json` files. Do not run two copies of the manager's sign-in flow at once.

## Usage

Move the highlight with `↑`/`↓` and press a digit for the action — rows are never typed by number. `Space` ticks a profile so an action (launch, close, shortcut, delete) can run on several at once. The top row, **main instance**, is your normal single-account Claude; only launch/close apply to it.

Creating a profile (`7`) also creates a desktop shortcut and offers to sign the profile in right away (see below). Double-click the shortcut to launch that profile directly without opening the menu.

Profiles live in their own folders, kept fully separate per app:

- `ClaudeProfiles/<name>/` for Claude
- `CodexProfiles/<name>/` for Codex

Deleting the folder is the same as deleting the profile.

## How multi-instance works

Claude and Codex use different mechanisms to enforce their single-instance behavior.

Claude profiles use Chromium's `--user-data-dir` plus a per-profile `LOCALAPPDATA` under `<profile>/.localappdata`. Some Claude versions redirect their data to `%LOCALAPPDATA%/Claude-Data/<family>` before checking the single-instance lock; isolating both locations prevents them from opening the main account instead. Child tools inherit that `LOCALAPPDATA`, so their local caches are also separate.

Codex profiles use `--user-data-dir` and `CODEX_ELECTRON_USER_DATA_PATH` for desktop data, plus `CODEX_HOME` for authentication. `CODEX_CLI_PATH` points to the installed app's bundled CLI when present, avoiding package-identity errors after updates. The launcher rediscovers the executable when an app update removes its old installation path.

## Signing in to a profile

Windows normally delivers the `claude://` sign-in callback to the default Claude instance. The tool uses a temporary snapshot of that instance so the browser callback can complete normally, then moves the signed-in state into your chosen profile.

(A subtlety: the default instance's data has lived in three different places across versions, so this tool **probes** for it rather than assuming an address — see [Known limitations](#known-limitations). Recent Claude versions keep it in `%LOCALAPPDATA%\Claude-Data\<family>`; older Claude versions kept it inside the MSIX container at `%LOCALAPPDATA%\Packages\<family>\LocalCache\Roaming\Claude`; an unpackaged install uses the plain `%APPDATA%\Claude`. Whichever one actually holds a profile is what gets moved around below.)

The tool works around this with a snapshot flow (press `2`, Claude only — also offered right after creating a profile):

1. Your main Claude data is stashed aside, and a fresh default Claude opens.
2. Sign in there with the account you want. Email and Google both work, since this is the real default app receiving the callback.
3. That's it — **no key to press**. The tool detects the sign-in on its own, captures the session, and puts your main data back. Press `q` at any point to cancel and restore.

How the detection works: the app is "logged in" when a `sessionKey` cookie is present (the OAuth token in `config.json` is derived from it). The cookie store is locked while the app runs, so the tool can't read it live — instead it watches the cookie database and its SQLite journal/WAL alongside `config.json` (a plain file, readable live). A cookie write immediately before or after OAuth is accepted, while unrelated writes from app startup are ignored. If you close the tool mid sign-in, it detects the interrupted state on next launch and offers to restore or drop the set-aside session.

Windows DPAPI encryption is scoped to the Windows user, not to the folder, so the moved session stays valid. Nothing is patched or hijacked.

## Usage tracker

Press `9` (works for both Claude and Codex). Each account — the main instance included — gets a block listing every limit it has, with its **own** reset time:

```
  ◆ Usage   Claude · 2 accounts

  Main instance   you@example.com
     5h      78%   resets in 2h
     7d      13%   resets in 6d 17h
     Fable    5%   resets in 6d 17h

  profilename2   other@example.com
     5h       0%
     7d       0%
```

The rows adapt to the account: whatever rolling windows it exposes (`5h`, `7d`, or a Free plan's `30d`) followed by any per-model weekly breakdowns (Claude's `Fable`, etc.). Nothing placeholder is shown — a row appears only if the account actually reports it.

- **Claude** reads each profile's claude.ai session straight from its own cookie store and queries the official `claude.ai/api/.../usage` endpoint (DPAPI + AES-GCM via Windows CNG through `ctypes`). A running instance holds an exclusive lock on its cookie store, so the session token is cached (DPAPI-encrypted, in `usage_cache.json`) whenever readable and reused while locked — launch a profile once and its usage stays visible whether the app is open or closed.
- **Codex** reads each profile's ChatGPT token from `<profile>/.codex/auth.json` and queries the `codex/usage` endpoint — no quota spent.

Both are per account, need no extra logins, and use only the Python standard library.

## Known limitations

**Use v2.0.1 or newer with recent Claude versions.** Earlier releases can mistake a relocated profile's empty old directory for its real data. Current code checks for profile contents, ignores `.user-data-left` markers, and stops sign-in if the real data cannot be found. Run `python diagnose_login.py --duration 0` to inspect the paths without changing anything.

**Cowork virtual machines:** chat and account isolation are supported, but Cowork's machine-wide VM service can resolve bundle paths outside a named profile. Concurrent VMs and a fix for that service are not verified; see [issue #5](https://github.com/Zoltak-Dev/ai-multi-instance/issues/5). This release does not claim to fix Cowork VM startup.

**The sign-in snapshot moves your real profile directory.** It is a rename, not a copy, and it is the mechanism the tool is built on. A stash is never deleted until it has been renamed back into place, and an interrupted run leaves it on disk for recovery — but close the app before running it, and do not interrupt it mid-flow.

**Profiles are isolation, not a sandbox.** Windows DPAPI is scoped to the Windows user rather than to the folder, so any profile's stored secrets stay readable by anything running as you. Separate accounts, same trust boundary.

**Codex signs in inside each profile.** Launch the profile and follow its normal sign-in screen; the snapshot action is only needed for Claude.

**Only one instance owns the `claude://` protocol at a time.** That is a Windows constraint rather than a tool one, and it is why sign-in needs the snapshot dance at all.

## Checks

```powershell
python -m unittest discover -v
python diagnose_login.py --duration 0
```

The tests use temporary data and mocked process launches; they do not close or move your real sessions. GitHub Actions runs them on Windows with Python 3.9 and 3.14 and builds both executables.

## Build

Standalone `.exe` build via PyInstaller, for shipping to users who don't have Python:

```powershell
python -m pip install pyinstaller==6.20.0
python -m PyInstaller --onefile --console --name ai-multi-instance --clean --noconfirm main.py
python -m PyInstaller --onefile --noconsole --name launcher --clean --noconfirm launcher.pyw
```

The two binaries land in `dist/`. Ship them in the same folder — `ai-multi-instance.exe` looks for `launcher.exe` next to itself to manage desktop shortcuts for both Claude and Codex.

Pre-built binaries are attached to each [release](https://github.com/Zoltak-Dev/ai-multi-instance/releases).

## Star History

If this tool saved you some headache, a ⭐ on the repo is appreciated — it's the only feedback signal I get and it really helps the project gain visibility. If you'd like to support its continued development, you can also [buy me a coffee on Ko-fi](https://ko-fi.com/zoltrak). Every donation helps me keep improving and maintaining the project. ❤️

<a href="https://www.star-history.com/?repos=Zoltak-Dev%2Fai-multi-instance&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&theme=dark&legend=top-left&sealed_token=AjPPiTEoI91pJvO3tq8VAVB4p8-33d7TyehYw_w6qztZhQkLnrnldsW_nR3I4zzVNgkRMm2NFa-7y-V4Pp6cBeSbaD0APavs6bbwwwZvA1K903na1cRLVtNI4LAtk4K58SWyy4lL9aq3SdoKJ0EUphIMsXaoL1Ng4kT3GmcYSJMQQnXZphtTThJeT0ys" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&legend=top-left&sealed_token=AjPPiTEoI91pJvO3tq8VAVB4p8-33d7TyehYw_w6qztZhQkLnrnldsW_nR3I4zzVNgkRMm2NFa-7y-V4Pp6cBeSbaD0APavs6bbwwwZvA1K903na1cRLVtNI4LAtk4K58SWyy4lL9aq3SdoKJ0EUphIMsXaoL1Ng4kT3GmcYSJMQQnXZphtTThJeT0ys" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&legend=top-left&sealed_token=AjPPiTEoI91pJvO3tq8VAVB4p8-33d7TyehYw_w6qztZhQkLnrnldsW_nR3I4zzVNgkRMm2NFa-7y-V4Pp6cBeSbaD0APavs6bbwwwZvA1K903na1cRLVtNI4LAtk4K58SWyy4lL9aq3SdoKJ0EUphIMsXaoL1Ng4kT3GmcYSJMQQnXZphtTThJeT0ys" />
 </picture>
</a>

## License

MIT
