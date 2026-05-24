# Claude Multi-Instance

Run multiple accounts of the [Claude desktop app](https://claude.ai/download) on Windows. The official client only supports one account at a time — this tool launches `claude.exe` with a dedicated profile directory so you can sign in to several accounts and switch between them.

```
────────────────────────────────────────────────────
  Claude Multi-Instance
────────────────────────────────────────────────────
  Claude desktop  : v1.8555.2.0
  Login patch     : on
  Google login →  : work

  Profiles (3)
    1. perso     [shortcut]
    2. test
    3. work      [shortcut]

    1  Launch profile(s)
    2  Close profile(s)
    3  Create a profile
    4  Rename a profile
    5  Toggle desktop shortcut
    6  Delete profile(s)
    7  Disable login patch
    0  Quit
```

## Install

Windows 10+ and Claude desktop installed.

**From source (recommended)** — requires Python 3.9+:

```powershell
git clone https://github.com/Zoltak-Dev/claude-multi-instance.git
cd claude-multi-instance
python main.py
```

No `pip install` — the project uses only the Python standard library.

**Pre-built binary (no Python required)** — download `claude-multi-instance-vX.Y.Z-windows.zip` from [Releases](https://github.com/Zoltak-Dev/claude-multi-instance/releases), unzip both `.exe` files in the same folder, and run `claude-multi-instance.exe`.

📹 **Video walkthrough** — installing the binary and disabling Smart App Control:

https://github.com/user-attachments/assets/eb0052db-64ee-4ddc-8155-e5b9d92ca40d

> ⚠️ **Windows blocks unsigned binaries by default.** Windows refuses to run any `.exe` without a code-signing certificate (which costs 300-600 €/year — not viable for a free project). To run the `.exe`:
>
> 1. **Windows Security** → **App & browser control** → **Smart App Control settings** → switch from **On** to **Off**
> 2. Launch `claude-multi-instance.exe`. Windows will show *"Windows protected your PC"* → click **More info** → **Run anyway**
>
> If you'd rather skip all this, use the source install above — no warning, no setup.

## Usage

The menu is numbered. Pick an action, then pick a profile by its number. For actions that support it, you can pass several numbers separated by spaces (`1 3 5`) to run on multiple profiles at once.

Creating a profile also creates a desktop shortcut. Double-click the shortcut to launch that profile without opening the menu — useful for daily use.

Profiles live in `ClaudeProfiles/<name>/`. Deleting the folder is the same as deleting the profile.

## Google login patch

Optional, toggled from the menu (`7`). When enabled, the OAuth callback after a Google sign-in lands in the last profile you launched, instead of whichever Claude install happens to own the `claude://` protocol at that moment.

It works by registering this tool as the `claude://` handler in `HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\claude`, with a valid Windows `UserChoice` hash so Claude can't silently reclaim the registration on its next launch. Disable it from the menu to revert.

The patch is a registry entry, so it stays active after you close the menu.

## Build

Standalone `.exe` build via PyInstaller, for shipping to users who don't have Python:

```powershell
pip install pyinstaller
pyinstaller --onefile --console   --name claude-multi-instance --clean --noconfirm main.py
pyinstaller --onefile --noconsole --name launcher              --clean --noconfirm launcher.pyw
```

The two binaries land in `dist/`. Ship them in the same folder — `claude-multi-instance.exe` looks for `launcher.exe` next to itself to wire up desktop shortcuts and the `claude://` handler. State (profiles, `.active_profile`) also lives next to the exe.

Pre-built binaries are attached to each [release](https://github.com/Zoltak-Dev/claude-multi-instance/releases).

---

If this tool saved you some headache, a ⭐ on the repo is appreciated — it's the only feedback signal I get and it really helps the project gain visibility.

## License

MIT
