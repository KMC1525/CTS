import plistlib

from cts import desktop


def test_linux_entry_and_icons(tmp_path):
    created = desktop.install_linux(["/home/u/.local/bin/cts"], home=tmp_path)
    entry = tmp_path / ".local/share/applications/cts.desktop"
    text = entry.read_text()
    assert 'Exec="/home/u/.local/bin/cts" "launch"' in text and "Icon=cts" in text and "Terminal=false" in text
    assert "Categories=" in text and entry in created
    assert (tmp_path / ".local/share/icons/hicolor/512x512/apps/cts.png").stat().st_size > 1000
    assert (tmp_path / ".local/share/icons/hicolor/scalable/apps/cts.svg").exists()
    assert len(desktop.uninstall_linux(tmp_path)) == 3 and not entry.exists()


def test_linux_exec_quotes_paths_with_spaces(tmp_path):
    desktop.install_linux(["/opt/My Apps/cts"], home=tmp_path)
    assert 'Exec="/opt/My Apps/cts" "launch"' in (tmp_path / ".local/share/applications/cts.desktop").read_text()


def test_macos_bundle(tmp_path):
    [app] = desktop.install_macos(["/Users/u/.local/bin/cts"], home=tmp_path)
    assert app == tmp_path / "Applications" / "CTS.app"
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "CTS" and info["CFBundleIconFile"] == "CTS" and info["CFBundlePackageType"] == "APPL"
    launcher = app / "Contents/MacOS/CTS"
    assert launcher.read_text().startswith("#!/bin/bash") and 'exec "/Users/u/.local/bin/cts" "launch"' in launcher.read_text()
    assert launcher.stat().st_mode & 0o111
    icns = (app / "Contents/Resources/CTS.icns").read_bytes()
    assert icns[:4] == b"icns" and int.from_bytes(icns[4:8], "big") == len(icns)
    desktop.install_macos(["/x/cts"], home=tmp_path)  # reinstall over an existing bundle
    assert desktop.uninstall_macos(tmp_path) == [app] and not app.exists()


def test_windows_shortcut_script(tmp_path):
    script = desktop.windows_shortcut_script([r"C:\Users\Me O'Neil\cts-gui.exe"], tmp_path / "cts.ico", tmp_path / "CTS.lnk")
    assert "WScript.Shell" in script and "CreateShortcut" in script
    assert r"TargetPath = 'C:\Users\Me O''Neil\cts-gui.exe'" in script  # single quotes are escaped
    assert "Arguments = 'launch'" in script and "IconLocation" in script


def test_icns_ico_png_assets_are_valid():
    from PIL import Image

    assert Image.open(desktop.ASSETS_DIR / "cts.png").size == (512, 512)
    ico = Image.open(desktop.ASSETS_DIR / "cts.ico")
    assert max(ico.info["sizes"]) == (256, 256)
