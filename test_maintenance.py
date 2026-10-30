"""Regression checks for app updates, isolation, and snapshot recovery."""
from __future__ import annotations

import json
import runpy
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, closing
from io import StringIO
from pathlib import Path
from unittest import mock

import diagnose_login
import engine
import main
import usage


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        previous = engine.current_app()
        self.addCleanup(engine.set_app, previous, persist=False)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        engine.set_app(engine.CLAUDE, persist=False)
        stack.enter_context(mock.patch.dict(engine.os.environ, {
            "LOCALAPPDATA": str(self.root / "Local"),
            "APPDATA": str(self.root / "Roaming"),
        }))
        self.profiles = self.root / "Profiles"
        stack.enter_context(mock.patch.object(engine, "PROFILES_DIR", self.profiles))
        stack.enter_context(mock.patch.object(engine, "STATE_FILE", self.root / "state.json"))
        stack.enter_context(mock.patch.object(usage, "_CACHE_FILE", self.root / "cache.json"))

    def _data(self, path: Path, account: str) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "Local State").write_text(account, encoding="utf-8")
        return path

    def _login(self, outcome: str, fresh_slot: Path) -> None:
        with ExitStack() as stack:
            for name in ("close_profile", "close_default_slot"):
                stack.enter_context(mock.patch.object(engine, name))
            stack.enter_context(mock.patch.object(engine, "default_slot_running", return_value=False))
            stack.enter_context(mock.patch.object(engine, "launch_default_slot",
                                                 side_effect=lambda: self._data(fresh_slot, "new")))
            stack.enter_context(mock.patch.object(main, "confirm", return_value=True))
            stack.enter_context(mock.patch.object(main, "pause"))
            stack.enter_context(mock.patch.object(main, "_detect_login", return_value=outcome))
            stack.enter_context(mock.patch.object(usage, "session_snapshot", return_value={"sessionKey": True}))
            stack.enter_context(mock.patch.object(main.sys, "stdout", new=StringIO()))
            main.login_flow("new-profile")

    def test_cancel_restores_main_in_all_three_layouts(self) -> None:
        for slot in engine.slot_candidates(engine.CLAUDE):
            with self.subTest(slot=slot):
                self._data(slot, "original")
                self._login("cancelled", slot)
                self.assertEqual((slot / "Local State").read_text(), "original")
                self.assertFalse(engine.has_orphan_backup())
                engine._rmtree(slot)

    def test_cancel_removes_fresh_data_relocated_during_signin(self) -> None:
        modern, old, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(old, "original")
        self._login("cancelled", modern)
        self.assertFalse(modern.exists())
        self.assertEqual(engine.default_slot_dir(), old)
        self.assertEqual((old / "Local State").read_text(), "original")

    def test_success_adopts_new_session_and_restores_main(self) -> None:
        modern, _, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "original")
        self._login("ready", modern)
        self.assertEqual((modern / "Local State").read_text(), "original")
        self.assertEqual((self.profiles / "new-profile" / "Local State").read_text(), "new")
        self.assertFalse(engine.has_orphan_backup())

    def test_first_signin_works_without_existing_default_data(self) -> None:
        modern, _, _ = engine.slot_candidates(engine.CLAUDE)
        self._login("ready", modern)
        self.assertEqual((self.profiles / "new-profile" / "Local State").read_text(), "new")

    def test_stash_refuses_to_overwrite_an_interrupted_session(self) -> None:
        modern, _, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "original")
        self.assertTrue(engine.stash_default_slot())
        backup = engine.backup_dir()
        self._data(modern, "fresh")
        with self.assertRaises(RuntimeError):
            engine.stash_default_slot()
        self.assertEqual((backup / "Local State").read_text(), "original")

    def test_failed_restore_keeps_backup_for_recovery(self) -> None:
        modern, _, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "original")
        engine.stash_default_slot()
        backup = engine.backup_dir()
        self._data(modern, "fresh")
        with mock.patch.object(engine, "_rmtree_retry", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                engine.restore_default_slot()
        self.assertEqual((backup / "Local State").read_text(), "original")

    def test_old_marker_only_backup_cannot_replace_real_main_data(self) -> None:
        modern, old, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "original")
        backup = old.with_name(old.name + engine.BACKUP_SUFFIX)
        backup.mkdir(parents=True)
        (backup / ".user-data-left.claude_pzs8sxrjxfjjc").touch()
        with self.assertRaisesRegex(RuntimeError, "Nothing was removed"):
            engine.restore_default_slot()
        self.assertEqual((modern / "Local State").read_text(), "original")

    def test_detector_follows_data_moved_during_startup(self) -> None:
        modern, old, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "new")
        (modern / "Network").mkdir()
        (modern / "Network" / "Cookies").write_bytes(b"written")
        (modern / "config.json").write_text(json.dumps({"oauth:tokenCache": "test"}))
        with (mock.patch.object(main.msvcrt, "kbhit", return_value=False),
              mock.patch.object(main.time, "sleep"),
              mock.patch.object(main.sys, "stdout", new=StringIO())):
            self.assertEqual(main._detect_login(old, timeout=1, follow_default=True), "ready")

    def test_diagnostic_uses_same_data_directory_as_manager(self) -> None:
        modern, old, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "original")
        old.mkdir(parents=True)
        (old / ".user-data-left.claude_pzs8sxrjxfjjc").touch()
        self.assertEqual(diagnose_login._default_slot()[0], engine.default_slot_dir())

    def test_update_invalidates_cached_executable(self) -> None:
        package = self.root / "Claude_99.0_x64__pzs8sxrjxfjjc"
        exe = package / "app" / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        with (mock.patch.dict(engine._exe_cache, {"claude": self.root / "removed.exe"}),
              mock.patch.object(engine.subprocess, "run", return_value=mock.Mock(stdout=str(package)))):
            self.assertEqual(engine.find_app_exe(), exe)

    def test_profile_environment_does_not_change_parent_environment(self) -> None:
        original = engine.os.environ["LOCALAPPDATA"]
        env = engine.profile_env("one")
        self.assertEqual(env["LOCALAPPDATA"], str(self.profiles / "one" / ".localappdata"))
        self.assertEqual(engine.os.environ["LOCALAPPDATA"], original)
        self.assertNotEqual(env["LOCALAPPDATA"], engine.profile_env("two")["LOCALAPPDATA"])

    def test_codex_launch_uses_bundled_cli_and_isolated_auth(self) -> None:
        # Patch the app without set_app(), keeping the temporary profile root.
        exe = self.root / "app" / "ChatGPT.exe"
        cli = exe.parent / "resources" / "codex.exe"
        cli.parent.mkdir(parents=True)
        cli.touch()
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine.subprocess, "Popen") as popen):
            engine.launch_profile("one", exe=exe)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_CLI_PATH"], str(cli))
        self.assertEqual(env["CODEX_HOME"], str(self.profiles / "one" / ".codex"))
        self.assertEqual(env["CODEX_ELECTRON_USER_DATA_PATH"], str(self.profiles / "one" / "electron"))

    def test_launch_failure_is_displayed_without_crashing_ui(self) -> None:
        state = main.State()
        state.profiles = ["one"]
        state.cursor = 1
        with mock.patch.object(engine, "launch_profile", side_effect=FileNotFoundError("app updated")):
            main.do_launch(state)
        self.assertIn("app updated", state.status)

    def test_process_detection_preserves_spaces_in_profile_names(self) -> None:
        one, two = self.profiles / "Work account", self.profiles / "Personal account"
        commands = [
            (1, "", f'claude.exe "--user-data-dir={one}"'),
            (2, "", f'claude.exe --user-data-dir="{one}" --type=renderer'),
            (3, "", f'claude.exe --user-data-dir "{two}"'),
        ]
        with mock.patch.object(engine, "_processes", return_value=commands):
            self.assertEqual(engine.running_profiles(), {"Work account": [1, 2], "Personal account": [3]})

    def test_launcher_displays_os_errors(self) -> None:
        launcher = runpy.run_path(str(Path(__file__).with_name("launcher.pyw")))
        with (mock.patch.object(engine, "find_app_exe", return_value=Path("fake.exe")),
              mock.patch.object(engine, "set_app"),
              mock.patch.object(engine, "launch_profile", side_effect=PermissionError("denied")),
              mock.patch.dict(launcher["main"].__globals__, {"_error": mock.Mock()}),
              mock.patch.object(main.sys, "argv", ["launcher.pyw", "one"])):
            self.assertEqual(launcher["main"](), 1)

    def test_invalid_names_cannot_delete_parent_or_other_directories(self) -> None:
        for name in (".", "..", "../other", "a/b", "C:\\other", "CON", "con.txt", "LPT1", "a.", "a ", "a\n"):
            with self.subTest(name=name):
                self.assertFalse(engine.valid_profile_name(name))
                with self.assertRaises(ValueError):
                    engine.delete_profile(name)
        self.assertTrue(engine.valid_profile_name("Compte personnel #1"))

    def test_failed_deletion_preserves_shortcut_and_reports_failure(self) -> None:
        engine.create_profile("one")
        with (mock.patch.object(engine, "_rmtree", side_effect=PermissionError("locked")),
              mock.patch.object(engine, "desktop_dir") as desktop):
            with self.assertRaises(PermissionError):
                engine.delete_profile("one")
        desktop.assert_not_called()

    def test_shortcut_failure_is_not_reported_as_success(self) -> None:
        with (mock.patch.object(engine, "desktop_dir", return_value=self.root),
              mock.patch.object(engine.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "powershell"))):
            with self.assertRaises(subprocess.CalledProcessError):
                engine.create_shortcut("one", exe=Path("fake.exe"))

    def test_corrupt_state_and_cache_are_ignored(self) -> None:
        engine.STATE_FILE.write_text("[]")
        usage._CACHE_FILE.write_text("null")
        self.assertEqual(engine._load_state(), {})
        self.assertEqual(usage._load_cache(), {})

    def test_absent_session_does_not_reuse_cached_login(self) -> None:
        with (mock.patch.object(usage, "_read_session", return_value=("absent", None, None)),
              mock.patch.object(usage, "_cache_get_session") as cached):
            self.assertEqual(usage._fetch_claude("one", self.root).error, "not signed in")
        cached.assert_not_called()

    def test_locked_sessions_use_directory_as_cache_key(self) -> None:
        with (mock.patch.object(usage, "_read_session", return_value=("locked", None, None)),
              mock.patch.object(usage, "_cache_get_session", return_value=None) as cached):
            usage._fetch_claude("Main instance", self.root)
        cached.assert_called_once_with(str(self.root.resolve()))

    def test_bad_account_does_not_break_other_usage_results(self) -> None:
        bad = self.root / "bad" / ".codex"
        bad.mkdir(parents=True)
        (bad / "auth.json").write_text('[]')
        results = usage.fetch_all([("bad", bad.parent), ("missing", self.root / "missing")], "codex")
        self.assertEqual([r.name for r in results], ["bad", "missing"])
        self.assertTrue(all(r.error for r in results))

    def test_cookie_paths_with_uri_characters_and_exact_domain_filter(self) -> None:
        profile = self.root / "Compte #1"
        (profile / "Network").mkdir(parents=True)
        db = profile / "Network" / "Cookies"
        with closing(sqlite3.connect(db)) as con:
            con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
            con.executemany("INSERT INTO cookies VALUES (?, 'sessionKey', ?)",
                            [(".claude.ai", b"good"), ("claude.ai.example.com", b"bad")])
            con.commit()
        with (mock.patch.object(usage, "_master_key", return_value=b"test"),
              mock.patch.object(usage, "_AesGcm"),
              mock.patch.object(usage, "_decrypt_value", side_effect=lambda b, *_: b.decode())):
            self.assertEqual(usage._read_claude_cookies(profile), {"sessionKey": "good"})


if __name__ == "__main__":
    unittest.main()
