"""Resumable file downloads (with progress and cancel) for Hugging Face files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from .catalog import HF, _headers


class DownloadCancelled(Exception):
    pass


def resolve_url(repo: str, path: str) -> str:
    return f"{HF}/{repo}/resolve/main/{quote(path)}?download=true"


def download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int = 0,
    token: str = "",
    on_progress: Callable[[int], None] | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
    client: httpx.Client | None = None,
) -> Path:
    """Download ``url`` to ``dest`` (resuming a previous ``.part`` file). Returns ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (not expected_size or dest.stat().st_size == expected_size):
        if on_progress:
            on_progress(dest.stat().st_size)
        return dest
    part = dest.with_name(dest.name + ".part")
    owns = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    try:
        for _attempt in range(2):
            have = part.stat().st_size if part.exists() else 0
            headers = dict(_headers(token))
            if have:
                headers["Range"] = f"bytes={have}-"
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 416:  # nothing left to fetch, or a stale .part
                    if expected_size and have == expected_size:
                        break
                    part.unlink(missing_ok=True)
                    continue
                if resp.status_code in (401, 403):
                    raise RuntimeError("Access denied by Hugging Face - this model needs a token (see Settings).")
                resp.raise_for_status()
                mode = "ab" if (have and resp.status_code == 206) else "wb"
                if mode == "wb":
                    have = 0
                with open(part, mode) as out:
                    for chunk in resp.iter_bytes(1024 * 256):
                        if is_cancelled():
                            raise DownloadCancelled()
                        out.write(chunk)
                        have += len(chunk)
                        if on_progress:
                            on_progress(have)
            break
        size = part.stat().st_size if part.exists() else 0
        if expected_size and size != expected_size:
            raise RuntimeError(f"Download incomplete ({size} of {expected_size} bytes). Try again to resume.")
        os.replace(part, dest)
        return dest
    finally:
        if owns:
            client.close()
