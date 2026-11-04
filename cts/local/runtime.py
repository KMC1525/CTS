"""Finds or installs ``llama-server`` (llama.cpp), the engine that runs local GGUF models."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx

from ..paths import runtime_dir
from .download import download_file

RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
EXE = "llama-server.exe" if os.name == "nt" else "llama-server"
_FLAVORS = ("cuda", "vulkan", "hip", "rocm", "sycl", "opencl", "kleidiai", "musa", "openvino", "adreno", "cann", "aclnn")
_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz")

MANUAL_HELP = (
    "Install llama.cpp yourself and CTS will find it: macOS `brew install llama.cpp`, "
    "Windows `winget install llama.cpp`, Linux see github.com/ggml-org/llama.cpp "
    "(or set CTS_LLAMA_SERVER to the llama-server binary)."
)


def system_arch() -> tuple[str, str]:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return system, arch


def find_server() -> Path | None:
    """Locate llama-server: env override, CTS runtime folder, then PATH."""
    override = os.environ.get("CTS_LLAMA_SERVER")
    if override and Path(override).is_file():
        return Path(override)
    for candidate in sorted(runtime_dir().rglob(EXE)):
        if candidate.is_file():
            return candidate
    found = shutil.which("llama-server")
    return Path(found) if found else None


def pick_asset(assets: list[dict[str, Any]], system: str, arch: str, flavor: str = "") -> dict[str, Any] | None:
    """Choose the right prebuilt llama.cpp archive for this machine.

    Windows/Linux x64 prefer the Vulkan build (any GPU vendor, works without CUDA) and fall
    back to CPU; macOS builds already include Metal. Set CTS_LLAMA_FLAVOR (cpu, vulkan,
    cuda...) to force a flavor.
    """
    os_token = {"windows": "win", "macos": "macos", "linux": "ubuntu"}.get(system, system)
    candidates = []
    for asset in assets:
        name = asset.get("name", "").lower()
        if not name.startswith("llama-") or "-bin-" not in name or not name.endswith(_ARCHIVE_SUFFIXES):
            continue
        if os_token not in name:
            continue
        if arch == "arm64" and "arm64" not in name:
            continue
        if arch == "x64" and ("arm64" in name or not ("x64" in name or "x86_64" in name or "amd64" in name)):
            continue
        candidates.append(asset)
    if not candidates:
        return None

    def flavors_of(asset: dict[str, Any]) -> set[str]:
        return {f for f in _FLAVORS if f in asset["name"].lower()}

    wanted = flavor.lower().strip()
    if wanted and wanted != "cpu":
        match = [a for a in candidates if wanted in flavors_of(a)]
        if match:
            return sorted(match, key=lambda a: len(a["name"]))[0]
    elif system in ("windows", "linux") and not wanted:
        vulkan = [a for a in candidates if flavors_of(a) == {"vulkan"}]
        if vulkan:
            return sorted(vulkan, key=lambda a: len(a["name"]))[0]
    plain = [a for a in candidates if not flavors_of(a)]
    pool = plain or candidates
    return sorted(pool, key=lambda a: len(a["name"]))[0]


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                target = (dest / info.filename).resolve()
                if root not in target.parents and target != root:
                    raise RuntimeError("Unsafe path in archive")
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                target = (dest / member.name).resolve()
                if root not in target.parents and target != root:
                    raise RuntimeError("Unsafe path in archive")
            tf.extractall(dest)


def install_runtime(
    on_progress: Callable[[str, int, int], None] | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download the latest prebuilt llama.cpp for this OS and return the llama-server path."""
    existing = find_server()
    if existing:
        return existing
    system, arch = system_arch()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        try:
            resp = client.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
            resp.raise_for_status()
            release = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Could not reach the llama.cpp release page ({exc}). {MANUAL_HELP}") from exc
        asset = pick_asset(release.get("assets", []), system, arch, os.environ.get("CTS_LLAMA_FLAVOR", ""))
        if asset is None:
            raise RuntimeError(f"No prebuilt llama.cpp found for {system}/{arch}. {MANUAL_HELP}")
        tag = release.get("tag_name", "latest")
        dest_dir = runtime_dir() / tag
        archive = runtime_dir() / asset["name"]
        if on_progress:
            on_progress(f"Downloading llama.cpp {tag}", 0, int(asset.get("size", 0)))
        download_file(
            asset["browser_download_url"], archive, expected_size=int(asset.get("size", 0)),
            on_progress=(lambda n: on_progress(f"Downloading llama.cpp {tag}", n, int(asset.get("size", 0))))
            if on_progress else None,
            is_cancelled=is_cancelled, client=client,
        )
    if on_progress:
        on_progress("Unpacking llama.cpp", 0, 0)
    _extract(archive, dest_dir)
    archive.unlink(missing_ok=True)
    server = next((p for p in dest_dir.rglob(EXE) if p.is_file()), None)
    if server is None:
        raise RuntimeError(f"llama-server was not found inside {asset['name']}. {MANUAL_HELP}")
    if os.name != "nt":
        for binary in server.parent.iterdir():
            if binary.is_file() and not binary.suffix:
                binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return server
