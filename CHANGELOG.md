# Changelog

All notable changes to CTS (Comic To Story) are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

To update an existing installation, run the install command again or run `cts update` (see the [README](README.md#updating)).

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
