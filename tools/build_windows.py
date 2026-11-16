"""Build two portable Windows executables with PyInstaller 6.22.3."""

from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    version = runpy.run_path(str(ROOT / "engine.py"))["MANAGER_VERSION"]
    file_version = tuple(map(int, version.split("."))) + (0,)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    output = dist / f"ai-multi-instance-v{version}-windows.zip"

    with tempfile.TemporaryDirectory(prefix="build-windows-", dir=dist) as temp:
        build_dir = Path(temp)
        executables = (
            ("main.py", "ai-multi-instance", "--console", "AI Multi-Instance"),
            ("launcher.pyw", "launcher", "--noconsole", "AI Multi-Instance profile launcher"),
        )
        for entry, name, console, description in executables:
            info = VSVersionInfo(
                ffi=FixedFileInfo(filevers=file_version, prodvers=file_version),
                kids=[
                    StringFileInfo([StringTable("040904B0", [
                        StringStruct("CompanyName", "Zoltak-Dev"),
                        StringStruct("FileDescription", description),
                        StringStruct("FileVersion", version),
                        StringStruct("InternalName", name),
                        StringStruct("OriginalFilename", f"{name}.exe"),
                        StringStruct("ProductName", "AI Multi-Instance"),
                        StringStruct("ProductVersion", version),
                    ])]),
                    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
                ],
            )
            version_file = build_dir / f"{name}-version.txt"
            version_file.write_text(str(info), encoding="utf-8")
            subprocess.run(
                [sys.executable, "-m", "PyInstaller", "--onefile", "--noupx", console,
                 "--name", name, "--version-file", str(version_file),
                 "--distpath", str(build_dir / "dist"), "--workpath", str(build_dir / "work"),
                 "--specpath", str(build_dir), entry],
                cwd=ROOT,
                check=True,
            )
        package = build_dir / output.name
        with ZipFile(package, "w", ZIP_DEFLATED) as archive:
            for _, name, _, _ in executables:
                archive.write(build_dir / "dist" / f"{name}.exe", f"{name}.exe")
            for name in ("LICENSE", "README.md", "CHANGELOG.md"):
                archive.write(ROOT / name, name)
        package.replace(output)

    print(output)


if __name__ == "__main__":
    main()
