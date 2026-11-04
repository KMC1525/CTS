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
