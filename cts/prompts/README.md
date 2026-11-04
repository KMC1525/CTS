# CTS system prompts

These Markdown files are the *system prompts* of the CTS agents. They are loaded at
run time, so you can tune the quality of your novels without touching any Python.

| File | Agent | What it controls |
| --- | --- | --- |
| `cover-identify.md` | Cover Identifier (vision) | Decides whether a page is a book cover (front, back, spine, jacket); covers are skipped and never become novel pages |
| `comic_analyst.md` | Comic Page Analyst (vision) | How a comic page is read: panel order, dialogue transcription, characters, stated ages, mood, safety flags |
| `novelist.md` | Novelist | The prose craft: voice, fidelity to the page, pacing, dialogue, continuity |
| `rating_sfw.md` | Novelist (SFW mode) | Content limits for the all-audiences version |
| `rating_nsfw.md` | Novelist (NSFW mode) | Content rules for the adults-only version, including the age rule: a clearly under-18 character halts the process; stated adults, or no age mentioned, continue |
| `editor.md` | Editor | The polish pass: fidelity check, style, length budget |
| `final-revisor.md` | Final Revisor | Reads the finished novel as a book before delivery: judges coherence page windows and the whole, and says which pages (or the whole volume) must be written again |
| `story_keeper.md` | Story Keeper | The running "story bible" that keeps names, looks and plot consistent |

## Customising

Copy a file to `<CTS data folder>/prompts/` (run `cts info` to see the folder) and edit it
there. A file in that folder wins over the built-in one with the same name, and your
edits survive upgrades. Delete your copy to go back to the default.

Each file starts with a small front-matter block:

```
---
role: The agent's role (shown to the model)
goal: The agent's one-line goal
---
```

Everything after the second `---` is the system prompt body.

## Your own system prompt

Separately from these files, the web UI has an optional **System prompt** box under the novel options. Whatever you write there is added to the novelist, editor and story keeper (after `novelist.md`/`editor.md`/`story_keeper.md` and before the rating policy) and is shown to the final revisor. It steers voice, tone and style; it never overrides the rating policy, the age rule or the one-page-per-page contract.
