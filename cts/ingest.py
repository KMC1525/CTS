"""Turn an uploaded folder or ZIP into ordered comic pages, and prepare images for the models."""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
ARCHIVE_EXTS = {".zip", ".cbz"}
MAX_ARCHIVE_BYTES = 12 * 1024**3  # refuse zip bombs
MAX_ARCHIVE_FILES = 20_000

# Folder names that look like "a separate volume" (vol 1, volume_02, v3, tome 4, book 5, #6, ...)
_VOLUME_NAME = re.compile(r"(^|[\s._\-\[(])(vol(ume)?|v|tome|book|issue|ch(apter)?|#)\s*\.?\s*\d+", re.I)

Image.MAX_IMAGE_PIXELS = 400_000_000  # comic scans are big; still guard against decompression bombs


class IngestError(ValueError):
    pass


def natural_key(text: str) -> list:
    """Sort key so that 'page2' < 'page10' and 'Vol 2' < 'Vol 10'."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text)]


def is_image(path: Path | str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_relative(raw: str) -> Path:
    """Sanitise a client-supplied relative path.

    Blocks '..' and drive letters; absolute paths are re-rooted. Characters that Windows cannot
    store (``<>:"|?*``) are replaced with ``_`` on every platform, so a folder called
    "Vol 1: Intro" works everywhere instead of silently losing its pages.
    """
    posix = PurePosixPath(raw.replace("\\", "/"))
    parts = [p for p in posix.parts if p not in ("", ".", "/")]
    if not parts or any(p == ".." for p in parts) or re.match(r"^[A-Za-z]:$", parts[0]):
        raise IngestError(f"Unsafe path: {raw!r}")
    return Path(*[_INVALID_CHARS.sub("_", p) for p in parts])


def _is_junk(rel: Path) -> bool:
    return any(p.startswith(".") or p == "__MACOSX" or p.lower() == "thumbs.db" for p in rel.parts)


def safe_extract_zip(archive: Path, dest: Path) -> int:
    """Extract an archive, guarding against zip-slip and zip bombs. Returns files extracted."""
    count = 0
    dest = dest.resolve()
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{archive.name} is not a valid ZIP/CBZ file") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ARCHIVE_FILES:
            raise IngestError("Archive contains too many files")
        if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
            raise IngestError("Archive is too large when extracted")
        for info in infos:
            try:
                rel = safe_relative(info.filename)
            except IngestError:
                continue  # skip hostile entries instead of failing the whole volume
            if _is_junk(rel):
                continue
            target = (dest / rel).resolve()
            if dest not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


@dataclass
class VolumeSpec:
    name: str
    pages: list[str]  # POSIX paths relative to the scanned root, in reading order


def _images_under(root: Path, base: Path) -> list[Path]:
    found = [p for p in base.rglob("*") if p.is_file() and is_image(p) and not _is_junk(p.relative_to(root))]
    return sorted(found, key=lambda p: natural_key(p.relative_to(base).as_posix()))


def discover_volumes(root: Path, fallback_name: str) -> list[VolumeSpec]:
    """Find the volume(s) inside an extracted upload.

    A folder/ZIP is ONE volume, except when it has no pages of its own and several
    sub-folders whose names look like volumes (vol 1, volume 02, v3, tome 4...): each of
    those becomes its own volume, in natural order.
    """
    root = root.resolve()
    top_level_images = [p for p in root.iterdir() if p.is_file() and is_image(p)]
    subdirs = sorted(
        (d for d in root.iterdir() if d.is_dir() and not _is_junk(d.relative_to(root))),
        key=lambda d: natural_key(d.name),
    )
    volume_dirs = [d for d in subdirs if _VOLUME_NAME.search(d.name) and _images_under(root, d)]
    if not top_level_images and len(volume_dirs) >= 2:
        return [
            VolumeSpec(d.name, [p.relative_to(root).as_posix() for p in _images_under(root, d)])
            for d in volume_dirs
        ]
    pages = _images_under(root, root)
    if not pages:
        raise IngestError("No comic pages found (supported images: jpg, png, webp, bmp, gif, tiff)")
    # A single wrapper folder ("Vol 1/001.jpg") gives the nicest volume name.
    name = fallback_name
    if not top_level_images and len(subdirs) == 1:
        name = subdirs[0].name
    return [VolumeSpec(name, [p.relative_to(root).as_posix() for p in pages])]


# --------------------------------------------------------------------------- images
def load_page_image(path: Path) -> Image.Image:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img.load()
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            return background
        return img.convert("RGB")


def encode_for_model(path: Path, max_side: int = 1568, quality: int = 85) -> tuple[bytes, str]:
    """Resize (never upscale) and JPEG-encode a page for a vision model."""
    img = load_page_image(path)
    longest = max(img.size)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"


def make_thumbnail(path: Path, max_side: int = 360) -> bytes:
    img = load_page_image(path)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    return buf.getvalue()


def make_preview(path: Path, max_side: int = 900) -> bytes:
    img = load_page_image(path)
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72)
    return buf.getvalue()
