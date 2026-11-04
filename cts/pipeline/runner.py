"""Runs one comic volume through the agents, page by page, with a checkpoint per page."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import llm as llm_mod
from ..export import Novel, NovelPage, write_all
from ..ingest import encode_for_model, make_preview, make_thumbnail
from . import budget
from .crew import PageContext, Steps
from .text import (
    CoverInfo, PageReview, clean_direction, clean_document, decide_rating, looks_like_refusal, mechanical_issues,
    minor_evidence, parse_cover, parse_flags, parse_review_window, parse_verdict, stop_requested, word_count,
)

Emit = Callable[[dict[str, Any]], None]


class Cancelled(Exception):
    """The user pressed Stop."""


class FatalError(Exception):
    """Retrying will not help (bad key, unknown model, no vision support...)."""


class StepFailed(Exception):
    """A step kept failing after retries; the page is marked failed, the volume goes on."""


class AgeHalt(Exception):
    """The NSFW edition met a character the comic clearly shows to be under 18: the whole process stops."""

    def __init__(self, volume_name: str, page: int, evidence: str):
        self.volume_name, self.page, self.evidence = volume_name, page, evidence
        super().__init__(
            f"NSFW edition stopped on page {page} of \"{volume_name}\": the comic makes it clear that a "
            f"character is under 18 ({evidence}). Nothing was written for that page. "
            "Generate the SFW edition of this comic instead."
        )

@dataclass
class RunOptions:
    rating: str = "sfw"
    words_per_page: int = 300
    language: str = "auto"
    carry_bible: bool = True
    skip_covers: bool = True
    final_review: bool = True  # the Final Revisor reads the whole novel before it is delivered
    custom_prompt: str = ""  # the user's optional system prompt (steers the narrative; SFW and NSFW alike)
    review_rounds: int = 2  # how many times a failed review may trigger a rewrite

    def normalised(self) -> "RunOptions":
        return RunOptions(
            rating="nsfw" if self.rating == "nsfw" else "sfw",
            words_per_page=max(80, min(1200, int(self.words_per_page or 300))),
            language=(self.language or "auto").strip() or "auto",
            carry_bible=bool(self.carry_bible),
            skip_covers=bool(self.skip_covers),
            final_review=bool(self.final_review),
            custom_prompt=clean_direction(self.custom_prompt),
            review_rounds=max(0, min(3, int(self.review_rounds))),
        )


@dataclass
class VolumeResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    bible: str = ""
    files: dict[str, str] = field(default_factory=dict)
    complete: bool = False
    tokens: int = 0


def record_path(work: Path, index: int) -> Path:
    return work / "pages" / f"page_{index:04d}.json"


def load_record(work: Path, index: int) -> dict[str, Any] | None:
    path = record_path(work, index)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def media_path(work: Path, kind: str, index: int) -> Path:
    return work / "media" / f"{kind}_{index:04d}.jpg"


def save_media(work: Path, index: int, source: Path) -> None:
    """Keep a small thumbnail and a readable preview next to the job so history works offline."""
    thumb, preview = media_path(work, "thumb", index), media_path(work, "preview", index)
    if thumb.exists() and preview.exists():
        return
    try:
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(make_thumbnail(source))
        preview.write_bytes(make_preview(source))
    except Exception:  # noqa: BLE001 - media is cosmetic, never fail a page over it
        pass


def save_record(work: Path, record: dict[str, Any]) -> None:
    path = record_path(work, record["index"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class VolumeRunner:
    def __init__(
        self,
        spec: llm_mod.LLMSpec,
        options: RunOptions,
        emit: Emit,
        should_cancel: Callable[[], bool],
        steps: Steps | None = None,
        retry_delays: tuple[float, ...] = (4.0, 12.0),
        recover: Callable[[BaseException], bool] | None = None,
    ):
        self.spec = spec
        self.options = options.normalised()
        self.emit = emit
        self.should_cancel = should_cancel
        self.steps = steps or Steps(spec, direction=self.options.custom_prompt)
        self.retry_delays = retry_delays
        # Called after a failed step; a local model whose server died is restarted there (True = restarted).
        self.recover = recover
        self.tokens = 0
        self.known_minors: set[str] = set()  # characters the comic established as under 18 (NSFW edition)
        self.revision_notes_all = ""  # set while a whole volume is redone after a final review
        self._minors0: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _check(self) -> None:
        if self.should_cancel():
            raise Cancelled()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check()
            time.sleep(min(0.25, max(0.0, end - time.monotonic())))

    def _log(self, level: str, message: str) -> None:
        self.emit({"type": "log", "level": level, "message": message})

    def _retry(self, fn: Callable[[], Any], what: str) -> Any:
        attempts = len(self.retry_delays) + 1
        for attempt in range(attempts):
            self._check()
            try:
                return fn()
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - provider SDKs raise many types
                self._check()  # Stop pressed while the call was failing/aborted: do not retry or restart anything
                message, fatal = llm_mod.describe_error(exc)
                if fatal:
                    raise FatalError(message) from exc
                if self.recover is not None:
                    try:
                        if self.recover(exc):
                            self._log("warn", "The local model had stopped, so it was started again.")
                    except Cancelled:
                        raise
                    except Exception as restart_error:  # noqa: BLE001
                        raise FatalError(f"The local model stopped and could not be restarted: {restart_error}") from exc
                if attempt == attempts - 1:
                    raise StepFailed(f"{what}: {message}") from exc
                delay = self.retry_delays[attempt]
                self._log("warn", f"{what} failed ({message}). Retrying in {delay:g}s...")
                self._sleep(delay)
        raise StepFailed(what)  # pragma: no cover

    # ------------------------------------------------------------------- covers
    @staticmethod
    def _cover_candidate(index: int, total: int, wide: bool = False) -> bool:
        """Covers sit at the very start/end of a volume: first and last two pages (three when ``wide``)."""
        edge = 3 if wide else 2
        return index <= edge or index > total - edge

    def _skipped_record(self, index: int, bible: str, cover: Any, raw: str, started: float) -> dict[str, Any]:
        label = cover.kind if cover.kind != "none" else "cover"
        what = f"{label} cover" if not label.endswith("cover") else label
        title = f": {cover.title}" if cover.title else ""
        self._log("info", f"Page {index} is a book {what}{title} - skipped, no novel page is written for it.")
        return {
            "index": index, "status": "skipped", "skip_reason": "cover", "page_type": "cover", "rating_used": "",
            "text": "", "analysis": raw, "words": 0, "flags": {}, "seconds": round(time.monotonic() - started, 1),
            "notes": [f"Book {what}{title} - not turned into a novel page."],
            "cover": {"kind": cover.kind, "title": cover.title, "volume": cover.volume, "confidence": cover.confidence},
            "bible_after": bible,
        }

    # ------------------------------------------------------------------- one page
    def process_page(self, vol: int, name: str, path: Path, index: int, total: int,
                     bible: str, history_pages: list[str], revision_notes: str = "") -> dict[str, Any]:
        started = time.monotonic()
        notes: list[str] = []
        ctx = PageContext(
            index=index, total=total, volume_name=name, bible=bible,
            history=budget.recent_history(history_pages, budget.history_token_budget(self.spec.context_window)),
            rating=self.options.rating, words_per_page=self.options.words_per_page,
            language=self.options.language, context_window=self.spec.context_window,
            revision_notes=revision_notes or self.revision_notes_all,
        )

        def step(which: str, status: str) -> None:
            self.emit({"type": "step", "volume": vol, "page": index, "step": which, "status": status})

        image, mime = encode_for_model(path, self.spec.max_image_side)
        token = llm_mod.register_image(image, mime)
        try:
            # 0 - is it a book cover? (first/last pages only; covers are skipped, never written) ----------
            if self.options.skip_covers and self._cover_candidate(index, total):
                step("cover", "running")
                try:
                    verdict = self._retry(lambda: self.steps.identify_cover(ctx, token), "Checking for a cover")
                    self.tokens += verdict.tokens
                    cover = parse_cover(verdict.text)
                    if cover.is_cover:
                        step("cover", "done")
                        return self._skipped_record(index, bible, cover, verdict.text, started)
                except StepFailed as exc:  # the check is optional: carry on and treat the page as a story page
                    notes.append(f"Could not check whether this page is a cover ({exc}).")
                step("cover", "done")

            # 1 - read the page ---------------------------------------------------
            step("analyze", "running")
            analysis = self._retry(lambda: self.steps.analyze(ctx, token), "Reading the page")
            self.tokens += analysis.tokens
            flags = parse_flags(analysis.text)
            if self.options.rating == "nsfw" and not flags.complete and analysis.text.strip():
                again = self._retry(
                    lambda: self.steps.analyze(
                        ctx, token,
                        reminder="IMPORTANT: your previous answer was missing the FLAGS block. "
                                 "End with the FLAGS block exactly as specified.",
                    ),
                    "Re-reading the page",
                )
                self.tokens += again.tokens
                if parse_flags(again.text).complete:
                    analysis, flags = again, parse_flags(again.text)
        finally:
            llm_mod.release_image(token)
        if len(analysis.text.strip()) < 20:
            raise FatalError("The model returned no description of the page. Is it a vision-capable model?")
        step("analyze", "done")

        # A page the analyst calls a cover, near the start/end of the volume, is a cover too.
        if self.options.skip_covers and flags.page_type == "cover" and self._cover_candidate(index, total, wide=True):
            return self._skipped_record(index, bible, CoverInfo(is_cover=True, kind="front", confidence="medium"),
                                        analysis.text, started)

        return self._compose_page(vol, name, index, total, ctx, analysis.text, flags, bible, started, notes)

    def _compose_page(self, vol: int, name: str, index: int, total: int, ctx: PageContext, analysis_text: str,
                      flags: Any, bible: str, started: float, notes: list[str]) -> dict[str, Any]:
        """Age rule -> write -> polish -> (condense) -> remember, from an existing page analysis.

        Used for a normal page and for rewriting a page after the final review (no need to read the image again).
        """
        def step(which: str, status: str) -> None:
            self.emit({"type": "step", "volume": vol, "page": index, "step": which, "status": status})

        # 2 - age rule + rating gate (plain code) -----------------------------------
        if self.options.rating == "nsfw":
            evidence = minor_evidence(flags, self.known_minors)
            self.known_minors |= {n.strip().lower() for n in flags.minor_names}
            if evidence:
                self.emit({"type": "age_halt", "volume": vol, "page": index, "evidence": evidence})
                raise AgeHalt(name, index, evidence)
        rating_used, safety_note = decide_rating(self.options.rating, flags)
        if safety_note:
            notes.append(safety_note)
            self._log("warn", f"Page {index}: {safety_note}")

        # 3 + 4 - write, then polish -------------------------------------------
        def write_with(rating: str, note: str | None) -> tuple[str, bool]:
            """Returns (page text, refused?). A refused draft taints the edit built on top of it."""
            step("write", "running")

            def draft_done() -> None:
                self._check()
                step("write", "done")
                step("polish", "running")

            draft, final = self._retry(
                lambda: self.steps.write_and_polish(ctx, analysis_text, rating, note, on_draft_done=draft_done),
                "Writing the page",
            )
            self.tokens += draft.tokens
            step("polish", "done")
            if stop_requested(draft.text) or stop_requested(final.text):  # the NSFW prompt told the model to stop
                evidence = (final.text or draft.text).replace("[[CTS-STOP-MINOR]]", "").strip()[:240]
                self.emit({"type": "age_halt", "volume": vol, "page": index, "evidence": evidence})
                raise AgeHalt(name, index, evidence or "the model reported a character under 18")
            text = final.text or draft.text
            refused = (not text.strip()) or looks_like_refusal(draft.text) or looks_like_refusal(text)
            return text, refused

        text, refused = write_with(rating_used, safety_note)
        if refused:
            if rating_used == "nsfw":
                notes.append("The model declined the adult version of this page; it was written in SFW terms.")
                self._log("warn", f"Page {index}: the model declined the NSFW version; falling back to SFW.")
                rating_used = "sfw"
                text, refused = write_with("sfw", None)
            if refused:
                text, refused = write_with("sfw", None)
            if refused:
                notes.append("The model refused to write this page.")
                self._log("error", f"Page {index}: the model refused to write this page.")
                text = f"[Page {index} could not be adapted: the model declined to write it.]"

        # Length guard: one comic page must stay one novel page.
        if word_count(text) > int(self.options.words_per_page * 1.35) and not text.startswith("[Page "):
            try:
                shorter = self._retry(lambda: self.steps.condense(ctx, text, rating_used), "Condensing the page")
                self.tokens += shorter.tokens
                if shorter.text and not looks_like_refusal(shorter.text) and word_count(shorter.text) < word_count(text):
                    text = shorter.text
            except StepFailed as exc:
                notes.append(f"Page is longer than the target ({exc}).")

        # 5 - remember ---------------------------------------------------------
        step("memory", "running")
        new_bible = bible
        try:
            kept = self._retry(lambda: self.steps.remember(ctx, analysis_text, text), "Updating the story bible")
            self.tokens += kept.tokens
            candidate = clean_document(kept.text)
            if word_count(candidate) >= 25 and not looks_like_refusal(candidate):
                new_bible = budget.trim_bible(candidate, budget.bible_word_cap(self.spec.context_window))
            else:
                notes.append("The story bible was not updated on this page (unusable answer).")
        except (StepFailed, FatalError) as exc:
            notes.append(f"The story bible was not updated on this page ({exc}).")
            self._log("warn", f"Page {index}: story bible not updated.")
        step("memory", "done")

        return {
            "index": index,
            "status": "done",
            "page_type": flags.page_type,
            "rating_used": rating_used,
            "text": text,
            "analysis": analysis_text,
            "notes": notes,
            "words": word_count(text),
            "flags": {"sexual_content": flags.sexual_content, "minor_involved": flags.minor_involved,
                      "violence": flags.violence},
            "seconds": round(time.monotonic() - started, 1),
            "bible_after": new_bible,
        }


    # ------------------------------------------------------------------ a volume
    def run_volume(self, vol: int, name: str, page_paths: list[Path], work: Path,
                   bible: str = "", prior_pages: list[str] | None = None) -> VolumeResult:
        total = len(page_paths)
        work.mkdir(parents=True, exist_ok=True)
        result = VolumeResult(bible=bible)
        prior = list(prior_pages or [])
        cancelled = False
        halt: AgeHalt | None = None
        if not self.options.carry_bible:
            self.known_minors = set()  # unrelated volumes may reuse names
        self._minors0 = set(self.known_minors)
        self.emit({"type": "volume_start", "volume": vol, "name": name, "total": total})

        try:
            self._run_pages(vol, name, page_paths, work, result, prior)
            written = any(r.get("status") != "skipped" for r in result.records)
            if self.options.final_review and written and len(result.records) == total:
                self._final_review(vol, name, page_paths, work, result, bible, prior)
        except Cancelled:
            cancelled = True
        except AgeHalt as stop:
            halt = stop
        finally:
            result.tokens = self.tokens
            if any(r.get("status") != "skipped" for r in result.records):
                partial = len(result.records) < total
                result.files = export_volume(work, name, result.records, self.options, partial=partial)
                self.emit({"type": "volume_files", "volume": vol, "files": result.files, "partial": partial})
        if halt is not None:
            raise halt
        if cancelled:
            raise Cancelled()
        result.complete = len(result.records) == total and all(r.get("status") in ("done", "skipped") for r in result.records)
        return result

    def _run_pages(self, vol: int, name: str, page_paths: list[Path], work: Path, result: VolumeResult,
                   prior_pages: list[str]) -> None:
        """Write every page of the volume in order (finished pages found on disk are replayed, not redone)."""
        total = len(page_paths)
        history: list[str] = list(prior_pages)
        failures_in_a_row = 0
        for index, path in enumerate(page_paths, start=1):
            self._check()
            record = load_record(work, index)
            if record and record.get("status") in ("done", "skipped"):
                self.emit({"type": "page_done", "volume": vol, "page": index, "replayed": True,
                           "record": _public_record(record)})
                result.records.append(record)
                result.bible = record.get("bible_after") or result.bible
                if record["status"] == "done":
                    history.append(record["text"])
                    if self.options.rating == "nsfw":  # remember who the comic established as a minor
                        self.known_minors |= {n.strip().lower() for n in parse_flags(record.get("analysis", "")).minor_names}
                continue

            save_media(work, index, path)
            self.emit({"type": "page_start", "volume": vol, "page": index})
            try:
                record = self.process_page(vol, name, path, index, total, result.bible, history)
                failures_in_a_row = 0
            except StepFailed as exc:
                failures_in_a_row += 1
                record = {"index": index, "status": "failed", "page_type": "story", "rating_used": "sfw",
                          "text": f"[Page {index} could not be generated: {exc}]", "analysis": "",
                          "notes": [str(exc)], "words": 0, "flags": {}, "seconds": 0, "bible_after": result.bible}
                self.emit({"type": "page_failed", "volume": vol, "page": index, "error": str(exc)})
                if failures_in_a_row >= 3:
                    raise FatalError("Three pages in a row failed - the provider seems to be down. "
                                     "Fix the problem and press Resume.") from exc
            save_record(work, record)
            result.records.append(record)
            result.bible = record["bible_after"]
            if record["status"] != "skipped":
                history.append(record["text"])
            self.emit({"type": "page_done", "volume": vol, "page": index, "record": _public_record(record),
                       "tokens": self.tokens})
            if record["status"] != "skipped":
                self.emit({"type": "bible", "volume": vol, "text": result.bible})

    # ----------------------------------------------------------------- final review
    def _final_review(self, vol: int, name: str, page_paths: list[Path], work: Path, result: VolumeResult,
                      initial_bible: str, prior_pages: list[str]) -> None:
        """The Final Revisor reads the finished novel as a book before it is delivered.

        Coherent -> it passes. Problems on some pages -> only those pages are written again (the saved page
        analysis is reused, the image is not read again). An incoherent book -> the whole volume is produced
        again. After ``review_rounds`` rewrites the novel is delivered anyway, with the open issues reported.
        """
        saved = load_review(work)
        digest = records_digest(result.records)
        if saved and saved.get("hash") == digest and saved.get("status") in ("passed", "warning"):
            self.emit({"type": "review_result", "volume": vol, "status": saved["status"], "rounds": saved["rounds"],
                       "replayed": True})
            return
        self.emit({"type": "review_start", "volume": vol})
        total = len(page_paths)
        rounds: list[dict[str, Any]] = []
        status = "warning"
        cache: dict[int, PageReview] = {}
        changed: set[int] | None = None  # None = review every page
        fixes_left = self.options.review_rounds
        attempt = 0
        while True:
            attempt += 1
            self._check()
            try:
                outcome = self._review_once(vol, result, total, cache, changed, attempt)
            except StepFailed as exc:
                self._log("warn", f"The final review could not run ({exc}); the novel is delivered without it.")
                rounds.append({"attempt": attempt, "verdict": "skipped", "reason": str(exc)[:300], "action": "none"})
                status = "skipped"
                break
            info = {"attempt": attempt, "verdict": outcome.verdict, "pages": sorted(outcome.pages),
                    "notes": outcome.notes, "reason": outcome.reason, "problems": outcome.problems, "action": "pass"}
            rounds.append(info)
            if outcome.passed:
                status = "passed"
                break
            if fixes_left <= 0:
                info["action"] = "delivered with open issues"
                status = "warning"
                break
            fixes_left -= 1
            if outcome.verdict == "redo-all":
                info["action"] = "redo-all"
                self._redo_all(vol, name, page_paths, work, result, initial_bible, prior_pages, outcome.notes)
                cache.clear()
                changed = None
            else:
                info["action"] = "fix-pages"
                changed = self._fix_pages(vol, name, page_paths, work, result, outcome, initial_bible, prior_pages)
        save_review(work, {"hash": records_digest(result.records), "status": status, "rounds": rounds})
        self.emit({"type": "review_result", "volume": vol, "status": status, "rounds": rounds})

    def _review_once(self, vol: int, result: VolumeResult, total: int, cache: dict[int, PageReview],
                     changed: set[int] | None, attempt: int) -> "ReviewOutcome":
        ctx_window = self.spec.context_window
        story = [(r["index"], r["text"]) for r in sorted(result.records, key=lambda r: r["index"])
                 if r.get("status") != "skipped"]
        texts = dict(story)
        mech = mechanical_issues(story)

        # A: windows of consecutive pages (only the pages that changed - and their neighbours - after a rewrite)
        if changed is None:
            targets = [(n, t) for n, t in story if n not in mech]
        else:
            near = {x + d for x in changed for d in (-1, 0, 1)}
            targets = [(n, t) for n, t in story if n in near and n not in mech]
        windows = budget.split_windows(targets, budget.review_window_tokens(ctx_window))
        for number, window in enumerate(windows, start=1):
            self._check()
            self.emit({"type": "review_step", "volume": vol, "step": "windows", "current": number, "total": len(windows),
                       "attempt": attempt})
            first = window[0][0]
            tail = ""
            if first - 1 in texts:
                tail = " ".join(texts[first - 1].split()[-110:])
            out = self._retry(
                lambda window=window, tail=tail: self.steps.review_window(
                    ctx_window, result.bible, window, tail, self.options.language),
                "Reviewing the novel")
            self.tokens += out.tokens
            parsed = parse_review_window(out.text)
            if not parsed:
                self._log("warn", f"The reviewer's answer for pages {window[0][0]}-{window[-1][0]} was not in the "
                                  "expected format; those pages are taken as coherent.")
            for n, text in window:
                cache[n] = parsed.get(n) or PageReview(ok=True, summary=first_sentence(text))
        for n, text in story:
            cache.setdefault(n, PageReview(ok=True, summary=first_sentence(text)))
        for n in list(cache):
            if n not in texts or n in mech:
                cache.pop(n, None)

        # B: the verdict on the whole novel
        problems = {n: pr.problem for n, pr in cache.items() if not pr.ok}
        summaries = [(n, cache[n].summary or first_sentence(texts[n])) if n in cache else (n, "(to be rewritten)")
                     for n, _ in story]
        summaries = budget.fit_summaries(summaries, set(problems) | set(mech), int(ctx_window * 0.35))
        self.emit({"type": "review_step", "volume": vol, "step": "global", "attempt": attempt})
        out = self._retry(
            lambda: self.steps.review_global(ctx_window, result.bible, total, summaries, problems, mech, attempt),
            "Judging the whole novel")
        self.tokens += out.tokens
        return decide_review(parse_verdict(out.text, total), problems, mech)

    def _fix_pages(self, vol: int, name: str, page_paths: list[Path], work: Path, result: VolumeResult,
                   outcome: "ReviewOutcome", initial_bible: str, prior_pages: list[str]) -> set[int]:
        """Write the flagged pages again (with the revisor's notes); everything else is kept as it is."""
        fixed: set[int] = set()
        for n in sorted(outcome.pages):
            self._check()
            position = next((i for i, r in enumerate(result.records) if r["index"] == n), None)
            if position is None:
                continue
            why = outcome.reasons.get(n, "")
            note = (outcome.notes + ("\n" if outcome.notes else "") + (f"The problem on this page: {why}" if why else "")).strip()
            self.emit({"type": "review_step", "volume": vol, "step": "fix", "page": n,
                       "current": len(fixed) + 1, "total": len(outcome.pages)})
            try:
                new = self._regenerate_page(vol, name, page_paths[n - 1], n, len(page_paths), result, position, note,
                                            initial_bible, prior_pages)
            except StepFailed as exc:
                self._log("warn", f"Page {n} could not be rewritten ({exc}); it stays as it was.")
                continue
            new["revisions"] = int(result.records[position].get("revisions", 0)) + 1
            new["notes"] = [*new.get("notes", []), f"Rewritten after the final review{': ' + why if why else ''}."]
            save_record(work, new)
            result.records[position] = new
            fixed.add(n)
            self.emit({"type": "page_done", "volume": vol, "page": n, "record": _public_record(new), "tokens": self.tokens,
                       "revised": True})
        if result.records:
            result.bible = result.records[-1].get("bible_after") or result.bible
            self.emit({"type": "bible", "volume": vol, "text": result.bible})
        return fixed

    def _regenerate_page(self, vol: int, name: str, path: Path, index: int, total: int, result: VolumeResult,
                         position: int, notes: str, initial_bible: str, prior_pages: list[str]) -> dict[str, Any]:
        record = result.records[position]
        before = [r for r in result.records[:position]]
        bible_before = (before[-1].get("bible_after") if before else "") or initial_bible
        history = list(prior_pages) + [r["text"] for r in before if r.get("status") == "done"]
        self.emit({"type": "page_start", "volume": vol, "page": index, "revision": True})
        analysis = record.get("analysis") or ""
        if len(analysis.strip()) >= 20 and record.get("status") in ("done", "failed"):
            started = time.monotonic()
            ctx = PageContext(
                index=index, total=total, volume_name=name, bible=bible_before,
                history=budget.recent_history(history, budget.history_token_budget(self.spec.context_window)),
                rating=self.options.rating, words_per_page=self.options.words_per_page, language=self.options.language,
                context_window=self.spec.context_window, revision_notes=notes,
                previous_version=(record.get("text") or "")[:3500],
            )
            return self._compose_page(vol, name, index, total, ctx, analysis, parse_flags(analysis), bible_before,
                                      started, [])
        # the page never got an analysis (it failed while being read): the whole process for this page again
        return self.process_page(vol, name, path, index, total, bible_before, history, revision_notes=notes)

    def _redo_all(self, vol: int, name: str, page_paths: list[Path], work: Path, result: VolumeResult,
                  initial_bible: str, prior_pages: list[str], notes: str) -> None:
        """The novel is incoherent as a whole: produce the entire volume again, guided by the revisor's notes."""
        self.emit({"type": "review_step", "volume": vol, "step": "redo-all"})
        self._log("warn", "The final review found the novel incoherent as a whole - writing the whole volume again.")
        for old in (work / "pages").glob("page_*.json"):
            old.unlink(missing_ok=True)
        result.records.clear()
        result.bible = initial_bible
        self.known_minors = set(self._minors0)
        self.revision_notes_all = notes
        try:
            self._run_pages(vol, name, page_paths, work, result, prior_pages)
        finally:
            self.revision_notes_all = ""


@dataclass
class ReviewOutcome:
    passed: bool
    verdict: str
    pages: set[int] = field(default_factory=set)
    notes: str = ""
    reason: str = ""
    problems: dict[int, str] = field(default_factory=dict)  # page -> what is wrong (for the report)
    reasons: dict[int, str] = field(default_factory=dict)  # page -> the problem to fix when it is rewritten


def decide_review(verdict: Any, problems: dict[int, str], mechanical: dict[int, str]) -> ReviewOutcome:
    """Turn the revisor's verdict plus the free mechanical findings into what to do.

    * pages that are empty / placeholders / refusals / duplicates are ALWAYS rewritten - they cannot pass;
    * ``coherent`` lets the novel through: page-level remarks the revisor judged minor are not acted on;
    * ``fix-pages`` rewrites the pages it names (or, if it names none, the pages flagged in the windows);
    * ``redo-all`` repeats the whole volume;
    * an answer that cannot be understood falls back to the page-level findings.
    """
    reasons = {**problems, **mechanical}
    report = {**problems, **{n: f"(automatic check) {why}" for n, why in mechanical.items()}}
    pages = set(mechanical)
    if verdict.verdict == "redo-all":
        return ReviewOutcome(False, "redo-all", set(), verdict.notes, verdict.reason, report, reasons)
    if verdict.verdict == "fix-pages":
        pages |= set(verdict.pages) or set(problems)
    elif verdict.verdict == "unknown":
        pages |= set(problems)
    outcome_verdict = "fix-pages" if pages else "coherent"
    return ReviewOutcome(not pages, outcome_verdict, pages, verdict.notes, verdict.reason, report,
                         {n: reasons.get(n, "") for n in pages})


def first_sentence(text: str, words: int = 22) -> str:
    sentence = " ".join((text or "").split())
    for stop in (". ", "! ", "? "):
        if stop in sentence[:300]:
            sentence = sentence[: sentence.index(stop) + 1]
            break
    return " ".join(sentence.split()[:words])


def records_digest(records: list[dict[str, Any]]) -> str:
    """Identifies the exact text of the volume, so an already-reviewed volume is not reviewed twice."""
    h = hashlib.sha1()
    for r in sorted(records, key=lambda r: r["index"]):
        h.update(f"{r['index']}|{r.get('status')}|{r.get('text', '')}".encode("utf-8"))
    return h.hexdigest()


def review_path(work: Path) -> Path:
    return work / "review.json"


def load_review(work: Path) -> dict[str, Any] | None:
    try:
        return json.loads(review_path(work).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_review(work: Path, data: dict[str, Any]) -> None:
    tmp = review_path(work).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(review_path(work))


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in
            ("index", "status", "page_type", "rating_used", "text", "analysis", "notes", "words", "seconds",
             "skip_reason", "cover", "revisions")}


def export_volume(work: Path, name: str, records: list[dict[str, Any]], options: RunOptions,
                  partial: bool = False) -> dict[str, str]:
    """Write TXT/DOCX/PDF. Skipped pages (covers) are left out and the novel pages are numbered 1..n."""
    ordered = sorted(records, key=lambda r: r["index"])
    story = [r for r in ordered if r.get("status") != "skipped"]
    covers = [r.get("cover") or {} for r in ordered if r.get("status") == "skipped"]
    printed = next((c["title"] for c in covers if c.get("title") and c.get("kind") in ("front", "wrap-around")), "") \
        or next((c["title"] for c in covers if c.get("title")), "")
    base = printed or name
    novel = Novel(
        title=base + (" (partial)" if partial else ""),
        pages=[NovelPage(n, r["text"], r.get("page_type", "story")) for n, r in enumerate(story, start=1)],
        language=options.language,
        rating=options.rating,
    )
    out_dir = work / "output"
    paths = write_all(novel, out_dir, name + ("_partial" if partial else ""))
    # drop a stale partial export once the volume is complete
    if not partial:
        for old in out_dir.glob("*_partial.*"):
            old.unlink(missing_ok=True)
    return {fmt: str(p.relative_to(work).as_posix()) for fmt, p in paths.items()}
