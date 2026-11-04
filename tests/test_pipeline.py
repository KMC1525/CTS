"""End-to-end pipeline tests: real CrewAI agents talking to a local mock of the OpenAI API."""
import json
import re

import pytest

from conftest import local_spec
from cts.pipeline import runner as runner_mod
from cts.pipeline.runner import Cancelled, FatalError, RunOptions, VolumeRunner
from mock_llm import ANALYSIS, BIBLE, PROSE


def make_runner(mock, options=None, cancel=lambda: False, **kw):
    events = []
    r = VolumeRunner(local_spec(mock), options or RunOptions(words_per_page=120), events.append, cancel,
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
        assert has_image == (role == "analyst")
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


def test_age_guard_forces_sfw_for_a_minor_on_a_sexual_page(mock_llm, pages, tmp_path):
    flagged = ANALYSIS.replace("sexual_content: none", "sexual_content: explicit").replace("minor_involved: no", "minor_involved: yes")
    mock = mock_llm(lambda role, msgs: {"analyst": flagged, "keeper": BIBLE}.get(role, PROSE))
    r, events = make_runner(mock, RunOptions(rating="nsfw", words_per_page=120))
    result = r.run_volume(0, "V", pages[:1], tmp_path / "w")
    rec = result.records[0]
    assert rec["rating_used"] == "sfw" and any("minor" in n for n in rec["notes"])
    writer_req = next(q for q in mock.requests if mock.role_of(q["messages"]) == "novelist")
    text = user_text(writer_req["messages"])
    assert "Rating for this page: SFW" in text and "SAFETY OVERRIDE" in text
    system = writer_req["messages"][0]["content"]
    assert "Rating: SFW" in system and "Rating: NSFW (adults only)" not in system  # the adult policy never reaches the model


def test_nsfw_edition_for_adult_pages(mock_llm, pages, tmp_path):
    adult = ANALYSIS.replace("sexual_content: none", "sexual_content: explicit")
    mock = mock_llm(lambda role, msgs: {"analyst": adult, "keeper": BIBLE}.get(role, PROSE))
    r, _ = make_runner(mock, RunOptions(rating="nsfw", words_per_page=120))
    rec = r.run_volume(0, "V", pages[:1], tmp_path / "w").records[0]
    assert rec["rating_used"] == "nsfw" and rec["notes"] == []
    writer_req = next(q for q in mock.requests if mock.role_of(q["messages"]) == "novelist")
    assert "Rating for this page: NSFW" in user_text(writer_req["messages"])
    assert "Hard limits" in writer_req["messages"][0]["content"]  # the age rule travels with the adult policy


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
