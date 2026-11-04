"""Installed local models: registry, background installs and removal."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .. import settings as settings_mod
from ..paths import models_dir
from . import catalog, download, runtime

_lock = threading.RLock()
installs: dict[str, dict[str, Any]] = {}  # install_id -> live progress (polled by the UI)
_cancel: dict[str, threading.Event] = {}


def _registry_file() -> Path:
    return models_dir() / "registry.json"


def _load() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_registry_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(reg: dict[str, dict[str, Any]]) -> None:
    tmp = _registry_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    tmp.replace(_registry_file())


def is_gguf(path: Path) -> bool:
    """A real GGUF file starts with the magic bytes 'GGUF' (a failed download is often an HTML error page)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"GGUF"
    except OSError:
        return False


def model_id(repo: str, quant: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", f"{repo.replace('/', '__')}__{quant}")


def list_installed() -> list[dict[str, Any]]:
    with _lock:
        reg = _load()
    out = []
    for m in reg.values():
        files_ok = all((models_dir() / f).is_file() for f in m["files"])
        out.append({**m, "ok": files_ok, "vision": bool(m.get("mmproj"))})
    return sorted(out, key=lambda m: m["installed_at"], reverse=True)


def get_installed(mid: str) -> dict[str, Any] | None:
    with _lock:
        return _load().get(mid)


def paths_for(m: dict[str, Any]) -> tuple[Path, Path | None]:
    """(first GGUF file, projector or None) as absolute paths."""
    first = models_dir() / sorted(m["files"])[0]
    mm = models_dir() / m["mmproj"] if m.get("mmproj") else None
    return first, mm


def delete(mid: str) -> bool:
    with _lock:
        reg = _load()
        m = reg.pop(mid, None)
        if m is None:
            return False
        _save(reg)
    for rel in m["files"] + ([m["mmproj"]] if m.get("mmproj") else []):
        still_used = any(rel in o["files"] or rel == o.get("mmproj") for o in reg.values())
        if not still_used:
            (models_dir() / rel).unlink(missing_ok=True)
    try:
        (models_dir() / m["repo"].replace("/", "__")).rmdir()
    except OSError:
        pass
    s = settings_mod.load()
    if s.local.active_model == mid:
        s.local.active_model = ""
        settings_mod.save(s)
    return True


def start_install(repo: str, quant: str, token: str = "") -> str:
    """Download the chosen quantization (+ vision projector) in a background thread."""
    repo = catalog.normalise_repo(repo)
    iid = model_id(repo, quant)
    with _lock:
        if installs.get(iid, {}).get("status") == "running":
            return iid
        installs[iid] = {"id": iid, "repo": repo, "quant": quant, "status": "running", "message": "Preparing...",
                         "done": 0, "total": 0, "started": time.time()}
        _cancel[iid] = threading.Event()
    threading.Thread(target=_install_worker, args=(iid, repo, quant, token), daemon=True).start()
    return iid


def cancel_install(iid: str) -> None:
    if iid in _cancel:
        _cancel[iid].set()


def _install_worker(iid: str, repo: str, quant: str, token: str) -> None:
    state = installs[iid]
    cancelled = _cancel[iid]
    try:
        files = catalog.list_repo_files(repo, token)
        info = catalog.describe_repo(files)
        chosen = next((q for q in info["quants"] if q["label"] == quant), None)
        if chosen is None:
            raise RuntimeError(f"Quantization {quant} was not found in {repo}")
        sizes = {f.path: f.size for f in files}
        wanted = list(chosen["files"])
        mmproj = info["default_mmproj"]
        if mmproj:
            wanted.append(mmproj)
        state["total"] = sum(sizes.get(p, 0) for p in wanted)
        done_before = 0
        repo_dir = repo.replace("/", "__")
        stored: list[str] = []
        for path in wanted:
            rel = f"{repo_dir}/{path.rsplit('/', 1)[-1]}"
            state["message"] = f"Downloading {path.rsplit('/', 1)[-1]}"

            def progress(n: int, base: int = done_before) -> None:
                state["done"] = base + n

            download.download_file(
                download.resolve_url(repo, path), models_dir() / rel, expected_size=sizes.get(path, 0),
                token=token, on_progress=progress, is_cancelled=cancelled.is_set,
            )
            if not is_gguf(models_dir() / rel):
                (models_dir() / rel).unlink(missing_ok=True)
                raise RuntimeError(f"{path.rsplit('/', 1)[-1]} was downloaded but is not a valid GGUF file "
                                   "(Hugging Face may have answered with an error page). Try again.")
            done_before += sizes.get(path, 0)
            stored.append(rel)
        entry = {
            "id": iid, "repo": repo, "quant": quant, "label": f"{repo.split('/')[-1]} - {quant}",
            "files": stored[: len(chosen["files"])], "mmproj": stored[-1] if mmproj else None,
            "size": sum(sizes.get(p, 0) for p in wanted), "installed_at": time.time(),
        }
        with _lock:
            reg = _load()
            reg[iid] = entry
            _save(reg)
        s = settings_mod.load()
        s.local.active_model = iid
        s.provider_type = "local"
        settings_mod.save(s)
        state.update(status="done", message="Installed", done=state["total"])
    except download.DownloadCancelled:
        state.update(status="cancelled", message="Cancelled (progress is kept so you can resume)")
    except Exception as exc:  # noqa: BLE001
        state.update(status="error", message=str(exc)[:500])


# ------------------------------------------------------------------- runtime install
runtime_install: dict[str, Any] = {"status": "idle", "message": "", "done": 0, "total": 0}


def start_runtime_install(update: bool = False, repair: bool = False) -> None:
    """Install / update / repair the llama.cpp engine in a background thread (progress is polled)."""
    with _lock:
        if runtime_install["status"] == "running":
            return
        runtime_install.update(status="running", message="Preparing...", done=0, total=0, detail="")

    def work() -> None:
        try:
            def progress(msg: str, done: int, total: int) -> None:
                runtime_install.update(message=msg, done=done, total=total)

            runtime.install_runtime(progress, update=update, repair=repair)
            runtime_install.update(status="done", message="Ready", done=0, total=0)
        except Exception as exc:  # noqa: BLE001
            runtime_install.update(status="error", message=str(exc)[:900])

    threading.Thread(target=work, daemon=True).start()


def runtime_status() -> dict[str, Any]:
    return {**runtime.runtime_info(), "install": dict(runtime_install)}
