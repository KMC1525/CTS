"""Text helpers: cleaning model output, reading the analyst's safety flags, spotting refusals."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
_MINOR_WORDS = re.compile(
    r"\b(child|children|kid|kids|teen|teens|teenage|teenager|teenagers|adolescent|preteen|underage|"
    r"minor|minors|schoolgirl|schoolboy|school uniform|toddler|infant|loli|lolicon|shota|"
    r"childlike|child-like|juvenile)\b",
    re.I,
)


@dataclass
class Flags:
    page_type: str = "story"
    sexual_content: str = "unknown"  # none | suggestive | explicit | unknown
    minor_involved: str = "unclear"  # no | yes | unclear
    violence: str = "unknown"
    complete: bool = False  # True when the analyst returned the FLAGS block

    @property
    def minor_signal(self) -> bool:
        return self.minor_involved != "no"


def _flag(pattern: str, text: str, allowed: tuple[str, ...]) -> str | None:
    m = re.search(pattern + r"\s*[:=]\s*\**\s*([A-Za-z_]+)", text, re.I)
    if m and m.group(1).lower() in allowed:
        return m.group(1).lower()
    return None


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
        flags.minor_involved = minor
    if violence:
        flags.violence = violence
    flags.complete = bool(sexual and minor)
    # Belt and braces: the dossier's own character descriptions can reveal a young character
    # the flag missed. Only the CHARACTERS section is scanned to avoid false alarms.
    chars = re.search(r"CHARACTERS ON PAGE\s*:(.*?)(?:\n[A-Z][A-Z ]{3,}:|\Z)", dossier, re.S)
    if chars and _MINOR_WORDS.search(chars.group(1)) and flags.minor_involved == "no":
        flags.minor_involved = "unclear"
    return flags


def decide_rating(requested: str, flags: Flags) -> tuple[str, str | None]:
    """Return (rating to use for this page, note if it was forced down to SFW).

    The age guard is enforced here in code, independent of what any model decides: a page
    that may involve a minor AND has any sexual content - or whose flags are missing - is
    never written in the adult edition.
    """
    if requested != "nsfw":
        return "sfw", None
    if flags.sexual_content == "none":
        return "nsfw", None
    if not flags.complete:
        return "sfw", "The analyst did not return safety flags, so this page was written in SFW terms."
    if flags.minor_signal:
        return "sfw", "Age guard: a character may be a minor, so this page was written without sexual content."
    return "nsfw", None


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
