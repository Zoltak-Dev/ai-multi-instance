"""Regression tests for the snapshot sign-in detector."""
from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import engine
import main


class CookieStoreSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.slot = Path(self.temp_dir.name)
        (self.slot / "Network").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_detector_accepts_database_change_that_happened_before_oauth(self) -> None:
        cookies = self.slot / "Network" / "Cookies"
        cookies.write_bytes(b"before")

        def oauth_appears_after_cookie_write(_cfg: Path) -> bool:
            # Claude can write sessionKey before it updates config.json. This
            # happens after _detect_login captured its pre-login baseline.
            cookies.write_bytes(b"after login")
            return True

        with (mock.patch.object(main.msvcrt, "kbhit", return_value=False),
              mock.patch.object(main, "_config_has_oauth",
                                side_effect=oauth_appears_after_cookie_write),
              mock.patch.object(main.time, "sleep", return_value=None),
              mock.patch.object(main.sys, "stdout", new=StringIO())):
            self.assertEqual(main._detect_login(self.slot, timeout=1), "ready")

    def test_detects_sqlite_journal_change(self) -> None:
        pre_login = main._cookie_store_signature(self.slot)

        (self.slot / "Network" / "Cookies-journal").write_bytes(b"commit")

        self.assertNotEqual(main._cookie_store_signature(self.slot), pre_login)

    def test_detector_ignores_stale_startup_write(self) -> None:
        cookies = self.slot / "Network" / "Cookies"
        cookies.write_bytes(b"initial")
        clock = 0.0
        config_checks = 0

        def monotonic() -> float:
            return clock

        def sleep(seconds: float) -> None:
            nonlocal clock
            clock += seconds

        def oauth_appears_later(_cfg: Path) -> bool:
            nonlocal config_checks
            config_checks += 1
            if config_checks == 1:
                cookies.write_bytes(b"startup write")
            return clock >= 3.0

        with (mock.patch.object(main.msvcrt, "kbhit", return_value=False),
              mock.patch.object(main, "_config_has_oauth",
                                side_effect=oauth_appears_later),
              mock.patch.object(main.time, "monotonic", side_effect=monotonic),
              mock.patch.object(main.time, "sleep", side_effect=sleep),
              mock.patch.object(main.sys, "stdout", new=StringIO())):
            self.assertEqual(main._detect_login(self.slot, timeout=4), "timeout")

    def test_unchanged_store_has_stable_signature(self) -> None:
        cookies = self.slot / "Network" / "Cookies"
        cookies.write_bytes(b"same")

        self.assertEqual(
            main._cookie_store_signature(self.slot),
            main._cookie_store_signature(self.slot),
        )


class SlotResolutionTests(unittest.TestCase):
    """The default slot must be found by occupancy, not by address.

    Claude Desktop 3.x relocated the default userData and left the old
    directory in place holding a single zero-byte `.user-data-left.<family>`
    marker. Resolving by address and testing `is_dir()` therefore finds a
    directory whose only purpose is to record that the profile moved out of it.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.local = self.root / "Local"
        self.roaming = self.root / "Roaming"
        self.local.mkdir()
        self.roaming.mkdir()
        self.family = engine.CLAUDE.package_prefix + engine.CLAUDE.publisher_hash
        self.env = {"LOCALAPPDATA": str(self.local), "APPDATA": str(self.roaming)}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_live(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "Local State").write_text("{}", encoding="utf-8")
        return path

    def _make_tombstone(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / f".user-data-left.{self.family.lower()}").write_bytes(b"")
        return path

    def _relocated(self) -> Path:
        return self.local / f"{engine.CLAUDE.userdata_dirname}-Data" / self.family.lower()

    def _packaged(self) -> Path:
        return (self.local / "Packages" / self.family / "LocalCache" / "Roaming"
                / engine.CLAUDE.userdata_dirname)

    def test_tombstone_is_not_live_userdata(self) -> None:
        tomb = self._make_tombstone(self._packaged())
        self.assertTrue(tomb.is_dir(), "precondition: the old directory still exists")
        self.assertFalse(engine._is_live_userdata(tomb))
        self.assertTrue(engine._has_relocation_marker(tomb))

    def test_directory_with_state_is_live_userdata(self) -> None:
        self.assertTrue(engine._is_live_userdata(self._make_live(self._packaged())))

    def test_empty_directory_is_not_live_userdata(self) -> None:
        empty = self._packaged()
        empty.mkdir(parents=True)
        self.assertFalse(engine._is_live_userdata(empty))

    def test_relocated_layout_wins_over_tombstoned_packaged_layout(self) -> None:
        self._make_tombstone(self._packaged())
        live = self._make_live(self._relocated())
        with mock.patch.dict(engine.os.environ, self.env, clear=False):
            self.assertEqual(engine.default_slot_dir(engine.CLAUDE), live)

    def test_packaged_layout_still_used_when_it_holds_the_profile(self) -> None:
        live = self._make_live(self._packaged())
        with mock.patch.dict(engine.os.environ, self.env, clear=False):
            self.assertEqual(engine.default_slot_dir(engine.CLAUDE), live)

    def test_no_live_slot_reports_none_and_names_the_relocation(self) -> None:
        tomb = self._make_tombstone(self._packaged())
        with mock.patch.dict(engine.os.environ, self.env, clear=False):
            self.assertIsNone(engine.default_slot_dir(engine.CLAUDE))
            self.assertIn(tomb, engine.relocated_slot_dirs(engine.CLAUDE))

    def test_stash_refuses_when_the_app_has_moved_out(self) -> None:
        self._make_tombstone(self._packaged())
        with mock.patch.dict(engine.os.environ, self.env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "relocated its user data"):
                engine.stash_default_slot()

    def test_restore_target_is_the_stash_origin(self) -> None:
        live = self._make_live(self._relocated())
        with mock.patch.dict(engine.os.environ, self.env, clear=False):
            backup = engine.backup_dir(engine.CLAUDE)
            self.assertEqual(backup.name, live.name + engine.BACKUP_SUFFIX)
            origin = backup.with_name(backup.name[: -len(engine.BACKUP_SUFFIX)])
            self.assertEqual(origin, live)

    def test_snapshot_support_does_not_depend_on_being_signed_in(self) -> None:
        with mock.patch.dict(engine.os.environ, self.env, clear=False):
            self.assertIsNone(engine.default_slot_dir(engine.CLAUDE))
            self.assertTrue(engine.supports_login_snapshot(engine.CLAUDE))

    def test_codex_has_no_snapshot_support(self) -> None:
        self.assertFalse(engine.supports_login_snapshot(engine.CODEX))
        self.assertIsNone(engine.default_slot_dir(engine.CODEX))
        self.assertIsNone(engine.backup_dir(engine.CODEX))


if __name__ == "__main__":
    unittest.main()
