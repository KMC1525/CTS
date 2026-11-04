"""System libraries that the prebuilt llama.cpp binaries need, and how to install them.

The prebuilt ``llama-server`` is dynamically linked: on a minimal Linux it can be missing
``libgomp`` (OpenMP) or ``libvulkan``; on a fresh Windows it needs the Microsoft Visual C++
Redistributable. CTS detects what is missing from the loader's own error message and installs it
when it safely can (root, or passwordless ``sudo``, or ``winget`` on Windows); otherwise it tells
the user the exact command to run.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from typing import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# shared library -> package name for each package manager
LIB_PACKAGES: dict[str, dict[str, str]] = {
    "libgomp.so.1": {"apt": "libgomp1", "dnf": "libgomp", "yum": "libgomp", "pacman": "gcc-libs",
                     "zypper": "libgomp1", "apk": "libgomp"},
    "libvulkan.so.1": {"apt": "libvulkan1", "dnf": "vulkan-loader", "yum": "vulkan-loader",
                       "pacman": "vulkan-icd-loader", "zypper": "libvulkan1", "apk": "vulkan-loader"},
    "libstdc++.so.6": {"apt": "libstdc++6", "dnf": "libstdc++", "yum": "libstdc++", "pacman": "gcc-libs",
                       "zypper": "libstdc++6", "apk": "libstdc++"},
    "libgcc_s.so.1": {"apt": "libgcc-s1", "dnf": "libgcc", "yum": "libgcc", "pacman": "gcc-libs",
                      "zypper": "libgcc_s1", "apk": "libgcc"},
    "libatomic.so.1": {"apt": "libatomic1", "dnf": "libatomic", "yum": "libatomic", "pacman": "gcc-libs",
                       "zypper": "libatomic1", "apk": "libatomic"},
    "libcurl.so.4": {"apt": "libcurl4", "dnf": "libcurl", "yum": "libcurl", "pacman": "curl",
                     "zypper": "libcurl4", "apk": "libcurl"},
}
_INSTALL_CMD: dict[str, list[str]] = {
    "apt": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "yum": ["yum", "install", "-y"],
    "pacman": ["pacman", "-S", "--noconfirm", "--needed"],
    "zypper": ["zypper", "--non-interactive", "install"],
    "apk": ["apk", "add"],
}
_MANAGER_BINARIES = (("apt", "apt-get"), ("dnf", "dnf"), ("yum", "yum"), ("pacman", "pacman"),
                     ("zypper", "zypper"), ("apk", "apk"))

# Windows loader errors when a DLL (e.g. VCRUNTIME140.dll) is missing / has the wrong version
WINDOWS_DLL_ERRORS = {0xC0000135, 3221225781, 0xC0000139, 3221225785, 0xC000007B, 3221225595}

_MISSING_LIB = re.compile(r"(lib[\w+.\-]+\.so(?:\.\d+)*)\s*(?:=>\s*not found|: cannot open shared object)")
_LDD_MISSING = re.compile(r"^\s*(\S+)\s*=>\s*not found", re.M)


def missing_libs_from_text(text: str) -> list[str]:
    """Libraries named in a loader error ('error while loading shared libraries: libX.so.1: ...')."""
    found = [m.group(1) for m in re.finditer(r"error while loading shared libraries:\s*(\S+?):", text)]
    found += _LDD_MISSING.findall(text)
    found += [m.group(1) for m in _MISSING_LIB.finditer(text)]
    seen: list[str] = []
    for lib in found:
        if lib not in seen:
            seen.append(lib)
    return seen


def missing_libs_via_ldd(binary: str, env: dict[str, str] | None = None, runner: Runner = subprocess.run) -> list[str]:
    """Ask ``ldd`` which libraries the binary cannot find (Linux only; empty if ldd is unavailable)."""
    if platform.system() != "Linux" or not shutil.which("ldd"):
        return []
    try:
        proc = runner(["ldd", binary], capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    return missing_libs_from_text(proc.stdout + proc.stderr)


def detect_package_manager(which: Callable[[str], str | None] = shutil.which) -> str | None:
    for name, binary in _MANAGER_BINARIES:
        if which(binary):
            return name
    return None


def packages_for(libs: list[str], manager: str) -> list[str]:
    out: list[str] = []
    for lib in libs:
        package = LIB_PACKAGES.get(lib, {}).get(manager)
        if package and package not in out:
            out.append(package)
    return out


def install_command(manager: str, packages: list[str]) -> list[str]:
    return [*_INSTALL_CMD[manager], *packages]


def _privileged(command: list[str], runner: Runner) -> list[str] | None:
    """Prefix with sudo when needed; None if we cannot elevate without asking for a password."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return command
    if shutil.which("sudo"):
        try:
            if runner(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0:
                return ["sudo", "-n", *command]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def install_system_packages(libs: list[str], runner: Runner = subprocess.run,
                            manager: str | None = None) -> tuple[bool, str]:
    """Try to install the packages that provide ``libs``. Returns (installed?, message)."""
    manager = manager or detect_package_manager()
    if not manager:
        return False, "Install these system libraries with your package manager: " + ", ".join(libs)
    packages = packages_for(libs, manager)
    if not packages:
        return False, "Install these system libraries with your package manager: " + ", ".join(libs)
    command = install_command(manager, packages)
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    shown = ("" if is_root else "sudo ") + " ".join(command)
    elevated = _privileged(command, runner)
    if elevated is None:
        return False, f"Missing system libraries. Run this once, then try again:  {shown}"
    try:
        proc = runner(elevated, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run the package manager ({exc}). Run:  {shown}"
    if proc.returncode != 0:
        return False, f"The package manager failed. Run:  {shown}\n{(proc.stderr or proc.stdout)[-300:]}"
    return True, f"Installed system libraries: {', '.join(packages)}"


# ---------------------------------------------------------------------------- Windows
def vc_redist_arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def install_vc_redist(runner: Runner = subprocess.run, which: Callable[[str], str | None] = shutil.which) -> tuple[bool, str]:
    """Install the Microsoft Visual C++ Redistributable (needed by llama.cpp on Windows)."""
    arch = vc_redist_arch()
    manual = f"Install the Microsoft Visual C++ Redistributable: https://aka.ms/vs/17/release/vc_redist.{arch}.exe"
    if which("winget"):
        try:
            proc = runner(
                ["winget", "install", "-e", "--id", f"Microsoft.VCRedist.2015+.{arch}", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"],
                capture_output=True, text=True, timeout=900,
            )
            # 0 = installed, 0x8A15002B = already installed / no applicable update
            if proc.returncode in (0, 0x8A15002B, 2316632107):
                return True, "Installed the Microsoft Visual C++ Redistributable"
        except (OSError, subprocess.SubprocessError):
            pass
    return False, manual


def hint_for(detail: str, returncode: int | None = None) -> str:
    """A short, actionable explanation for a binary that will not start."""
    libs = missing_libs_from_text(detail)
    if libs:
        manager = detect_package_manager()
        packages = packages_for(libs, manager) if manager else []
        if manager and packages:
            prefix = "" if hasattr(os, "geteuid") and os.geteuid() == 0 else "sudo "
            return f"Missing system libraries ({', '.join(libs)}). Install them with:  {prefix}{' '.join(install_command(manager, packages))}"
        return f"Missing system libraries: {', '.join(libs)}"
    if returncode is not None and returncode in WINDOWS_DLL_ERRORS:
        return ("Windows could not load a required DLL - install the Microsoft Visual C++ Redistributable "
                f"(https://aka.ms/vs/17/release/vc_redist.{vc_redist_arch()}.exe) and try again.")
    if "exec format error" in detail.lower() or "bad cpu type" in detail.lower():
        return "This build does not match your CPU architecture."
    if "illegal instruction" in detail.lower():
        return "Your CPU lacks an instruction set this build needs; CTS will try the CPU-only build."
    return ""


def try_fix(detail: str, returncode: int | None = None, binary: str | None = None,
            runner: Runner = subprocess.run) -> tuple[bool, str]:
    """Attempt to repair what ``detail`` complains about. Returns (changed something?, message)."""
    system = platform.system()
    if system == "Linux":
        libs = missing_libs_from_text(detail)
        if binary:
            libs += [lib for lib in missing_libs_via_ldd(binary, runner=runner) if lib not in libs]
        if libs:
            return install_system_packages(libs, runner)
    if system == "Windows" and returncode is not None and returncode in WINDOWS_DLL_ERRORS:
        return install_vc_redist(runner)
    return False, hint_for(detail, returncode)
