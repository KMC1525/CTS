# CTS - Comic To Story

**CTS turns comic volumes into novels.** Drop a folder or ZIP of comic pages, and a team of AI agents reads every page, writes it as prose, edits it, and keeps the story consistent from the first page to the last - producing a complete novel in either an **SFW** or an **NSFW** edition.

- **One comic page = one novel page.** Pacing, order and dialogue stay faithful to the original.
- **Multi-step agents** built with [CrewAI](https://github.com/crewAIInc/crewAI): *Read → Write → Polish → Remember* for every page.
- **Bring your own AI:** frontier models (Claude, ChatGPT, Grok, Gemini, OpenRouter, any OpenAI-compatible endpoint) **or** local GGUF models from Hugging Face, with the quantization of your choice.
- **Batch mode:** attach several volumes and CTS writes them one after another (volume 1, then 2, then 3...), carrying names and plot between volumes.
- **Export** the finished novel as **TXT, DOCX or PDF** (6 x 9 in pages, one novel page per comic page).
- **A clean, dark web UI** (plain HTML, CSS and JavaScript - no build step) that runs locally on your computer.
- **Private by default:** everything runs on your machine; CrewAI telemetry is switched off. Comic pages only leave your computer if *you* pick a hosted AI provider.

> **Status: v0.2 (early release).** The pipeline, UI and Linux installer have been exercised end to end, and the llama.cpp installer is covered by tests that simulate GitHub releases. The Windows and macOS installers follow the same design but have not been verified on real machines yet - please [open an issue](https://github.com/KMC1525/CTS/issues) if anything misbehaves. See the [changelog](CHANGELOG.md) for what is new.

---

## Install

CTS runs on **Windows, macOS and Linux**. The installer sets everything up for you: a private Python environment, CTS and all of its libraries (CrewAI, FastAPI, Pillow, python-docx, ReportLab...), the llama.cpp engine for local models, and an entry in your **applications list** (Start Menu / Launchpad / app menu). You do not need to install Python yourself.

### macOS and Linux

```bash
curl -fsSL https://raw.githubusercontent.com/KMC1525/CTS/main/install.sh | bash
```

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/KMC1525/CTS/main/install.ps1 | iex
```

When it finishes, open **CTS** from your applications list, or run `cts` in a terminal. It starts a small local server and opens the app in your browser.

<details>
<summary>What the installer does, and options</summary>

1. Installs [`uv`](https://docs.astral.sh/uv/) if it is missing (it can fetch the right Python for you).
2. Creates a private Python 3.12 environment (`~/.cts` on macOS/Linux, `%LOCALAPPDATA%\Programs\CTS` on Windows).
3. Installs CTS and **every** dependency into it (CrewAI with its Anthropic and Gemini clients, FastAPI, Pillow, python-docx, ReportLab...).
4. Installs the prebuilt [llama.cpp](https://github.com/ggml-org/llama.cpp) engine used for local models, **runs it to check that it starts**, and installs the system libraries it needs when it can (see [llama.cpp engine](#the-llamacpp-engine)). If this step fails, the rest of CTS is still installed and you can retry later.
5. Runs `cts doctor --fix`, which checks every library and repairs what is missing.
6. Adds CTS to your applications list and puts a `cts` command on your `PATH`.

Options when you run the script from a checkout: `--no-runtime`, `--no-desktop`, `--dev` (editable install), `--uninstall`, `--purge` (Windows: `-NoRuntime`, `-NoDesktop`, `-Dev`, `-Uninstall`, `-Purge`). Set `CTS_REF` to install a different branch or tag.

If `uv` cannot be installed, the scripts fall back to a system Python 3.10-3.13.

**Uninstall:** `./install.sh --uninstall` (add `--purge` to also delete your settings, models and novels), or on Windows `.\install.ps1 -Uninstall`.

</details>

### Updating

Run the same install command again, or run `cts update`. An update **reinstalls CTS from the latest code, upgrades every Python library, updates llama.cpp to its newest release (and re-checks its system libraries), and refreshes your applications-menu entry.** Your settings, models, uploaded volumes and generated novels are kept. To update only the llama.cpp engine, use `cts setup-runtime --update` or the **Update** button in *Settings → Local models*.

---

## Quick start

1. **Choose an AI** - click **Settings** (bottom-left).
   - *Frontier AI:* pick a provider, paste your API key, choose a vision-capable model.
   - *Local models (GGUF):* pick a model, press **Show quantizations**, choose one, and **Install**.
2. **Attach your comic** - drag a folder or ZIP/CBZ onto the **zip box** on the left (or use the *ZIP / CBZ* and *Folder* buttons). Add as many volumes as you like.
3. **Pick the options** - SFW or NSFW edition, words per page, novel language.
4. **Press Generate.** Watch the **story box** on the right: every page shows its progress through *Read → Write → Polish → Remember*, and the novel text appears as it is written.
5. **Download** the volume as **TXT**, **DOCX** or **PDF** when it is ready. You can stop at any time and download what exists so far, or resume later.

---

## How it works

```mermaid
flowchart LR
    A[Folder / ZIP / CBZ] --> B[Pages in reading order]
    B --> C["Read<br/>Comic Page Analyst (vision)"]
    C --> D{Age and rating gate<br/>plain code}
    D --> E["Write<br/>Novelist"]
    E --> F["Polish<br/>Editor"]
    F --> G["Remember<br/>Story Keeper"]
    G -->|story bible| C
    G -->|story bible| E
    F --> H[TXT / DOCX / PDF]
```

For **every page**, CTS runs three small CrewAI crews:

| Step | Agent | What it does |
| --- | --- | --- |
| **Read** | Comic Page Analyst | Looks at the page image: reading order (including right-to-left manga), every panel, all dialogue and captions verbatim, characters, mood - plus safety flags. |
| **Write** | Novelist | Writes exactly one page of prose from the analysis, the **story bible** and the most recent novel pages. |
| **Polish** | Editor | Checks fidelity against the analysis, continuity, style and the word budget. Over-long pages are condensed. |
| **Remember** | Story Keeper | Updates the **story bible** (voice, characters, places, plot so far, open threads) so the next page - and the next volume - stays consistent. |

Progress is saved page by page, so an interrupted job can be **resumed** without repeating finished pages.

### Context window

*Settings → Context window* tells CTS how much text your model can hold. CTS uses it to decide how much story-so-far (bible + recent pages) it feeds to each page. For local models it is also passed to llama.cpp, so bigger windows need more RAM/VRAM.

| Window | Notes |
| --- | --- |
| 8K | Practical minimum. Little memory of previous pages. |
| 16K-32K | Good default for local models and most pages. |
| 64K+ | Best continuity; use with hosted models or lots of memory. |

### SFW and NSFW editions

- **SFW** keeps romance and tension but closes the door on sexual scenes and avoids gratuitous gore.
- **NSFW** is an adults-only edition: where the comic shows or implies sexual content between **adult** characters, it is written fully.
- **Age guard (always on).** The analyst flags whether any character is, or looks like, a minor. A page that has sexual content **and** may involve a minor - or whose safety flags are missing - is *always* written without sexual content, regardless of the selected edition. This check is plain code, not a prompt a model could talk its way around. It relies on the model's reading of the page, so no automated check is perfect: review your output.
- Hosted providers may **decline** NSFW pages. CTS detects a refusal, falls back to the SFW version of that page, and tells you. **Local models are the most reliable choice for the NSFW edition.**

### Input

- One folder or ZIP/CBZ = **one volume**. Images: JPG, PNG, WebP, BMP, GIF, TIFF. Pages are ordered naturally (`2.jpg` before `10.jpg`).
- If a folder/ZIP contains several sub-folders named like volumes (`Vol 1`, `Volume 02`, `v3`, `Tome 4`...) and no pages of its own, each sub-folder becomes its own volume.
- Volumes are generated in the order shown in the zip box - use the arrows to reorder.

### Output

Each volume produces a `TXT`, `DOCX` and `PDF`: a title page, then one novel page per comic page on 6 x 9 in pages (long pages shrink slightly to stay on one page). Files live in your CTS data folder (`cts info` shows where) and are also available from the download buttons.

---

## AI providers

### Frontier (hosted) models

| Provider | Notes |
| --- | --- |
| Anthropic Claude | Native client. |
| OpenAI ChatGPT | Native client. |
| xAI Grok | OpenAI-compatible endpoint (`https://api.x.ai/v1`). |
| Google Gemini | Native client. |
| OpenRouter | One key, many models - use a vision-capable model. |
| Custom endpoint | Any OpenAI-compatible server (LM Studio, vLLM, Ollama `/v1`, a gateway...). |

The model **must accept images**. The model name box accepts any model id, so new models work as soon as your provider offers them. API keys are stored only in your local `settings.json` (owner-readable), and are never sent back to the browser. As a fallback CTS reads the usual environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`).

> Hosted AI usage is billed by your provider. A page costs roughly four model calls (one with an image), so a 200-page volume is on the order of 800 calls - check your provider's pricing first.

### Local models (GGUF via Hugging Face)

CTS runs local models with [llama.cpp](https://github.com/ggml-org/llama.cpp)'s `llama-server`. In *Settings → Local models*:

1. Make sure the engine is ready - the installer normally did this; otherwise press **Install llama.cpp** (see [below](#the-llamacpp-engine)).
2. Choose a curated vision model, or **Other Hugging Face repository...** to search or paste any `owner/model-GGUF` repo.
3. Press **Show quantizations** and pick one. Smaller (`Q4_K_M`, `IQ4_XS`...) is faster and lighter; larger (`Q6_K`, `Q8_0`) is closer to the original quality. Sizes are shown before you download.
4. Press **Install**. The model and its vision projector (`mmproj`) are downloaded with progress, and the download resumes if interrupted.

The model must be **vision-capable** (a repository that ships an `mmproj` file) or it cannot read comic pages. Gated models need a Hugging Face token (*Settings → Local models*).

### The llama.cpp engine

CTS downloads the official prebuilt llama.cpp for your system and manages it for you:

- **Picks the right build.** macOS: the native build (Metal GPU support is included). Windows and Linux: the **Vulkan** build (works with NVIDIA, AMD and Intel GPUs without extra toolkits), with the plain **CPU** build as an automatic fallback.
- **Checks that it works.** The engine is run (`llama-server --version`) before CTS accepts it. If a GPU build does not start on your machine, CTS installs the CPU build instead.
- **Installs missing system libraries.** On Linux it detects a missing `libgomp` / `libvulkan` and installs it with your package manager when it can (as root, or with passwordless `sudo`); otherwise it prints the exact command to run. On Windows it installs the Microsoft Visual C++ Redistributable with `winget` when the engine reports a missing DLL.
- **Updates.** *Settings → Local models → Update* (or `cts setup-runtime --update`) moves to the newest llama.cpp release; new model families often need a newer engine.
- **Works around GitHub limits.** llama.cpp publishes its builds as pre-releases (tags `b12111`...), so CTS lists the releases and picks the newest build tag itself. If the GitHub API is rate-limited or blocked, it reads the public releases feed instead; if the newest build's files are not uploaded yet, it uses the previous build. Set `GITHUB_TOKEN` to raise the API limit.
- **Installs safely.** The engine is unpacked into a temporary folder and only moved into place once it works, so a failed install never leaves a broken engine behind.
- **Respects yours.** If `llama-server` is already on your `PATH` (for example from `brew` or `winget`) or set with `CTS_LLAMA_SERVER`, CTS uses it and never replaces it.

**Troubleshooting:** run `cts doctor` (add `--fix` to repair). In *Settings → Local models*, **Check** runs the engine and shows the exact error, and **Repair** reinstalls it. To force a build, run `cts setup-runtime --repair --flavor cpu` (or `vulkan`, `cuda`...); `cuda` builds also download the matching CUDA runtime archive. To use your own build, set `CTS_LLAMA_SERVER` to the `llama-server` binary.

---

## Customising the prompts

The quality of the novels and of the page analysis is driven by Markdown system prompts shipped in [`cts/prompts/`](cts/prompts). Copy any of them to your CTS data folder under `prompts/` (run `cts info` for the exact path) and edit it there - your copy wins over the built-in one and survives upgrades. See [`cts/prompts/README.md`](cts/prompts/README.md).

## Command line

| Command | What it does |
| --- | --- |
| `cts` / `cts launch` | Start CTS in the background (if needed) and open it in the browser. |
| `cts serve` | Run the server in this terminal (`--port`, `--no-browser`). |
| `cts stop` | Stop the background server. |
| `cts update` | Update CTS, all its Python libraries and llama.cpp (re-runs the installer). |
| `cts doctor` | Check every library CTS needs, including llama.cpp and its system libraries. `--fix` repairs what is missing. |
| `cts setup-runtime` | Install the llama.cpp engine. `--update` moves to the newest release, `--repair` reinstalls it, `--flavor cpu\|vulkan\|cuda` forces a build. |
| `cts install-desktop` / `cts uninstall-desktop` | Add/remove CTS in your applications list. |
| `cts info` | Show the data folder, custom-prompt folder and engine status. |
| `cts purge --yes` | Delete all CTS data (settings, models, uploads, novels). |

## Privacy and security

- The server listens on `127.0.0.1` only and rejects requests from other websites (Host and Origin checks).
- CrewAI telemetry and tracing are disabled.
- Uploads, models and novels stay in your data folder. Frontier providers receive page images and text only when you select them.

## Development

```bash
git clone https://github.com/KMC1525/CTS.git && cd CTS
./install.sh --dev --no-runtime --no-desktop            # editable install into ~/.cts
uv pip install --python ~/.cts/venv/bin/python -e ".[dev]"   # adds pytest
~/.cts/venv/bin/python -m pytest
~/.cts/venv/bin/cts serve
```

Layout: `cts/pipeline/` (CrewAI agents and runner), `cts/prompts/` (system prompts), `cts/local/` (Hugging Face and llama.cpp), `cts/web/` (the vanilla front end), `cts/server.py` (API), `cts/export.py` (TXT/DOCX/PDF), `install.sh` / `install.ps1`.

The tests run the whole pipeline through real CrewAI against a local mock of the OpenAI API, so they need no keys and no network.

## Responsible use

CTS adapts comics into prose with generative AI. Only use material you have the right to use, and respect the copyright of the comics you convert. The NSFW edition is for adults; you are responsible for the content you generate and share. AI output can contain mistakes - read it before you publish it.

## License

[MIT](LICENSE) - Copyright (c) 2026 KMC
