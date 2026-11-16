# Changelog

[Release instructions](.github/signpath/SETUP.md#publish-a-windows-release)

## Unreleased

## v2.1.0 — Shared history and live usage

<!-- source-only -->

- Add Shared history for Codex. Switch accounts in the main window while keeping local chats, or open separate windows in Isolated profiles. Confirm switches that close the current window.
- Add cancellable Codex browser sign-in and improve Claude sign-in recovery.
- Protect existing Claude data when more than one profile location is in use.
- Show remaining quotas in the menu and fetch current limits in Usage. Save the name or quota sort order across restarts.
- Improve desktop shortcuts, including a shortcut to the manager. Close instances in the background so the menu stays responsive.
- Check Git clones and GitHub ZIP downloads for changes to the program files. Offer fast-forward updates for clean clones of the official repository.
- Move profile manager settings and caches into `data/` and migrate existing files on startup.

## v2.0.1 — 2026-10-30

- Integrate #6: use Codex's bundled CLI to avoid package-identity startup errors. Thanks @nchiari.
- Integrate #9: find Claude's relocated data by its contents and restore backups to their original location. Thanks @gsuthrld01042026-commits.
- Isolate Claude's relocation target per profile, following the workaround confirmed in #7. Thanks @aszczu and @fichmand.
- Rediscover app executables after updates instead of crashing with WinError 2 (#8).
- Track profile names containing spaces correctly, including the top-level process when closing an instance.
- Follow data relocation during sign-in; support a first sign-in with no existing main data; recover correctly after cancellation or a failed restore.
- Reject invalid Windows profile names, report deletion and shortcut failures, and show launch errors in both entry points.
- Key usage caches by profile directory, avoid stale logins for signed-out profiles, and keep a corrupt account file from breaking the whole usage view.
- Share data-location checks with the diagnostic; handle cookie database paths containing `#` and release diagnostic SQLite handles promptly.
- Add Windows CI for Python 3.9 and 3.14, including builds of both executables. Fix Python 3.9 shortcut-launcher annotations.

Cowork VM startup and concurrent VMs remain unverified (#5). The full browser sign-in has not been tested with a new account; snapshot data moves and recovery are tested using temporary profiles.
