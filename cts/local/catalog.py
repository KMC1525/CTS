"""Hugging Face catalog: curated vision-capable GGUF models and quantization discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

HF = "https://huggingface.co"

# Vision-capable GGUF repos (each ships an `mmproj` projector so the model can read pages).
CURATED: list[dict[str, str]] = [
    {"id": "gemma3-4b", "label": "Gemma 3 4B - light, runs on most laptops", "repo": "ggml-org/gemma-3-4b-it-GGUF"},
    {"id": "qwen25vl-7b", "label": "Qwen2.5-VL 7B - strong at reading comic text", "repo": "ggml-org/Qwen2.5-VL-7B-Instruct-GGUF"},
    {"id": "qwen3vl-8b", "label": "Qwen3-VL 8B - newer, many quantizations", "repo": "unsloth/Qwen3-VL-8B-Instruct-GGUF"},
    {"id": "gemma3-12b", "label": "Gemma 3 12B - better prose, needs ~10 GB", "repo": "unsloth/gemma-3-12b-it-GGUF"},
    {"id": "smolvlm2", "label": "SmolVLM2 2.2B - tiny, for weak machines", "repo": "ggml-org/SmolVLM2-2.2B-Instruct-GGUF"},
]

_QUANT = re.compile(r"(?:^|[-_.])((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32))$", re.I)
_SHARD = re.compile(r"^(?P<base>.+)-(?P<i>\d{5})-of-(?P<n>\d{5})$")
_PREFERRED = ["Q4_K_M", "UD-Q4_K_XL", "Q4_K_S", "IQ4_XS", "Q4_0", "Q5_K_M", "Q8_0"]
_HINTS = [
    (re.compile(r"^(UD-)?(I?Q[12])", re.I), "smallest - noticeably lower quality"),
    (re.compile(r"^(UD-)?(I?Q3)", re.I), "small - some quality loss"),
    (re.compile(r"^(UD-)?(I?Q4)", re.I), "balanced - recommended for most people"),
    (re.compile(r"^(UD-)?(I?Q5)", re.I), "high quality"),
    (re.compile(r"^(UD-)?(I?Q6)", re.I), "very high quality"),
    (re.compile(r"^(UD-)?(I?Q8)", re.I), "near-lossless"),
    (re.compile(r"^(BF16|F16|F32)$", re.I), "full precision - very large"),
]


@dataclass
class RepoFile:
    path: str
    size: int


def normalise_repo(raw: str) -> str:
    """Accept 'owner/name' or a full huggingface.co URL."""
    text = raw.strip()
    text = re.sub(r"^https?://(www\.)?huggingface\.co/", "", text).strip("/")
    text = text.split("/tree/")[0].split("/blob/")[0].split("/resolve/")[0]
    if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", text):
        raise ValueError("Enter a Hugging Face repo like 'owner/model-name-GGUF'")
    return text


def _headers(token: str = "") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def list_repo_files(repo: str, token: str = "", client: httpx.Client | None = None) -> list[RepoFile]:
    owns = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        resp = client.get(f"{HF}/api/models/{repo}/tree/main", params={"recursive": "true"}, headers=_headers(token))
        if resp.status_code == 404:
            raise ValueError(f"Repository not found: {repo}")
        if resp.status_code in (401, 403):
            raise ValueError("This repository is gated or private - add a Hugging Face token in Settings.")
        resp.raise_for_status()
        return [
            RepoFile(e["path"], int((e.get("lfs") or {}).get("size") or e.get("size") or 0))
            for e in resp.json() if e.get("type") == "file"
        ]
    finally:
        if owns:
            client.close()


def search_models(query: str, token: str = "", limit: int = 15) -> list[dict[str, Any]]:
    params = {"search": query, "filter": "gguf", "pipeline_tag": "image-text-to-text",
              "sort": "downloads", "direction": "-1", "limit": str(limit)}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(f"{HF}/api/models", params=params, headers=_headers(token))
        resp.raise_for_status()
        return [{"repo": m["id"], "downloads": m.get("downloads", 0), "likes": m.get("likes", 0)} for m in resp.json()]


def quant_label(stem: str) -> str:
    m = _QUANT.search(stem)
    return m.group(1).upper() if m else stem


def _hint(label: str) -> str:
    for pattern, text in _HINTS:
        if pattern.search(label):
            return text
    return ""


def describe_repo(files: list[RepoFile]) -> dict[str, Any]:
    """Group GGUF files into installable quantizations and find the vision projector."""
    groups: dict[str, dict[str, Any]] = {}
    mmproj: list[RepoFile] = []
    for f in files:
        base = f.path.rsplit("/", 1)[-1]
        if not base.lower().endswith(".gguf"):
            continue
        if base.lower().startswith("mmproj"):
            mmproj.append(f)
            continue
        stem = base[:-5]
        shard = _SHARD.match(stem)
        key_stem = shard.group("base") if shard else stem
        folder = f.path.rsplit("/", 1)[0] if "/" in f.path else ""
        key = f"{folder}/{key_stem}"
        label = quant_label(key_stem)
        group = groups.setdefault(key, {"label": label, "files": [], "size": 0})
        group["files"].append(f.path)
        group["size"] += f.size

    quants = []
    for group in groups.values():
        group["files"].sort()
        quants.append({"label": group["label"], "files": group["files"], "size": group["size"],
                       "hint": _hint(group["label"])})
    # de-duplicate identical labels (keep the first file set, smallest first)
    quants.sort(key=lambda q: q["size"])
    seen: set[str] = set()
    unique = []
    for q in quants:
        if q["label"] in seen:
            q["label"] = f'{q["label"]} ({q["files"][0].rsplit("/", 1)[-1]})'
        seen.add(q["label"])
        unique.append(q)
    recommended = next((q["label"] for p in _PREFERRED for q in unique if q["label"].upper() == p), None)
    if recommended is None and unique:
        recommended = unique[len(unique) // 2]["label"]
    return {
        "quants": unique,
        "recommended": recommended,
        "mmproj": [{"path": m.path, "size": m.size} for m in mmproj],
        "default_mmproj": pick_mmproj([m.path for m in mmproj]),
        "vision": bool(mmproj),
    }


def pick_mmproj(paths: list[str]) -> str | None:
    """Prefer the f16 projector (best quality/size), then bf16, q8, f32."""
    if not paths:
        return None
    for token in ("f16", "bf16", "q8", "f32"):
        for p in paths:
            name = p.rsplit("/", 1)[-1].lower()
            if token == "f16" and "bf16" in name:
                continue
            if token in name:
                return p
    return sorted(paths)[0]
