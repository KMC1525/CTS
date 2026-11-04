"""Context-window budgeting: how much story memory fits next to the page being written."""

from __future__ import annotations


# Reading a comic page needs room for the page image (~1-2K tokens), the system prompts, the story bible and
# the answer. Below this a request cannot fit and every page would fail.
MIN_WORKING_CONTEXT = 8192


def estimate_tokens(text: str) -> int:
    """Cheap, slightly pessimistic token estimate (works across languages and providers)."""
    return max(1, int(len(text) / 3.3))


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def bible_word_cap(context_window: int) -> int:
    return clamp(context_window // 40, 220, 1200)


def history_token_budget(context_window: int) -> int:
    return clamp(context_window // 5, 400, 12_000)


def analyst_output_cap(context_window: int) -> int:
    return clamp(int(context_window * 0.30), 700, 3000)


def writer_output_cap(context_window: int, words_per_page: int) -> int:
    return clamp(int(context_window * 0.25), 600, max(2048, words_per_page * 3))


def recent_history(pages: list[str], token_budget: int) -> str:
    """The most recent novel pages that fit the budget (oldest first), always at least a slice of the last."""
    if not pages:
        return ""
    chosen: list[str] = []
    used = 0
    for text in reversed(pages):
        cost = estimate_tokens(text)
        if used + cost > token_budget:
            if not chosen:  # even the last page alone is too big: keep its tail
                chosen.append(text[-int(token_budget * 3.3):])
            break
        chosen.append(text)
        used += cost
    return "\n\n---\n\n".join(reversed(chosen))


def trim_bible(bible: str, word_cap: int) -> str:
    """Hard safety net if a model returns a bible longer than allowed (keeps the start)."""
    if len(bible.split()) <= word_cap * 1.5:
        return bible
    return bible[: int(word_cap * 1.5 * 6)].rstrip() + "\n..."


# ----------------------------------------------------------------- final review
def review_window_tokens(context_window: int) -> int:
    """How much novel text one review window may hold (the rest of the context is prompt, bible and answer)."""
    return clamp(int(context_window * 0.40), 1500, 24_000)


def review_output_cap(pages_in_window: int) -> int:
    """One short line per page (summary + problem)."""
    return clamp(95 * pages_in_window + 200, 400, 4000)


def split_windows(pages: list[tuple[int, str]], token_budget: int) -> list[list[tuple[int, str]]]:
    """Group consecutive pages into windows of at most ``token_budget`` tokens (a window has >= 1 page).

    A gap in the page numbers (pages that need no review) always starts a new window.
    """
    windows: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    used = 0
    for number, text in pages:
        cost = estimate_tokens(text)
        gap = bool(current) and number != current[-1][0] + 1
        if current and (gap or used + cost > token_budget):
            windows.append(current)
            current, used = [], 0
        current.append((number, text))
        used += cost
    if current:
        windows.append(current)
    return windows


def fit_summaries(lines: list[tuple[int, str]], flagged: set[int], token_budget: int) -> list[tuple[int, str]]:
    """Shrink the per-page summaries until they fit; flagged pages keep their full line."""
    def cost(items: list[tuple[int, str]]) -> int:
        return sum(estimate_tokens(f"{n}: {t}") for n, t in items)

    if cost(lines) <= token_budget:
        return lines
    shortened = [(n, t if n in flagged else " ".join(t.split()[:10])) for n, t in lines]
    if cost(shortened) <= token_budget:
        return shortened
    keep = [(n, t) for n, t in shortened if n in flagged]
    rest = [(n, t) for n, t in shortened if n not in flagged]
    step = max(2, -(-len(rest) // max(1, (token_budget - cost(keep)) // 18)))  # evenly sampled pages
    return sorted(keep + rest[::step])
