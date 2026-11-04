import io
import zipfile

import pytest
from PIL import Image

from cts import ingest


def png(path, size=(40, 60)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (10, 20, 30)).save(path)


def test_natural_order():
    names = ["p10.jpg", "p2.jpg", "p1.jpg", "Vol 10", "Vol 2"]
    # numbers compare as numbers, letters case-insensitively
    assert sorted(names, key=ingest.natural_key) == ["p1.jpg", "p2.jpg", "p10.jpg", "Vol 2", "Vol 10"]


def test_single_folder_is_one_volume(tmp_path):
    for n in (10, 2, 1):
        png(tmp_path / f"{n}.png")
    (tmp_path / ".hidden.png").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    [vol] = ingest.discover_volumes(tmp_path, "My Comic")
    assert vol.name == "My Comic"
    assert vol.pages == ["1.png", "2.png", "10.png"]


def test_wrapper_folder_names_the_volume(tmp_path):
    png(tmp_path / "Berserk Vol 3" / "001.png")
    [vol] = ingest.discover_volumes(tmp_path, "upload")
    assert vol.name == "Berserk Vol 3" and vol.pages == ["Berserk Vol 3/001.png"]


def test_volume_like_subfolders_become_volumes(tmp_path):
    for v in ("Vol 2", "Vol 10", "Vol 1"):
        png(tmp_path / v / "1.png")
    vols = ingest.discover_volumes(tmp_path, "series")
    assert [v.name for v in vols] == ["Vol 1", "Vol 2", "Vol 10"]


def test_chapter_folders_without_volume_names_stay_one_volume(tmp_path):
    for c in ("intro", "story"):
        png(tmp_path / c / "1.png")
    [vol] = ingest.discover_volumes(tmp_path, "x")
    assert len(vol.pages) == 2


def test_no_pages_is_an_error(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    with pytest.raises(ingest.IngestError):
        ingest.discover_volumes(tmp_path, "x")


def test_zip_slip_and_junk_are_skipped(tmp_path):
    archive = tmp_path / "a.zip"
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.png", buf.getvalue())
        zf.writestr("/abs.png", buf.getvalue())
        zf.writestr("__MACOSX/._1.png", b"junk")
        zf.writestr("ok/1.png", buf.getvalue())
    dest = tmp_path / "out"
    dest.mkdir()
    # "../evil.png" is dropped, "/abs.png" is re-rooted inside dest, __MACOSX junk is ignored
    assert ingest.safe_extract_zip(archive, dest) == 2
    assert not (tmp_path / "evil.png").exists() and not (tmp_path.parent / "evil.png").exists()
    assert (dest / "ok" / "1.png").exists() and (dest / "abs.png").exists()
    assert not (dest / "__MACOSX").exists()


def test_bad_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ingest.IngestError):
        ingest.safe_extract_zip(bad, tmp_path)


def test_safe_relative_rejects_traversal():
    for bad in ("../x.png", "a/../../x.png", "", "C:/x.png"):
        with pytest.raises(ingest.IngestError):
            ingest.safe_relative(bad)
    assert ingest.safe_relative("a\\b/c.png").as_posix() == "a/b/c.png"


def test_characters_windows_cannot_store_are_replaced_not_dropped(tmp_path):
    assert ingest.safe_relative("Vol 1: Intro/What?.png").as_posix() == "Vol 1_ Intro/What_.png"
    archive = tmp_path / "a.zip"
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Vol 1: Intro/001.png", buf.getvalue())
    dest = tmp_path / "out"
    dest.mkdir()
    assert ingest.safe_extract_zip(archive, dest) == 1 and (dest / "Vol 1_ Intro" / "001.png").exists()


def test_encode_resizes_and_flattens_alpha(tmp_path):
    p = tmp_path / "wide.png"
    Image.new("RGBA", (3000, 1000), (255, 0, 0, 0)).save(p)
    data, mime = ingest.encode_for_model(p, max_side=1000)
    img = Image.open(io.BytesIO(data))
    assert mime == "image/jpeg" and img.size == (1000, 333) and img.mode == "RGB"
    assert img.getpixel((5, 5))[0] > 240  # transparent -> white, not black
