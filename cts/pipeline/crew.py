"""CrewAI wiring: one function per agent step (analyse, write+polish, condense, remember).

Every step builds a small Crew (agents + tasks, sequential process). Three Crews per page
instead of one big Crew is deliberate: between analysis and writing, CTS applies a
deterministic age/rating gate in code that no prompt can talk its way around.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Callable

from .. import llm as llm_mod
from .. import prompts_loader as prompts
from . import budget
from .text import clean_document, clean_prose, language_instruction


# CrewAI warns that task callbacks cannot be checkpointed. CTS never restores crews (it keeps its
# own page-level checkpoints), so the warning is just noise.
warnings.filterwarnings("ignore", message="A function callback cannot be saved in a checkpoint")


@dataclass
class StepOutput:
    text: str
    tokens: int = 0


@dataclass
class PageContext:
    index: int
    total: int
    volume_name: str
    bible: str
    history: str
    rating: str
    words_per_page: int
    language: str
    context_window: int


def _agent(prompt_names: list[str], llm: Any, extra: str = ""):
    from crewai import Agent

    loaded = [prompts.load(n) for n in prompt_names]
    primary = loaded[0]
    backstory = "\n\n".join(p.system_text() for p in loaded) + (f"\n\n{extra}" if extra else "")
    return Agent(
        role=primary.role,
        goal=primary.goal,
        backstory=backstory,
        llm=llm,
        verbose=False,
        allow_delegation=False,
        max_iter=3,
        max_retry_limit=1,
        respect_context_window=True,
        cache=False,
    )


def _run(agents: list, tasks: list) -> tuple[list[str], int]:
    from crewai import Crew, Process

    crew = Crew(
        agents=agents,
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
        memory=False,
        cache=False,
        tracing=False,
    )
    crew.kickoff()
    outputs = [str(t.output.raw if t.output is not None else "") for t in tasks]
    tokens = 0
    try:
        tokens = int(getattr(crew.usage_metrics, "total_tokens", 0) or 0)
    except Exception:  # metrics are best-effort
        pass
    return outputs, tokens


def _task(description: str, expected: str, agent, *, context: list | None = None,
          callback: Callable[[Any], None] | None = None):
    from crewai import Task

    kwargs: dict[str, Any] = dict(description=description, expected_output=expected, agent=agent,
                                  context=context if context is not None else [])
    if callback is not None:
        kwargs["callback"] = callback
    return Task(**kwargs)


class Steps:
    """All agent steps for one run, sharing one provider configuration."""

    def __init__(self, spec: llm_mod.LLMSpec):
        self.spec = spec
        llm_mod.ensure_vision_hook()

    def _llm(self, temperature: float, max_tokens: int):
        return llm_mod.build_llm(self.spec, temperature=temperature, max_tokens=max_tokens)

    # ---------------------------------------------------------------- 1. analyse
    def analyze(self, ctx: PageContext, image_token: str, reminder: str = "") -> StepOutput:
        llm = self._llm(0.2, budget.analyst_output_cap(ctx.context_window))
        agent = _agent(["comic_analyst"], llm)
        description = (
            f"Analyse page {ctx.index} of {ctx.total} of the comic volume \"{ctx.volume_name}\".\n"
            f"{llm_mod.image_marker(image_token)}\n\n"
            f"The novel will be written in: {language_instruction(ctx.language)}. "
            "If the lettering is in a different language, add translations as your instructions say.\n\n"
            "Story bible so far (use it ONLY to reuse the same character names and labels; "
            f"do not copy it into your answer):\n{ctx.bible.strip() or '(empty - this is the start of the story)'}\n\n"
            "Describe the page now, following your output format exactly, and finish with the FLAGS block."
            f"{(chr(10) + chr(10) + reminder) if reminder else ''}"
        )
        task = _task(description, "A complete page dossier in the required format, ending with the FLAGS block.", agent)
        outputs, tokens = _run([agent], [task])
        return StepOutput(clean_document(outputs[0]), tokens)

    # ------------------------------------------------------------ 2+3. write, polish
    def write_and_polish(
        self, ctx: PageContext, dossier: str, rating: str, safety_note: str | None,
        on_draft_done: Callable[[], None] | None = None,
    ) -> tuple[StepOutput, StepOutput]:
        cap = budget.writer_output_cap(ctx.context_window, ctx.words_per_page)
        rating_prompt = "rating_nsfw" if rating == "nsfw" else "rating_sfw"
        writer = _agent(["novelist", rating_prompt], self._llm(0.85, cap))
        editor = _agent(["editor", rating_prompt], self._llm(0.4, cap))
        limit = int(ctx.words_per_page * 1.25)
        safety = (
            "\nSAFETY OVERRIDE FOR THIS PAGE: a character on this page may be a minor. Write the scene "
            "WITHOUT any sexual or suggestive content: no nudity described, no desire, no touching with "
            "sexual intent. Cut away or keep it plainly non-sexual and continue the plot.\n"
            if safety_note else ""
        )

        draft_desc = (
            f"Write novel page {ctx.index} of {ctx.total} (volume \"{ctx.volume_name}\"). "
            f"Language: {language_instruction(ctx.language)}.\n"
            f"Length: about {ctx.words_per_page} words (never more than {limit}).\n"
            f"Rating for this page: {rating.upper()}.{safety}\n\n"
            f"=== STORY BIBLE ===\n{ctx.bible.strip() or '(empty - this is the start of the story; set the voice yourself)'}\n\n"
            f"=== PREVIOUS NOVEL PAGES (continue from here, do not repeat) ===\n{ctx.history.strip() or '(none - this is the first page)'}\n\n"
            f"=== DOSSIER OF COMIC PAGE {ctx.index} (your only source for this page) ===\n{dossier}\n\n"
            "Write only the prose of this page."
        )
        draft_task = _task(
            draft_desc, "The prose of this single novel page as plain paragraphs, with nothing else.", writer,
            callback=(lambda _out: on_draft_done()) if on_draft_done else None,
        )
        edit_desc = (
            f"Edit the draft of novel page {ctx.index} (given as context). Rating: {rating.upper()}.{safety}\n"
            f"Word budget: about {ctx.words_per_page} words, never more than {limit}. "
            f"Language: {language_instruction(ctx.language)}.\n\n"
            f"Check it against the dossier of the comic page and the story bible below, fix problems, "
            f"and return only the final page text.\n\n"
            f"=== STORY BIBLE ===\n{ctx.bible.strip() or '(empty)'}\n\n"
            f"=== DOSSIER OF COMIC PAGE {ctx.index} ===\n{dossier}"
        )
        edit_task = _task(
            edit_desc, "The final text of this novel page as plain paragraphs, with nothing else.", editor,
            context=[draft_task],
        )
        outputs, tokens = _run([writer, editor], [draft_task, edit_task])
        return StepOutput(clean_prose(outputs[0]), tokens), StepOutput(clean_prose(outputs[1]), 0)

    # --------------------------------------------------------------- condense
    def condense(self, ctx: PageContext, text: str, rating: str) -> StepOutput:
        cap = budget.writer_output_cap(ctx.context_window, ctx.words_per_page)
        agent = _agent(["editor", "rating_nsfw" if rating == "nsfw" else "rating_sfw"], self._llm(0.3, cap))
        task = _task(
            f"The page below is too long. Condense it to about {ctx.words_per_page} words WITHOUT dropping any "
            "event or line of dialogue: tighten the prose, merge sentences, cut filler. Keep the same language, "
            f"voice and tense. Return only the page text.\n\n=== PAGE ===\n{text}",
            "The condensed page as plain paragraphs, with nothing else.", agent,
        )
        outputs, tokens = _run([agent], [task])
        return StepOutput(clean_prose(outputs[0]), tokens)

    # -------------------------------------------------------------- remember
    def remember(self, ctx: PageContext, dossier: str, final_text: str) -> StepOutput:
        cap_words = budget.bible_word_cap(ctx.context_window)
        agent = _agent(["story_keeper"], self._llm(0.2, min(4000, int(cap_words * 3) + 400)))
        task = _task(
            f"Update the story bible after page {ctx.index} of {ctx.total} (volume \"{ctx.volume_name}\").\n"
            f"The novel language is {language_instruction(ctx.language)}. Maximum length: {cap_words} words.\n\n"
            f"=== CURRENT BIBLE ===\n{ctx.bible.strip() or '(empty - create it from the template)'}\n\n"
            f"=== DOSSIER OF COMIC PAGE {ctx.index} ===\n{dossier}\n\n"
            f"=== NOVEL PAGE {ctx.index} JUST WRITTEN ===\n{final_text}\n\n"
            "Return the complete updated bible using the template.",
            "The complete updated story bible in the template format.", agent,
        )
        outputs, tokens = _run([agent], [task])
        return StepOutput(clean_document(outputs[0]), tokens)
