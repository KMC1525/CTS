from cts.pipeline import text
from cts.pipeline.budget import recent_history, trim_bible, history_token_budget


def test_clean_prose_strips_scaffolding():
    raw = ("<think>plan the page</think>Here is the page:\n```\n# Page 3\n\n**Mira** walked on.\n\n\n\nShe *did not* look back.\n```")
    assert text.clean_prose(raw) == "Mira walked on.\n\nShe did not look back."


def test_unclosed_think_is_dropped():
    assert text.clean_prose("<think>endless reasoning with no end") == ""


FLAGS_OK = "PAGE TYPE: splash\nCHARACTERS ON PAGE:\n- Mira: adult woman\nFLAGS:\nsexual_content: explicit\nminor_involved: no\nviolence: mild\n"


def test_parse_flags():
    f = text.parse_flags(FLAGS_OK)
    assert (f.page_type, f.sexual_content, f.minor_involved, f.violence, f.complete) == ("splash", "explicit", "no", "mild", True)


def test_missing_flags_are_incomplete():
    f = text.parse_flags("PAGE TYPE: story\nSETTING: a room")
    assert not f.complete and f.minor_involved == "unclear"


def test_age_guard_decisions():
    ok = text.parse_flags(FLAGS_OK)
    assert text.decide_rating("nsfw", ok) == ("nsfw", None)
    assert text.decide_rating("sfw", ok)[0] == "sfw"
    minor = text.parse_flags(FLAGS_OK.replace("minor_involved: no", "minor_involved: yes"))
    rating, note = text.decide_rating("nsfw", minor)
    assert rating == "sfw" and "minor" in note
    unclear = text.parse_flags(FLAGS_OK.replace("minor_involved: no", "minor_involved: unclear"))
    assert text.decide_rating("nsfw", unclear)[0] == "sfw"
    # no sexual content at all -> the edition is irrelevant, keep NSFW prose rules for violence etc.
    clean = text.parse_flags(FLAGS_OK.replace("explicit", "none").replace("minor_involved: no", "minor_involved: yes"))
    assert text.decide_rating("nsfw", clean)[0] == "nsfw"


def test_missing_flags_fail_safe_when_sexual_unknown():
    flags = text.parse_flags("PAGE TYPE: story")
    assert flags.sexual_content == "unknown"
    # unknown sexual content + unknown age must never be written as adult content
    rating, note = text.decide_rating("nsfw", flags)
    assert rating == "sfw" and note


def test_character_description_can_reveal_a_minor_the_flag_missed():
    dossier = FLAGS_OK.replace("adult woman", "teenage girl in a school uniform")
    f = text.parse_flags(dossier)
    assert f.minor_involved == "unclear"
    assert text.decide_rating("nsfw", f)[0] == "sfw"


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
