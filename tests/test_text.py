import pytest

from cts.pipeline import text
from cts.pipeline.budget import recent_history, trim_bible, history_token_budget


def test_clean_prose_strips_scaffolding():
    raw = ("<think>plan the page</think>Here is the page:\n```\n# Page 3\n\n**Mira** walked on.\n\n\n\nShe *did not* look back.\n```")
    assert text.clean_prose(raw) == "Mira walked on.\n\nShe did not look back."


def test_unclosed_think_is_dropped():
    assert text.clean_prose("<think>endless reasoning with no end") == ""


def dossier(chars="- Mira: short black hair; APPARENT AGE: adult; STATED AGE: not stated", sexual="explicit",
            minor="no", evidence="none"):
    return (f"PAGE TYPE: splash\nCHARACTERS ON PAGE:\n{chars}\nMOOD AND PACING: calm\nFLAGS:\nsexual_content: {sexual}\n"
            f"minor_involved: {minor}\nminor_evidence: {evidence}\nviolence: mild\n")


def test_parse_flags():
    f = text.parse_flags(dossier())
    assert (f.page_type, f.sexual_content, f.minor_involved, f.violence, f.complete) == ("splash", "explicit", "no", "mild", True)
    assert f.characters == ["Mira"] and f.minor_names == [] and f.minor_evidence == ""


def test_missing_flags_are_incomplete_and_treated_as_not_clear():
    f = text.parse_flags("PAGE TYPE: story\nSETTING: a room")
    assert not f.complete and f.minor_involved == "no" and not f.minor_clear


def test_unclear_means_the_comic_did_not_make_it_clear_so_it_is_not_a_minor():
    f = text.parse_flags(dossier(minor="unclear"))
    assert f.minor_involved == "no" and text.minor_evidence(f) is None


# --- the age rule: only what the COMIC says counts ---------------------------------------------------
def test_age_not_mentioned_means_adult_and_the_page_continues():
    f = text.parse_flags(dossier(chars="- Mira: schoolgirl look, small build; APPARENT AGE: teen; STATED AGE: not stated"))
    assert text.minor_evidence(f) is None  # looking young / wearing a uniform is not "clear"
    assert text.decide_rating("nsfw", f) == ("nsfw", None)


def test_stated_adult_age_continues():
    f = text.parse_flags(dossier(chars="- Mira: dark hair; APPARENT AGE: young adult; STATED AGE: 18\n- Ken: grey suit; APPARENT AGE: adult; STATED AGE: adult"))
    assert text.minor_evidence(f) is None and f.minor_names == []


@pytest.mark.parametrize("stated", ["17", "16", "12", "minor", "under 18"])
def test_a_stated_age_under_18_is_clear_even_if_the_flag_says_no(stated):
    f = text.parse_flags(dossier(chars=f"- Mira: dark hair; APPARENT AGE: adult; STATED AGE: {stated}", minor="no"))
    assert f.minor_names == ["Mira"] and "Mira" in text.minor_evidence(f)


def test_the_flag_with_evidence_is_enough():
    f = text.parse_flags(dossier(minor="yes", evidence="Mira - the caption says she is 15"))
    assert f.minor_clear and text.minor_evidence(f) == "Mira - the caption says she is 15"
    assert text.minor_evidence(text.parse_flags(dossier(minor="yes"))).startswith("the comic makes it clear")


def test_a_character_established_as_a_minor_stays_a_minor_on_later_pages():
    later = text.parse_flags(dossier(chars="- Mira: red coat; APPARENT AGE: adult; STATED AGE: not stated"))
    assert text.minor_evidence(later) is None
    assert "established as under 18" in text.minor_evidence(later, {"mira"})
    assert text.minor_evidence(later, {"someone else"}) is None


def test_decide_rating_only_lowers_to_sfw_when_the_analyst_returned_no_flags():
    assert text.decide_rating("sfw", text.parse_flags(dossier()))[0] == "sfw"
    assert text.decide_rating("nsfw", text.parse_flags(dossier(sexual="none"))) == ("nsfw", None)
    rating, note = text.decide_rating("nsfw", text.parse_flags("PAGE TYPE: story\nSETTING: x\nsexual_content: explicit"))
    assert rating == "sfw" and "safety flags" in note


def test_stop_token():
    assert text.stop_requested("[[CTS-STOP-MINOR]] Mira is 15 according to the caption.")
    assert not text.stop_requested("Rain hammered the rooftop.")


# --- covers ----------------------------------------------------------------------------------------
def test_parse_cover_front():
    c = text.parse_cover("IS_COVER: yes\nCOVER_KIND: front\nCONFIDENCE: high\nTITLE: Midnight Rooftop\nVOLUME: 1\nEVIDENCE: Title logo.")
    assert (c.is_cover, c.kind, c.title, c.volume, c.confidence) == (True, "front", "Midnight Rooftop", "1", "high")


def test_low_confidence_or_no_is_not_a_cover():
    assert not text.parse_cover("IS_COVER: yes\nCOVER_KIND: front\nCONFIDENCE: low\nTITLE: none").is_cover
    assert not text.parse_cover("IS_COVER: no\nCOVER_KIND: none\nCONFIDENCE: high").is_cover
    assert not text.parse_cover("I think this might be a cover?").is_cover
    c = text.parse_cover("**IS_COVER:** yes\n**CONFIDENCE:** medium\n**TITLE:** none")
    assert c.is_cover and c.title == ""  # markdown bold and 'none' titles are handled


def test_refusal_detection():
    assert text.looks_like_refusal("I'm sorry, but I can't help with that.")
    assert text.looks_like_refusal("Desculpe, não posso ajudar com isso.")
    assert not text.looks_like_refusal("\"I can't,\" she whispered. " + "word " * 120)
    assert not text.looks_like_refusal("Rain hammered the rooftop.")


def test_recent_history_respects_budget():
    pages = [f"page {i} " + "x" * 400 for i in range(10)]
    out = recent_history(pages, 250)
    assert "page 9" in out and "page 0" not in out
    assert recent_history([], 100) == ""
    assert len(recent_history(["y" * 5000], 100)) < 400  # a single huge page is truncated to its tail
    assert history_token_budget(8192) < history_token_budget(131072)


def test_trim_bible_keeps_structure():
    bible = "# Story Bible\n" + "\n".join(f"- line {i} " + "word " * 10 for i in range(500))
    cut = trim_bible(bible, 200)
    assert cut.startswith("# Story Bible\n- line 0") and len(cut) < len(bible)
