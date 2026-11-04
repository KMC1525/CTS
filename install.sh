#!/usr/bin/env bash
# CTS (Comic To Story) installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/KMC1525/CTS/main/install.sh | bash
#
# What it does: installs `uv` (a tiny Python manager) if needed, creates a private Python 3.12
# environment in ~/.cts, installs CTS and every library it needs, downloads the llama.cpp engine
# for local models, and adds CTS to your applications menu.
#
# Options (when running the file directly):  --no-runtime  --no-desktop  --dev  --uninstall  --purge
# Environment:  CTS_REF (branch/tag, default main)  CTS_INSTALL_HOME (default ~/.cts)  CTS_BIN_DIR (default ~/.local/bin)
set -euo pipefail

main() {
  local REPO="KMC1525/CTS"
  local REF="${CTS_REF:-main}"
  local INSTALL_HOME="${CTS_INSTALL_HOME:-$HOME/.cts}"
  local BIN_DIR="${CTS_BIN_DIR:-$HOME/.local/bin}"
  local PY_VERSION="${CTS_PYTHON:-3.12}"
  local VENV="$INSTALL_HOME/venv"
  local DO_RUNTIME=1 DO_DESKTOP=1 DEV=0 UNINSTALL=0 PURGE=0

  for arg in "$@"; do
    case "$arg" in
      --no-runtime) DO_RUNTIME=0 ;;
      --no-desktop) DO_DESKTOP=0 ;;
      --dev) DEV=1 ;;
      --uninstall) UNINSTALL=1 ;;
      --purge) PURGE=1 ;;
      -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; return 0 ;;
      *) echo "Unknown option: $arg" >&2; return 2 ;;
    esac
  done

  say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
  warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
  die()  { printf '\033[31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

  case "$(uname -s)" in
    Linux)  OS=linux ;;
    Darwin) OS=macos ;;
    *) die "This installer supports macOS and Linux. On Windows use install.ps1." ;;
  esac

  # ------------------------------------------------------------------ uninstall
  if [ "$UNINSTALL" = 1 ]; then
    say "Removing CTS"
    if [ -x "$VENV/bin/cts" ]; then
      "$VENV/bin/cts" stop >/dev/null 2>&1 || true
      "$VENV/bin/cts" uninstall-desktop || true
      [ "$PURGE" = 1 ] && "$VENV/bin/cts" purge --yes || true
    fi
    rm -f "$BIN_DIR/cts"
    rm -rf "$INSTALL_HOME"
    say "CTS was removed."
    [ "$PURGE" = 1 ] || echo "Your settings, models and novels were kept. Re-run with --uninstall --purge to delete them too."
    return 0
  fi

  # --------------------------------------------------------------------- source
  local SRC="" SELF_DIR=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/pyproject.toml" ] && [ -d "$SELF_DIR/cts" ]; then
    SRC="$SELF_DIR"
    say "Installing CTS from this folder ($SRC)"
  else
    SRC="https://github.com/$REPO/archive/$REF.zip"
    say "Installing CTS ($REF) from GitHub"
  fi

  # ------------------------------------------------------------------------- uv
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (Python manager)"
    if command -v curl >/dev/null 2>&1; then
      curl -LsSf https://astral.sh/uv/install.sh | sh || warn "Could not install uv."
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- https://astral.sh/uv/install.sh | sh || warn "Could not install uv."
    else
      warn "Neither curl nor wget is available."
    fi
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  fi

  mkdir -p "$INSTALL_HOME" "$BIN_DIR"
  local PIP_ARGS=("$SRC")
  if [ "$DEV" = 1 ] && [ -d "$SRC" ]; then PIP_ARGS=(-e "$SRC"); fi

  if command -v uv >/dev/null 2>&1; then
    say "Creating a private Python $PY_VERSION environment"
    uv venv --python "$PY_VERSION" --allow-existing "$VENV" >/dev/null || die "uv could not create the environment."
    say "Installing CTS and its libraries (CrewAI, FastAPI, Pillow...). This can take a few minutes."
    uv pip install --python "$VENV/bin/python" --upgrade "${PIP_ARGS[@]}" || die "Installing the Python packages failed."
  else
    warn "uv is unavailable - falling back to your system Python."
    local PY=""
    for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
      if command -v "$candidate" >/dev/null 2>&1 &&
         "$candidate" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)' 2>/dev/null; then
        PY="$candidate"; break
      fi
    done
    [ -n "$PY" ] || die "Python 3.10-3.13 is required. Install it (macOS: brew install python@3.12 / Linux: your package manager) and re-run."
    "$PY" -m venv "$VENV" || die "Could not create a virtual environment (on Debian/Ubuntu: sudo apt install python3-venv)."
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --upgrade "${PIP_ARGS[@]}" || die "Installing the Python packages failed."
  fi

  ln -sf "$VENV/bin/cts" "$BIN_DIR/cts"

  # --------------------------------------------------------- llama.cpp (local AI)
  if [ "$DO_RUNTIME" = 1 ]; then
    say "Installing the llama.cpp engine (runs local GGUF models)"
    "$VENV/bin/cts" setup-runtime || warn "Skipped. You can install it later from Settings > Local models."
  fi

  # -------------------------------------------------------- applications menu
  if [ "$DO_DESKTOP" = 1 ]; then
    say "Adding CTS to your applications"
    "$VENV/bin/cts" install-desktop --exec "$BIN_DIR/cts" || warn "Could not add CTS to the applications menu."
  fi

  say "CTS is installed."
  case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "Add $BIN_DIR to your PATH to use the 'cts' command:  export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
  if [ "$OS" = macos ]; then
    echo "Open it from Launchpad / Spotlight (search for CTS), or run:  cts"
  else
    echo "Open it from your applications menu (search for CTS), or run:  cts"
  fi
}

main "$@"
