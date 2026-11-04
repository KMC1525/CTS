"""Write a finished novel as TXT, DOCX or PDF - one comic page = one novel page."""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

FORMATS = ("txt", "docx", "pdf")
PAGE_W_IN, PAGE_H_IN = 6.0, 9.0  # trade paperback


@dataclass
class NovelPage:
    index: int  # 1-based, equals the comic page number
    text: str
    page_type: str = "story"


@dataclass
class Novel:
    title: str
    pages: list[NovelPage]
    language: str = ""
    rating: str = "sfw"
    subtitle: str = "A novel adapted from the comic, page for page"
    colophon: str = "Generated with CTS (Comic To Story). AI-generated adaptation."
    meta: dict = field(default_factory=dict)

    @property
    def rating_label(self) -> str:
        return "NSFW edition (adults only)" if self.rating == "nsfw" else "SFW edition"


def split_paragraphs(text: str) -> list[str]:
    parts = [re.sub(r"[ \t]*\n[ \t]*", " ", p).strip() for p in re.split(r"\n\s*\n", text.strip())]
    return [p for p in parts if p]


def _body_font_size(novel: Novel, base: float = 11.0) -> float:
    """Shrink the body font a little when the longest page would not fit a 6x9in page."""
    longest = max((len(p.text.split()) for p in novel.pages), default=0)
    capacity_at_base = 330.0
    if longest <= capacity_at_base:
        return base
    return max(8.5, round(base * math.sqrt(capacity_at_base / longest) * 2) / 2)


# ----------------------------------------------------------------------------- TXT
def write_txt(novel: Novel, path: Path) -> Path:
    lines = [novel.title.upper(), "", novel.subtitle, f"({novel.rating_label})", ""]
    for page in novel.pages:
        lines += ["", f"--- Page {page.index} ---", "", "\n\n".join(split_paragraphs(page.text)) or "", ""]
    lines += ["", "-" * 40, novel.colophon, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------- DOCX
def write_docx(novel: Novel, path: Path) -> Path:
    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    doc = Document()
    size = _body_font_size(novel)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(size)
    rfonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(attr), "Times New Roman")
    rfonts.set(qn("w:eastAsia"), "MS Mincho")
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.15

    def configure(section) -> None:
        section.page_width, section.page_height = Inches(PAGE_W_IN), Inches(PAGE_H_IN)
        section.left_margin = section.right_margin = Inches(0.75)
        section.top_margin, section.bottom_margin = Inches(0.7), Inches(0.8)

    first = doc.sections[0]
    configure(first)

    # Title page ------------------------------------------------------------
    for _ in range(5):
        doc.add_paragraph()
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(novel.title)
    run.bold, run.font.size = True, Pt(26)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.paragraph_format.space_before = Pt(14)
    r = sub.add_run(novel.subtitle)
    r.italic, r.font.size = True, Pt(12)
    badge = doc.add_paragraph()
    badge.alignment = WD_ALIGN_PARAGRAPH.CENTER
    badge.paragraph_format.space_before = Pt(8)
    badge.add_run(novel.rating_label).font.size = Pt(10)
    colophon = doc.add_paragraph()
    colophon.alignment = WD_ALIGN_PARAGRAPH.CENTER
    colophon.paragraph_format.space_before = Pt(160)
    colophon.add_run(novel.colophon).font.size = Pt(8.5)

    # Body section: numbering restarts at 1 so novel page N shows "N" ---------
    body = doc.add_section(WD_SECTION.NEW_PAGE)
    configure(body)
    pg_num = OxmlElement("w:pgNumType")
    pg_num.set(qn("w:start"), "1")
    body._sectPr.append(pg_num)
    body.footer.is_linked_to_previous = False
    footer_p = body.footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer_p.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
    run.font.size = Pt(9)

    for n, page in enumerate(novel.pages):
        paragraphs = split_paragraphs(page.text) or [""]
        for i, para in enumerate(paragraphs):
            p = doc.add_paragraph(para)
            p.paragraph_format.first_line_indent = Inches(0.25) if i else Inches(0)
            p.paragraph_format.space_after = Pt(4)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            if page.page_type in ("cover", "title"):
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Inches(0)
        if n < len(novel.pages) - 1:
            doc.paragraphs[-1].add_run().add_break(WD_BREAK.PAGE)

    doc.core_properties.title = novel.title
    doc.core_properties.author = "CTS - Comic To Story"
    doc.save(str(path))
    return path


# ------------------------------------------------------------------------------ PDF
_FONT_CANDIDATES: list[tuple[str, str | None, int]] = [
    # (regular, bold, subfont index) - first one that registers wins
    ("C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/timesbd.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Times New Roman.ttf", "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf", 0),
    ("/usr/share/fonts/liberation/LiberationSerif-Regular.ttf", "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 0),
    ("/usr/share/fonts/dejavu/DejaVuSerif.ttf", "/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf", 0),
]
_CJK_CANDIDATES: list[tuple[str, int]] = [
    ("C:/Windows/Fonts/msgothic.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", 0),
]
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def _register_fonts(sample_text: str) -> tuple[str, str, set[int] | None]:
    """Return (regular, bold, supported-codepoints|None) registered with reportlab."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates: list[tuple[str, str | None, int]] = []
    if _CJK_RE.search(sample_text):
        candidates += [(p, None, idx) for p, idx in _CJK_CANDIDATES]
    candidates += _FONT_CANDIDATES
    for n, (regular, bold, idx) in enumerate(candidates):
        if not Path(regular).is_file():
            continue
        try:
            reg_name, bold_name = f"CTSRegular{n}", f"CTSBold{n}"
            font = TTFont(reg_name, regular, subfontIndex=idx)
            pdfmetrics.registerFont(font)
            if bold and Path(bold).is_file():
                pdfmetrics.registerFont(TTFont(bold_name, bold, subfontIndex=idx))
            else:
                bold_name = reg_name
            return reg_name, bold_name, set(font.face.charToGlyph.keys())
        except Exception:  # unreadable font file: try the next one
            continue
    return "Times-Roman", "Times-Bold", None  # built-in: Latin-1 / WinAnsi only


def _printable(text: str, supported: set[int] | None) -> str:
    if supported is not None:
        return "".join(ch if (ord(ch) in supported or ch in "\n\t") else "?" for ch in text)
    return text.encode("cp1252", errors="replace").decode("cp1252")


def write_pdf(novel: Novel, path: Path) -> Path:
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.platypus import KeepInFrame, Paragraph

    page_w, page_h = PAGE_W_IN * inch, PAGE_H_IN * inch
    sample = novel.title + " ".join(p.text for p in novel.pages)
    regular, bold, supported = _register_fonts(sample)
    size = _body_font_size(novel)

    body_style = ParagraphStyle(
        "body", fontName=regular, fontSize=size, leading=size * 1.32, alignment=TA_JUSTIFY, spaceAfter=size * 0.4
    )
    indent_style = ParagraphStyle("indent", parent=body_style, firstLineIndent=size * 1.6)
    center_style = ParagraphStyle("center", parent=body_style, alignment=TA_CENTER, firstLineIndent=0)
    title_style = ParagraphStyle("title", fontName=bold, fontSize=26, leading=32, alignment=TA_CENTER)
    sub_style = ParagraphStyle("sub", fontName=regular, fontSize=12, leading=16, alignment=TA_CENTER)
    small_style = ParagraphStyle("small", fontName=regular, fontSize=8.5, leading=11, alignment=TA_CENTER)

    margin_x, margin_top, margin_bottom = 0.75 * inch, 0.7 * inch, 0.85 * inch
    frame_w, frame_h = page_w - 2 * margin_x, page_h - margin_top - margin_bottom

    def para(text: str, style) -> Paragraph:
        return Paragraph(escape(_printable(text, supported)), style)

    c = canvas.Canvas(str(path), pagesize=(page_w, page_h))
    c.setTitle(novel.title)
    c.setAuthor("CTS - Comic To Story")

    # Title page
    flow = KeepInFrame(
        frame_w, frame_h,
        [
            para(novel.title, title_style),
            para(" ", sub_style),
            para(novel.subtitle, sub_style),
            para(novel.rating_label, sub_style),
            para(" ", sub_style), para(" ", sub_style), para(" ", sub_style),
            para(novel.colophon, small_style),
        ],
        mode="shrink",
    )
    w, h = flow.wrapOn(c, frame_w, frame_h)
    flow.drawOn(c, margin_x, page_h - margin_top - h - frame_h * 0.28)
    c.showPage()

    # One PDF page per comic page.
    for page in novel.pages:
        paragraphs = split_paragraphs(page.text) or [""]
        centered = page.page_type in ("cover", "title")
        flowables = [
            para(text, center_style if centered else (indent_style if i else body_style))
            for i, text in enumerate(paragraphs)
        ]
        kif = KeepInFrame(frame_w, frame_h, flowables, mode="shrink")
        kif.wrapOn(c, frame_w, frame_h)
        kif.drawOn(c, margin_x, margin_bottom)
        c.setFont(regular, 9)
        c.drawCentredString(page_w / 2, margin_bottom * 0.45, str(page.index))
        c.showPage()

    c.save()
    return path


WRITERS = {"txt": write_txt, "docx": write_docx, "pdf": write_pdf}


def write_all(novel: Novel, out_dir: Path, stem: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\- ]+", "_", stem).strip(" ._") or "novel"
    results: dict[str, Path] = {}
    for fmt, writer in WRITERS.items():
        results[fmt] = writer(novel, out_dir / f"{safe}.{fmt}")
    return results
