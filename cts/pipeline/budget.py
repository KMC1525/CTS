"""Context-window budgeting: how much story memory fits next to the page being written."""

from __future__ import annotations


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
