"""End-to-end pipeline tests: real CrewAI agents talking to a local mock of the OpenAI API."""
import json
import re

import pytest

from conftest import local_spec
from cts.pipeline import runner as runner_mod
from cts.pipeline.runner import AgeHalt, Cancelled, FatalError, RunOptions, VolumeRunner
from mock_llm import ANALYSIS, BIBLE, FRONT_COVER, NOT_COVER, PROSE


def make_runner(mock, options=None, cancel=lambda: False, **kw):
    events = []
    # covers are off by default here: tiny test volumes would otherwise add a cover check to every page
    r = VolumeRunner(local_spec(mock), options or RunOptions(words_per_page=120, skip_covers=False), events.append, cancel,
                     retry_delays=(0.05, 0.05), **kw)
    return r, events


def user_text(messages):
    out = []
    for m in messages:
        c = m["content"]
        out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict)))
    return "\n".join(out)


def test_full_volume_runs_every_agent_and_exports(mock_llm, pages, tmp_path):
    mock = mock_llm()
    r, events = make_runner(mock)
    result = r.run_volume(0, "Vol 1", pages, tmp_path / "work")
    assert result.complete and len(result.records) == 3
    roles = [mock.role_of(q["messages"]) for q in mock.requests]
    assert {x: roles.count(x) for x in set(roles)} == {"analyst": 3, "novelist": 3, "editor": 3, "keeper": 3}
    assert mock.images_seen() == 3  # exactly one image per comic page, only to the analyst
    assert all(r_["text"] == PROSE and r_["status"] == "done" for r_ in result.records)
    assert result.bible.startswith("# Story Bible")
    assert set(result.files) == {"txt", "docx", "pdf"} and (tmp_path / "work" / result.files["pdf"]).exists()
    first_steps = [(e["step"], e["status"]) for e in events if e["type"] == "step" and e["page"] == 1]
    assert first_steps == [("analyze", "running"), ("analyze", "done"), ("write", "running"), ("write", "done"),
                           ("polish", "running"), ("polish", "done"), ("memory", "running"), ("memory", "done")]


def test_only_the_analyst_sees_the_image_and_context_flows_forward(mock_llm, pages, tmp_path):
    mock = mock_llm()
    r, _ = make_runner(mock)
    r.run_volume(0, "Vol 1", pages, tmp_path / "work")
    for req in mock.requests:
        role = mock.role_of(req["messages"])
        has_image = any(isinstance(m["content"], list) and any(p.get("type") == "image_url" for p in m["content"]) for m in req["messages"])
        assert has_image == (role in ("analyst", "cover"))
    novelist = [q for q in mock.requests if mock.role_of(q["messages"]) == "novelist"]
    assert "(none - this is the first page)" in user_text(novelist[0]["messages"])
    second = user_text(novelist[1]["messages"])
    assert PROSE.split(".")[0] in second and "# Story Bible" in second  # previous page + bible reach page 2
    assert "Rain hammered" in user_text(novelist[1]["messages"])


def test_braces_and_odd_text_in_model_output_do_not_break_crewai(mock_llm, pages, tmp_path):
    analysis = ANALYSIS.replace('"I told you', '"{name} {0} {{x}} I told you')
    mock = mock_llm(lambda role, msgs: {"analyst": analysis, "keeper": BIBLE}.get(role, PROSE))
    r, _ = make_runner(mock)
    assert r.run_volume(0, "V", pages[:1], tmp_path / "w").complete


def adult_dossier(chars="- Mira: short black hair, red coat; APPARENT AGE: adult; STATED AGE: not stated",
                  sexual="explicit", minor="no", evidence="none"):
    return (f"PAGE TYPE: story\nREADING DIRECTION: left-to-right\nSETTING: a bedroom\nCHARACTERS ON PAGE:\n{chars}\n"
            f"MOOD AND PACING: intimate\nFLAGS:\nsexual_content: {sexual}\nminor_involved: {minor}\n"
            f"minor_evidence: {evidence}\nviolence: none\n")


def run_nsfw(mock_llm, pages, tmp_path, analysis, n=1, writer=None):
    def handler(role, msgs):
        if role == "analyst":
            return analysis if not callable(analysis) else analysis(msgs)
        if role == "keeper":
            return BIBLE
        return writer(msgs) if writer else PROSE

    mock = mock_llm(handler)
    r, events = make_runner(mock, RunOptions(rating="nsfw", words_per_page=120, skip_covers=False))
    return mock, r, events, lambda: r.run_volume(0, "Midnight Rooftop", pages[:n], tmp_path / "w")


@pytest.mark.parametrize("dossier_kwargs,why", [
    (dict(minor="yes", evidence="Mira - the caption says she is 15"), "caption says she is 15"),
    (dict(chars="- Mira: dark hair; APPARENT AGE: adult; STATED AGE: 16"), "stated age under 18"),
    (dict(sexual="none", minor="yes", evidence="Mira - a young child"), "young child"),  # no sexual content: still stops
])
def test_a_character_the_comic_shows_to_be_under_18_stops_the_whole_nsfw_process(mock_llm, pages, tmp_path, dossier_kwargs, why):
    mock, r, events, go = run_nsfw(mock_llm, pages, tmp_path, adult_dossier(**dossier_kwargs))
    with pytest.raises(AgeHalt) as halt:
        go()
    assert why in str(halt.value) and halt.value.page == 1 and "under 18" in str(halt.value)
    assert "SFW edition" in str(halt.value)
    roles = [mock.role_of(q["messages"]) for q in mock.requests]
    assert "novelist" not in roles and "editor" not in roles  # nothing was written for that page
    assert [e for e in events if e["type"] == "age_halt"][0]["page"] == 1
    assert not (tmp_path / "w" / "pages" / "page_0001.json").exists()


def test_halting_keeps_the_pages_written_before_it(mock_llm, pages, tmp_path):
    calls = {"n": 0}

    def analysis(msgs):
        calls["n"] += 1
        return adult_dossier(sexual="none") if calls["n"] < 3 else adult_dossier(minor="yes", evidence="Ken - 14 years old")

    mock, r, events, go = run_nsfw(mock_llm, pages, tmp_path, analysis, n=3)
    with pytest.raises(AgeHalt) as halt:
        go()
    assert halt.value.page == 3
    assert (tmp_path / "w/pages/page_0001.json").exists() and (tmp_path / "w/pages/page_0002.json").exists()
    assert list((tmp_path / "w/output").glob("*_partial.pdf"))  # the first two pages are exported as a partial novel


@pytest.mark.parametrize("chars", [
    "- Mira: school uniform, small build; APPARENT AGE: teen; STATED AGE: not stated",   # age not mentioned -> adult
    "- Mira: dark hair; APPARENT AGE: young adult; STATED AGE: 18",                       # 18 is an adult
    "- Mira: grey suit; APPARENT AGE: adult; STATED AGE: adult\n- Ken: beard; APPARENT AGE: adult; STATED AGE: 41",
])
def test_adults_and_unmentioned_ages_continue_in_the_nsfw_edition(mock_llm, pages, tmp_path, chars):
    mock, r, events, go = run_nsfw(mock_llm, pages, tmp_path, adult_dossier(chars=chars))
    rec = go().records[0]
    assert rec["status"] == "done" and rec["rating_used"] == "nsfw" and rec["notes"] == []
    writer_req = next(q for q in mock.requests if mock.role_of(q["messages"]) == "novelist")
    assert "Rating for this page: NSFW" in user_text(writer_req["messages"])
    system = writer_req["messages"][0]["content"]
    assert "Age rule" in system and "[[CTS-STOP-MINOR]]" in system and "No age is mentioned -> continue" in system


def test_a_minor_established_earlier_stays_a_minor_on_later_pages_and_after_a_resume(mock_llm, pages, tmp_path):
    state = {"n": 0}

    def analysis(msgs):
        state["n"] += 1
        # page 1 is not explicit and states the age on the first line, page 2 shows the same character without it
        if state["n"] == 1:
            return adult_dossier(sexual="none", chars="- Mira: red coat; APPARENT AGE: adult; STATED AGE: 15", minor="no")
        return adult_dossier(sexual="explicit")

    # SFW first: minors are irrelevant there, nothing halts and the pages are saved
    mock = mock_llm(lambda role, msgs: {"analyst": analysis(msgs), "keeper": BIBLE}.get(role, PROSE))
    sfw, _ = make_runner(mock, RunOptions(rating="sfw", words_per_page=120, skip_covers=False))
    assert sfw.run_volume(0, "V", pages[:2], tmp_path / "w").complete

    # now the NSFW edition over the same pages: page 1 already states the age -> halts there
    state["n"] = 0
    nsfw, _ = make_runner(mock, RunOptions(rating="nsfw", words_per_page=120, skip_covers=False))
    with pytest.raises(AgeHalt) as halt:
        nsfw.run_volume(0, "V", pages[:2], tmp_path / "w2")
    assert halt.value.page == 1


def test_known_minor_is_remembered_across_pages(mock_llm, pages, tmp_path):
    stated = adult_dossier(sexual="none", chars="- Mira: red coat; APPARENT AGE: adult; STATED AGE: not stated")
    r_mock = mock_llm(lambda role, msgs: {"analyst": stated, "keeper": BIBLE}.get(role, PROSE))
    r, _ = make_runner(r_mock, RunOptions(rating="nsfw", words_per_page=120, skip_covers=False))
    r.known_minors = {"mira"}  # established on an earlier page / volume
    with pytest.raises(AgeHalt, match="established as under 18"):
        r.run_volume(0, "V", pages[:1], tmp_path / "w")


def test_the_model_can_stop_the_process_itself_with_the_stop_token(mock_llm, pages, tmp_path):
    mock, r, events, go = run_nsfw(
        mock_llm, pages, tmp_path, adult_dossier(),
        writer=lambda msgs: "[[CTS-STOP-MINOR]] Mira is described as a 14-year-old in the caption.")
    with pytest.raises(AgeHalt, match="14-year-old"):
        go()


def test_the_sfw_edition_never_halts_for_ages(mock_llm, pages, tmp_path):
    flagged = adult_dossier(minor="yes", evidence="Mira - 15", chars="- Mira: x; APPARENT AGE: teen; STATED AGE: 15")
    mock = mock_llm(lambda role, msgs: {"analyst": flagged, "keeper": BIBLE}.get(role, PROSE))
    r, _ = make_runner(mock, RunOptions(rating="sfw", words_per_page=120, skip_covers=False))
    rec = r.run_volume(0, "V", pages[:1], tmp_path / "w").records[0]
    assert rec["status"] == "done" and rec["rating_used"] == "sfw"
    writer_req = next(q for q in mock.requests if mock.role_of(q["messages"]) == "novelist")
    assert "Age rule" not in writer_req["messages"][0]["content"]  # the adult policy never reaches an SFW run


def test_nsfw_edition_for_adult_pages(mock_llm, pages, tmp_path):
    mock, r, events, go = run_nsfw(mock_llm, pages, tmp_path, adult_dossier())
    rec = go().records[0]
    assert rec["rating_used"] == "nsfw" and rec["notes"] == []


def test_missing_flags_trigger_one_retry_then_fail_safe(mock_llm, pages, tmp_path):
    no_flags = ANALYSIS.split("FLAGS:")[0]
    mock = mock_llm(lambda role, msgs: {"analyst": no_flags, "keeper": BIBLE}.get(role, PROSE))
    r, _ = make_runner(mock, RunOptions(rating="nsfw", words_per_page=120))
    rec = r.run_volume(0, "V", pages[:1], tmp_path / "w").records[0]
    assert sum(1 for q in mock.requests if mock.role_of(q["messages"]) == "analyst") == 2
    assert rec["rating_used"] == "sfw" and any("safety flags" in n for n in rec["notes"])


def test_nsfw_refusal_falls_back_to_sfw(mock_llm, pages, tmp_path):
    adult = ANALYSIS.replace("sexual_content: none", "sexual_content: explicit")

    def handler(role, msgs):
        if role == "analyst":
            return adult
        if role == "keeper":
            return BIBLE
        return "I'm sorry, but I can't help with that request." if "Rating for this page: NSFW" in user_text(msgs) else PROSE

    mock = mock_llm(handler)
    r, _ = make_runner(mock, RunOptions(rating="nsfw", words_per_page=120))
    rec = r.run_volume(0, "V", pages[:1], tmp_path / "w").records[0]
    assert rec["rating_used"] == "sfw" and rec["text"] == PROSE and any("declined" in n for n in rec["notes"])


def test_total_refusal_leaves_a_placeholder_and_the_book_keeps_its_page_count(mock_llm, pages, tmp_path):
    mock = mock_llm(lambda role, msgs: {"analyst": ANALYSIS, "keeper": BIBLE}.get(role, "I cannot assist with this."))
    r, _ = make_runner(mock)
    rec = r.run_volume(0, "V", pages[:1], tmp_path / "w").records[0]
    assert rec["text"].startswith("[Page 1 could not be adapted")


def test_overlong_pages_are_condensed(mock_llm, pages, tmp_path):
    long_text = "word " * 400

    def handler(role, msgs):
        if role == "analyst":
            return ANALYSIS
        if role == "keeper":
            return BIBLE
        return PROSE if "too long" in user_text(msgs) else long_text

    mock = mock_llm(handler)
    r, _ = make_runner(mock)
    rec = r.run_volume(0, "V", pages[:1], tmp_path / "w").records[0]
    assert rec["text"] == PROSE and rec["words"] < 40


class FlakySteps:
    """Stands in for the CrewAI steps so the runner's own retry policy can be tested exactly."""

    def __init__(self, failures, error):
        self.failures, self.error, self.calls = failures, error, 0

    def analyze(self, ctx, token, reminder=""):
        from cts.pipeline.crew import StepOutput

        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return StepOutput(ANALYSIS, 5)

    def write_and_polish(self, ctx, dossier, rating, note, on_draft_done=None):
        from cts.pipeline.crew import StepOutput

        if on_draft_done:
            on_draft_done()
        return StepOutput(PROSE, 5), StepOutput(PROSE, 0)

    def condense(self, ctx, text, rating):
        raise AssertionError("not expected")

    def remember(self, ctx, dossier, final_text):
        from cts.pipeline.crew import StepOutput

        return StepOutput(BIBLE, 5)


def test_transient_errors_are_retried_with_a_visible_log(mock_llm, pages, tmp_path):
    steps = FlakySteps(2, ConnectionError("Connection reset by peer"))
    r, events = make_runner(mock_llm(), steps=steps)
    assert r.run_volume(0, "V", pages[:1], tmp_path / "w").complete and steps.calls == 3
    assert [e["message"][:25] for e in events if e["type"] == "log"] == ["Reading the page failed (", "Reading the page failed ("]


def test_auth_errors_are_fatal_immediately(mock_llm, pages, tmp_path):
    steps = FlakySteps(99, RuntimeError("Error code: 401 - invalid api key"))
    r, _ = make_runner(mock_llm(), steps=steps)
    with pytest.raises(FatalError, match="401"):
        r.run_volume(0, "V", pages[:1], tmp_path / "w")
    assert steps.calls == 1  # no pointless retries


def test_persistent_transient_errors_fail_the_page_not_the_volume(mock_llm, pages, tmp_path):
    steps = FlakySteps(3, ConnectionError("down"))  # 3 tries (initial + 2 retries) all fail on page 1 only
    r, events = make_runner(mock_llm(), steps=steps)
    result = r.run_volume(0, "V", pages[:2], tmp_path / "w")
    assert [rec["status"] for rec in result.records] == ["failed", "done"] and not result.complete
    assert result.records[0]["text"].startswith("[Page 1 could not be generated")


def test_http_level_auth_error_from_the_provider_is_fatal(mock_llm, pages, tmp_path):
    def unauthorized(role, msgs):
        from fastapi import HTTPException

        raise HTTPException(401, "invalid api key")

    r, _ = make_runner(mock_llm(unauthorized))
    with pytest.raises(FatalError):
        r.run_volume(0, "V", pages[:1], tmp_path / "w2")


def test_cancel_exports_partial_and_resume_skips_finished_pages(mock_llm, pages, tmp_path):
    mock = mock_llm()
    work = tmp_path / "work"
    done_pages = {"n": 0}
    r, events = make_runner(mock, cancel=lambda: done_pages["n"] >= 1)
    r.emit = lambda e: (events.append(e), done_pages.__setitem__("n", done_pages["n"] + (e["type"] == "page_done")))
    with pytest.raises(Cancelled):
        r.run_volume(0, "Vol 1", pages, work)
    assert (work / "pages" / "page_0001.json").exists() and not (work / "pages" / "page_0002.json").exists()
    partial = list((work / "output").glob("*_partial.*"))
    assert {p.suffix for p in partial} == {".txt", ".docx", ".pdf"}
    calls_before = len(mock.requests)

    r2, events2 = make_runner(mock)
    result = r2.run_volume(0, "Vol 1", pages, work)
    assert result.complete and len(result.records) == 3
    assert len(mock.requests) - calls_before == 8  # only pages 2 and 3 (4 calls each) were generated
    assert any(e["type"] == "page_done" and e.get("replayed") for e in events2)
    assert not list((work / "output").glob("*_partial.*"))  # stale partial export removed
    assert result.bible.startswith("# Story Bible")  # bible restored from the checkpoint


def test_three_failed_pages_in_a_row_abort_the_volume(mock_llm, pages, tmp_path):
    def always_broken(role, msgs):
        raise RuntimeError("upstream exploded")

    mock = mock_llm(always_broken)
    r, events = make_runner(mock)
    with pytest.raises(FatalError, match="Three pages"):
        r.run_volume(0, "V", pages * 2, tmp_path / "w")
    assert sum(1 for e in events if e["type"] == "page_failed") == 3


def test_checkpoint_records_are_valid_json(mock_llm, pages, tmp_path):
    mock = mock_llm()
    r, _ = make_runner(mock)
    r.run_volume(0, "V", pages[:1], tmp_path / "w")
    rec = json.loads((tmp_path / "w/pages/page_0001.json").read_text())
    assert rec["index"] == 1 and rec["words"] > 0 and rec["flags"]["minor_involved"] == "no" and rec["bible_after"]
    assert (tmp_path / "w/media/thumb_0001.jpg").exists() and (tmp_path / "w/media/preview_0001.jpg").exists()


# ================================================ v0.3: book covers ================================================
def cover_runner(mock, **opts):
    options = RunOptions(words_per_page=120, **opts)
    return make_runner(mock, options)


def cover_handler(cover_pages, verdict=FRONT_COVER):
    """Answers the cover check with a cover verdict for the given 1-based page numbers (found via the task text)."""
    def handler(role, msgs):
        if role == "cover":
            number = int(re.search(r"Is page (\d+) of", user_text(msgs)).group(1))
            return verdict if number in cover_pages else NOT_COVER
        return {"analyst": ANALYSIS, "keeper": BIBLE}.get(role, PROSE)
    return handler


@pytest.fixture
def ten_pages(tmp_path):
    from PIL import Image

    folder = tmp_path / "ten"
    folder.mkdir()
    out = []
    for i in range(1, 11):
        p = folder / f"{i:02d}.png"
        Image.new("RGB", (120, 180), (i * 20, 50, 90)).save(p)
        out.append(p)
    return out


def test_a_front_cover_is_identified_skipped_and_no_page_is_written_from_it(mock_llm, ten_pages, tmp_path):
    mock = mock_llm(cover_handler({1}))
    r, events = cover_runner(mock)
    result = r.run_volume(0, "Vol 1", ten_pages, tmp_path / "w")
    first = result.records[0]
    assert first["status"] == "skipped" and first["skip_reason"] == "cover" and first["text"] == ""
    assert first["cover"] == {"kind": "front", "title": "Midnight Rooftop", "volume": "1", "confidence": "high"}
    # the cover was never analysed or written: its page produced exactly one request (the cover check)
    by_page = {}
    for q in mock.requests:
        m = re.search(r"[Pp]age (\d+)", user_text(q["messages"][-1:]))  # the task text of this call
        if m:
            by_page.setdefault(int(m.group(1)), []).append(mock.role_of(q["messages"]))
    assert by_page[1] == ["cover"] and by_page[5] == ["analyst", "novelist", "editor", "keeper"]
    assert result.complete and [r_["status"] for r_ in result.records].count("skipped") == 1
    done = [e for e in events if e["type"] == "page_done" and e["page"] == 1][0]
    assert done["record"]["status"] == "skipped"
    # a cover adds nothing to the story memory
    assert [e for e in events if e["type"] == "bible"][0]["text"].startswith("# Story Bible")


def test_only_the_first_and_last_two_pages_are_checked(mock_llm, ten_pages, tmp_path):
    mock = mock_llm(cover_handler(set()))
    r, _ = cover_runner(mock)
    r.run_volume(0, "V", ten_pages, tmp_path / "w")
    checked = sorted(int(re.search(r"Is page (\d+) of", user_text(q["messages"])).group(1))
                     for q in mock.requests if mock.role_of(q["messages"]) == "cover")
    assert checked == [1, 2, 9, 10]


def test_front_and_back_covers_are_both_skipped_and_the_novel_is_renumbered_with_the_printed_title(mock_llm, ten_pages, tmp_path):
    import docx
    from pypdf import PdfReader

    mock = mock_llm(cover_handler({1, 10}))
    r, _ = cover_runner(mock)
    result = r.run_volume(0, "vol_one", ten_pages, tmp_path / "w")
    assert [rec["status"] for rec in result.records].count("skipped") == 2
    pdf = PdfReader(str(tmp_path / "w" / result.files["pdf"]))
    assert len(pdf.pages) == 1 + 8  # title page + 8 story pages (not 10)
    txt = (tmp_path / "w" / result.files["txt"]).read_text(encoding="utf-8")
    assert txt.startswith("MIDNIGHT ROOFTOP") and txt.count("--- Page ") == 8
    assert "--- Page 1 ---" in txt and "--- Page 8 ---" in txt and "--- Page 9 ---" not in txt  # numbered 1..n
    assert "Midnight Rooftop" in docx.Document(str(tmp_path / "w" / result.files["docx"])).paragraphs[5].text


def test_a_low_confidence_guess_is_not_treated_as_a_cover(mock_llm, ten_pages, tmp_path):
    guess = FRONT_COVER.replace("CONFIDENCE: high", "CONFIDENCE: low")
    mock = mock_llm(cover_handler({1}, verdict=guess))
    r, _ = cover_runner(mock)
    rec = r.run_volume(0, "V", ten_pages[:3], tmp_path / "w").records[0]
    assert rec["status"] == "done" and rec["text"] == PROSE  # conservative: a real story page is never dropped


def test_skip_covers_off_means_no_check_and_every_page_is_written(mock_llm, ten_pages, tmp_path):
    mock = mock_llm(cover_handler({1}))
    r, _ = cover_runner(mock, skip_covers=False)
    result = r.run_volume(0, "V", ten_pages[:3], tmp_path / "w")
    assert all(rec["status"] == "done" for rec in result.records)
    assert not any(mock.role_of(q["messages"]) == "cover" for q in mock.requests)


def test_the_analyst_can_also_recognise_a_cover_near_the_edges_but_not_in_the_middle(mock_llm, ten_pages, tmp_path):
    cover_dossier = ANALYSIS.replace("PAGE TYPE: story", "PAGE TYPE: cover")
    state = {"page": 0}

    def handler(role, msgs):
        if role == "cover":
            return NOT_COVER
        if role == "analyst":
            state["page"] = int(re.search(r"Analyse page (\d+) of", user_text(msgs)).group(1))
            return cover_dossier if state["page"] in (3, 6) else ANALYSIS  # page 3 is at the edge, page 6 in the middle
        return {"keeper": BIBLE}.get(role, PROSE)

    r, _ = cover_runner(mock_llm(handler))
    recs = r.run_volume(0, "V", ten_pages, tmp_path / "w").records
    assert recs[2]["status"] == "skipped"  # page 3: within the first three pages
    assert recs[5]["status"] == "done"  # page 6: a chapter-title-style "cover" in the middle stays in the novel


def test_a_failing_cover_check_does_not_fail_the_page(mock_llm, ten_pages, tmp_path):
    def handler(role, msgs):
        if role == "cover":
            raise RuntimeError("provider hiccup")
        return {"analyst": ANALYSIS, "keeper": BIBLE}.get(role, PROSE)

    r, _ = cover_runner(mock_llm(handler))
    r.retry_delays = (0.01,)
    rec = r.run_volume(0, "V", ten_pages[:2], tmp_path / "w").records[0]
    assert rec["status"] == "done" and any("cover" in n for n in rec["notes"])


def test_skipped_covers_are_checkpointed_and_replayed_on_resume(mock_llm, ten_pages, tmp_path):
    mock = mock_llm(cover_handler({1}))
    r, _ = cover_runner(mock)
    work = tmp_path / "w"
    r.run_volume(0, "V", ten_pages[:3], work)
    assert json.loads((work / "pages/page_0001.json").read_text())["status"] == "skipped"
    before = len(mock.requests)
    r2, events2 = cover_runner(mock)
    result = r2.run_volume(0, "V", ten_pages[:3], work)
    assert len(mock.requests) == before and result.complete
    assert [e["record"]["status"] for e in events2 if e["type"] == "page_done" and e.get("replayed")][0] == "skipped"


# ============================== v0.3: the local server dies in the middle of a job ==============================
def test_a_dead_local_server_is_restarted_and_the_page_continues(mock_llm, pages, tmp_path):
    restarts = []
    steps = FlakySteps(1, ConnectionError("Connection error."))

    def recover(exc):
        restarts.append(type(exc).__name__)
        return True

    r, events = make_runner(mock_llm(), steps=steps, recover=recover)
    assert r.run_volume(0, "V", pages[:1], tmp_path / "w").complete
    assert restarts == ["ConnectionError"] and any("started again" in e.get("message", "") for e in events)


def test_when_the_server_cannot_be_restarted_the_job_fails_with_the_reason(mock_llm, pages, tmp_path):
    def recover(exc):
        raise RuntimeError("out of memory")

    r, _ = make_runner(mock_llm(), steps=FlakySteps(5, ConnectionError("Connection error.")), recover=recover)
    with pytest.raises(FatalError, match="could not be restarted: out of memory"):
        r.run_volume(0, "V", pages[:1], tmp_path / "w")


def test_pressing_stop_while_a_call_fails_does_not_restart_the_server(mock_llm, pages, tmp_path):
    stopped = {"flag": False}
    calls = []

    class StopSteps(FlakySteps):
        def analyze(self, ctx, token, reminder=""):
            stopped["flag"] = True  # the user pressed Stop; the engine is being killed, so this call fails
            raise ConnectionError("Connection error.")

    r, _ = make_runner(mock_llm(), steps=StopSteps(0, None), recover=lambda e: calls.append(e) or True,
                       cancel=lambda: stopped["flag"])
    with pytest.raises(Cancelled):
        r.run_volume(0, "V", pages[:1], tmp_path / "w")
    assert calls == []
