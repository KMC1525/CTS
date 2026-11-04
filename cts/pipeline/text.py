"""Text helpers: cleaning model output, reading the analyst's safety flags, spotting refusals."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PAGE_TYPES = ("story", "splash", "cover", "title", "credits", "advertisement", "extras", "blank")

_THINK_CLOSED = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think(?:ing)?>.*\Z", re.S | re.I)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$", re.M)
_PREAMBLE = re.compile(r"^(here(?:'s| is| are)|sure|certainly|okay|ok|of course|claro|aqui est)[^\n]{0,120}:\s*\n", re.I)
_PAGE_HEADER = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?(?:page|página|pagina|p\.)\s*\d+[^\n]{0,20}(?:\*\*)?\s*$\n?", re.I | re.M)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_ITALIC = re.compile(r"(?<![\w*])[*_]([^*_\n]+?)[*_](?![\w*])")


def strip_reasoning(text: str) -> str:
    text = _THINK_CLOSED.sub("", text)
    return _THINK_OPEN.sub("", text)


def clean_prose(text: str) -> str:
    """Normalise a model's page output to plain paragraphs."""
    text = strip_reasoning(text or "").strip()
    text = _FENCE.sub("", text).strip()
    text = _PREAMBLE.sub("", text, count=1)
    text = _PAGE_HEADER.sub("", text)
    text = _HEADING.sub("", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_document(text: str) -> str:
    """For dossiers / bibles: keep markdown, only drop reasoning and code fences."""
    text = strip_reasoning(text or "").strip()
    return _FENCE.sub("", text).strip()


def word_count(text: str) -> int:
    return len(text.split())


# ------------------------------------------------------------------- safety flags
STOP_MINOR = "[[CTS-STOP-MINOR]]"  # what the NSFW prompts tell the model to answer when the comic shows a minor


def stop_requested(text: str) -> bool:
    return STOP_MINOR in (text or "")


@dataclass
class Flags:
    page_type: str = "story"
    sexual_content: str = "unknown"  # none | suggestive | explicit | unknown
    minor_involved: str = "no"  # yes | no  (yes only when the COMIC makes it clear; see rating_nsfw.md)
    minor_evidence: str = ""
    violence: str = "unknown"
    complete: bool = False  # True when the analyst returned the FLAGS block
    characters: list[str] = field(default_factory=list)  # names listed on the page
    minor_names: list[str] = field(default_factory=list)  # characters whose STATED AGE is under 18

    @property
    def minor_clear(self) -> bool:
        return self.minor_involved == "yes" or bool(self.minor_names)


def _flag(pattern: str, text: str, allowed: tuple[str, ...]) -> str | None:
    m = re.search(pattern + r"\s*[:=]\s*\**\s*([A-Za-z_]+)", text, re.I)
    if m and m.group(1).lower() in allowed:
        return m.group(1).lower()
    return None


_SECTION = re.compile(r"CHARACTERS ON PAGE\s*:(.*?)(?:\n[A-Z][A-Z ]{3,}:|\Z)", re.S)
_CHAR_LINE = re.compile(r"^\s*[-*\u2022]\s*\**([^:\n]+?)\**\s*:(.*)$", re.M)
_STATED_AGE = re.compile(r"STATED AGE\s*:\s*\**\s*([^;\n|]+)", re.I)


def _stated_is_minor(value: str) -> bool:
    """True when the STATED AGE the analyst copied from the comic is clearly under 18."""
    value = value.strip().lower()
    if re.search(r"\bnot stated\b|\bunknown\b|\bn/a\b", value):
        return False
    if re.search(r"\bminor\b|\bunderage\b|\bunder\s*\d{1,2}\b|\bchild\b|\bkid\b|\bteen", value):
        return True  # checked first: in "under 18" the number is a limit, not an age
    number = re.search(r"\b(\d{1,2})\b", value)
    return bool(number) and int(number.group(1)) < 18


def parse_flags(dossier: str) -> Flags:
    flags = Flags()
    m = re.search(r"PAGE TYPE\s*:\s*\**\s*([A-Za-z]+)", dossier, re.I)
    if m and m.group(1).lower() in PAGE_TYPES:
        flags.page_type = m.group(1).lower()
    sexual = _flag(r"sexual_content", dossier, ("none", "suggestive", "explicit"))
    minor = _flag(r"minor_involved", dossier, ("no", "yes", "unclear"))
    violence = _flag(r"violence", dossier, ("none", "mild", "graphic"))
    if sexual:
        flags.sexual_content = sexual
    if minor:
        flags.minor_involved = "yes" if minor == "yes" else "no"  # "unclear" = the comic did not make it clear
    if violence:
        flags.violence = violence
    flags.complete = bool(sexual and minor)
    ev = re.search(r"minor_evidence\s*[:=]\s*(.+)", dossier, re.I)
    if ev and ev.group(1).strip().lower().strip("*`\"'. ") not in ("", "none", "n/a", "no"):
        flags.minor_evidence = ev.group(1).strip().strip("*`")[:240]
    section = _SECTION.search(dossier)
    if section:
        for line in _CHAR_LINE.finditer(section.group(1)):
            name, rest = line.group(1).strip(), line.group(2)
            flags.characters.append(name)
            stated = _STATED_AGE.search(rest)
            if stated and _stated_is_minor(stated.group(1)):
                flags.minor_names.append(name)
    return flags


def minor_evidence(flags: Flags, known_minors: set[str] | None = None) -> str | None:
    """Why this page must stop the NSFW edition, or None. Only what the COMIC makes clear counts.

    * the analyst flagged ``minor_involved: yes`` (stated age under 18, called a child/teen/minor, school
      grade, unmistakably a young child), or
    * a character's STATED AGE (copied from the comic) is under 18, or
    * a character who was established as a minor on an earlier page appears again.
    Characters whose age is not mentioned are adults, and drawing style alone never decides.
    """
    if flags.minor_involved == "yes":
        return flags.minor_evidence or "the comic makes it clear that a character is under 18"
    if flags.minor_names:
        return f"{', '.join(flags.minor_names)}: stated age under 18"
    if known_minors:
        again = [c for c in flags.characters if c.strip().lower() in known_minors]
        if again:
            return f"{', '.join(again)} was established as under 18 earlier in the story"
    return None


def decide_rating(requested: str, flags: Flags) -> tuple[str, str | None]:
    """Return (rating to use for this page, note if it was lowered to SFW).

    Pages with a clearly under-18 character never get here in the NSFW edition (they halt the process
    first, see ``minor_evidence``). What is left is a safety net for a malfunctioning analyst: if the
    page may be sexual but the analyst did not return its flags, nothing can be verified, so the page
    is written in SFW terms.
    """
    if requested != "nsfw":
        return "sfw", None
    if flags.sexual_content == "none":
        return "nsfw", None
    if not flags.complete:
        return "sfw", "The analyst did not return safety flags, so this page was written in SFW terms."
    return "nsfw", None


# ------------------------------------------------------------------------ covers
@dataclass
class CoverInfo:
    is_cover: bool = False  # yes AND not a low-confidence guess
    kind: str = "none"
    confidence: str = "low"
    title: str = ""
    volume: str = ""
    evidence: str = ""
    answered_yes: bool = False


def _field(name: str, text: str) -> str:
    m = re.search(rf"^\s*\**{name}\**\s*[:=]\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip().strip("*`\"' ") if m else ""


def parse_cover(text: str) -> CoverInfo:
    info = CoverInfo()
    info.answered_yes = _field("IS_COVER", text).lower().startswith("yes")
    kind = _field("COVER_KIND", text).lower()
    info.kind = kind if kind in ("front", "back", "spine", "inside", "wrap-around") else "none"
    conf = _field("CONFIDENCE", text).lower()
    info.confidence = conf if conf in ("high", "medium", "low") else "low"
    for attr, key in (("title", "TITLE"), ("volume", "VOLUME"), ("evidence", "EVIDENCE")):
        value = _field(key, text)
        setattr(info, attr, "" if value.lower() in ("none", "n/a", "") else value[:160])
    info.is_cover = info.answered_yes and info.confidence != "low"
    return info


# ----------------------------------------------------------------------- refusals
_REFUSAL = re.compile(
    r"^\W*(i['’]?m sorry|i am sorry|sorry[,.]|i can['’]?t|i cannot|i can not|i won['’]?t|i['’]?m unable|"
    r"i am unable|i['’]?m not able|unable to (?:help|assist|comply|continue)|as an ai|"
    r"i must decline|i apologi[sz]e|desculpe|lamento|não posso|nao posso|no puedo|lo siento)",
    re.I,
)


def looks_like_refusal(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(_REFUSAL.match(stripped)) and word_count(stripped) < 90


LANGUAGES: dict[str, str] = {
    "auto": "Same as comic",
    "English": "English",
    "Brazilian Portuguese": "Portuguese (Brazil)",
    "Spanish": "Spanish",
    "French": "French",
    "German": "German",
    "Italian": "Italian",
    "Japanese": "Japanese",
    "Chinese": "Chinese",
    "Korean": "Korean",
    "Russian": "Russian",
}


def language_instruction(language: str) -> str:
    if language in ("", "auto"):
        return ("the language of the comic's own lettering "
                "(if the lettering is Japanese, Chinese or Korean, write the novel in English)")
    return language


# ------------------------------------------------------------------- final review
MAX_DIRECTION_CHARS = 6000


def clean_direction(text: str | None) -> str:
    """The user's optional system prompt: trimmed, no control characters, capped in length."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (text or "")).replace("\r\n", "\n").strip()
    return cleaned[:MAX_DIRECTION_CHARS].rstrip()


def direction_block(text: str) -> str:
    """The author's direction as it is given to the writing agents (the rating policy is appended after it)."""
    if not text:
        return ""
    return (
        "## Author's direction (supplied by the user for this novel)\n\n"
        f"{text}\n\n"
        "How to use it: it steers the voice, tone, style, point of view, tense, vocabulary, emphasis and the "
        "choices you make when turning the comic into prose, on every page. It does NOT change the events or "
        "the dialogue that the comic page shows, the page-for-page contract, the rating policy or the age rule "
        "below: those always win over it."
    )


@dataclass
class PageReview:
    ok: bool
    summary: str = ""
    problem: str = ""


_REVIEW_LINE = re.compile(
    r"^\s*[-*]?\s*\**PAGE\s+(\d+)\**\s*[|:-]\s*\**(OK|ISSUE)\**\s*(?:[|:-]\s*(.*))?$", re.I | re.M)


def parse_review_window(text: str) -> dict[int, PageReview]:
    """Read the revisor's one-line-per-page answer. Pages it did not mention are simply absent."""
    out: dict[int, PageReview] = {}
    for m in _REVIEW_LINE.finditer(text or ""):
        rest = m.group(3) or ""
        summary = re.search(r"summary\s*:\s*(.*?)(?:\s*\|\s*problem\s*:|$)", rest, re.I)
        problem = re.search(r"problem\s*:\s*(.*)$", rest, re.I)
        ok = m.group(2).upper() == "OK"
        out[int(m.group(1))] = PageReview(
            ok=ok,
            summary=(summary.group(1).strip() if summary else rest.split("|")[0].strip())[:200],
            problem="" if ok else ((problem.group(1).strip() if problem else rest.strip()) or "inconsistent with the rest of the novel")[:300],
        )
    return out


def parse_page_list(value: str, total: int) -> list[int]:
    """'4, 17-19' -> [4, 17, 18, 19]; 'none' -> []; clipped to 1..total."""
    value = (value or "").strip().lower()
    if not value or value.startswith(("none", "n/a", "-")):
        return []
    pages: set[int] = set()
    for part in re.split(r"[,;\s]+(?:and\s+)?", value):
        rng = re.fullmatch(r"(\d+)\s*(?:-|–|to)\s*(\d+)", part)
        if rng:
            low, high = int(rng.group(1)), int(rng.group(2))
            pages.update(range(min(low, high), min(max(low, high), total) + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(p for p in pages if 1 <= p <= total)


@dataclass
class Verdict:
    verdict: str = "unknown"  # coherent | fix-pages | redo-all | unknown (answer not understood)
    pages: list[int] = field(default_factory=list)
    all_pages: bool = False
    notes: str = ""
    reason: str = ""


def parse_verdict(text: str, total: int) -> Verdict:
    v = Verdict()
    m = re.search(r"VERDICT\s*[:=]\s*\**\s*(coherent|fix-pages|fix pages|redo-all|redo all)", text or "", re.I)
    if m:
        v.verdict = m.group(1).lower().replace(" ", "-")
    pages = re.search(r"^\s*\**PAGES\**\s*[:=]\s*(.+)$", text or "", re.I | re.M)
    if pages:
        raw = pages.group(1).strip().strip("*`")
        v.all_pages = raw.lower().startswith("all")
        v.pages = [] if v.all_pages else parse_page_list(raw, total)
    for attr, key in (("notes", "NOTES"), ("reason", "REASON")):
        found = re.search(rf"^\s*\**{key}\**\s*[:=]\s*(.+(?:\n(?!\s*\**[A-Z]+\**\s*[:=]).+)*)", text or "", re.I | re.M)
        if found:
            value = " ".join(found.group(1).split())[:600]
            setattr(v, attr, "" if value.strip(" .*`").lower() in ("none", "n/a") else value)
    return v


_PLACEHOLDER = re.compile(r"^\[Page \d+ could not be", re.I)
_LEAK = re.compile(r"\[\[CTS-|\bas an ai\b|^\s*(here is|here's) (the|your) (page|text)", re.I)


def _shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def mechanical_issues(pages: list[tuple[int, str]]) -> dict[int, str]:
    """Free, deterministic checks over (page number, text) pairs: nothing here needs an LLM.

    Catches what can never be coherent: empty pages, placeholders for pages that failed, refusals,
    leaked instructions and a page that repeats the one before it.
    """
    issues: dict[int, str] = {}
    previous: tuple[int, set] | None = None
    for number, text in pages:
        body = (text or "").strip()
        if not body:
            issues[number] = "the page is empty"
        elif _PLACEHOLDER.match(body):
            issues[number] = "the page was never written (placeholder)"
        elif looks_like_refusal(body):
            issues[number] = "the page is a refusal, not prose"
        elif _LEAK.search(body):
            issues[number] = "the page contains an instruction or an assistant remark instead of only prose"
        else:
            current = _shingles(body)
            if previous and len(current) >= 8 and len(previous[1]) >= 8:
                overlap = len(current & previous[1]) / max(1, min(len(current), len(previous[1])))
                if overlap >= 0.8:
                    issues[number] = f"the page repeats page {previous[0]} almost word for word"
            previous = (number, current)
    return issues
