"""Regression tests for the snapshot sign-in detector."""
from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
