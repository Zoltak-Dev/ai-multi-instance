# Claude / Codex Multi-Instance

Run multiple accounts of the [Claude desktop app](https://claude.ai/download) **and** the [Codex desktop app](https://chatgpt.com/codex) on Windows. The official clients only support one account at a time. This tool launches each app with a dedicated profile directory per account, allowing several accounts to run side by side and be switched instantly.

![Claude / Codex Multi-Instance preview](https://github.com/user-attachments/assets/3c8f5415-9795-490f-a177-153f42b21f44)

The interface is arrow-key driven: move the highlight with `↑`/`↓`, then press a digit for an action (`1` launch, `2` sign-in, `3` close, `4` rename, `5` shortcut, `6` delete, `7` new, `8` switch app, `9` usage, `0` quit). Because rows are picked with the arrows and never by number, a digit is always an action — no ambiguity. Press `Space` to tick several profiles and act on them at once. The list's first row is always the **main instance** (your normal Claude, launched without a profile) so you can launch or close it from here too; the running/idle state refreshes on its own. The accent colour follows the app — Claude's orange, Codex's blue.

## Install

Windows 10+ and the Claude and/or Codex desktop app installed.

**From source (recommended)** — requires Python 3.9+:

```powershell
git clone https://github.com/Zoltak-Dev/ai-multi-instance.git
cd ai-multi-instance
python main.py
```

No `pip install` — the project uses only the Python standard library.

**Pre-built binary (no Python required)** — download `ai-multi-instance-vX.Y.Z-windows.zip` from [Releases](https://github.com/Zoltak-Dev/ai-multi-instance/releases), unzip both `.exe` files in the same folder, and run `ai-multi-instance.exe`.

📹 **Video walkthrough** — Disable Smart App Control:

https://github.com/user-attachments/assets/a46521fc-482f-4d55-9375-8c0cc38c79f0



> ⚠️ **Windows blocks unsigned binaries by default.** Windows refuses to run any `.exe` without a code-signing certificate (which costs 300-600 €/year — not viable for a free project). To run the `.exe`:
>
> 1. **Windows Security** → **App & browser control** → **Smart App Control settings** → switch from **On** to **Off**
> 2. Launch `ai-multi-instance.exe`. Windows will show *"Windows protected your PC"* → click **More info** → **Run anyway**
>
> If you'd rather skip all this, use the source install above — no warning, no setup.

## Usage

Move the highlight with `↑`/`↓` and press a digit for the action — rows are never typed by number. `Space` ticks a profile so an action (launch, close, shortcut, delete) can run on several at once. The top row, **main instance**, is your normal single-account Claude; only launch/close apply to it.

Creating a profile (`7`) also creates a desktop shortcut and offers to sign the profile in right away (see below). Double-click the shortcut to launch that profile directly without opening the menu.

Profiles live in their own folders, kept fully separate per app:

- `ClaudeProfiles/<name>/` for Claude
- `CodexProfiles/<name>/` for Codex

Deleting the folder is the same as deleting the profile.

## How multi-instance works

Claude and Codex use different mechanisms to enforce their single-instance behavior.

The Claude desktop app respects Chromium's standard `--user-data-dir` flag: different value, different singleton lock, allowing multiple instances to run side by side.

Codex behaves differently. Its `bootstrap.js` calls `app.setPath('userData', ...)` to a hardcoded path **before** `app.requestSingleInstanceLock()`, so the CLI flag never reaches the singleton check and any second launch exits immediately.

## Signing in to a profile

A profile instance can never complete a sign-in on its own: the sign-in callback (`claude://…` — email magic link or Google OAuth alike) is delivered by Windows to the MSIX package, which always activates the **default** app instance, never a profile. Redirecting the protocol per profile is not possible on current Windows — the UCPD driver ignores programmatic `UserChoice` registry writes, and MSIX protocol activation bypasses the registry entirely.

(A subtlety: the default instance thinks its data lives in `%APPDATA%\Claude`, but MSIX filesystem virtualization redirects it to `%LOCALAPPDATA%\Packages\<family>\LocalCache\Roaming\Claude` — that folder is what actually gets moved around below.)

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

## Build

Standalone `.exe` build via PyInstaller, for shipping to users who don't have Python:

```powershell
pip install pyinstaller
pyinstaller --onefile --console --name ai-multi-instance --clean --noconfirm main.py
pyinstaller --onefile --noconsole --name launcher --clean --noconfirm launcher.pyw
```

The two binaries land in `dist/`. Ship them in the same folder — `ai-multi-instance.exe` looks for `launcher.exe` next to itself to manage desktop shortcuts for both Claude and Codex.

Pre-built binaries are attached to each [release](https://github.com/Zoltak-Dev/ai-multi-instance/releases).

## Star History

If this tool saved you some headache, a ⭐ on the repo is appreciated — it's the only feedback signal I get and it really helps the project gain visibility.

<a href="https://www.star-history.com/?repos=Zoltak-Dev%2Fai-multi-instance&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&theme=dark&legend=top-left&sealed_token=AjPPiTEoI91pJvO3tq8VAVB4p8-33d7TyehYw_w6qztZhQkLnrnldsW_nR3I4zzVNgkRMm2NFa-7y-V4Pp6cBeSbaD0APavs6bbwwwZvA1K903na1cRLVtNI4LAtk4K58SWyy4lL9aq3SdoKJ0EUphIMsXaoL1Ng4kT3GmcYSJMQQnXZphtTThJeT0ys" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&legend=top-left&sealed_token=AjPPiTEoI91pJvO3tq8VAVB4p8-33d7TyehYw_w6qztZhQkLnrnldsW_nR3I4zzVNgkRMm2NFa-7y-V4Pp6cBeSbaD0APavs6bbwwwZvA1K903na1cRLVtNI4LAtk4K58SWyy4lL9aq3SdoKJ0EUphIMsXaoL1Ng4kT3GmcYSJMQQnXZphtTThJeT0ys" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&legend=top-left&sealed_token=AjPPiTEoI91pJvO3tq8VAVB4p8-33d7TyehYw_w6qztZhQkLnrnldsW_nR3I4zzVNgkRMm2NFa-7y-V4Pp6cBeSbaD0APavs6bbwwwZvA1K903na1cRLVtNI4LAtk4K58SWyy4lL9aq3SdoKJ0EUphIMsXaoL1Ng4kT3GmcYSJMQQnXZphtTThJeT0ys" />
 </picture>
</a>

## License

MIT
