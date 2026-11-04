# CTS system prompts

These Markdown files are the *system prompts* of the CTS agents. They are loaded at
run time, so you can tune the quality of your novels without touching any Python.

| File | Agent | What it controls |
| --- | --- | --- |
| `comic_analyst.md` | Comic Page Analyst (vision) | How a comic page is read: panel order, dialogue transcription, characters, mood, safety flags |
| `novelist.md` | Novelist | The prose craft: voice, fidelity to the page, pacing, dialogue, continuity |
| `rating_sfw.md` | Novelist (SFW mode) | Content limits for the all-audiences version |
| `rating_nsfw.md` | Novelist (NSFW mode) | Content rules for the adults-only version (includes the non-negotiable age guard) |
| `editor.md` | Editor | The polish pass: fidelity check, style, length budget |
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
