# Claude Multi-Instance

Run multiple isolated profiles of the **Claude desktop app** on Windows — each with its own sessions, conversations and settings.

The official Claude desktop client doesn't support multiple accounts. This tool launches `claude.exe` with a dedicated `--user-data-dir` per profile, manages desktop shortcuts, and (optionally) routes the Google login callback to the right profile.

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

  Tip: multi-select with spaces, e.g. '1 3 5'
  >
```

## Features

- **Isolated profiles** — each profile has its own data directory, no cross-contamination
- **Survives Claude updates** — the MSIX install path changes on every Claude update; the launcher re-resolves it at every launch
- **Desktop shortcuts** — one click to launch a profile (optional, per profile)
- **Multi-select** — apply Launch / Close / Toggle shortcut / Delete to multiple profiles at once (`1 3 5`)
- **Close running sessions** — kill the Claude process tree for a profile without touching the others
- **Google login routing** — optional patch so the OAuth callback lands in the right profile instead of the default Claude
- **No dependencies** — pure standard library

## Requirements

- Windows 10 or later
- Python 3.9+
- Claude desktop installed (MSIX package from the Microsoft Store or the official installer)

## Install

```powershell
git clone https://github.com/<your-user>/claude-multi-instance.git
cd claude-multi-instance
python main.py
```

That's it. No `pip install`, no virtualenv.

## Usage

Run `python main.py`. The menu is self-explanatory:

| Key | Action |
|----|--------|
| `1` | Launch one or more profiles |
| `2` | Close one or more running profiles (kills the process tree) |
| `3` | Create a new profile (also creates a desktop shortcut) |
| `4` | Rename a profile (folder + shortcut + login target) |
| `5` | Toggle the desktop shortcut for a profile |
| `6` | Delete a profile and its data |
| `7` | Enable / disable the Google login patch |
| `0` | Quit |

For any action that takes a profile number, you can pass multiple numbers separated by spaces: `1 3 5` applies the action to profiles 1, 3 and 5.

Desktop shortcuts work independently — double-click a `.lnk` on your desktop to launch that profile without opening the menu.

## How it works

Each profile is just a folder under `ClaudeProfiles/`. Launching a profile runs:

```
claude.exe --user-data-dir="...\ClaudeProfiles\<name>"
```

`claude.exe` lives inside an MSIX package whose path changes on every Claude update (almost daily). To avoid breaking shortcuts every time, shortcuts don't target `claude.exe` directly — they target `launcher.pyw`, which re-resolves the current Claude path at every launch via `Get-AppxPackage`.

### Google login patch

When you sign in with Google, Claude finishes the flow with a `claude://login/...` URL. By default, Windows routes this to the default Claude install, so the session lands on whichever profile last claimed the protocol.

The patch registers `launcher.pyw` as the `claude://` handler in the Windows registry. When a callback fires, `launcher.pyw` reads `.active_profile` (updated every time you launch a profile from this tool) and forwards the URL to that profile.

The patch works even when `main.py` is closed — it's a persistent registry entry. Disable it from the menu (`7`) if you want Claude's default behavior back.

The handler claim survives Claude restarts because we set the Windows `UserChoice` key with a valid hash (the same algorithm used by SetUserFTA and Mozilla's WindowsUserChoice). This is necessary because Claude rewrites its `claude://` registration on every launch, and without `UserChoice` taking precedence, our handler would be silently replaced. The hash computation lives in `_userchoice.py` and is the only opaque part of the code.

## Project layout

| File | Role |
|------|------|
| `main.py` | Console menu |
| `engine.py` | Profile / shortcut / process management |
| `launcher.pyw` | Headless launcher used by shortcuts and the `claude://` handler |
| `_userchoice.py` | Windows `UserChoice` hash computation (low-level, don't touch) |
| `ClaudeProfiles/` | Per-profile `--user-data-dir` folders (gitignored) |
| `.active_profile` | Plain text file: name of the last profile launched (gitignored) |

## FAQ

**Will this affect my existing Claude install?**
No. Your default Claude profile (under `%LOCALAPPDATA%\AnthropicClaude\`) is untouched. Each profile created here uses its own data directory.

**Can I run several profiles at the same time?**
Yes — that's the point. Each profile is a separate Claude process with its own data directory.

**The Google login patch is on but the callback still goes to the wrong profile.**
The patch routes the callback to the **last profile launched from this tool**. If you launched another Claude instance outside the tool (e.g. by clicking the regular Claude shortcut), it may have stolen the `claude://` registration. Re-launch a profile from this tool (option `1`) to reclaim it, or toggle the patch off/on (`7` twice).

**Deleting a profile says "Could not delete folder".**
Claude is still running and holding files open. Close it (option `2`, then `6`) or quit Claude manually, then retry.

**Can I sync profiles across machines?**
Copy a `ClaudeProfiles/<name>/` folder. Claude stores its session token inside, so you'll be signed in on the other machine.

## License

MIT — see [LICENSE](LICENSE).
