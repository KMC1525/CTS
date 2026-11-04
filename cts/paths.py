"""Filesystem locations used by CTS (all per-user, never inside the install dir)."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
ASSETS_DIR = PACKAGE_DIR / "assets"


def data_dir() -> Path:
    """Root folder for settings, models, uploads and generated novels.

    Override with the ``CTS_HOME`` environment variable.
    """
    override = os.environ.get("CTS_HOME")
    root = Path(override).expanduser() if override else Path(user_data_dir("CTS", appauthor=False))
    root.mkdir(parents=True, exist_ok=True)
    return root


def subdir(*parts: str) -> Path:
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    return data_dir() / "settings.json"


def models_dir() -> Path:
    return subdir("models")


def runtime_dir() -> Path:
    return subdir("runtime")


def workspace_dir() -> Path:
    return subdir("workspace")


def jobs_dir() -> Path:
    return subdir("jobs")


def logs_dir() -> Path:
    return subdir("logs")


def user_prompts_dir() -> Path:
    """Folder where users can drop edited copies of the built-in prompts."""
    return data_dir() / "prompts"


def state_file() -> Path:
    """Small JSON file describing the running server (pid/port) for ``cts launch``/``stop``."""
    return data_dir() / "server.json"
