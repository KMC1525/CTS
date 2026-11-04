# Changelog

All notable changes to CTS (Comic To Story) are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

To update an existing installation, run the install command again or run `cts update` (see the [README](README.md#updating)).

## 0.4.0 - 2026-11-04

### Added
- **`final-revisor` system prompt and Final Revisor agent** (`cts/prompts/final-revisor.md`). Before a volume is delivered, the finished novel is read as a book. The revisor reads it in windows of consecutive pages sized to the model's context window (with the story bible), writes a one-line summary and any concrete problem for every page, and then gives a verdict on the whole novel:
  - **`coherent`** - the novel passes and is delivered as it is.
  - **`fix-pages`** - only the named pages are written again, with the revisor's notes and the faulty version in front of the writer. The saved page analysis is reused, so the comic image is **not** read again, and every other page is kept untouched. Only the rewritten pages and their neighbours are reviewed again.
  - **`redo-all`** - the whole volume is produced again (images included), guided by the revisor's notes, when the book is incoherent as a whole.
- **Automatic (no-LLM) checks** for pages that can never be coherent - empty pages, placeholders for failed pages, refusals, leaked instructions or markers, and a page that repeats the previous one. These pages are always written again, even when the revisor says the rest is fine (this also gives pages that failed during generation a second chance).
- **Bounded:** at most two rewrites per volume (`review_rounds`); if problems remain the novel is delivered with the open issues reported, and if the review cannot run the novel is delivered without it. A volume that was already reviewed is not reviewed again on resume unless its text changed (`review.json` is saved with the job).
- **"Final review before delivery" option** (on by default; `final_review` in the API) and a **Review** button in the story box with a report of every round (verdict, reason, pages rewritten, problems found). Rewritten pages are marked *revised*, and a page being rewritten keeps its old text on screen meanwhile.
- **Optional "System prompt" section** under the novel options (`custom_prompt` in the API, up to 6,000 characters, remembered between sessions). It steers the narrative's voice, tone, style, point of view and tense in **both the SFW and the NSFW edition**. It is given to the novelist, the editor and the story keeper (after their built-in prompt and *before* the rating policy, so the rating rules and the age rule stay the last thing the model reads) and to the final revisor (so the requested style is not flagged as a defect). It cannot change what happens on a comic page, the page-for-page contract, the rating policy or the age rule - the age rule is enforced by code on the page analysis, so no system prompt can switch it off.
- Tests for all of the above, including mutation-checked tests for each decision path of the review (pass, rewrite pages, redo all, rewrite limit, mechanical findings, review failure, unreadable answers, resume), the order of the prompt sections and the effect of the system prompt on both editions.

### Changed
- `VolumeRunner.run_volume` now writes the pages and then runs the review before exporting, so the downloadable files always contain the reviewed novel. The page-writing step was split out so that a single page can be rewritten from its saved analysis.
- The story keeper's prompt is also steered by the system prompt, so the voice recorded in the story bible matches the direction.
- Job and story-box state now carry the review report per volume; a stopped job no longer leaves a review marked as running.

## 0.3.0 - 2026-11-04

### Added
- **`cover-identify` system prompt and Cover Identifier agent** (`cts/prompts/cover-identify.md`). On the first two and last two pages of every volume, one short vision call decides whether the page is a book cover (front, back, spine, flap, wrap-around). **A cover is skipped: it is not analysed and no novel page is written from it.** The verdict must be at least medium-confidence, so a real story page is never dropped on a guess; chapter title pages, splash pages and advertisements are not covers. If the analyst itself classifies a page near the start or end of the volume as a cover, it is skipped too. A title printed on a front cover becomes the title of the exported novel.
- **Skip book covers** option (on by default; `skip_covers` in the API) and a "skipped" card in the story box that shows the detected cover and its title. Skipped pages count as finished, are saved in the checkpoint and are replayed on resume.
- **New job status `halted`** ("stopped by age rule") with the character and the reason; a halted job keeps the pages written before it as a partial novel and cannot be resumed.
- **Local engine start-up checks:** the model and `mmproj` files are verified to be real GGUF files (before starting, and right after downloading); `/props` confirms that vision really loaded and reports the context the server really has.
- **Pre-flight checks** when a job is created: engine installed, model files present, vision projector present, context window of at least 8,192 tokens - each with a message that says what to do.
- Tests for all of the above, including regression tests that reproduce each local-connection problem below.

### Changed
- **NSFW age rule (as requested).** The comic decides: if it makes clear that a character is **under 18**, the whole process **stops** (the NSFW prompt tells the model to answer `[[CTS-STOP-MINOR]]`, and plain code stops on a stated age below 18 or on the analyst's `minor_involved: yes`; a character established as a minor on one page stays a minor later); if it makes clear the character is **18 or older**, or **does not mention ages**, generation **continues** and everyone is treated as an adult. Previously a page with possible minors was silently rewritten in SFW terms, judged partly by how characters look. Characters who merely *look* young but whose age is not mentioned now continue in the NSFW edition, as specified; a character who is unmistakably a young child still counts as clearly under 18. The SFW edition does not apply the rule.
- The analyst's page description now reports each character's **stated age** separately from the apparent age, and the story bible records ages only as the comic states them (`minor`, `adult`, a number or `not stated`). `minor_involved` is `yes` or `no` (`unclear` is read as `no`).
- Exports leave covers out and number the novel pages 1..n.
- The smallest context window is 8,192 tokens (the 4,096 preset was removed): a comic page, the prompts and the answer do not fit in less.

### Fixed - connection between the local model and CTS
- **Proxies broke local models.** The health check and the LLM client obeyed `HTTP_PROXY` / `ALL_PROXY`, so on a network with a proxy (and no `NO_PROXY` for localhost) the engine on 127.0.0.1 looked dead or calls timed out. Loopback traffic now never uses a proxy.
- **Forced `-ngl 99` disabled llama.cpp's automatic memory fit**, so models that did not fit in VRAM failed instead of being adjusted. On builds that support `--fit` CTS no longer passes `-ngl`; older builds still get `-ngl 99`. Flags are detected from `llama-server --help`, and an unknown flag is never passed.
- **"Thinking" models returned empty pages**: hidden reasoning used the whole answer budget. CTS starts the engine with `--reasoning off` (or `--reasoning-budget 0` on builds without it).
- **Vision and context were never verified.** A model that started without image support, or with less context than requested, only failed in the middle of a generation. CTS now reads `/props` after start-up, refuses a server without vision, and plans its prompt budgets with the context the server really has.
- **A dead engine ended the job.** If llama-server died mid-job (out of memory, killed) three pages failed and the job aborted. CTS now restarts the engine and retries; if it cannot, the job stops with the reason.
- **Quit, model deletion and Stop could hang.** Stopping the engine waited for the loading lock (up to 15 minutes while a model loaded). `stop()` now interrupts a load at once, and pressing **Stop** during a local job stops the engine so a long generation ends immediately.
- **Orphaned engines.** A `llama-server` left behind by a CTS that was killed kept using RAM. A pid file lets the next start remove it (never a process that is not llama-server).
- **Port races** between picking a free port and the engine binding it are retried with another port.
- **Damaged or incomplete model files** (a download that was really an error page, a missing `mmproj`) are caught before starting the engine, with advice, instead of an obscure engine crash.
- **llama.cpp's own errors were not understood**: a request that exceeds the context window (`exceed_context_size_error`) or an image sent to a model without vision (`image input is not supported`) were retried three times and then reported as a generic failure. They are now recognised as unfixable by retrying and explained.
- Loading the model now shows elapsed time, and a stale "running" state is no longer left on pages and volumes after a job stops.

## 0.2.1 - 2026-11-04

### Fixed
- **llama.cpp could not be installed** - `llama-v0.9.0-bin-ubuntu-vulkan-x64.tar.gz: 404 Not Found`. llama.cpp publishes every build as a GitHub *pre-release* (tags like `b12111`), and GitHub's "latest release" endpoint (API and website redirect alike) skips pre-releases and returns an unrelated release - `v0.9.0` - that has no binaries. CTS 0.2.0 trusted that answer and built download URLs from it. CTS now lists the releases and only accepts tags shaped like `b<number>`, newest first, and never calls the "latest" endpoint.
- If the GitHub API is rate-limited or blocked, CTS reads the public releases feed (`releases.atom`) instead of the old redirect trick.
- If the newest build is listed but its files are not uploaded yet (404), CTS falls back to the previous build instead of failing. A build that downloads but does not run on this computer does not trigger downloads of older builds.
- Failed attempts are reported plainly in the error message (for example "not published (404)") together with what to do next.

### Added
- Regression tests that reproduce the report: an unrelated `v0.9.0` release is served as "latest" and must never be used, via both the API and the feed.

## 0.2.0 - 2026-11-04

This release is about installing and updating **everything** CTS needs - above all the llama.cpp engine for local models - and making that process reliable.

### Added
- **`cts update`** - updates CTS, every Python library and llama.cpp in one step (re-runs the official installer; on Windows it hands over to a new PowerShell window so `cts.exe` can be replaced).
- **`cts doctor [--fix]`** - checks every Python library CTS needs plus the llama.cpp engine and its system libraries, and repairs what is missing.
- **`cts setup-runtime --update | --repair | --flavor cpu|vulkan|cuda`** - update the llama.cpp engine to the newest release, reinstall it, or force a specific build.
- **Engine controls in the UI** (*Settings → Local models*): the installed llama.cpp version and build, an **Update** button, a **Check** button that runs the engine and shows the exact error with a suggested fix, and a **Repair** button.
- **System-library installer** (`cts/local/deps.py`): when `llama-server` cannot start, CTS reads the loader error and installs the missing pieces - `libgomp`/`libvulkan`/`libstdc++` on Linux through `apt`, `dnf`, `yum`, `pacman`, `zypper` or `apk` (as root or with passwordless `sudo`, otherwise it prints the exact command), and the Microsoft Visual C++ Redistributable on Windows through `winget`.
- **CUDA support**: CUDA builds now also download the separate `cudart` archive they need (`CTS_LLAMA_FLAVOR=cuda`).
- **`POST /api/local/runtime/check`** endpoint, and `update` / `repair` options on `POST /api/local/runtime/install`.
- Support for `GITHUB_TOKEN` / `GH_TOKEN` to raise the GitHub API rate limit when looking up llama.cpp releases.
- `CHANGELOG.md`.
- Tests: the llama.cpp installer is now covered with simulated GitHub releases using the **real asset names** from llama.cpp's release workflow (installation, GPU-to-CPU fallback, updates, API-less fallback, symlinked data folders), plus tests for `doctor`, `update`, the new API endpoints, system-library handling, and that every front-end module parses as an ES module.

### Fixed
- **llama.cpp was never updated.** The old installer returned as soon as any `llama-server` existed, so "updating" did nothing and old engines could not run newer models. A CTS-managed engine (including those installed by 0.1) is now moved to the newest release on update; engines you installed yourself (`brew`, `winget`, `PATH`, `CTS_LLAMA_SERVER`) are used as they are and never replaced.
- **The downloaded engine was never run.** A build that could not start (missing `libgomp`/`libvulkan`, missing Visual C++ runtime, wrong CPU) only failed later, when generating. It is now executed (`--version`) before being accepted, repaired if possible, and **replaced by the plain CPU build automatically** if the preferred Vulkan/GPU build does not work.
- **Installation was not atomic.** The engine is now unpacked into a temporary folder and moved into place only after it works, so an interrupted or failed install can no longer leave a half-extracted engine that CTS would try to use. Old versions and cached archives are cleaned up afterwards.
- **GitHub API failures stopped the install.** When the API is rate-limited (60 requests per hour per IP) or blocked, CTS now reads the latest release tag from the github.com redirect and derives the download names from llama.cpp's naming scheme.
- **Damaged downloads** are discarded and the next build is tried instead of failing with an obscure error.
- **The installer could keep an old copy of CTS.** `install.sh` / `install.ps1` now always reinstall CTS itself (`--reinstall-package`, `--refresh-package`) while upgrading every dependency, instead of trusting a cached download with an unchanged version number.
- **Updating while CTS was running** could fail on Windows because the running `cts.exe` was locked; the installers now stop the background server first.
- `--no-runtime` is now respected by the final health check, and a failed llama.cpp step no longer repeats the same error twice.
- A local model that dies at startup now reports *why* (for example the missing library and the command that fixes it) instead of only suggesting that RAM might be too low.

### Changed
- The installers print the installed CTS version and run `cts doctor --fix` at the end.
- The `llama-server` environment (library search paths) is now built in one place and shared by the installer's check and the running server.

## 0.1.0 - 2026-11-04

First release.

### Added
- **Multi-step CrewAI pipeline**: for every comic page - *Read* (vision analyst) → age/rating gate → *Write* (novelist) → *Polish* (editor) → *Remember* (story keeper). One comic page becomes exactly one novel page.
- **Batch generation**: several volumes are written one after another with the story bible carried between them; page-level checkpoints with stop / resume.
- **SFW and NSFW editions** with a code-level age guard (pages that may involve a minor are never written with sexual content) and automatic fallback to the SFW version when a hosted model declines.
- **Markdown system prompts** in `cts/prompts/` for page analysis, the novelist, the editor, the story keeper and both rating policies; overridable from the user data folder.
- **Providers**: Claude, ChatGPT, Grok, Gemini, OpenRouter and any OpenAI-compatible endpoint, or **local GGUF models from Hugging Face** with a choice of quantization, resumable downloads and a managed llama.cpp server.
- **Configurable context window**, used to budget how much story-so-far each page receives and passed to the local model.
- **Export to TXT, DOCX and PDF** (6 x 9 in pages).
- **Web UI** in plain HTML, CSS and JavaScript, Grok-inspired dark theme: zip box (left) with drag and drop of folders and ZIPs, story box (right) with live per-page progress, settings (bottom-left).
- **Installers** for macOS, Linux (`install.sh`) and Windows (`install.ps1`) that set up a private Python environment, all libraries, and an entry in the applications list.
- CLI: `cts`, `serve`, `launch`, `stop`, `setup-runtime`, `install-desktop`, `uninstall-desktop`, `info`, `purge`.
- Localhost-only server with Host/Origin checks; CrewAI telemetry disabled.
- Natural ordering of volumes (`Vol 2` before `Vol 10`), and folder/ZIP entries with characters Windows cannot store (such as `Vol 1: Intro`) are sanitised instead of silently losing their pages.
- Test suite that runs the real CrewAI pipeline against a local mock of the OpenAI API.
