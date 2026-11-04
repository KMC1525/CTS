"""Runs one comic volume through the agents, page by page, with a checkpoint per page."""

from __future__ import annotations

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
    CoverInfo, clean_document, decide_rating, looks_like_refusal, minor_evidence, parse_cover, parse_flags, stop_requested,
    word_count,
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

    def normalised(self) -> "RunOptions":
        return RunOptions(
            rating="nsfw" if self.rating == "nsfw" else "sfw",
            words_per_page=max(80, min(1200, int(self.words_per_page or 300))),
            language=(self.language or "auto").strip() or "auto",
            carry_bible=bool(self.carry_bible),
            skip_covers=bool(self.skip_covers),
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
        self.steps = steps or Steps(spec)
        self.retry_delays = retry_delays
        # Called after a failed step; a local model whose server died is restarted there (True = restarted).
        self.recover = recover
        self.tokens = 0
        self.known_minors: set[str] = set()  # characters the comic established as under 18 (NSFW edition)

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
                     bible: str, history_pages: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        notes: list[str] = []
        ctx = PageContext(
            index=index, total=total, volume_name=name, bible=bible,
            history=budget.recent_history(history_pages, budget.history_token_budget(self.spec.context_window)),
            rating=self.options.rating, words_per_page=self.options.words_per_page,
            language=self.options.language, context_window=self.spec.context_window,
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
                lambda: self.steps.write_and_polish(ctx, analysis.text, rating, note, on_draft_done=draft_done),
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
            kept = self._retry(lambda: self.steps.remember(ctx, analysis.text, text), "Updating the story bible")
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
            "analysis": analysis.text,
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
        history: list[str] = list(prior_pages or [])
        failures_in_a_row = 0
        cancelled = False
        halt: AgeHalt | None = None
        if not self.options.carry_bible:
            self.known_minors = set()  # unrelated volumes may reuse names
        self.emit({"type": "volume_start", "volume": vol, "name": name, "total": total})

        try:
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


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in
            ("index", "status", "page_type", "rating_used", "text", "analysis", "notes", "words", "seconds",
             "skip_reason", "cover")}


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
