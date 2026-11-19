import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    def test_occupied_desktop_folder_is_preserved(self):
        installer = Path(__file__).resolve().parents[1] / "install-ai-multi-instance.bat"
        with tempfile.TemporaryDirectory(prefix="AI Setup & ") as desktop:
            project = Path(desktop) / "ai-multi-instance"
            project.mkdir()
            data = project / "existing.txt"
            data.write_text("keep me", encoding="utf-8")

            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 r"$parts = [IO.File]::ReadAllText($env:SETUP_FILE) -split '(?m)^# Installer\r?$', 2; "
                 "& ([scriptblock]::Create($parts[1])) -DesktopDirectory $env:SETUP_DESKTOP"],
                env=dict(os.environ, SETUP_FILE=str(installer), SETUP_DESKTOP=desktop),
                capture_output=True, text=True, timeout=20,
            )

            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("This location is already in use", result.stdout)
            self.assertEqual(data.read_text(encoding="utf-8"), "keep me")
