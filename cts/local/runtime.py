"""Finds, installs, verifies and updates ``llama-server`` (llama.cpp), the engine for local GGUF models.

Release asset names follow llama.cpp's own release workflow (checked against its ``release.yml``):

* macOS   ``llama-<tag>-bin-macos-{arm64,x64}.tar.gz``             (Metal is built in)
* Linux   ``llama-<tag>-bin-ubuntu-{x64,arm64}.tar.gz``, ``...ubuntu-vulkan-{x64,arm64}.tar.gz``,
          ``...ubuntu-cuda-<ver>-x64.tar.gz`` + ``cudart-llama-<tag>-bin-ubuntu-cuda-<ver>-x64.tar.gz``
* Windows ``llama-<tag>-bin-win-{cpu,vulkan}-{x64,arm64}.zip``, ``...win-cuda-<ver>-x64.zip``
          + ``cudart-llama-bin-win-cuda-<ver>-x64.zip`` (note: no tag in the cudart name)

What this module guarantees (v0.2):

* the downloaded binary is **run** (``--version``) before it is accepted;
* missing system libraries / the VC++ runtime are installed when possible (see ``deps``);
* if the preferred GPU build will not start, the plain CPU build is tried automatically;
* installs are atomic (temp folder, then rename) and never leave a half-extracted engine behind;
* ``update=True`` moves a CTS-managed engine to the newest release;
* if the GitHub API is rate-limited or blocked, the release tag is read from the website redirect
  and the asset names are derived from the naming scheme above.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx

from ..paths import runtime_dir
from . import deps
from .download import DownloadCancelled, download_file

GITHUB = "https://github.com/ggml-org/llama.cpp"
# NOTE: llama.cpp publishes every build as a *pre-release* (tags b12111, b12112...). GitHub's
# "latest release" endpoint ignores pre-releases and answers with an unrelated release (e.g. v0.9.0), so it
# must never be used: list the releases and keep the ones tagged b<number>.
RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=40"
RELEASES_ATOM = f"{GITHUB}/releases.atom"
BUILD_TAG = re.compile(r"^b\d+$")
EXE = "llama-server.exe" if os.name == "nt" else "llama-server"
_FLAVORS = ("cuda", "vulkan", "hip", "rocm", "sycl", "opencl", "kleidiai", "musa", "openvino", "adreno", "cann",
            "aclnn", "snapdragon", "zdnn")
_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz")

MANUAL_HELP = (
    "You can install llama.cpp yourself and CTS will find it: macOS `brew install llama.cpp`, "
    "Windows `winget install llama.cpp`, Linux see github.com/ggml-org/llama.cpp "
    "(or set CTS_LLAMA_SERVER to the llama-server binary)."
)

Progress = Callable[[str, int, int], None]


# ---------------------------------------------------------------------------- locating
def system_arch() -> tuple[str, str]:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return system, arch


def _state_file() -> Path:
    return runtime_dir() / "current.json"


def managed_info() -> dict[str, Any] | None:
    """The CTS-managed engine recorded by the last successful install, if it is still on disk."""
    try:
        info = json.loads(_state_file().read_text(encoding="utf-8"))
        if Path(info["path"]).is_file():
            return info
    except (OSError, ValueError, KeyError):
        pass
    return None


def tag_number(tag: str) -> int:
    match = re.search(r"(\d+)", tag or "")
    return int(match.group(1)) if match else 0


def _inside_runtime(path: Path) -> bool:
    try:
        return runtime_dir().resolve() in path.resolve().parents
    except OSError:
        return False


def find_server() -> Path | None:
    """Locate llama-server: CTS_LLAMA_SERVER, the CTS-managed engine, PATH, then legacy installs."""
    override = os.environ.get("CTS_LLAMA_SERVER")
    if override and Path(override).is_file():
        return Path(override)
    info = managed_info()
    if info:
        return Path(info["path"])
    found = shutil.which("llama-server")
    if found:
        return Path(found)
    # engines installed by CTS 0.1 (no state file): newest folder first
    for candidate in sorted(runtime_dir().rglob(EXE), reverse=True):
        if candidate.is_file() and candidate.parent.parent.name != "downloads" and ".tmp" not in str(candidate):
            return candidate
    return None


def runtime_info() -> dict[str, Any]:
    """Describe the engine in use (cheap: does not run it)."""
    path = find_server()
    if path is None:
        return {"installed": False, "path": "", "origin": "", "tag": "", "flavor": ""}
    info = managed_info()
    if os.environ.get("CTS_LLAMA_SERVER") and Path(os.environ["CTS_LLAMA_SERVER"]) == path:
        return {"installed": True, "path": str(path), "origin": "env", "tag": "", "flavor": ""}
    if info and Path(info["path"]) == path:
        return {"installed": True, "path": str(path), "origin": "cts", "tag": info.get("tag", ""),
                "flavor": info.get("flavor", "")}
    if _inside_runtime(path):  # installed by CTS 0.1, before the state file existed
        return {"installed": True, "path": str(path), "origin": "cts", "tag": "", "flavor": ""}
    return {"installed": True, "path": str(path), "origin": "system", "tag": "", "flavor": ""}


def runtime_env(binary: Path | str) -> dict[str, str]:
    """Environment that lets the binary find the shared libraries shipped next to it."""
    env = dict(os.environ)
    folder = str(Path(binary).parent)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        env[var] = folder + os.pathsep + env.get(var, "")
    if os.name == "nt":
        env["PATH"] = folder + os.pathsep + env.get("PATH", "")
    return env


def verify(binary: Path | str, timeout: int = 45) -> tuple[bool, str, int | None]:
    """Run ``llama-server --version``. Returns (ok, detail, return code)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=timeout,
                              env=runtime_env(binary), cwd=str(Path(binary).parent), creationflags=flags)
    except subprocess.TimeoutExpired:
        return False, "llama-server did not answer --version in time", None
    except OSError as exc:
        return False, f"cannot execute llama-server: {exc}", None
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        line = next((ln for ln in output.splitlines() if "version" in ln.lower()), output.splitlines()[0] if output else "")
        return True, line.strip(), 0
    return False, f"exit code {proc.returncode}: {output[-700:]}", proc.returncode


def check() -> dict[str, Any]:
    """Run the installed engine and report whether it works (used by the UI and ``cts doctor``)."""
    info = runtime_info()
    if not info["installed"]:
        return {**info, "ok": False, "detail": "llama.cpp is not installed", "hint": ""}
    ok, detail, code = verify(info["path"])
    return {**info, "ok": ok, "detail": detail, "hint": "" if ok else deps.hint_for(detail, code)}


# ----------------------------------------------------------------------- choosing assets
def _flavors_of(name: str) -> set[str]:
    lowered = name.lower()
    return {f for f in _FLAVORS if f in lowered}


def rank_assets(assets: list[dict[str, Any]], system: str, arch: str, flavor: str = "") -> list[dict[str, Any]]:
    """Archives that fit this machine, best first. The plain CPU build is always the last resort.

    Windows/Linux prefer Vulkan (any GPU vendor, no CUDA toolkit needed); macOS builds include
    Metal. ``flavor`` (or CTS_LLAMA_FLAVOR) forces ``cpu``, ``vulkan``, ``cuda``...
    """
    os_token = {"windows": "win", "macos": "macos", "linux": "ubuntu"}.get(system, system)
    candidates: list[dict[str, Any]] = []
    for asset in assets:
        name = asset.get("name", "").lower()
        if not name.startswith("llama-") or "-bin-" not in name or not name.endswith(_ARCHIVE_SUFFIXES):
            continue
        if f"-{os_token}-" not in name:
            continue
        if arch == "arm64" and "arm64" not in name:
            continue
        if arch == "x64" and ("arm64" in name or not ("x64" in name or "x86_64" in name or "amd64" in name)):
            continue
        candidates.append(asset)

    by_len = lambda items: sorted(items, key=lambda a: len(a["name"]))  # noqa: E731 - shortest name = least exotic
    plain = by_len([a for a in candidates if not _flavors_of(a["name"])])
    wanted = flavor.lower().strip()
    ordered: list[dict[str, Any]] = []
    if wanted and wanted != "cpu":
        ordered += by_len([a for a in candidates if wanted in _flavors_of(a["name"])])
    elif not wanted and system in ("windows", "linux"):
        ordered += by_len([a for a in candidates if _flavors_of(a["name"]) == {"vulkan"}])
    ordered += plain
    unique: list[dict[str, Any]] = []
    for asset in ordered:
        if asset not in unique:
            unique.append(asset)
    return unique


def pick_asset(assets: list[dict[str, Any]], system: str, arch: str, flavor: str = "") -> dict[str, Any] | None:
    ranked = rank_assets(assets, system, arch, flavor)
    return ranked[0] if ranked else None


def companion_assets(assets: list[dict[str, Any]], primary: dict[str, Any]) -> list[dict[str, Any]]:
    """CUDA builds need the separate ``cudart`` archive (CUDA runtime DLLs/libs) next to the binaries."""
    match = re.search(r"cuda-[\d.]+-(?:x64|arm64)", primary["name"].lower())
    if not match:
        return []
    return [a for a in assets if a.get("name", "").lower().startswith("cudart-") and match.group(0) in a["name"].lower()]


def synthesize_assets(tag: str, system: str, arch: str) -> list[dict[str, Any]]:
    """Asset names derived from llama.cpp's release naming scheme (used when the API is unavailable)."""
    names: list[str] = []
    if system == "macos":
        names = [f"llama-{tag}-bin-macos-{arch}.tar.gz"]
    elif system == "linux":
        names = [f"llama-{tag}-bin-ubuntu-vulkan-{arch}.tar.gz", f"llama-{tag}-bin-ubuntu-{arch}.tar.gz"]
    elif system == "windows":
        if arch == "x64":
            names.append(f"llama-{tag}-bin-win-vulkan-x64.zip")
        names.append(f"llama-{tag}-bin-win-cpu-{arch}.zip")
    return [{"name": n, "size": 0, "browser_download_url": f"{GITHUB}/releases/download/{tag}/{n}"} for n in names]


# --------------------------------------------------------------------------- releases
def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "CTS-comic-to-story"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def release_candidates(client: httpx.Client, limit: int = 4) -> list[dict[str, Any]]:
    """The newest llama.cpp builds, newest first, as [{tag_name, assets, source}].

    Only tags shaped like ``b12111`` count: other releases in the repository (``v0.9.0``...) have no
    binaries. Uses the GitHub API and, when that is rate-limited or blocked (60 requests/hour per IP),
    the public releases feed, which has no such limit.
    """
    problems: list[str] = []
    try:
        resp = client.get(RELEASES_API, headers=_github_headers())
        if resp.status_code == 200:
            found = [r for r in resp.json() if BUILD_TAG.match(str(r.get("tag_name", ""))) and not r.get("draft")]
            found.sort(key=lambda r: tag_number(r["tag_name"]), reverse=True)
            if found:
                return [{"tag_name": r["tag_name"], "assets": r.get("assets", []), "source": "api"} for r in found[:limit]]
            problems.append("the GitHub API listed no llama.cpp builds")
        else:
            problems.append(f"GitHub API answered {resp.status_code}")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        problems.append(f"GitHub API: {exc}")
    try:
        resp = client.get(RELEASES_ATOM, headers={"User-Agent": "CTS-comic-to-story"})
        if resp.status_code == 200:
            tags = sorted({t for t in re.findall(r"/releases/tag/([^\"'<>\s?#]+)", resp.text) if BUILD_TAG.match(t)},
                          key=tag_number, reverse=True)
            if tags:
                return [{"tag_name": t, "assets": [], "source": "feed"} for t in tags[:limit]]
            problems.append("the releases feed listed no llama.cpp builds")
        else:
            problems.append(f"releases feed answered {resp.status_code}")
    except httpx.HTTPError as exc:
        problems.append(f"releases feed: {exc}")
    raise RuntimeError(f"Could not find the latest llama.cpp build ({'; '.join(problems)}). {MANUAL_HELP}")


def latest_release(client: httpx.Client) -> dict[str, Any]:
    return release_candidates(client, limit=1)[0]


# ----------------------------------------------------------------------------- install
def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def inside(name: str) -> bool:
        target = (dest / name).resolve()
        return target == root or root in target.parents

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            if not all(inside(info.filename) for info in zf.infolist()):
                raise RuntimeError("Unsafe path in archive")
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            if not all(inside(member.name) for member in tf.getmembers()):
                raise RuntimeError("Unsafe path in archive")
            try:
                tf.extractall(dest, filter="data")  # Python 3.12+ (and security backports)
            except TypeError:
                tf.extractall(dest)


def _make_executable(folder: Path) -> None:
    if os.name == "nt":
        return
    for item in folder.rglob("*"):
        if item.is_file() and not item.is_symlink() and (item.name.startswith("llama-") or item.suffix == ""):
            item.chmod(item.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _flavor_label(name: str) -> str:
    flavors = sorted(_flavors_of(name))
    if flavors:
        return "+".join(flavors)
    return "metal" if "macos" in name.lower() else "cpu"


def _install_asset(
    client: httpx.Client, tag: str, asset: dict[str, Any], companions: list[dict[str, Any]],
    on_progress: Progress | None, is_cancelled: Callable[[], bool],
) -> tuple[Path, str]:
    """Download, unpack, verify (and repair) one build. Returns (server path, flavor label)."""
    flavor = _flavor_label(asset["name"])
    downloads = runtime_dir() / "downloads"
    staging = runtime_dir() / f"{tag}-{flavor}.tmp"
    final = runtime_dir() / f"{tag}-{flavor}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for item in [asset, *companions]:
            label = f"Downloading {item['name']}"
            total = int(item.get("size", 0) or 0)
            archive = downloads / item["name"]
            if on_progress:
                on_progress(label, 0, total)
            download_file(
                item["browser_download_url"], archive, expected_size=total,
                on_progress=(lambda n, label=label, total=total: on_progress(label, n, total)) if on_progress else None,
                is_cancelled=is_cancelled, client=client,
            )
            if on_progress:
                on_progress(f"Unpacking {item['name']}", 0, 0)
            try:
                _extract(archive, staging)
            except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError) as exc:
                archive.unlink(missing_ok=True)  # a corrupt download must not be reused
                raise RuntimeError(f"{item['name']} is damaged ({exc}); download it again") from exc
        server = next((p for p in sorted(staging.rglob(EXE)) if p.is_file()), None)
        if server is None:
            raise RuntimeError(f"llama-server was not found inside {asset['name']}")
        _make_executable(staging)

        if on_progress:
            on_progress("Checking that llama.cpp starts", 0, 0)
        ok, detail, code = verify(server)
        if not ok:
            changed, message = deps.try_fix(detail, code, str(server))
            if on_progress:
                on_progress(message[:140] or "Repairing", 0, 0)
            if changed:
                ok, detail, code = verify(server)
            if not ok:
                hint = deps.hint_for(detail, code) or message
                raise RuntimeError(f"it does not start on this computer ({detail[:200]}). {hint}".strip())

        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
        relative = server.relative_to(staging)
        return final / relative, flavor
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _cleanup_old(keep_name: str) -> None:
    """Remove superseded engines, leftovers of failed installs and cached archives.

    Compared by folder *name*: paths can differ by symlinks (macOS /var -> /private/var) and
    deleting the engine we just installed would be the worst possible outcome.
    """
    for child in runtime_dir().iterdir():
        if child.name in (keep_name, "current.json") or not child.is_dir():
            continue
        if child.name == "downloads" or child.suffix == ".tmp" or re.match(r"^b?\d+", child.name):
            shutil.rmtree(child, ignore_errors=True)


def install_runtime(
    on_progress: Progress | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
    update: bool = False,
    repair: bool = False,
    flavor: str | None = None,
    client: httpx.Client | None = None,
) -> Path:
    """Make sure a working llama-server exists and return its path.

    * default      - keep a working engine, install one if there is none (or it does not start);
    * ``update``   - move a CTS-managed engine to the newest llama.cpp release;
    * ``repair``   - reinstall even if an engine seems present.
    """
    system, arch = system_arch()
    wanted_flavor = flavor if flavor is not None else os.environ.get("CTS_LLAMA_FLAVOR", "")
    existing = find_server()
    managed = managed_info()
    cts_owned = bool(existing) and (_inside_runtime(existing) or (managed is not None and Path(managed["path"]) == existing))

    if existing and not repair:
        ok, _detail, _code = verify(existing)
        if ok and not cts_owned:
            return existing  # brew/winget/PATH/CTS_LLAMA_SERVER installs are the user's to update
        if ok and not update:
            return existing

    owns = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    try:
        releases = release_candidates(client)
        tag = releases[0]["tag_name"]
        installed_tag = managed.get("tag", "") if managed else ""
        if (existing and not repair and cts_owned and update and installed_tag
                and tag_number(tag) <= tag_number(installed_tag) and verify(existing)[0]):
            return existing  # already on the newest release

        failures: list[str] = []
        for release in releases:
            tag = release["tag_name"]
            assets = release["assets"]
            ranked = rank_assets(assets, system, arch, wanted_flavor) if assets else []
            if not ranked:  # no asset list (feed fallback) or nothing for this machine in it
                ranked = rank_assets(synthesize_assets(tag, system, arch), system, arch, wanted_flavor)
            if not ranked:
                failures.append(f"{tag}: no prebuilt build for {system}/{arch}")
                continue
            only_missing_files = True
            for asset in ranked:
                try:
                    server, flavor_label = _install_asset(
                        client, tag, asset, companion_assets(assets, asset), on_progress, is_cancelled)
                except (DownloadCancelled, KeyboardInterrupt):
                    raise
                except Exception as exc:  # noqa: BLE001 - try the next build (e.g. Vulkan -> CPU)
                    missing = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
                    only_missing_files = only_missing_files and missing
                    failures.append(f"{asset['name']}: {'not published (404)' if missing else exc}")
                    if on_progress:
                        on_progress("That build did not work, trying another", 0, 0)
                    continue
                _state_file().write_text(json.dumps({"tag": tag, "flavor": flavor_label, "path": str(server),
                                                     "asset": asset["name"]}), encoding="utf-8")
                _cleanup_old(_top_level(server).name)
                return server
            if not only_missing_files:
                break  # the build exists but does not run here; an older one will not help
        raise RuntimeError("llama.cpp could not be installed:\n  - " + "\n  - ".join(failures) + f"\n{MANUAL_HELP}")
    finally:
        if owns:
            client.close()


def _top_level(server: Path) -> Path:
    """The folder directly under runtime/ that contains ``server``."""
    base = runtime_dir().resolve()
    for parent in server.resolve().parents:
        if parent.parent == base:
            return parent
    return server.parent
