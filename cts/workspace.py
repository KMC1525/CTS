"""Server-side store for uploaded comic volumes (folders and ZIPs)."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from . import ingest
from .paths import workspace_dir

_lock = threading.RLock()


def _root() -> Path:
    return workspace_dir()


def _index_file() -> Path:
    return _root() / "volumes.json"


def _load_index() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_index_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    tmp = _index_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_index_file())


def attachment_dir(aid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", aid):
        raise ingest.IngestError("Bad attachment id")
    return _root() / "attachments" / aid


def create_attachment(label: str) -> str:
    aid = uuid.uuid4().hex[:12]
    (attachment_dir(aid) / "src").mkdir(parents=True)
    (attachment_dir(aid) / "label.txt").write_text(label, encoding="utf-8")
    return aid


def attachment_file_path(aid: str, relative: str) -> Path:
    rel = ingest.safe_relative(relative)
    target = (attachment_dir(aid) / "src" / rel).resolve()
    base = (attachment_dir(aid) / "src").resolve()
    if base not in target.parents:
        raise ingest.IngestError("Unsafe path")
    return target


def _clean_name(raw: str) -> str:
    name = re.sub(r"\.(zip|cbz)$", "", raw.strip(), flags=re.I)
    name = re.sub(r"[_]+", " ", name).strip()
    return name or "Untitled volume"


def finalize_attachment(aid: str) -> list[dict[str, Any]]:
    """Index everything uploaded under ``aid`` and return the volume(s) found."""
    base = attachment_dir(aid)
    src = base / "src"
    if not src.is_dir():
        raise ingest.IngestError("Nothing was uploaded")
    label_file = base / "label.txt"
    label = _clean_name(label_file.read_text(encoding="utf-8") if label_file.exists() else "Volume")

    specs: list[tuple[str, Path, ingest.VolumeSpec]] = []  # (name, root, spec)
    archives = sorted(
        (p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in ingest.ARCHIVE_EXTS),
        key=lambda p: ingest.natural_key(p.name),
    )
    for n, archive in enumerate(archives):
        dest = base / "extracted" / f"{n:02d}"
        dest.mkdir(parents=True, exist_ok=True)
        ingest.safe_extract_zip(archive, dest)
        archive.unlink(missing_ok=True)  # keep only the extracted pages
        for spec in ingest.discover_volumes(dest, _clean_name(archive.name)):
            specs.append((spec.name, dest, spec))

    has_loose = any(p.is_file() and ingest.is_image(p) for p in src.rglob("*"))
    if has_loose:
        for spec in ingest.discover_volumes(src, label):
            specs.append((spec.name, src, spec))
    if not specs:
        raise ingest.IngestError("No comic pages found (supported images: jpg, png, webp, bmp, gif, tiff)")

    created: list[dict[str, Any]] = []
    with _lock:
        index = _load_index()
        for name, root, spec in sorted(specs, key=lambda s: ingest.natural_key(s[0])):
            vid = uuid.uuid4().hex[:12]
            record = {
                "id": vid,
                "name": name,
                "attachment": aid,
                "root": root.relative_to(base).as_posix(),
                "pages": spec.pages,
                "created": time.time(),
            }
            index[vid] = record
            created.append(public(record))
        _save_index(index)
    return created


def public(record: dict[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "name": record["name"], "pages": len(record["pages"])}


def list_volumes() -> list[dict[str, Any]]:
    with _lock:
        records = _load_index().values()
        return [public(r) for r in sorted(records, key=lambda r: (ingest.natural_key(r["name"]), r["created"]))]


def get(vid: str) -> dict[str, Any] | None:
    with _lock:
        return _load_index().get(vid)


def page_paths(record: dict[str, Any]) -> list[Path]:
    base = attachment_dir(record["attachment"]) / record["root"]
    return [base / rel for rel in record["pages"]]


def page_path(record: dict[str, Any], index: int) -> Path | None:
    paths = page_paths(record)
    return paths[index] if 0 <= index < len(paths) else None


def delete(vid: str) -> bool:
    with _lock:
        index = _load_index()
        record = index.pop(vid, None)
        if record is None:
            return False
        _save_index(index)
        if not any(r["attachment"] == record["attachment"] for r in index.values()):
            shutil.rmtree(attachment_dir(record["attachment"]), ignore_errors=True)
        shutil.rmtree(_root() / "thumbs" / vid, ignore_errors=True)
        return True


def thumbnail(record: dict[str, Any], index: int) -> bytes | None:
    path = page_path(record, index)
    if path is None or not path.is_file():
        return None
    cache = _root() / "thumbs" / record["id"] / f"{index}.jpg"
    if cache.is_file():
        return cache.read_bytes()
    data = ingest.make_thumbnail(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data


def iter_pages(record: dict[str, Any]) -> Iterator[Path]:
    yield from page_paths(record)
