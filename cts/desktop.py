"""Makes CTS show up in the operating system's application list.

* Linux   - a freedesktop ``.desktop`` entry + icons in ``~/.local/share``
* macOS   - a tiny ``CTS.app`` bundle in ``~/Applications`` (shows in Launchpad / Spotlight)
* Windows - a Start Menu shortcut (``CTS.lnk``) via PowerShell

The launcher always runs ``cts launch``: start the local server if needed, then open the browser.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .paths import ASSETS_DIR, data_dir

APP_ID = "io.github.kmc1525.cts"
COMMENT = "Turn comic volumes into novels with AI agents"


def current_system() -> str:
    return {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
        __import__("platform").system(), "linux"
    )


def default_exec() -> list[str]:
    """Command that starts CTS from this Python environment."""
    scripts = Path(sys.executable).resolve().parent
    name = "cts-gui.exe" if os.name == "nt" else "cts"
    candidate = scripts / name
    if candidate.exists():
        return [str(candidate)]
    return [sys.executable, "-m", "cts"]


def _quote_exec(command: list[str]) -> str:
    """Quote for a .desktop Exec= line / shell script."""
    return " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"' for part in command)


# ------------------------------------------------------------------------- Linux
def desktop_entry(command: list[str]) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        "Name=CTS",
        "GenericName=Comic To Story",
        f"Comment={COMMENT}",
        f"Exec={_quote_exec(command + ['launch'])}",
        "Icon=cts",
        "Terminal=false",
        "Categories=Office;Utility;Graphics;",
        "Keywords=comic;manga;novel;story;ai;",
        "StartupNotify=true",
        "",
    ])


def _xdg_data_home(home: Path | None) -> Path:
    if home is not None:
        return home / ".local" / "share"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def install_linux(command: list[str], home: Path | None = None) -> list[Path]:
    share = _xdg_data_home(home)
    created: list[Path] = []
    icon_png = share / "icons" / "hicolor" / "512x512" / "apps" / "cts.png"
    icon_svg = share / "icons" / "hicolor" / "scalable" / "apps" / "cts.svg"
    for src, dst in ((ASSETS_DIR / "cts.png", icon_png), (ASSETS_DIR / "cts.svg", icon_svg)):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        created.append(dst)
    entry = share / "applications" / "cts.desktop"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(desktop_entry(command), encoding="utf-8")
    entry.chmod(0o755)
    created.append(entry)
    for tool, args in (("update-desktop-database", [str(entry.parent)]),
                       ("gtk-update-icon-cache", ["-f", "-t", str(share / "icons" / "hicolor")])):
        if shutil.which(tool):
            subprocess.run([tool, *args], check=False, capture_output=True)
    return created


def uninstall_linux(home: Path | None = None) -> list[Path]:
    share = _xdg_data_home(home)
    removed = []
    for path in (share / "applications" / "cts.desktop",
                 share / "icons" / "hicolor" / "512x512" / "apps" / "cts.png",
                 share / "icons" / "hicolor" / "scalable" / "apps" / "cts.svg"):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


# ------------------------------------------------------------------------- macOS
def mac_info_plist() -> bytes:
    return plistlib.dumps({
        "CFBundleName": "CTS",
        "CFBundleDisplayName": "CTS",
        "CFBundleIdentifier": APP_ID,
        "CFBundleExecutable": "CTS",
        "CFBundleIconFile": "CTS",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": True,  # the launcher exits right away; no Dock icon bounce
        "NSHighResolutionCapable": True,
    })


def install_macos(command: list[str], home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    app = home / "Applications" / "CTS.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(mac_info_plist())
    shutil.copyfile(ASSETS_DIR / "cts.icns", app / "Contents" / "Resources" / "CTS.icns")
    launcher = app / "Contents" / "MacOS" / "CTS"
    launcher.write_text(
        "#!/bin/bash\n"
        '# Finder starts apps with a minimal PATH; the launcher uses an absolute path instead.\n'
        f"exec {_quote_exec(command + ['launch'])}\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    subprocess.run(["touch", str(app)], check=False, capture_output=True)
    return [app]


def uninstall_macos(home: Path | None = None) -> list[Path]:
    app = (home or Path.home()) / "Applications" / "CTS.app"
    if app.exists():
        shutil.rmtree(app)
        return [app]
    return []


# ----------------------------------------------------------------------- Windows
def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_shortcut_script(command: list[str], icon: Path, lnk: Path) -> str:
    target, args = command[0], " ".join(f'"{a}"' if " " in a else a for a in command[1:] + ["launch"])
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$link = $shell.CreateShortcut({_ps(str(lnk))})",
        f"$link.TargetPath = {_ps(target)}",
        f"$link.Arguments = {_ps(args)}",
        f"$link.WorkingDirectory = {_ps(str(Path(target).parent))}",
        f"$link.IconLocation = {_ps(str(icon))}",
        f"$link.Description = {_ps(COMMENT)}",
        "$link.WindowStyle = 7",
        "$link.Save()",
    ])


def start_menu_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def install_windows(command: list[str]) -> list[Path]:
    icon = data_dir() / "cts.ico"
    shutil.copyfile(ASSETS_DIR / "cts.ico", icon)  # a stable path that survives upgrades
    lnk = start_menu_dir() / "CTS.lnk"
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = windows_shortcut_script(command, icon, lnk)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not create the Start Menu shortcut: {result.stderr.strip() or result.stdout.strip()}")
    return [lnk, icon]


def uninstall_windows() -> list[Path]:
    removed = []
    for path in (start_menu_dir() / "CTS.lnk", data_dir() / "cts.ico"):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


# ------------------------------------------------------------------------ public
def install(command: list[str] | None = None, system: str | None = None) -> list[Path]:
    command = command or default_exec()
    system = system or current_system()
    if system == "linux":
        return install_linux(command)
    if system == "macos":
        return install_macos(command)
    if system == "windows":
        return install_windows(command)
    raise RuntimeError(f"Unsupported platform: {system}")


def uninstall(system: str | None = None) -> list[Path]:
    system = system or current_system()
    return {"linux": uninstall_linux, "macos": uninstall_macos, "windows": uninstall_windows}[system]()
