"""Regression checks for app updates, isolation, and snapshot recovery."""
from __future__ import annotations

import base64
import json
import runpy
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, closing
from io import StringIO
from pathlib import Path
from threading import Event
from unittest import mock

import engine
import main
import usage
from tools import diagnose_login


def _github_tree(files: dict[str, str]) -> dict:
    return {"tree": [{"path": path, "type": "blob", "sha": sha} for path, sha in files.items()],
            "truncated": False}


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

    def test_console_hotkeys_follow_digit_positions_and_respect_num_lock(self) -> None:
        events = []
        expected = []

        def key(char, virtual_key, scan_code, result, *, down=True, modifiers=0, repeat=1):
            events.append(main._InputRecord(1, main._KeyEvent(down, repeat, virtual_key, scan_code, char, modifiers)))
            expected.append(result)

        for i, char in enumerate('&é"\'(§è!çà'):
            digit = "1234567890"[i]
            key(char, ord(digit), i + 2, digit)
            key(char, ord(digit), i + 2, "", down=False)
        key("3", 0x33, 0x04, "3", modifiers=0x10)  # Shift
        key("#", 0x33, 0x04, "#", modifiers=0x09)  # AltGr
        key("3", 0x63, 0x51, "3", modifiers=0x20)  # Numpad with Num Lock
        key("\x00", 0x26, 0x48, "UP")  # Numpad arrow with Num Lock off
        key("\x00", 0x28, 0x50, "DOWN", modifiers=0x100)  # Dedicated arrow key
        key("a", 0x41, 0x10, "a")
        with (mock.patch.object(main, "_read_console_key", side_effect=events),
              mock.patch.object(main, "_held_launch_keys", set())):
            self.assertEqual([main.getkey() for _ in events], expected)

    def test_console_launch_confirmation_requires_a_fresh_press(self) -> None:
        for char, virtual_key, scan, modifiers, action in (("&", 0x31, 0x02, 0, "1"),
                                                           ("1", 0x61, 0x4F, 0x20, "1"),
                                                           ("\r", 0x0D, 0x1C, 0, "ENTER"),
                                                           ("\r", 0x0D, 0x1C, 0x100, "ENTER")):
            events = [main._InputRecord(1, main._KeyEvent(down, repeat, virtual_key, scan, char, modifiers))
                      for down, repeat in ((True, 3), (True, 1), (False, 1), (True, 1))]
            with (self.subTest(char=char, modifiers=modifiers),
                  mock.patch.object(main, "_read_console_key", side_effect=events),
                  mock.patch.object(main, "_held_launch_keys", set())):
                self.assertEqual([main.getkey() for _ in events], [action, "", "", action])

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

    def test_claude_signin_refuses_multiple_existing_data_folders(self) -> None:
        modern, old, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(modern, "modern")
        self._data(old, "old")
        with self.assertRaisesRegex(RuntimeError, "Multiple Claude data folders"):
            engine.stash_default_slot()
        self.assertEqual((modern / "Local State").read_text(), "modern")
        self.assertEqual((old / "Local State").read_text(), "old")
        self.assertFalse(engine.has_orphan_backup())

    def test_claude_recovery_preserves_the_other_data_folder(self) -> None:
        modern, old, _ = engine.slot_candidates(engine.CLAUDE)
        self._data(old, "original")
        engine.stash_default_slot()
        backup = engine.backup_dir()
        self._data(modern, "other profile")
        with self.assertRaisesRegex(RuntimeError, "Move it aside"):
            engine.restore_default_slot()
        self.assertEqual((modern / "Local State").read_text(), "other profile")
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

    def _codex_auth_fixture(self, path: Path, subject: str, refresh: str) -> bytes:
        claims = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode()
        data = json.dumps({"tokens": {
            "id_token": f"header.{claims}.signature", "access_token": "test",
            "account_id": "test-workspace", "refresh_token": refresh,
        }}).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def test_codex_switch_reuses_main_history_and_keeps_both_accounts(self) -> None:
        home = self.root / "main-home"
        auth = home / "auth.json"
        original = self._codex_auth_fixture(auth, "original", "initial")
        history = home / "sessions" / "existing.jsonl"
        history.parent.mkdir()
        history.write_bytes(b"existing conversation")
        target = self.profiles / "work" / ".codex" / "auth.json"
        work = self._codex_auth_fixture(target, "work", "initial")
        exe = self.root / "ChatGPT.exe"
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "close_default_slot") as close,
              mock.patch.object(engine, "default_slot_running") as redundant_scan,
              mock.patch.object(engine, "running_profiles", return_value={}),
              mock.patch.object(engine, "launch_default_slot") as launch):
            engine.launch_account("work", exe)
            self.assertEqual(auth.read_bytes(), work)
            refreshed = self._codex_auth_fixture(auth, "work", "refreshed")
            engine.os.utime(auth, ns=(target.stat().st_mtime_ns + 1_000_000_000,) * 2)
            engine.launch_account("Original main account", exe)
            self.assertEqual(auth.read_bytes(), original)
            self.assertEqual(target.read_bytes(), refreshed)
            self.assertEqual(history.read_bytes(), b"existing conversation")
            self.assertEqual(launch.call_args_list, [mock.call(exe), mock.call(exe)])
        self.assertEqual(close.call_args_list, [mock.call(wait=20, confirm_restart=None)] * 2)
        redundant_scan.assert_not_called()

    def test_codex_sign_in_can_cancel_without_changing_saved_auth(self) -> None:
        exe = self.root / "app" / "ChatGPT.exe"
        cli = exe.parent / "resources" / "codex.exe"
        cli.parent.mkdir(parents=True)
        cli.touch()
        home = self.root / "main-home"
        target = self.profiles / "work" / ".codex" / "auth.json"
        previous = self._codex_auth_fixture(target, "work", "previous")
        original = self._codex_auth_fixture(home / "auth.json", "personal", "original")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "find_app_exe", return_value=exe),
              mock.patch.object(engine, "running_profiles", return_value={}),
              mock.patch.object(engine, "launch_default_slot") as launch,
              mock.patch.object(main.sys, "stdout", new=StringIO()) as output,
              mock.patch("builtins.input", return_value="y") as answer,
              mock.patch.object(main.msvcrt, "kbhit", return_value=True),
              mock.patch.object(main, "getkey", return_value="ESC")):
            for cancel in (True, False):
                with self.subTest(cancel=cancel):
                    login = mock.MagicMock(returncode=None if cancel else 0)
                    login.__enter__.return_value = login
                    login.poll.return_value = login.returncode
                    fresh = None

                    def start(args, *, env, stdin):
                        nonlocal fresh
                        self.assertEqual(args[:2], [str(cli), "login"])
                        self.assertEqual(stdin, subprocess.DEVNULL)
                        stage = Path(env["CODEX_HOME"])
                        self.assertEqual(stage.parent, home)
                        fresh = self._codex_auth_fixture(stage / "auth.json", "work", "fresh")
                        return login

                    state = main.State()
                    with mock.patch.object(engine.subprocess, "Popen", side_effect=start) as spawn:
                        main.sign_in_profile("work", state)
                    spawn.assert_called_once()
                    if cancel:
                        login.kill.assert_called_once_with()
                        login.wait.assert_called_once_with()
                        self.assertIn("cancelled", state.status)
                    else:
                        login.kill.assert_not_called()
                        self.assertIn("signed in", state.status)
                    self.assertEqual(target.read_bytes(), previous if cancel else fresh)
                    self.assertEqual((home / "auth.json").read_bytes(), original)
                    self.assertEqual(list(home.glob(".multi-instance-login-*")), [])
        launch.assert_not_called()
        self.assertIn("replaces this profile's saved login", output.getvalue())
        self.assertEqual(answer.call_args_list, [mock.call("\n  Continue? [y/N] ")] * 2)

    def test_codex_sign_in_can_cancel_at_the_confirmation(self) -> None:
        target = self.profiles / "work" / ".codex" / "auth.json"
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(main.sys, "stdout", new=StringIO()),
              mock.patch.object(engine, "sign_in_codex") as login,
              mock.patch("builtins.input", side_effect=[KeyboardInterrupt, ""]) as answer):
            state = main.State()
            engine.create_profile("work")
            main.sign_in_profile("work", state)
            self.assertIn("cancelled", state.status)
            self._codex_auth_fixture(target, "work", "previous")
            main.sign_in_profile("work", state)
            self.assertIn("cancelled", state.status)
        login.assert_not_called()
        self.assertEqual(answer.call_args_list, [mock.call("\n  Continue? [Y/n] "),
                                                mock.call("\n  Continue? [y/N] ")])

    def test_codex_can_restore_isolated_launch_mode(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        original = self._codex_auth_fixture(main_auth, "personal", "initial")
        self._codex_auth_fixture(self.profiles / "work" / ".codex" / "auth.json", "work", "initial")
        engine.STATE_FILE.write_text('{"selected_app": "codex"}', encoding="utf-8")
        state = main.State()
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "find_app_exe", return_value=None),
              mock.patch.object(engine, "default_slot_running", return_value=True),
              mock.patch.object(engine, "close_default_slot") as close_main,
              mock.patch.object(engine, "launch_default_slot") as launch_main,
              mock.patch.object(engine, "launch_profile") as launch):
            self.assertTrue(engine.shared_codex_history())
            self.assertIn("Shared history", "\n".join(main.render(state)))
            main._dispatch("h", state)
            self.assertFalse(engine.shared_codex_history())
            self.assertIn("Isolated profiles", "\n".join(main.render(state)))
            engine.launch_account("work")
            self.assertIs(engine._load_state()["codex_shared_history"], False)
            main._dispatch("h", state)
            self.assertTrue(engine.shared_codex_history())
            self.assertIs(engine._load_state()["codex_shared_history"], True)
        launch.assert_called_once_with("work", exe=None)
        close_main.assert_not_called()
        launch_main.assert_not_called()
        self.assertEqual(main_auth.read_bytes(), original)

    def test_isolating_shared_codex_returns_the_latest_login_to_its_profile(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        target = self.profiles / "work" / ".codex" / "auth.json"
        saved = self._codex_auth_fixture(target, "work", "initial")
        refreshed = self._codex_auth_fixture(main_auth, "work", "refreshed")
        main_mtime = target.stat().st_mtime_ns + 1_000_000_000
        engine.os.utime(main_auth, ns=(main_mtime, main_mtime))
        history = home / "sessions" / "existing.jsonl"
        history.parent.mkdir()
        history.write_bytes(b"existing conversation")
        engine.STATE_FILE.write_text('{"codex_shared_history": true}', encoding="utf-8")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", return_value=({}, [123])) as running,
              mock.patch.object(engine, "close_default_slot") as close_main,
              mock.patch.object(engine, "launch_profile") as launch):
            state = main.State()
            main._dispatch("h", state)
            self.assertRegex(state.status.lower(), "close.*main")
            self.assertTrue(engine.shared_codex_history())
            self.assertEqual(target.read_bytes(), saved)
            self.assertEqual(main_auth.read_bytes(), refreshed)

            def refresh_on_exit():
                self._codex_auth_fixture(main_auth, "work", "refreshed again on exit")
                engine.os.utime(main_auth, ns=(main_mtime, main_mtime))
                return {}, []

            running.side_effect = refresh_on_exit
            main._dispatch("h", state)
            final_login = main_auth.read_bytes()
            self.assertNotEqual(final_login, refreshed)
            self.assertIn("changed", state.status)
            self.assertTrue(engine.shared_codex_history())
            self.assertEqual(target.read_bytes(), saved)

            running.side_effect = None
            running.return_value = ({}, [])
            main._dispatch("h", state)
            self.assertFalse(engine.shared_codex_history())
            self.assertFalse(main_auth.exists())
            self.assertEqual(target.read_bytes(), final_login)
            self.assertEqual(history.read_bytes(), b"existing conversation")
            engine.launch_account("work")
        close_main.assert_not_called()
        launch.assert_called_once_with("work", exe=None)

    def test_failed_isolation_preference_save_preserves_main_login(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        target = self.profiles / "work" / ".codex" / "auth.json"
        self._codex_auth_fixture(target, "work", "initial")
        refreshed = self._codex_auth_fixture(main_auth, "work", "refreshed")
        main_mtime = target.stat().st_mtime_ns + 1_000_000_000
        engine.os.utime(main_auth, ns=(main_mtime, main_mtime))
        engine.STATE_FILE.write_text('{"codex_shared_history": true}', encoding="utf-8")
        write_atomic = engine._write_atomic

        def refuse_preferences(path, data, **kwargs):
            if path == engine.STATE_FILE:
                raise PermissionError("blocked")
            return write_atomic(path, data, **kwargs)

        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", return_value=({}, [])),
              mock.patch.object(engine, "_write_atomic", side_effect=refuse_preferences)):
            with self.assertRaisesRegex(PermissionError, "blocked"):
                engine.toggle_codex_history()
            self.assertTrue(engine.shared_codex_history())
            self.assertEqual(main_auth.read_bytes(), refreshed)
            self.assertEqual(main_auth.stat().st_mtime_ns, main_mtime)

    def test_detach_codex_profile_saves_login_and_keeps_shared_history(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        target = self.profiles / "work" / ".codex" / "auth.json"
        saved = self._codex_auth_fixture(target, "work", "initial")
        refreshed = self._codex_auth_fixture(main_auth, "work", "refreshed")
        main_mtime = target.stat().st_mtime_ns + 1_000_000_000
        engine.os.utime(main_auth, ns=(main_mtime, main_mtime))
        history = home / "sessions" / "existing.jsonl"
        history.parent.mkdir()
        history.write_bytes(b"existing conversation")
        settings = '{"codex_shared_history": true}'
        engine.STATE_FILE.write_text(settings, encoding="utf-8")

        state = main.State()
        state.refresh()
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", return_value=({}, [123])) as running,
              mock.patch.object(main, "_instances", {engine.CODEX: (frozenset(), True, "work")}),
              mock.patch.object(main, "_menu_usage", {})):
            self.assertIn("Detach profile", "\n".join(main.render(state)))
            main._dispatch("d", state)
            self.assertIn("Close the main Codex instance", state.status)
            self.assertEqual(main_auth.read_bytes(), refreshed)
            self.assertEqual(target.read_bytes(), saved)

            running.return_value = ({}, [])
            main._dispatch("d", state)
            self.assertIn("Profile removed", state.status)
            self.assertFalse(engine.detach_codex_profile())
            self.assertTrue(engine.shared_codex_history())
        self.assertFalse(main_auth.exists())
        self.assertEqual(target.read_bytes(), refreshed)
        self.assertEqual(target.stat().st_mtime_ns, main_mtime)
        self.assertEqual(history.read_bytes(), b"existing conversation")
        self.assertEqual(engine.STATE_FILE.read_text(encoding="utf-8"), settings)

    def test_attach_codex_profile_preserves_main_login_and_checks_current_state(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        original = self._codex_auth_fixture(main_auth, "personal", "original")
        work = self._codex_auth_fixture(self.profiles / "work" / ".codex" / "auth.json", "work", "initial")
        alternate = self.profiles / "alternate" / ".codex" / "auth.json"
        self._codex_auth_fixture(alternate, "alternate", "initial")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", return_value=({}, [123])) as running,
              mock.patch.object(engine, "launch_default_slot") as launch,
              mock.patch.object(engine, "_record_launch") as record):
            with self.assertRaisesRegex(RuntimeError, "Close the main Codex instance"):
                engine.attach_codex_profile("work")
            running.return_value = ({"work": [123]}, [])
            with self.assertRaisesRegex(RuntimeError, "Close the isolated Codex profile"):
                engine.attach_codex_profile("work")
            self.assertEqual(main_auth.read_bytes(), original)
            self.assertFalse((self.profiles / "Original main account").exists())

            running.return_value = ({}, [])
            alternate.write_bytes(work)
            with self.assertRaisesRegex(RuntimeError, "Another profile has the same Codex login"):
                engine.attach_codex_profile("work")
            self._codex_auth_fixture(alternate, "alternate", "initial")
            engine.attach_codex_profile("work")
            self.assertEqual((self.profiles / "Original main account" / ".codex" / "auth.json").read_bytes(),
                             original)
            with self.assertRaisesRegex(RuntimeError, "A profile is already loaded"):
                engine.attach_codex_profile("alternate")
            with self.assertRaisesRegex(RuntimeError, "profile loaded.*changed"):
                engine.detach_codex_profile(expected_profile="alternate")
            self.assertEqual(main_auth.read_bytes(), work)
            self.assertEqual(engine.main_codex_profile(), "work")
            launch.assert_not_called()
            record.assert_not_called()

    def test_detach_codex_profile_leaves_nonmatching_main_login(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        original = self._codex_auth_fixture(main_auth, "personal", "original")
        self._codex_auth_fixture(self.profiles / "work" / ".codex" / "auth.json", "work", "initial")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances") as running):
            self.assertFalse(engine.detach_codex_profile())
            self.assertEqual(main_auth.read_bytes(), original)
        running.assert_not_called()

    def test_d_attaches_the_selected_profile_and_can_detach_it_again(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        profiles = self.root / engine.CODEX.profiles_dirname
        work = self._codex_auth_fixture(profiles / "Work" / ".codex" / "auth.json", "work", "saved")
        state = main.State()
        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "PROFILES_DIR", profiles),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", return_value=({}, [])),
              mock.patch.object(engine, "all_running_instances", return_value={engine.CODEX: ({}, [])}),
              mock.patch.object(engine, "launch_default_slot") as launch,
              mock.patch.object(engine, "_record_launch") as record,
              mock.patch.object(main, "_instances", {}),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.dict(main._refresh_usage, {engine.CODEX: Event()}),
              mock.patch.object(main, "_refresh_instances", Event())):
            state.refresh()
            main._scan_instances()
            self.assertIn("Attach profile", "\n".join(main.render(state)))
            main._dispatch("d", state)
            self.assertIn("Select one Codex profile", state.status)
            self.assertFalse(main_auth.exists())

            state.move(1)
            main._dispatch("d", state)
            self.assertEqual(main_auth.read_bytes(), work)
            self.assertIn("Attached 'Work'", state.status)

            main._scan_instances()
            frame = "\n".join(main.render(state))
            self.assertIn("Main instance", frame)
            self.assertIn("(Work)", frame)
            self.assertIn("Detach profile", frame)
            main._dispatch("d", state)
            self.assertFalse(main_auth.exists())
            main._scan_instances()
            self.assertIn("Attach profile", "\n".join(main.render(state)))
            self.assertTrue(engine.shared_codex_history())
        launch.assert_not_called()
        record.assert_not_called()

    def test_codex_isolated_profile_requires_sign_in_before_launch(self) -> None:
        engine.create_profile("work")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=self.root / "main-home"),
              mock.patch.object(engine, "launch_profile") as launch):
            with self.assertRaisesRegex(RuntimeError, "press 2"):
                engine.launch_account("work")
        launch.assert_not_called()

    def test_codex_main_profile_tracks_auth_and_profile_rename(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        self._codex_auth_fixture(self.profiles / "work" / ".codex" / "auth.json", "work", "saved")
        self._codex_auth_fixture(main_auth, "work", "refreshed")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home)):
            self.assertEqual(engine.main_codex_profile(), "work")
            (self.profiles / "work").rename(self.profiles / "renamed")
            self.assertEqual(engine.main_codex_profile(), "renamed")
            self._codex_auth_fixture(main_auth, "external-login", "fresh")
            self.assertIsNone(engine.main_codex_profile())

    def test_codex_isolated_launch_reuses_refreshed_main_auth_after_close(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        target = self.profiles / "work" / ".codex" / "auth.json"
        original = self._codex_auth_fixture(target, "work", "initial")
        refreshed = self._codex_auth_fixture(main_auth, "work", "refreshed")
        engine.STATE_FILE.write_text('{"codex_shared_history": false}', encoding="utf-8")
        engine.os.utime(main_auth, ns=(target.stat().st_mtime_ns + 1_000_000_000,) * 2)
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_profiles", return_value={}),
              mock.patch.object(engine, "default_slot_running", side_effect=[True, False]),
              mock.patch.object(engine, "launch_profile") as launch):
            with self.assertRaisesRegex(RuntimeError, "main Codex"):
                engine.launch_account("work")
            launch.assert_not_called()
            self.assertEqual(target.read_bytes(), original)
            engine.launch_account("work")
        launch.assert_called_once_with("work", exe=None)
        self.assertEqual(target.read_bytes(), refreshed)

    def test_codex_main_launch_reuses_refreshed_isolated_auth_after_close(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        target = self.profiles / "work" / ".codex" / "auth.json"
        original = self._codex_auth_fixture(main_auth, "work", "initial")
        refreshed = self._codex_auth_fixture(target, "work", "refreshed")
        engine.os.utime(target, ns=(main_auth.stat().st_mtime_ns + 1_000_000_000,) * 2)
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", side_effect=[({"work": [1]}, []), ({}, [])]),
              mock.patch.object(engine, "launch_default_slot") as launch):
            with self.assertRaisesRegex(RuntimeError, "isolated Codex profile 'work'"):
                engine.launch_main()
            launch.assert_not_called()
            self.assertEqual(main_auth.read_bytes(), original)
            engine.launch_main()
        launch.assert_called_once_with()
        self.assertEqual(main_auth.read_bytes(), refreshed)

    def test_codex_switch_stops_when_process_check_fails(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        previous = self._codex_auth_fixture(main_auth, "personal", "initial")
        self._codex_auth_fixture(self.profiles / "work" / ".codex" / "auth.json", "work", "initial")
        exe = self.root / "ChatGPT.exe"
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "close_default_slot") as close,
              mock.patch.object(engine, "launch_default_slot") as launch,
              mock.patch.object(engine.subprocess, "run") as scan):
            for error in (subprocess.TimeoutExpired("powershell", 10),
                          subprocess.CalledProcessError(1, "powershell")):
                with self.subTest(error=error):
                    scan.side_effect = error
                    with self.assertRaisesRegex(RuntimeError, "Could not check which Codex instances"):
                        engine.launch_account("work", exe)
                    self.assertEqual(main_auth.read_bytes(), previous)
            close.assert_not_called()
            launch.assert_not_called()

    def test_codex_failed_switch_restores_auth_and_freshness(self) -> None:
        home = self.root / "main-home"
        main_auth = home / "auth.json"
        previous = self._codex_auth_fixture(main_auth, "personal", "initial")
        previous_mtime = main_auth.stat().st_mtime_ns - 1_000_000_000
        engine.os.utime(main_auth, ns=(previous_mtime, previous_mtime))
        self._codex_auth_fixture(self.profiles / "work" / ".codex" / "auth.json", "work", "initial")
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "_processes", return_value=[]),
              mock.patch.object(engine, "launch_default_slot", side_effect=PermissionError("blocked"))):
            with self.assertRaisesRegex(PermissionError, "blocked"):
                engine.launch_account("work", self.root / "ChatGPT.exe")
        self.assertEqual(main_auth.read_bytes(), previous)
        self.assertEqual(main_auth.stat().st_mtime_ns, previous_mtime)

    def test_new_profile_is_selected_even_when_shortcut_fails(self) -> None:
        engine.create_profile("old")
        state = main.State()
        state.refresh()
        state.checked.add("old")
        with (mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "ask", return_value="new"),
              mock.patch.object(main, "confirm", return_value=True),
              mock.patch.object(engine, "create_shortcut", side_effect=PermissionError("blocked")),
              mock.patch.object(main.sys, "stdout", new=StringIO()),
              mock.patch.object(main, "sign_in_profile") as sign_in):
            main.do_new(state)
        sign_in.assert_called_once_with("new", state)
        self.assertEqual(state.targets(), ["new"])
        self.assertIn("Shortcut failed", state.status)

    def test_only_zero_quits_the_main_menu(self) -> None:
        state = main.State()
        self.assertTrue(main._dispatch("ESC", state))
        self.assertFalse(main._dispatch("0", state))
        self.assertTrue(main._dispatch("q", state))

    def test_shared_switch_confirms_the_running_main_before_closing_it(self) -> None:
        home = self.root / "main-home"
        auth = home / "auth.json"
        original = self._codex_auth_fixture(auth, "personal", "initial")
        work = self._codex_auth_fixture(self.profiles / "Work" / ".codex" / "auth.json", "work", "initial")
        exe = self.root / "ChatGPT.exe"
        pids = [123]
        state = main.State()
        state.refresh()
        state.cursor = 1

        def close(pids_to_close):
            self.assertEqual(pids_to_close, [456])
            pids.clear()
            return 1

        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "find_app_exe", return_value=exe),
              mock.patch.object(engine, "running_profiles", return_value={}),
              mock.patch.object(engine, "default_slot_pids", side_effect=lambda: list(pids)),
              mock.patch.object(engine, "_kill_pids", side_effect=close) as kill,
              mock.patch.object(engine, "_record_launch") as record,
              mock.patch.object(engine, "launch_default_slot") as launch):
            main._dispatch("ENTER", state)
            self.assertIn(f"{main.YELLOW}Switching to 'Work' will close Main instance", "\n".join(main.render(state)))
            kill.assert_not_called()
            record.assert_not_called()
            self.assertEqual(auth.read_bytes(), original)

            self.assertTrue(main._dispatch("ESC", state))
            self.assertIsNone(state.pending_switch)
            main._dispatch("1", state)  # first press after cancellation
            kill.assert_not_called()
            pids[:] = [456]
            main._dispatch("1", state)  # different window now running
            kill.assert_not_called()
            main._dispatch("", state)  # ignored console event
            main._dispatch("ENTER", state)
        self.assertEqual(auth.read_bytes(), work)
        kill.assert_called_once_with([456])
        record.assert_called_once()
        launch.assert_called_once_with(exe)

    def test_confirmation_is_skipped_when_no_main_window_will_close(self) -> None:
        home = self.root / "main-home"
        auth = home / "auth.json"
        named = self.profiles / "Work"
        self._codex_auth_fixture(named / ".codex" / "auth.json", "work", "initial")
        exe = self.root / "ChatGPT.exe"
        for app, shared, current_user in ((engine.CLAUDE, False, "personal"),
                                          (engine.CODEX, False, "personal"),
                                          (engine.CODEX, True, "work"),
                                          (engine.CODEX, True, "personal")):
            with (self.subTest(app=app.key, shared=shared, current_user=current_user),
                  mock.patch.object(engine, "_current", app),
                  mock.patch.object(engine, "codex_main_home", return_value=home),
                  mock.patch.object(engine, "find_app_exe", return_value=exe),
                  mock.patch.object(engine, "running_profiles", return_value={}),
                  mock.patch.object(engine, "default_slot_pids",
                                    return_value=[123] if not shared or current_user == "work" else []),
                  mock.patch.object(engine, "_kill_pids", return_value=0),
                  mock.patch.object(engine, "_record_launch") as record,
                  mock.patch.object(engine, "launch_default_slot") as launch_main,
                  mock.patch.object(engine, "launch_profile") as launch_profile):
                self._codex_auth_fixture(auth, current_user, "initial")
                engine.STATE_FILE.write_text(json.dumps({"codex_shared_history": shared}))
                state = main.State()
                state.refresh()
                state.cursor = 1
                main._dispatch("ENTER", state)
                self.assertIsNone(state.pending_switch)
                self.assertEqual(launch_main.call_count, int(shared))
                self.assertEqual(launch_profile.call_count, int(not shared))
                record.assert_called_once()

    def test_launch_failure_is_displayed_without_crashing_ui(self) -> None:
        engine.create_profile("one")
        state = main.State()
        state.profiles = ["one"]
        state.cursor = 1
        with mock.patch.object(engine, "launch_profile", side_effect=FileNotFoundError("app updated")):
            main.do_launch(state)
        self.assertIn("app updated", state.status)

    def test_process_detection_preserves_spaces_in_profile_names(self) -> None:
        one, two = self.profiles / "Work account", self.profiles / "Personal account"
        commands = [
            (engine.CLAUDE, 1, "", f'claude.exe "--user-data-dir={one}"'),
            (engine.CLAUDE, 2, "", f'claude.exe --user-data-dir="{one}" --type=renderer'),
            (engine.CLAUDE, 3, "", f'claude.exe --user-data-dir "{two}"'),
        ]
        with mock.patch.object(engine, "_processes", return_value=commands):
            self.assertEqual(engine.running_profiles(), {"Work account": [1, 2], "Personal account": [3]})

    def test_launcher_displays_os_errors(self) -> None:
        launcher = runpy.run_path(str(engine.LAUNCHER))
        engine.create_profile("one")
        show_error = mock.Mock()
        with (mock.patch.object(engine, "initialize_runtime"),
              mock.patch.object(engine, "set_app"),
              mock.patch.object(engine, "launch_profile", side_effect=PermissionError("denied")),
              mock.patch.dict(launcher["main"].__globals__, {"_error": show_error}),
              mock.patch.object(main.sys, "argv", ["launcher.pyw", "one"])):
            self.assertEqual(launcher["main"](), 1)
        show_error.assert_called_once_with("denied", "Claude Multi-Instance")

    def test_stale_claude_shortcut_does_not_recreate_profile(self) -> None:
        launcher = runpy.run_path(str(engine.LAUNCHER))
        show_error = mock.Mock()
        with (mock.patch.object(engine, "initialize_runtime"),
              mock.patch.object(engine, "set_app"),
              mock.patch.object(engine, "launch_profile") as launch,
              mock.patch.dict(launcher["main"].__globals__, {"_error": show_error}),
              mock.patch.object(main.sys, "argv", ["launcher.pyw", "--app=claude", "deleted"])):
            self.assertEqual(launcher["main"](), 1)
        launch.assert_not_called()
        show_error.assert_called_once_with("Claude profile 'deleted' does not exist.", "Claude Multi-Instance")

    def test_codex_desktop_shortcut_confirms_before_closing_main(self) -> None:
        launcher = runpy.run_path(str(engine.LAUNCHER))
        home = self.root / "main-home"
        auth = home / "auth.json"
        original = self._codex_auth_fixture(auth, "personal", "original")
        target = self.root / engine.CODEX.profiles_dirname / "Work" / ".codex" / "auth.json"
        work = self._codex_auth_fixture(target, "work", "saved")
        pids = [123]

        def close(pids_to_close):
            self.assertEqual(pids_to_close, [456])
            pids.clear()
            return 1

        def replace_main(*_args):
            pids[:] = [456]
            return 1

        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "initialize_runtime"),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "find_app_exe", return_value=self.root / "ChatGPT.exe"),
              mock.patch.object(engine, "running_profiles", return_value={}),
              mock.patch.object(engine, "default_slot_pids", side_effect=lambda: list(pids)),
              mock.patch.object(engine, "_kill_pids", side_effect=close) as kill,
              mock.patch.object(engine, "_record_launch") as record,
              mock.patch.object(engine, "launch_default_slot") as launch,
              mock.patch.object(main.windll.user32, "MessageBoxW", return_value=2) as dialog,
              mock.patch.dict(launcher["main"].__globals__, {"_error": mock.Mock()}),
              mock.patch.object(main.sys, "argv", ["launcher.pyw", "--app=codex", "Work"])):
            self.assertEqual(launcher["main"](), 0)
            self.assertIn("Switch to 'Work'", dialog.call_args.args[1])
            self.assertEqual(dialog.call_args.args[3] & 0x100, 0x100)  # Cancel is the default.
            kill.assert_not_called()
            launch.assert_not_called()
            record.assert_not_called()
            self.assertEqual(auth.read_bytes(), original)

            dialog.side_effect = replace_main
            self.assertEqual(launcher["main"](), 1)
            kill.assert_not_called()
            self.assertEqual(auth.read_bytes(), original)

            dialog.side_effect = None
            dialog.return_value = 1
            self.assertEqual(launcher["main"](), 0)
            kill.assert_called_once_with([456])
            launch.assert_called_once()
            record.assert_called_once()
            self.assertEqual(auth.read_bytes(), work)

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

    def test_main_row_creates_an_absolute_manager_shortcut(self) -> None:
        project = self.root / "Source with spaces"
        for frozen in (False, True):
            with (self.subTest(frozen=frozen),
                  mock.patch.object(engine, "PROJECT_DIR", project),
                  mock.patch.object(engine.sys, "frozen", frozen, create=True),
                  mock.patch.object(engine, "desktop_dir", return_value=self.root),
                  mock.patch.object(engine, "find_app_exe", return_value=None),
                  mock.patch.object(engine.subprocess, "run") as run):
                state = main.State()
                main._dispatch("5", state)
                self.assertIn("Created AI Multi-Instance shortcut", state.status)
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["SC_LNK"], str(self.root / "AI Multi-Instance.lnk"))
                self.assertEqual(env["SC_TARGET"], engine.sys.executable)
                self.assertEqual(env["SC_WORK"], str(project))
                self.assertEqual(env["SC_ARGS"], "" if frozen else f'"{project / "main.py"}"')

                manager_actions = next(line for line in main.render(state) if "Create menu link" in line)
                link = engine.manager_shortcut_path()
                link.touch()
                self.assertTrue(any("Delete menu link" in line for line in main.render(state)))
                main._dispatch("5", state)
                self.assertIn("Deleted AI Multi-Instance shortcut", state.status)
                self.assertFalse(link.exists())

                engine.create_profile("Work")
                state.refresh()
                state.cursor = 1
                profile_actions = next(line for line in main.render(state) if "Create shortcut" in line)
                self.assertEqual(manager_actions.index("Usage"), profile_actions.index("Usage"))
                main._dispatch("5", state)
                self.assertIn("Created shortcut for Work", state.status)
                link = self.root / engine.shortcut_filename("Work")
                link.touch()
                self.assertTrue(any("Delete shortcut" in line for line in main.render(state)))
                main._dispatch("5", state)
                self.assertIn("Deleted shortcut for Work", state.status)
                self.assertFalse(link.exists())

    def test_rename_keeps_existing_shortcut_in_both_apps(self) -> None:
        cases = ((engine.CLAUDE, "Work", "work"), (engine.CODEX, "Office", "Équipe $1"))
        for app, old, new in cases:
            with (self.subTest(app=app.key),
                  mock.patch.object(engine, "_current", app),
                  mock.patch.object(engine, "desktop_dir", return_value=self.root),
                  mock.patch.object(engine, "_notify_shortcut_rename", wraps=engine._notify_shortcut_rename) as notify):
                engine.create_profile(old)
                old_link = engine.create_shortcut(old, exe=Path(engine.sys.executable))
                original_id = old_link.stat().st_ino
                new_link = self.root / engine.shortcut_filename(new)
                engine.rename_profile(old, new)

                self.assertEqual(new_link.stat().st_ino, original_id)
                self.assertIn(new_link.name, {file.name for file in self.root.glob("*.lnk")})
                self.assertNotIn(old_link.name, {file.name for file in self.root.glob("*.lnk")})
                self.assertEqual([p.name for p in engine.list_profiles() if p.name == new], [new])
                notify.assert_called_once()
                self.assertEqual(tuple(str(path) for path in notify.call_args.args),
                                 (str(old_link), str(new_link)))
                output = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
                     "$link=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TEST_LINK);"
                     "$link.Arguments; $link.IconLocation"],
                    env=dict(engine.os.environ, TEST_LINK=str(new_link)),
                    check=True, capture_output=True, encoding="utf-8", timeout=10,
                ).stdout.splitlines()
                self.assertEqual(output, [engine._launcher_invocation(new)[1], f"{engine.sys.executable},0"])

    def test_rename_handles_profiles_without_a_shortcut_and_desktop_collisions(self) -> None:
        old = engine.create_profile("old")
        occupied = self.root / engine.shortcut_filename("new")
        occupied.write_bytes(b"existing desktop item")
        with mock.patch.object(engine, "desktop_dir", return_value=self.root):
            with self.assertRaisesRegex(FileExistsError, "desktop item"):
                engine.rename_profile("old", "new")
            self.assertTrue(old.is_dir())
            self.assertEqual(occupied.read_bytes(), b"existing desktop item")
            engine.rename_profile("old", "unused")
        self.assertFalse(old.exists())
        self.assertTrue((self.profiles / "unused").is_dir())
        self.assertFalse((self.root / engine.shortcut_filename("unused")).exists())

    def test_failed_shortcut_rename_restores_profile_and_link(self) -> None:
        old = engine.create_profile("old")
        link = self.root / engine.shortcut_filename("old")
        link.write_bytes(b"original")

        def fail_to_save(path, *args, **kwargs):
            path.write_bytes(b"interrupted save")
            raise subprocess.CalledProcessError(1, "powershell")

        with (mock.patch.object(engine, "desktop_dir", return_value=self.root),
              mock.patch.object(engine, "_save_shortcut", side_effect=fail_to_save),
              mock.patch.object(engine, "_notify_shortcut_rename") as notify):
            with self.assertRaises(subprocess.CalledProcessError):
                engine.rename_profile("old", "new")
        self.assertTrue(old.is_dir())
        self.assertFalse((self.profiles / "new").exists())
        self.assertEqual(link.read_bytes(), b"original")
        self.assertFalse((self.root / engine.shortcut_filename("new")).exists())
        notify.assert_not_called()

    def test_corrupt_state_and_cache_are_ignored(self) -> None:
        engine.STATE_FILE.write_text("[]")
        usage._CACHE_FILE.write_text("null")
        self.assertEqual(engine._load_state(), {})
        self.assertEqual(usage._load_cache(), {})

    def test_runtime_layout_moves_legacy_files_without_overwriting_current_state(self) -> None:
        runtime = self.root / "data"
        state = runtime / "state.json"
        legacy_state = self.root / "state.json"
        legacy_state.write_text('{"selected_app":"codex","launch_count":109}', encoding="utf-8")
        (self.root / "state.lock").touch()
        (self.root / "usage_cache.json").write_bytes(b"encrypted cache fixture")
        (self.root / "launch-count.db").write_bytes(b"legacy database fixture")
        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "RUNTIME_DIR", runtime),
              mock.patch.object(engine, "STATE_FILE", state)):
            engine.initialize_runtime()
            self.assertEqual(engine.current_app(), engine.CODEX)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["launch_count"], 109)
            self.assertEqual((runtime / "usage_cache.json").read_bytes(), b"encrypted cache fixture")
            self.assertEqual((runtime / "launch-count.db").read_bytes(), b"legacy database fixture")
            self.assertFalse(legacy_state.exists())
            self.assertFalse((self.root / "state.lock").exists())
            legacy_state.write_text('{"selected_app":"claude"}', encoding="utf-8")
            engine.initialize_runtime()
            self.assertEqual(engine.current_app(), engine.CODEX)
            self.assertTrue(legacy_state.exists())

    def test_menu_does_not_delete_a_running_profile(self) -> None:
        engine.create_profile("work")
        state = main.State()
        state.refresh()
        state.cursor = 1
        with (mock.patch.object(engine, "running_profiles", return_value={"WORK": [1]}),
              mock.patch.object(engine, "delete_profile") as delete,
              mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "ask", return_value="yes")):
            main.do_delete(state)
        delete.assert_not_called()
        self.assertIn("Close 'work' first", state.status)

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

    def test_claude_session_cache_rejects_a_replaced_cookie_store(self) -> None:
        cookies = self.root / "Network" / "Cookies"
        cookies.parent.mkdir()
        cookies.write_bytes(b"old cookies")
        profile = str(self.root)
        with (mock.patch.object(usage, "_dpapi_protect", side_effect=lambda data: data),
              mock.patch.object(usage, "_dpapi_unprotect", side_effect=lambda data: data)):
            usage._cache_put_session(profile, "old-session", usage._cookie_file_id(profile))
            self.assertEqual(usage._cache_get_session(profile), "old-session")
            replacement = cookies.with_name("replacement")
            replacement.write_bytes(b"new cookies")
            replacement.replace(cookies)
            self.assertIsNone(usage._cache_get_session(profile))

    def test_bad_account_does_not_break_other_usage_results(self) -> None:
        bad = self.root / "bad" / ".codex"
        bad.mkdir(parents=True)
        (bad / "auth.json").write_text('[]')
        results = usage.fetch_all([("bad", bad.parent), ("missing", self.root / "missing")], "codex")
        self.assertEqual([r.name for r in results], ["bad", "missing"])
        self.assertTrue(all(r.error for r in results))

    def test_background_usage_request_does_not_block_exit(self) -> None:
        script = """
from pathlib import Path
from threading import Event, Thread
import usage

started = Event()
blocked = Event()

def fetch(*args):
    started.set()
    blocked.wait()

usage._fetch_codex = fetch
Thread(target=usage.fetch_all, args=([("work", Path("."))], "codex"), daemon=True).start()
assert started.wait(3)
"""
        subprocess.run([engine.sys.executable, "-B", "-c", script], cwd=engine.PROJECT_DIR,
                       capture_output=True, check=True, timeout=10)

    def test_recreated_profile_ignores_old_and_in_flight_usage(self) -> None:
        app, name = engine.CLAUDE, "work"
        key = (app, name)
        state = main.State()
        state.profiles = [name]
        state.cursor = 1
        engine.create_profile(name)
        previous = usage.ProfileUsage(name, ok=True, rows=[("5h", 80, "")])
        refresh = Event()
        with (mock.patch.object(main, "_menu_usage", {key: (0, previous)}),
              mock.patch.object(main, "_refresh_usage", {app: refresh}),
              mock.patch.object(main, "_check_profiles_closed"),
              mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "ask", side_effect=["yes", name]),
              mock.patch.object(main, "confirm", return_value=False),
              mock.patch.object(engine, "desktop_dir", return_value=self.root),
              mock.patch.object(engine, "create_shortcut")):
            main.do_delete(state)
            self.assertFalse((self.profiles / name).exists())
            self.assertIsNone(main._menu_usage[key][1])
            refresh.clear()
            main.do_new(state)
            self.assertTrue((self.profiles / name).is_dir())
            self.assertTrue(refresh.is_set())
            main._publish_usage(app, self.profiles, None, {name: previous}, 0)
            self.assertIsNone(main._menu_usage[key][1])
            current = usage.ProfileUsage(name, error="not signed in")
            main._publish_usage(app, self.profiles, None, {name: current}, main.time.monotonic())
            self.assertEqual(main._menu_usage[key][1], current)

    def test_claude_login_only_invalidates_usage_on_success(self) -> None:
        app, name = engine.CLAUDE, "work"
        key = (app, name)
        previous = usage.ProfileUsage(name, ok=True)
        refresh = Event()
        with (mock.patch.object(main, "_menu_usage", {key: (0, previous)}),
              mock.patch.object(main, "_refresh_usage", {app: refresh}),
              mock.patch.object(main, "_refresh_instances", Event()),
              mock.patch.object(main, "login_flow", side_effect=[False, True])):
            state = main.State()
            main.sign_in_profile(name, state)
            self.assertEqual(main._menu_usage[key][1], previous)
            self.assertFalse(refresh.is_set())
            main.sign_in_profile(name, state)
            self.assertIsNone(main._menu_usage[key][1])
            self.assertTrue(refresh.is_set())

    def test_usage_skips_invalid_quota_rows(self) -> None:
        self._codex_auth_fixture(self.root / ".codex" / "auth.json", "work", "saved")
        codex_payload = {
            "email": 123,
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 18000, "used_percent": "20", "reset_at": {}},
                "secondary_window": {"limit_window_seconds": 604800, "used_percent": "NaN"},
            },
        }
        with mock.patch.object(usage, "_codex_get", return_value=codex_payload):
            codex = usage._fetch_codex("work", self.root)
        self.assertTrue(codex.ok)
        self.assertEqual(codex.account, "—")
        self.assertEqual(codex.rows, [("5h", 20.0, "")])

        claude_payload = {
            "five_hour": {"utilization": 75, "resets_at": 123},
            "seven_day": {"utilization": {}},
            "limits": [{"kind": "weekly_scoped", "scope": {"model": 123}, "percent": 50}],
        }
        with (mock.patch.object(usage, "_read_session", return_value=("live", "test-token", None)),
              mock.patch.object(usage, "_cache_put_session"),
              mock.patch.object(usage, "_api_get", side_effect=[[{"id": "test", "name": 123}], claude_payload])):
            claude = usage._fetch_claude("work", self.root)
        self.assertTrue(claude.ok)
        self.assertEqual(claude.account, "—")
        self.assertEqual(claude.rows, [("5h", 75.0, "")])

    def test_codex_restores_same_account_quotas_before_background_fetch(self) -> None:
        app, profile = engine.CODEX, "Work"
        profiles = self.root / app.profiles_dirname
        auth = profiles / profile / ".codex" / "auth.json"
        home = self.root / "default" / ".codex"
        self._codex_auth_fixture(auth, "work", "saved")
        self._codex_auth_fixture(home / "auth.json", "work", "rotated")
        ready, scan = Event(), Event()
        scan.set()

        expired = {"limit_window_seconds": 604800, "used_percent": 50, "reset_at": 1}
        payload = {"rate_limit": {"primary_window": {"limit_window_seconds": 18000, "used_percent": 25},
                                  "secondary_window": expired}}
        with mock.patch.object(usage, "_codex_get", return_value=payload):
            usage.fetch_all([(profile, profiles / profile)], app.key)

        fetch_live = usage.fetch_as_ready

        def verify_first_paint(targets, app_key):
            self.assertTrue(ready.is_set())
            state = main.State()
            state.profiles = [profile]
            screen = next(line for line in main.render(state) if "Main instance" in line)
            self.assertIn("75%", screen)
            self.assertNotIn("(7d)", screen)
            self.assertEqual(targets, [(f"Main instance · {profile}", home.parent)])
            yield from fetch_live(targets, app_key)

        payload["rate_limit"]["primary_window"]["used_percent"] = 10
        with (mock.patch.object(engine, "_current", app),
              mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(main, "_instances", {app: (frozenset(), True, profile)}),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main, "_refresh_usage", {app: Event()}),
              mock.patch.object(main.time, "sleep", side_effect=StopIteration),
              mock.patch.object(usage, "fetch_as_ready", side_effect=verify_first_paint),
              mock.patch.object(usage, "_codex_get", return_value=payload) as get_usage):
            with self.assertRaises(StopIteration):
                main._usage_probe(app, scan, ready)
            self.assertEqual(main._menu_usage[(app, profile)][1].rows[0], ("5h", 10, ""))
        get_usage.assert_called_once()
        self._codex_auth_fixture(auth, "different-user", "fresh")
        self.assertEqual(usage.load_quotas(app.key, {profile: (profile, profiles / profile)}), {})

    def test_claude_quota_cache_keeps_verified_values_until_auth_is_refused(self) -> None:
        app = engine.CLAUDE
        target = ("Work", self.root)
        targets = {"Work": target}
        usage._write_cache({"existing": {"session": "encrypted fixture"}})
        with (mock.patch.object(usage, "_read_session", return_value=("live", "current-session", None)) as session,
              mock.patch.object(usage, "_cache_put_session"),
              mock.patch.object(usage, "_api_get", side_effect=[
                  [{"id": "test"}], {"five_hour": {"utilization": 20}},
                  OSError("offline"), usage.urllib.error.HTTPError("https://example.invalid", 403, "", {}, None),
              ])):
            usage.fetch_all([target], app.key)
            restored = usage.load_quotas(app.key, targets)
            self.assertEqual(restored["Work"].rows, [("5h", 20, "")])
            with mock.patch.object(main, "_menu_usage", {(app, "Work"): (1, restored["Work"])}):
                offline = usage.fetch_all([target], app.key)[0]
                main._publish_usage(app, self.root, None, {"Work": offline}, 2)
                self.assertTrue(main._menu_usage[(app, "Work")][1].ok)
                self.assertEqual(usage.load_quotas(app.key, targets), restored)
                session.return_value = ("live", "replacement-session", None)
                self.assertEqual(usage.load_quotas(app.key, targets), {})
                session.return_value = ("live", "current-session", None)
                refused = usage.fetch_all([target], app.key)[0]
                main._publish_usage(app, self.root, None, {"Work": refused}, 3)
                self.assertFalse(main._menu_usage[(app, "Work")][1].ok)
            self.assertEqual(usage.load_quotas(app.key, targets), {})
        cache = usage._CACHE_FILE.read_text(encoding="utf-8")
        self.assertIn("encrypted fixture", cache)
        self.assertNotIn("current-session", cache)

    def test_usage_credentials_are_not_forwarded_on_redirects(self) -> None:
        with mock.patch.object(usage.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            usage._api_get(usage._ORGS_URL, "sessionKey=test-token")
            usage._codex_get(usage._CODEX_USAGE_URL, "test-token", "test-account")
        for call, names in zip(urlopen.call_args_list, [("Cookie",), ("Authorization", "Chatgpt-account-id")]):
            request = call.args[0]
            redirected = usage.urllib.request.HTTPRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "https://example.invalid",
            )
            for name in names:
                self.assertTrue(request.has_header(name))
                self.assertFalse(redirected.has_header(name))

    def test_usage_reload_shows_main_profile_and_percentage_remaining(self) -> None:
        state = main.State()
        state.profiles = ["Work account", "Personal account"]

        def fetch(targets, app):
            self.assertEqual(app, "codex")
            return [usage.ProfileUsage(name, ok=True, account="test@example.com",
                                       rows=[("5h", 100, ""), ("7d", 20, "")])
                    for name, _ in targets]

        home = self.root / ".codex"
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "main_codex_profile",
                                side_effect=[name for name in state.profiles for _ in range(2)]),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main.time, "monotonic", side_effect=[10, 20]),
              mock.patch.object(usage, "fetch_all", side_effect=fetch) as load,
              mock.patch.object(main, "getkey", side_effect=["r", "ENTER"]),
              mock.patch.object(main, "ask") as ask,
              mock.patch.object(main, "_draw") as draw):
            main.action_usage(state)
            app = engine.CODEX
            key = (app, "Personal account")
            self.assertEqual(main._menu_usage[key][0], 20)
            self.assertEqual(main._menu_usage[key][1].name, "Main instance · Personal account")
            main._publish_usage(app, self.root, None,
                                {"Personal account": usage.ProfileUsage("Stale")}, 15)
            self.assertEqual(main._menu_usage[key][1].rows[0], ("5h", 100, ""))

        ask.assert_not_called()
        self.assertEqual(load.call_count, 2)
        self.assertEqual(load.call_args_list[0].args[0], [
            ("Main instance · Work account", home.parent),
            ("Personal account", engine.PROFILES_DIR / "Personal account"),
        ])
        self.assertEqual(load.call_args_list[1].args[0], [
            ("Main instance · Personal account", home.parent),
            ("Work account", engine.PROFILES_DIR / "Work account"),
        ])
        rendered = "\n".join(draw.call_args.args[0])
        self.assertIn("Main instance · Personal account", rendered)
        self.assertIn(f"{main.RED}  0%{main.RESET} left", rendered)
        self.assertIn(f"{main.GREEN} 80%{main.RESET} left", rendered)
        self.assertIn(f"{main.GREY}·{main.RESET}", rendered)
        self.assertIn("\n  5h ", rendered)
        self.assertIn("\n  7d ", rendered)

    def test_usage_can_be_left_while_request_is_pending_in_both_apps(self) -> None:
        for app in (engine.CLAUDE, engine.CODEX):
            state = main.State()
            state.profiles = ["Work"]
            started, release, finished = Event(), Event(), Event()
            target = ("Work", self.profiles / "Work")
            cached = usage.ProfileUsage("Work", ok=True, rows=[("5h", 25, "")])

            def fetch(targets, app_key):
                started.set()
                try:
                    release.wait(2)
                    return [usage.ProfileUsage("Work", ok=True, rows=[("5h", 5, "")])]
                finally:
                    finished.set()

            with (self.subTest(app=app.key),
                  mock.patch.object(engine, "_current", app),
                  mock.patch.object(engine, "main_codex_profile", return_value=None),
                  mock.patch.object(main, "_usage_targets", return_value={"Work": target}),
                  mock.patch.object(main, "_menu_usage", {(app, "Work"): (0, cached)}),
                  mock.patch.object(usage, "fetch_all", side_effect=fetch) as load,
                  mock.patch.object(main.msvcrt, "kbhit", side_effect=started.is_set),
                  mock.patch.object(main, "getkey", return_value="ESC"),
                  mock.patch.object(main, "_draw") as draw):
                try:
                    main.action_usage(state)
                    self.assertFalse(finished.is_set())
                    draw.assert_called_once()
                    frame = "\n".join(draw.call_args.args[0])
                    self.assertIn("fetching…", frame)
                    self.assertNotIn("%", frame)
                    load.assert_called_once_with([target], app.key)
                finally:
                    release.set()
                    self.assertTrue(finished.wait(3))

    def test_usage_reports_live_request_errors_without_cached_quotas(self) -> None:
        for app in (engine.CLAUDE, engine.CODEX):
            state = main.State()
            state.profiles = ["Work"]
            target = ("Work", self.profiles / "Work")
            cached = usage.ProfileUsage("Work", ok=True, rows=[("5h", 25, "")])
            offline = usage.ProfileUsage("Work", error="network unavailable", temporary_error=True)
            with (self.subTest(app=app.key),
                  mock.patch.object(engine, "_current", app),
                  mock.patch.object(engine, "main_codex_profile", return_value=None),
                  mock.patch.object(main, "_usage_targets", return_value={"Work": target}),
                  mock.patch.object(main, "_menu_usage", {(app, "Work"): (0, cached)}),
                  mock.patch.object(usage, "fetch_all", return_value=[offline]) as load,
                  mock.patch.object(main.msvcrt, "kbhit", return_value=False),
                  mock.patch.object(main, "getkey", return_value="ENTER"),
                  mock.patch.object(main, "_draw") as draw):
                main.action_usage(state)
                self.assertEqual(draw.call_count, 2)
                self.assertIn("fetching…", "\n".join(draw.call_args_list[0].args[0]))
                frame = "\n".join(draw.call_args.args[0])
                self.assertIn("network unavailable", frame)
                self.assertNotIn("%", frame)
                load.assert_called_once_with([target], app.key)

    def test_usage_and_main_share_quota_order_after_reload(self) -> None:
        app = engine.CODEX
        state = main.State()
        state.profiles = ["Alpha", "Beta", "Work"]
        state.sort_by_usage = True
        quotas = iter([{"Alpha": 80, "Beta": 20}, {"Alpha": 10, "Beta": 90}])

        def fetch(targets, _app):
            used = next(quotas)
            return [usage.ProfileUsage(name, ok=True, rows=[("5h", used.get(name, 50), "")])
                    for name, _ in targets]

        with (mock.patch.object(engine, "_current", app),
              mock.patch.object(engine, "main_codex_profile", return_value="Work"),
              mock.patch.object(engine, "shared_codex_history", return_value=True),
              mock.patch.object(main, "_instances", {app: (frozenset(), True, "Work")}),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main, "_draw"),
              mock.patch.object(main, "getkey", side_effect=["r", "ENTER"]),
              mock.patch.object(usage, "fetch_all", side_effect=fetch),
              mock.patch.object(main, "render_usage", wraps=main.render_usage) as render_usage):
            main.action_usage(state)
            main_screen = "\n".join(main.render(state))

        self.assertEqual([[item.name for item in call.args[0]] for call in render_usage.call_args_list],
                         [["Main instance · Work", "Beta", "Alpha"],
                          ["Main instance · Work", "Alpha", "Beta"]])
        self.assertLess(main_screen.index("Main instance"), main_screen.index("Alpha"))
        self.assertLess(main_screen.index("Alpha"), main_screen.index("Beta"))
        self.assertEqual(main_screen.count("Work"), 1)

    def test_sort_is_shared_and_saved_from_usage_and_main(self) -> None:
        engine.STATE_FILE.write_text('{"launch_count": 42}', encoding="utf-8")
        state = main.State()
        self.assertFalse(state.sort_by_usage)
        state.profiles = ["Alpha", "Beta"]
        results = [
            usage.ProfileUsage("Main instance", ok=True),
            usage.ProfileUsage("Alpha", ok=True, rows=[("5h", 80, "")]),
            usage.ProfileUsage("Beta", ok=True, rows=[("5h", 20, "")]),
        ]
        with (mock.patch.object(engine, "default_slot_dir", return_value=self.root),
              mock.patch.object(engine, "list_profiles", return_value=[self.root / name for name in state.profiles]),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main, "_draw") as draw,
              mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "getkey", side_effect=["t", "t", "t", "ENTER"]),
              mock.patch.object(usage, "fetch_all", return_value=results) as fetch):
            main._dispatch("8", state)
            main_screen = "\n".join(main.render(state))
            reopened = main.State()
            self.assertTrue(reopened.sort_by_usage)
            main._dispatch("t", reopened)
            self.assertFalse(main.State().sort_by_usage)

        fetch.assert_called_once()
        self.assertEqual(draw.call_count, 5)
        for call, sort_by_usage in zip(draw.call_args_list[1:], (False, True, False, True)):
            frame = "\n".join(call.args[0])
            self.assertIn(f"Sort ({'quota' if sort_by_usage else 'name'})", frame)
            first, second = ("Beta", "Alpha") if sort_by_usage else ("Alpha", "Beta")
            self.assertLess(frame.index("Main instance"), frame.index(first))
            self.assertLess(frame.index(first), frame.index(second))
        self.assertTrue(state.sort_by_usage)
        self.assertLess(main_screen.index("Beta"), main_screen.index("Alpha"))
        self.assertIn("T Sort (quota)", main_screen)
        self.assertEqual(json.loads(engine.STATE_FILE.read_text(encoding="utf-8")),
                         {"launch_count": 42, "sort_by_usage": False})

    def test_usage_and_donation_menu_keys(self) -> None:
        state = main.State()
        with (mock.patch.object(main, "action_usage") as show_usage,
              mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "_draw") as draw,
              mock.patch.object(main, "getkey", side_effect=["l", "k", "ENTER"]),
              mock.patch.object(main.subprocess, "run") as copy_address,
              mock.patch.object(main.os, "startfile") as open_url):
            self.assertTrue(main._dispatch("8", state))
            show_usage.assert_called_once_with(state)
            self.assertTrue(main._dispatch("9", state))
        donation = "\n".join(draw.call_args.args[0])
        self.assertIn("LXFPr2dZj9ymAF7VbggkCXth8nmac36wbE", donation)
        self.assertIn("https://ko-fi.com/zoltrak", donation)
        copy_address.assert_called_once_with(["clip.exe"], input=main.LTC_ADDRESS, text=True, check=True,
                                             creationflags=engine.NO_WINDOW, timeout=5)
        open_url.assert_called_once_with(main.KOFI_URL)
        self.assertIn("LTC address copied.", "\n".join(draw.call_args_list[1].args[0]))

    def test_menu_shows_app_keys_and_distinguishes_the_main_profile(self) -> None:
        state = main.State()
        state.profiles = ["Work account", "Personal"]
        app = engine.CODEX
        running = frozenset({"Personal"})
        shared = usage.ProfileUsage("Main instance", ok=True, rows=[("7d", 30, ""), ("5h", 80, "")])
        results = {(app, "Work account"): (0, shared),
                   (app, "Personal"): (0, usage.ProfileUsage("Personal", error="forbidden"))}
        with (mock.patch.object(engine, "_current", app),
              mock.patch.object(engine, "shared_codex_history", return_value=True) as shared_history,
              mock.patch.object(main, "_app_versions", {app: "26.1111.2228.0"}),
              mock.patch.object(main, "_menu_usage", results),
              mock.patch.object(main, "_instances", {app: (running, True, "Work account")})):
            lines = main.render(state)
            self.assertIn(f"v{engine.MANAGER_VERSION}", lines[1])
            self.assertNotIn("26.1111.2228.0", lines[1])
            self.assertIn("Codex client v26.1111.2228.0", lines[-1])
            header = next(line for line in lines if "Switch to" in line)
            main_row = next(line for line in lines if "Main instance" in line)
            personal_row = next(line for line in lines if "Personal " in line)
            self.assertIn(f"{main.CODEX_BLUE}{main.BOLD}C{main.RESET} Switch to Claude", header)
            self.assertNotIn("Codex", header)
            self.assertIn("Change mode", header)
            self.assertIn(f"{main.GREEN}●{main.RESET}", main_row)
            self.assertNotIn("running", main_row)
            self.assertIn(f"Main instance{main.RESET}{main.MINT_GREEN} (Work account){main.RESET}", main_row)
            self.assertEqual([line for line in lines if "Work account" in line], [main_row])
            self.assertEqual(lines[lines.index(main_row) + 1], personal_row)
            self.assertEqual(lines[lines.index(personal_row) + 1:lines.index(personal_row) + 3],
                             ["", f"  {main.GREY}{main._SEP}{main.RESET}"])
            self.assertTrue(main_row.endswith(f"{main.GREEN}●{main.RESET} {main.GREEN}✓{main.RESET} "
                                              f"{main.YELLOW}20%{main.RESET} {main.GREY}(5h){main.RESET} "
                                              f"{main.GREEN}70%{main.RESET} {main.GREY}(7d){main.RESET}"))
            self.assertTrue(personal_row.endswith(f"{main.GREEN}●{main.RESET} {main.GREY}✓{main.RESET}"))
            main._instances[app] = (running, False, "Work account")
            lines = main.render(state)
            idle_main = next(line for line in lines if "Main instance" in line)
            self.assertEqual([line for line in lines if "Work account" in line], [idle_main])
            self.assertIn(f"{main.GREY}○{main.RESET}", idle_main)

            shared_history.return_value = False
            lines = main.render(state)
            idle_main = next(line for line in lines if "Main instance" in line)
            self.assertNotIn("Work account", idle_main)
            self.assertTrue(idle_main.endswith(f"{main.GREY}○{main.RESET}"))
            self.assertTrue(any("Work account " in line for line in lines))
            with mock.patch.object(engine, "_current", engine.CLAUDE):
                header = next(line for line in main.render(state) if "Switch to" in line)
                self.assertIn(f"{main.ORANGE}{main.BOLD}C{main.RESET} Switch to Codex", header)
                self.assertNotIn("Claude", header)

    def test_first_menu_frame_after_switch_uses_both_apps_from_one_scan(self) -> None:
        claude, codex = engine.CLAUDE, engine.CODEX
        claude_profile = self.root / claude.profiles_dirname / "Claude work"
        codex_profile = self.root / codex.profiles_dirname / "Codex work"
        claude_profile.mkdir(parents=True)
        codex_profile.mkdir(parents=True)
        codex_exe = self.root / f"{codex.package_prefix}1" / "app" / codex.exe_name
        output = (f'{claude.exe_name.upper()}|12||claude.exe --user-data-dir="{claude_profile}"\n'
                  f'{codex.exe_name}|34|{codex_exe}|"{codex_exe}"\n')
        result = usage.ProfileUsage(codex_profile.name, ok=True, rows=[("5h", 25, "")])
        versions = {claude: "3.1623.1.0", codex: "26.1111.2228.0"}
        state = main.State()
        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "main_codex_profile", return_value=codex_profile.name),
              mock.patch.object(engine, "shared_codex_history", side_effect=lambda: engine.current_app() is codex),
              mock.patch.object(engine.subprocess, "run", return_value=mock.Mock(stdout=output)) as scan,
              mock.patch.object(main, "_instances", {}),
              mock.patch.object(main, "_app_versions", versions),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main, "_refresh_usage", {codex: Event()}),
              mock.patch.object(main, "_refresh_instances") as refresh):
            engine.set_app(claude, persist=False)
            state.refresh()
            scan_ready = Event()
            refresh.wait.side_effect = StopIteration
            with self.assertRaises(StopIteration):
                main._running_probe(scan_ready)
            with (mock.patch.object(usage, "fetch_as_ready", return_value=[(0, result)]) as fetch,
                  mock.patch.object(main.time, "sleep", side_effect=[None, StopIteration])):
                with self.assertRaises(StopIteration):
                    main._usage_probe(codex, scan_ready, None)
            fetch.assert_called_once()
            claude_frame = main.render(state)
            self.assertTrue(any(claude_profile.name in line and "●" in line for line in claude_frame))
            self.assertIn("Claude client v3.1623.1.0", claude_frame[-1])

            for _ in range(2):
                main._dispatch("c", state)
                codex_frame = main.render(state)
                main_row = next(line for line in codex_frame if "Main instance" in line)
                self.assertIn(f" ({codex_profile.name})", main_row)
                self.assertIn("●", main_row)
                self.assertIn(f"{main.GREEN}✓", main_row)
                self.assertIn("75%", main_row)
                self.assertIn("Codex client v26.1111.2228.0", codex_frame[-1])
                self.assertEqual([line for line in codex_frame if codex_profile.name in line], [main_row])
                main._dispatch("c", state)
                self.assertEqual(main.render(state), claude_frame)

        scan.assert_called_once()
        self.assertIn("Name='claude.exe' OR Name='ChatGPT.exe'", scan.call_args.args[0][-1])

    def test_startup_draws_main_profile_and_quotas_on_first_frame(self) -> None:
        app, profile = engine.CODEX, "Work"
        result = usage.ProfileUsage(profile, ok=True, rows=[("5h", 25, ""), ("7d", 10, "")])
        engine.save_client_version(app, "26.1111.2228.0")

        def scan(ready):
            main._instances = {app: (frozenset(), True, profile)}
            ready.set()

        def load_usage(app, scan_ready, usage_ready):
            if usage_ready is None:
                return
            scan_ready.wait()
            main._menu_usage = {(app, profile): (0, result)}
            usage_ready.set()

        with (mock.patch.object(engine, "_current", app),
              mock.patch.object(engine, "list_profiles", return_value=[self.root / profile]),
              mock.patch.object(engine, "shared_codex_history", return_value=True),
              mock.patch.object(engine, "check_update", return_value=None),
              mock.patch.object(main, "_instances", {}),
              mock.patch.object(main, "_app_versions", {}),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main, "_running_probe", side_effect=scan),
              mock.patch.object(main, "_version_probe"),
              mock.patch.object(main, "_usage_probe", side_effect=load_usage),
              mock.patch.object(main, "_show_launch_milestone"),
              mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "_hide_cursor"),
              mock.patch.object(main.msvcrt, "kbhit", return_value=True),
              mock.patch.object(main, "getkey", return_value="0"),
              mock.patch.object(main, "_draw") as draw):
            main.loop()
        draw.assert_called_once()
        lines = draw.call_args.args[0]
        row = next(line for line in lines if "Main instance" in line)
        self.assertIn(f" ({profile})", row)
        self.assertIn("●", row)
        self.assertIn("75%", row)
        self.assertIn("90%", row)
        self.assertNotIn("…", row)
        self.assertEqual(sum(profile in line for line in lines), 1)
        self.assertIn("Codex client v26.1111.2228.0", lines[-1])

    def test_version_probe_updates_only_the_checked_client(self) -> None:
        codex, claude = engine.CODEX, engine.CLAUDE
        engine.save_client_version(codex, "26.1111.2228.0")
        engine.save_client_version(claude, "3.1623.1.0")
        exe = self.root / "OpenAI.Codex_26.1112.2228.0" / "app" / codex.exe_name
        with (mock.patch.object(main, "_app_versions", engine.saved_client_versions()),
              mock.patch.object(engine, "find_app_exe", return_value=exe) as discover,
              mock.patch.object(main.time, "sleep", side_effect=StopIteration)):
            with self.assertRaises(StopIteration):
                main._version_probe(codex)
            self.assertEqual(main._app_versions, {codex: "26.1112.2228.0", claude: "3.1623.1.0"})
            self.assertEqual(engine.saved_client_versions(), main._app_versions)
        discover.assert_called_once_with(app=codex)

    def test_navigation_skips_only_the_profile_running_in_main(self) -> None:
        state = main.State()
        state.profiles = ["First", "Shared", "Last"]
        state.cursor = 3
        state.checked = {"Shared", "Last"}
        app = engine.CODEX
        with (mock.patch.object(engine, "_current", app),
              mock.patch.object(engine, "shared_codex_history", return_value=True) as shared_history,
              mock.patch.object(main, "_instances", {app: (frozenset(), True, "Shared")})):
            main.render(state)
            self.assertEqual(state.current_profile(), "Last")
            self.assertEqual(state.targets(), ["Last"])
            state.move(-1)
            self.assertEqual(state.current_profile(), "First")
            state.move(1)
            self.assertEqual(state.current_profile(), "Last")

            main._instances[app] = (frozenset(), False, "Shared")
            main.render(state)
            state.move(-1)
            self.assertEqual(state.current_profile(), "First")

            shared_history.return_value = False
            main.render(state)
            state.move(1)
            self.assertEqual(state.current_profile(), "Shared")

            main._instances[app] = (frozenset(), True, "Shared")
            main.render(state)
            self.assertTrue(state.is_main())

            main._instances[app] = (frozenset({"Shared"}), True, "Shared")
            main.render(state)
            state.move(1)
            state.move(1)
            self.assertEqual(state.current_profile(), "Shared")

    def test_quota_sort_preserves_selected_account_and_uses_the_current_app(self) -> None:
        names = ["Alpha", "Beta", "Free", "Unverified", "Zulu"]
        for app in (engine.CLAUDE, engine.CODEX):
            with self.subTest(app=app.key):
                cached = {
                    (app, "Alpha"): (0, usage.ProfileUsage("Alpha", ok=True, rows=[("5h", 30, ""), ("7d", 10, "")])),
                    (app, "Beta"): (0, usage.ProfileUsage("Beta", ok=True, rows=[("5h", 30, ""), ("7d", 40, "")])),
                    (app, "Free"): (0, usage.ProfileUsage("Free", ok=True, rows=[("30d", 0, "")])),
                    (app, "Zulu"): (0, usage.ProfileUsage("Zulu", ok=True, rows=[("5h", 10, "")])),
                }
                state = main.State()
                state.profiles = names.copy()
                state.cursor = 2
                with (mock.patch.object(engine, "_current", app),
                      mock.patch.object(engine, "list_profiles", return_value=[self.root / name for name in names]),
                      mock.patch.object(main, "_menu_usage", cached)):
                    main._dispatch("t", state)
                    self.assertEqual(state.profiles, ["Zulu", "Alpha", "Beta", "Free", "Unverified"])
                    self.assertEqual(state.current_profile(), "Beta")
                    main._menu_usage = {**cached, (app, "Beta"): (1, usage.ProfileUsage(
                        "Beta", ok=True, rows=[("5h", 0, "")],
                    ))}
                    main.render(state)
                    self.assertEqual(state.profiles[0], "Beta")
                    self.assertEqual(state.current_profile(), "Beta")
                    main._dispatch("t", state)
                    self.assertEqual(state.profiles, names)
                    self.assertEqual(state.current_profile(), "Beta")

    def test_menu_refresh_targets_only_the_highlighted_profile_in_both_apps(self) -> None:
        for app in (engine.CLAUDE, engine.CODEX):
            with self.subTest(app=app.key):
                waiting, release = Event(), Event()
                state = main.State()
                state.profiles = ["First", "Second"]
                state.cursor = 1
                state.checked.add("Second")
                paths = [self.profiles / name for name in state.profiles]
                result = usage.ProfileUsage("First", ok=True, rows=[("5h", 50, "")])

                def fetch(targets, key):
                    waiting.set()
                    self.assertTrue(release.wait(5))
                    self.assertEqual(targets, [("First", paths[0])])
                    self.assertEqual(key, app.key)
                    return [result]

                with (mock.patch.object(engine, "_current", app),
                      mock.patch.object(engine, "list_profiles", return_value=paths),
                      mock.patch.object(main, "_menu_usage", {}),
                      mock.patch.object(usage, "fetch_all", side_effect=fetch) as request):
                    main._dispatch("r", state)
                    _, future = state.refreshing
                    try:
                        self.assertTrue(waiting.wait(2))
                        self.assertIn("Refreshing First", state.status)
                        main._dispatch("DOWN", state)
                        self.assertEqual(state.current_profile(), "Second")
                    finally:
                        release.set()
                    future.result(timeout=3)
                    main._poll_usage_refresh(state)
                    self.assertEqual(main._menu_usage[(app, "First")][1], result)
                    self.assertIn("Refreshed First", state.status)
                    request.assert_called_once()

    def test_menu_refresh_uses_main_codex_session_for_shared_account(self) -> None:
        state = main.State()
        result = usage.ProfileUsage("Main instance · Shared", ok=True)
        with (mock.patch.object(engine, "_current", engine.CODEX),
              mock.patch.object(engine, "main_codex_profile", return_value="Shared"),
              mock.patch.object(engine, "codex_main_home", return_value=self.root / ".codex"),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(usage, "fetch_all", return_value=[result]) as fetch):
            main.refresh_selected_usage(state)
            _, future = state.refreshing
            future.result(timeout=3)
            self.assertEqual(main._menu_usage[(engine.CODEX, "Shared")][1], result)
        fetch.assert_called_once_with([("Main instance · Shared", self.root)], "codex")

    def test_notice_drops_early_keys_and_waits_three_seconds(self) -> None:
        with (mock.patch.object(main, "_draw") as draw,
              mock.patch.object(main, "getkey", return_value="ENTER") as key,
              mock.patch.object(main.msvcrt, "kbhit", side_effect=[True, False, False, False]),
              mock.patch.object(main.time, "monotonic", side_effect=[0, 0, 0.5, 1, 2, 3]),
              mock.patch.object(main.time, "sleep") as sleep):
            main._notice_delay(["Y Open GitHub   ·   N Later"])
        frames = ["\n".join(call.args[0]) for call in draw.call_args_list]
        self.assertEqual(len(frames), 3)
        for remaining, frame in zip((3, 2, 1), frames):
            self.assertIn("Y Open GitHub   ·   N Later", frame)
            self.assertIn(f"Options available in {remaining} second", frame)
        key.assert_called_once_with()
        self.assertEqual(sleep.call_count, 4)

    def test_launch_counter_migrates_existing_database_into_manager_state(self) -> None:
        settings = {"selected_app": "claude", "codex_shared_history": True}
        engine.STATE_FILE.write_text(json.dumps(settings), encoding="utf-8")
        legacy = self.root / "launch-count.db"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE launches (id INTEGER PRIMARY KEY, count INTEGER, shown INTEGER)")
            db.execute("INSERT INTO launches VALUES (1, 199, 1)")

        self.assertIsNone(engine.take_launch_milestone())
        self.assertFalse(legacy.exists())
        self.assertEqual(engine._load_state(), {**settings, "launch_count": 199, "donation_milestone": 1})

        with mock.patch.object(engine, "launch_default_slot"):
            engine.launch_main()
        self.assertEqual(engine.take_launch_milestone(), 200)
        self.assertIsNone(engine.take_launch_milestone())
        self.assertFalse(legacy.exists())
        self.assertEqual(engine._load_state(), {**settings, "launch_count": 200, "donation_milestone": 2})

    def test_launch_counter_offers_donation_once_per_100_successful_launches(self) -> None:
        engine.create_profile("named")
        with (mock.patch.object(engine, "launch_default_slot"), mock.patch.object(engine, "launch_profile")):
            for _ in range(98):
                engine.launch_main()
            engine.launch_account("named")
            self.assertIsNone(engine.take_launch_milestone())
            with mock.patch.object(engine, "launch_profile", side_effect=RuntimeError("launch failed")):
                with self.assertRaisesRegex(RuntimeError, "launch failed"):
                    engine.launch_account("named")
            self.assertIsNone(engine.take_launch_milestone())
            engine.launch_main()
            self.assertEqual(engine.take_launch_milestone(), 100)
            engine.launch_account("named")
            self.assertIsNone(engine.take_launch_milestone())
            for _ in range(98):
                engine.launch_main()
            self.assertIsNone(engine.take_launch_milestone())
            engine.launch_main()
            self.assertEqual(engine.take_launch_milestone(), 200)
            self.assertIsNone(engine.take_launch_milestone())
            for _ in range(103):
                engine.launch_main()
            self.assertEqual(engine.take_launch_milestone(), 303)
            self.assertIsNone(engine.take_launch_milestone())

    def test_exe_update_chooses_newest_stable_windows_download(self) -> None:
        releases = [
            {"tag_name": "v3.0.0", "assets": []},
            {"tag_name": "v2.0.9", "assets": [{"name": "ai-multi-instance-v2.0.9-windows.zip"}]},
            {"tag_name": "v4.0.0", "prerelease": True,
             "assets": [{"name": "ai-multi-instance-v4.0.0-windows.zip"}]},
            {"tag_name": "v2.0.10", "assets": [{"name": "ai-multi-instance-v2.0.10-windows.zip"}]},
        ]
        with (mock.patch.object(engine.sys, "frozen", True, create=True),
              mock.patch.object(engine, "MANAGER_VERSION", "2.0.1"),
              mock.patch.object(engine, "_github_data", return_value=releases) as github):
            self.assertEqual(engine.check_update(), (
                "Version v2.0.10", "https://github.com/Zoltak-Dev/ai-multi-instance/releases/tag/v2.0.10",
            ))
            github.assert_called_once_with("releases?per_page=30")

    def test_source_update_compares_local_commit_with_upstream_main(self) -> None:
        head, upstream = "a" * 40, "b" * 40
        comparison = {"status": "behind", "base_commit": {"sha": upstream},
                      "files": [{"filename": "README.md"}]}
        newest = _github_tree({"main.py": "c" * 40})
        installed = _github_tree({"main.py": "d" * 40})
        with (mock.patch.object(engine.sys, "frozen", False, create=True),
              mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine.subprocess, "run", return_value=mock.Mock(stdout=head + "\n")) as git,
              mock.patch.object(engine, "_github_data", side_effect=[comparison, newest, installed, {"status": "ahead"}]) as github):
            (self.root / ".git").mkdir()
            self.assertEqual(engine.check_update(), (
                "Source commit bbbbbbb", f"https://github.com/Zoltak-Dev/ai-multi-instance/compare/{head}...main",
            ))
            self.assertEqual(github.call_args_list, [mock.call(f"compare/main...{head}?per_page=1"),
                                                    mock.call(f"git/trees/{upstream}"),
                                                    mock.call(f"git/trees/{head}")])
            git.assert_called_once()
            self.assertIsNone(engine.check_update())

    def test_zip_update_compares_embedded_revision_or_release_tag(self) -> None:
        archived, upstream = "a" * 40, "b" * 40
        comparison = {"status": "behind", "base_commit": {"sha": upstream}}
        newest = _github_tree({"launcher.pyw": "c" * 40})
        installed = _github_tree({"launcher.pyw": "d" * 40})
        with (mock.patch.object(engine.sys, "frozen", False, create=True),
              mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "MANAGER_VERSION", "2.0.1"),
              mock.patch.object(engine.subprocess, "run") as git,
              mock.patch.object(engine, "_github_data", side_effect=[comparison, newest, installed] * 2) as github):
            with mock.patch.object(engine, "_SOURCE_ARCHIVE_REVISION", archived):
                self.assertEqual(engine.check_update(), (
                    "Source commit bbbbbbb", f"https://github.com/Zoltak-Dev/ai-multi-instance/compare/{archived}...main",
                ))
                github.assert_any_call(f"compare/main...{archived}?per_page=1")
                github.assert_called_with(f"git/trees/{archived}")
            with mock.patch.object(engine, "_SOURCE_ARCHIVE_REVISION", "$Format:%H$"):
                self.assertEqual(engine.check_update(), (
                    "Source commit bbbbbbb (after v2.0.1)",
                    "https://github.com/Zoltak-Dev/ai-multi-instance/compare/v2.0.1...main",
                ))
                github.assert_any_call("compare/main...v2.0.1?per_page=1")
                github.assert_called_with("git/trees/v2.0.1")
            git.assert_not_called()

    def test_source_update_ignores_changes_outside_the_program(self) -> None:
        upstream = "b" * 40
        installed = _github_tree({"engine.py": "c" * 40, "README.md": "d" * 40})
        newest = _github_tree({"engine.py": "c" * 40, "README.md": "e" * 40,
                               "CHANGELOG.md": "f" * 40, "tests/test_maintenance.py": "a" * 40})
        for status in ("behind", "diverged"):
            comparison = {"status": status, "base_commit": {"sha": upstream}}
            with (self.subTest(status=status),
                  mock.patch.object(engine, "PROJECT_DIR", self.root),
                  mock.patch.object(engine, "_SOURCE_ARCHIVE_REVISION", "a" * 40),
                  mock.patch.object(engine, "_github_data", side_effect=[comparison, newest, installed])):
                self.assertIsNone(engine._source_update())

    def test_source_update_ignores_incomplete_file_lists(self) -> None:
        comparison = {"status": "diverged", "base_commit": {"sha": "b" * 40}}
        newest = _github_tree({"usage.py": "c" * 40})
        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "_SOURCE_ARCHIVE_REVISION", "$Format:%H$"),
              mock.patch.object(engine, "_github_data", side_effect=[
                  comparison, newest, None,
                  comparison, {"tree": [], "truncated": True},
              ])):
            self.assertIsNone(engine._source_update())
            self.assertIsNone(engine._source_update())

    @unittest.skipUnless(shutil.which("git"), "Git is not installed")
    def test_auto_update_fast_forwards_and_preserves_local_changes(self) -> None:
        source = self.root / "upstream"
        checkout = self.root / "checkout"
        source.mkdir()

        def git(root, *args):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, timeout=15)

        def commit(message):
            git(source, "add", "manager.txt")
            git(source, "-c", "user.name=Update Test", "-c", "user.email=update@example.com",
                "-c", "commit.gpgsign=false", "commit", "-m", message)

        git(source, "init", "--initial-branch=main")
        file = source / "manager.txt"
        file.write_text("original", encoding="utf-8")
        commit("Original")
        git(self.root, "clone", str(source), str(checkout))
        file.write_text("updated", encoding="utf-8")
        commit("Update")

        original_git = engine._git

        def git_with_official_origin(*args, **kwargs):
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/Zoltak-Dev/ai-multi-instance.git"
            return original_git(*args, **kwargs)

        with (mock.patch.object(engine, "PROJECT_DIR", checkout),
              mock.patch.object(engine, "_git", side_effect=git_with_official_origin)):
            local = checkout / file.name
            local.write_text("local change", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "local changes"):
                engine.apply_source_update()
            self.assertEqual(local.read_text(encoding="utf-8"), "local change")
            local.write_text("original", encoding="utf-8")
            engine.apply_source_update()
            self.assertEqual(local.read_text(encoding="utf-8"), "updated")

    def test_milestone_and_update_notices_show_count_and_release_link(self) -> None:
        url = "https://github.com/Zoltak-Dev/ai-multi-instance/releases/tag/v2.0.2"
        downloads = "https://github.com/Zoltak-Dev/ai-multi-instance/releases/expanded_assets/v2.0.2"
        with (mock.patch.object(engine, "take_launch_milestone", return_value=100),
              mock.patch.object(engine.sys, "frozen", True, create=True),
              mock.patch.object(main, "_notice_delay") as delay,
              mock.patch.object(main, "_draw"), mock.patch.object(main, "_full_clear"),
              mock.patch.object(main, "getkey", side_effect=["ENTER", "y", "n"]),
              mock.patch.object(main.os, "startfile", side_effect=OSError) as browser):
            main._show_launch_milestone()
            main._show_update("Version v2.0.2", url)
        donation, update = ["\n".join(call.args[0]) for call in delay.call_args_list]
        self.assertIn("100 times", donation)
        self.assertIn("Copy LTC address", donation)
        self.assertIn("Open Ko-fi", donation)
        self.assertIn("v2.0.2", update)
        self.assertIn(downloads, update)
        self.assertIn("Update", update)
        self.assertIn("What's new", update)
        self.assertIn("Later", update)
        browser.assert_called_once_with(downloads)

    def test_source_update_popup_routes_update_and_release_details(self) -> None:
        repo = "https://github.com/Zoltak-Dev/ai-multi-instance"
        changes = f"{repo}/compare/v2.0.1...main"
        for auto, keys, target, result in (
            (True, ["y", "ENTER"], None, False),
            (True, ["g", "n"], changes, True),
            (False, ["y"], repo, True),
            (False, ["n"], None, True),
        ):
            with (self.subTest(auto_update=auto, key=keys[0]),
                  mock.patch.object(engine.sys, "frozen", False, create=True),
                  mock.patch.object(engine, "can_auto_update", return_value=auto),
                  mock.patch.object(engine, "apply_source_update") as apply_update,
                  mock.patch.object(main, "_notice_delay"),
                  mock.patch.object(main, "_draw"),
                  mock.patch.object(main, "getkey", side_effect=keys),
                  mock.patch.object(main.os, "startfile") as browser):
                self.assertIs(main._show_update("Source commit bbbbbbb", changes), result)
                self.assertEqual(apply_update.call_count, int(auto and keys[0] == "y"))
                self.assertEqual(browser.call_args, mock.call(target) if target is not None else None)

    def test_claude_usage_reads_relocated_profile(self) -> None:
        profile = engine.create_profile("work")
        family = (engine.CLAUDE.package_prefix + engine.CLAUDE.publisher_hash).lower()
        relocated = self._data(profile / ".localappdata" / "Claude-Data" / family, "work")
        with (mock.patch.object(usage, "_read_session", return_value=("locked", None, None)) as session,
              mock.patch.object(usage, "_cache_get_session", return_value=None) as cache):
            self.assertFalse(usage._fetch_claude("work", profile).ok)
        session.assert_called_once_with(relocated)
        cache.assert_called_once_with(str(relocated.resolve()))

    def test_usage_probe_keeps_idle_batch_synchronized_after_main_switch(self) -> None:
        app = engine.CODEX
        profiles = [self.root / name for name in ("Work", "Personal", "Idle")]
        scan_ready, main_usage_ready = Event(), Event()
        scan_ready.set()

        def verify(targets, app_key):
            self.assertEqual(app_key, "codex")
            for i, (name, _) in enumerate(targets):
                if name == "Personal" and fetch.call_count == 1:
                    self.assertTrue(main_usage_ready.is_set())
                    self.assertIn((app, "Work"), main._menu_usage)
                yield i, usage.ProfileUsage(name, ok=True)

        def switch_main():
            main._instances[app] = (frozenset(), True, "Personal")

        with (mock.patch.object(engine, "_current", app),
              mock.patch.object(engine, "list_profiles", return_value=profiles),
              mock.patch.object(engine, "main_codex_profile", side_effect=lambda root: main._instances[app][2]),
              mock.patch.object(main, "_instances", {app: (frozenset(), True, "Work")}),
              mock.patch.object(main, "_menu_usage", {}),
              mock.patch.object(main, "_refresh_usage", {app: Event()}),
              mock.patch.object(main.time, "monotonic", side_effect=[0, 60, 70, 129, 130, 179, 180]),
              mock.patch.object(main.time, "sleep") as sleep,
              mock.patch.object(usage, "fetch_as_ready", side_effect=verify) as fetch):
            def advance(_seconds):
                if sleep.call_count == 2:
                    switch_main()
                if sleep.call_count == 7:
                    raise StopIteration

            sleep.side_effect = advance
            with self.assertRaises(StopIteration):
                main._usage_probe(app, scan_ready, main_usage_ready)
            self.assertEqual({name: value[0] for (_, name), value in main._menu_usage.items()},
                             {"Work": 180, "Personal": 130, "Idle": 180})
            main._publish_usage(app, self.root, "Work", {None: usage.ProfileUsage("Stale main")}, 200)
            self.assertEqual(main._menu_usage[(app, "Work")][0], 180)
        self.assertEqual([[name for name, _ in call.args[0]] for call in fetch.call_args_list],
                         [["Main instance · Work", "Personal", "Idle"], ["Main instance · Work"],
                          ["Main instance · Personal"], ["Main instance · Personal"], ["Work", "Idle"]])

    def test_usage_badge_stays_compact_when_windows_are_unavailable(self) -> None:
        result = usage.ProfileUsage("Free", ok=True, rows=[("30d", 80, "")])
        self.assertEqual(main._usage_badge(result), f" {main.GREEN}✓{main.RESET}")
        self.assertEqual(main._usage_badge(None), "")
        self.assertEqual(main._usage_badge(usage.ProfileUsage("Unavailable")), f" {main.GREY}✓{main.RESET}")
        result.rows.append(("7d", 84, ""))
        self.assertEqual(main._usage_badge(result),
                         f" {main.GREEN}✓{main.RESET} {main.RED}16%{main.RESET} {main.GREY}(7d){main.RESET}")

    def test_close_keeps_menu_responsive_and_uses_selected_app_and_profiles(self) -> None:
        profiles = self.root / engine.CODEX.profiles_dirname
        names = ["Personal", "Work account"]
        for name in names:
            (profiles / name).mkdir(parents=True)
        entered, release = Event(), Event()

        def scan(app):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test scan timed out")
            return [(app, i, "", f'{app.exe_name} "--user-data-dir={profiles / name}"')
                    for i, name in enumerate(names, start=1)]

        state = main.State()
        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "_processes", side_effect=scan) as processes,
              mock.patch.object(engine, "_kill_pids", return_value=2) as kill,
              mock.patch.object(main, "do_launch") as launch):
            main._dispatch("c", state)
            self.assertIs(engine.current_app(), engine.CODEX)
            state.cursor = 1
            state.checked.update(names)
            main._dispatch("3", state)
            _, result = state.closing
            try:
                self.assertTrue(entered.wait(2))
                main._dispatch("1", state)
                launch.assert_not_called()
                main._dispatch("c", state)
                self.assertIs(engine.current_app(), engine.CLAUDE)
            finally:
                release.set()
                result.result(timeout=3)
            self.assertIn("closed 2 processes", result.result())
            main._poll_close(state)
            self.assertIsNone(state.closing)
            processes.assert_called_once_with(engine.CODEX)
            kill.assert_called_once_with([1, 2])

    def test_close_targets_main_from_its_named_codex_profile(self) -> None:
        home = self.root / "main-home"
        profiles = self.root / engine.CODEX.profiles_dirname
        self._codex_auth_fixture(profiles / "Work account" / ".codex" / "auth.json", "work", "saved")
        self._codex_auth_fixture(home / "auth.json", "work", "refreshed")
        with (mock.patch.object(engine, "PROJECT_DIR", self.root),
              mock.patch.object(engine, "codex_main_home", return_value=home),
              mock.patch.object(engine, "running_instances", return_value=({"Personal": [2]}, [1])),
              mock.patch.object(engine, "_kill_pids") as kill):
            engine.close_instances(engine.CODEX, ["Work account"])
            engine.close_instances(engine.CODEX, None)
            engine.close_instances(engine.CODEX, ["Personal"])
            engine.close_instances(engine.CODEX, ["Work account", "Personal"])
        self.assertEqual(kill.call_args_list, [mock.call([1]), mock.call([1]), mock.call([2]), mock.call([2, 1])])

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

    def test_usage_reset_formats_minutes(self) -> None:
        now = usage.datetime.fromisoformat("2026-11-15T12:00:00+00:00")
        cases = [
            ("2026-11-15T11:59:59Z", "now"),
            ("2026-11-15T12:00:45Z", "in 45s"),
            ("2026-11-15T12:27:03Z", "in 27m"),
            ("2026-11-15T13:00:00Z", "in 1h"),
            ("2026-11-15T13:12:31Z", "in 1h 12m"),
            ("2026-11-17T13:12:31Z", "in 2d 1h 12m"),
        ]
        with mock.patch.object(usage, "datetime", wraps=usage.datetime) as clock:
            clock.now.return_value = now
            for reset_at, expected in cases:
                with self.subTest(reset_at=reset_at):
                    self.assertEqual(usage.humanize_reset(reset_at), expected)

    def test_usage_remaining_colors_at_thresholds(self) -> None:
        for remaining, color in ((100, main.GREEN), (51, main.GREEN), (50, main.YELLOW),
                                 (20, main.YELLOW), (19, main.RED), (0, main.RED)):
            with self.subTest(remaining=remaining):
                self.assertEqual(main._usage_remaining(100 - remaining), f"{color}{remaining:>3}%{main.RESET}")

    def test_failed_claude_profile_replacement_restores_previous_data(self) -> None:
        slot = self._data(engine.slot_candidates(engine.CLAUDE)[0], "fresh")
        profile = self._data(self.profiles / "work", "original")
        move_retry = engine._move_retry

        def fail_final_move(src, dst):
            if src.name == "fresh" and dst == profile:
                raise PermissionError("blocked")
            move_retry(src, dst)

        with (mock.patch.object(engine, "default_slot_dir", return_value=slot),
              mock.patch.object(engine, "_move_retry", side_effect=fail_final_move)):
            with self.assertRaisesRegex(PermissionError, "blocked"):
                engine.adopt_default_into("work")
        self.assertEqual((profile / "Local State").read_text(), "original")
        self.assertEqual(list(self.root.glob(".mi-adopt-*")), [])

    def test_shortcut_parser_accepts_a_profile_named_like_an_app_option(self) -> None:
        parse = runpy.run_path(str(engine.LAUNCHER))["_parse_args"]
        self.assertEqual(parse(["--app=codex", "--app=work"]), (engine.CODEX, "--app=work"))
        with self.assertRaisesRegex(ValueError, "--app=claude or --app=codex"):
            parse(["--app=work", "name"])

    def test_usage_explains_the_same_request_failures_in_both_apps(self) -> None:
        self._codex_auth_fixture(self.root / ".codex" / "auth.json", "work", "saved")
        cases = [(usage.urllib.error.HTTPError("https://example.invalid", code, "", {}, None), reason)
                 for code, reason in ((401, "session expired or invalid — sign in again"),
                                      (403, "access denied"),
                                      (429, "too many requests — try again later"))]
        cases.append((usage.urllib.error.URLError("offline"), "network unavailable"))
        with (mock.patch.object(usage, "_read_session", return_value=("live", "test-token", None)),
              mock.patch.object(usage, "_cache_put_session"),
              mock.patch.object(usage, "_cache_drop")):
            for error, reason in cases:
                with (self.subTest(reason=reason),
                      mock.patch.object(usage, "_api_get", side_effect=error),
                      mock.patch.object(usage, "_codex_get", side_effect=error)):
                    for app in (engine.CLAUDE, engine.CODEX):
                        result = usage.fetch_all([("work", self.root)], app.key)[0]
                        self.assertFalse(result.ok)
                        self.assertEqual(result.error, reason)
                        screen = "\n".join(main.render_usage([result], app.display))
                        self.assertIn(f"{main.GREY}✓", screen)
                        self.assertIn(reason, screen)

    def test_temporary_usage_failure_expires_windows_from_original_check(self) -> None:
        checked_at = usage.datetime.fromisoformat("2026-11-16T10:00:00+00:00").timestamp()
        weekly = ("7d", 25, "2026-11-16T16:00:00+00:00")
        key = (engine.CLAUDE, "Work")
        checked = usage.ProfileUsage("Work", ok=True, rows=[("5h", 100, ""), weekly],
                                     quota_key="work-session", checked_at=checked_at)
        offline = usage.ProfileUsage("Work", error="network unavailable", quota_key="work-session",
                                     temporary_error=True)
        with (mock.patch.object(main, "_menu_usage", {key: (10, checked)}),
              mock.patch.object(main.time, "time", return_value=checked_at + 5 * 3600 + 1) as clock):
            main._publish_usage(engine.CLAUDE, self.root, None, {"Work": offline}, 20)
            attempted_at, retained = main._menu_usage[key]
            self.assertEqual(attempted_at, 20)
            self.assertEqual(retained.checked_at, checked_at)
            self.assertEqual(retained.rows, [weekly])
            self.assertNotIn("(5h)", main._usage_badge(retained))
            self.assertIn("75%", main._usage_badge(retained))
            clock.return_value = checked_at + 6 * 3600
            main._publish_usage(engine.CLAUDE, self.root, None, {"Work": offline}, 30)
            self.assertEqual(main._menu_usage[key], (30, offline))


if __name__ == "__main__":
    unittest.main()
