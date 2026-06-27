# Claude / Codex Multi-Instance

Run multiple accounts of the [Claude desktop app](https://claude.ai/download) **and** the [Codex desktop app](https://chatgpt.com/codex) on Windows. The official clients only support one account at a time. This tool launches each app with a dedicated profile directory per account, allowing several accounts to run side by side and be switched instantly.

![Claude / Codex Multi-Instance preview](https://github.com/user-attachments/assets/01e0c061-2441-4a37-92f0-a4d22ae76d18)

Press `7` to switch between Claude and Codex. Profiles and shortcuts are managed independently for each app.

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

📹 **Video walkthrough** — installing the binary and disabling Smart App Control:

https://github.com/user-attachments/assets/eb0052db-64ee-4ddc-8155-e5b9d92ca40d

> ⚠️ **Windows blocks unsigned binaries by default.** Windows refuses to run any `.exe` without a code-signing certificate (which costs 300-600 €/year — not viable for a free project). To run the `.exe`:
>
> 1. **Windows Security** → **App & browser control** → **Smart App Control settings** → switch from **On** to **Off**
> 2. Launch `ai-multi-instance.exe`. Windows will show *"Windows protected your PC"* → click **More info** → **Run anyway**
>
> If you'd rather skip all this, use the source install above — no warning, no setup.

## Usage

The menu is numbered. Pick an action, then pick a profile by its number. For actions that support it, you can pass several numbers separated by spaces (`1 3 5`) to run on multiple profiles at once.

Creating a profile also creates a desktop shortcut. Double-click the shortcut to launch that profile directly without opening the menu.

Profiles live in their own folders, kept fully separate per app:

- `ClaudeProfiles/<name>/` for Claude
- `CodexProfiles/<name>/` for Codex

Deleting the folder is the same as deleting the profile.

## How multi-instance works

Claude and Codex use different mechanisms to enforce their single-instance behavior.

The Claude desktop app respects Chromium's standard `--user-data-dir` flag: different value, different singleton lock, allowing multiple instances to run side by side.

Codex behaves differently. Its `bootstrap.js` calls `app.setPath('userData', ...)` to a hardcoded path **before** `app.requestSingleInstanceLock()`, so the CLI flag never reaches the singleton check and any second launch exits immediately.

## Usage tracker

Claude only, from the menu (`8`). Shows each account's current limits side by side — 5-hour session, 7-day weekly (and Opus), plus when each resets:

```
  Profile   Account                 5h     7d   Opus  Resets
  work      you@example.com         0%    11%    n/a  in 1d 17h
  perso     other@example.com       0%    10%    n/a  in 4d 12h
```

It reads each profile's claude.ai session straight from that profile's own cookie store and queries the official `claude.ai/api/.../usage` endpoint — per account, no logins required, with no third-party dependencies (DPAPI + AES-GCM via Windows CNG through `ctypes`).

A running instance holds an exclusive lock on its own cookie store (so the profile you're actively using is the one you can't read). To work around this, the session token is cached — DPAPI-encrypted, in `usage_cache.json` — whenever the store is readable, and reused while it's locked. In practice: launch a profile at least once from this tool, and its usage stays visible whether the app is open or closed.

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

<a href="https://www.star-history.com/?type=date&repos=Zoltak-Dev%2Fai-multi-instance">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&theme=dark&legend=top-left" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&legend=top-left" />
    <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Zoltak-Dev/ai-multi-instance&type=date&legend=top-left" />
  </picture>
</a>

## License

MIT
