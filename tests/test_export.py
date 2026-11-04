import docx
from pypdf import PdfReader

from cts.export import Novel, NovelPage, write_all


def sample(n=5, words=120, language="English"):
    pages = [NovelPage(i, ("Mira looked at the rain. " * (words // 5)).strip() + "\n\n\"Go,\" she said.") for i in range(1, n + 1)]
    pages[0].page_type = "cover"
    return Novel("Vol. 1 - Café ção", pages, language=language, rating="nsfw")


def test_all_formats_one_pdf_page_per_comic_page(tmp_path):
    files = write_all(sample(), tmp_path, "Vol 1 / test")
    assert set(files) == {"txt", "docx", "pdf"} and all(p.exists() for p in files.values())
    assert len(PdfReader(str(files["pdf"])).pages) == 1 + 5  # title page + one page per comic page
    txt = files["txt"].read_text(encoding="utf-8")
    assert txt.count("--- Page ") == 5 and "NSFW edition" in txt
    d = docx.Document(str(files["docx"]))
    assert len(d.sections) == 2 and "Mira looked" in "\n".join(p.text for p in d.paragraphs)


def test_long_pages_still_fit_one_pdf_page(tmp_path):
    files = write_all(sample(n=2, words=700), tmp_path, "long")
    assert len(PdfReader(str(files["pdf"])).pages) == 3


def test_unicode_and_cjk_do_not_crash(tmp_path):
    novel = Novel("日本語の小説", [NovelPage(1, "こんにちは世界。 emoji 😀 ünïcödé")])
    files = write_all(novel, tmp_path, "cjk")
    assert len(PdfReader(str(files["pdf"])).pages) == 2
