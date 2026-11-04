"""Starts and supervises a local ``llama-server`` for the selected GGUF model."""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from ..paths import logs_dir
from . import deps, models, runtime


class LocalServerError(RuntimeError):
    pass


class LlamaServer:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._key: tuple | None = None
        self._port = 0
        self._lock = threading.Lock()
        atexit.register(self.stop)

    @property
    def endpoint(self) -> str | None:
        if self._proc and self._proc.poll() is None:
            return f"http://127.0.0.1:{self._port}/v1"
        return None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def ensure(self, model_id: str, context_window: int, on_status: Callable[[str], None] = lambda _m: None,
               should_cancel: Callable[[], bool] = lambda: False) -> str:
        """Start (or reuse) a server for ``model_id``; returns the OpenAI-compatible endpoint."""
        with self._lock:
            model = models.get_installed(model_id)
            if model is None:
                raise LocalServerError("No local model is installed/selected. Open Settings > Local models.")
            main, mmproj = models.paths_for(model)
            if not main.is_file():
                raise LocalServerError("The model file is missing. Reinstall it in Settings.")
            if mmproj is None:
                raise LocalServerError("This model has no vision projector (mmproj), so it cannot read comic pages. "
                                       "Pick a vision-capable model.")
            binary = runtime.find_server()
            if binary is None:
                raise LocalServerError("llama.cpp is not installed yet. Open Settings > Local models and press "
                                       "'Install llama.cpp'. " + runtime.MANUAL_HELP)
            key = (model_id, context_window, str(binary))
            if self.endpoint and self._key == key:
                return self.endpoint
            self._stop_locked()

            self._port = self._free_port()
            cmd = [str(binary), "-m", str(main), "--mmproj", str(mmproj), "-c", str(context_window),
                   "-np", "1", "--host", "127.0.0.1", "--port", str(self._port), "--jinja", "-ngl", "99"]
            env = runtime.runtime_env(binary)
            lib_dir = str(Path(binary).parent)
            log_path = logs_dir() / "llama-server.log"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            on_status("Loading the local model (this can take a minute)...")
            with open(log_path, "wb") as log:
                self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=lib_dir,
                                              creationflags=flags)
            self._key = key
            try:
                self._wait_ready(log_path, should_cancel)
            except BaseException:
                self._stop_locked()
                raise
            on_status("Local model ready")
            return self.endpoint or ""

    def _wait_ready(self, log_path: Path, should_cancel: Callable[[], bool], timeout: float = 900.0) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self._port}/health"
        with httpx.Client(timeout=3) as client:
            while time.monotonic() < deadline:
                if should_cancel():
                    raise LocalServerError("Cancelled while loading the model")
                if self._proc is None or self._proc.poll() is not None:
                    tail = log_path.read_text(errors="replace")[-1200:] if log_path.exists() else ""
                    code = self._proc.returncode if self._proc is not None else None
                    hint = deps.hint_for(tail, code) or "Is there enough RAM for this model and context size?"
                    raise LocalServerError(f"llama-server stopped while loading the model. {hint}\n{tail}")
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        raise LocalServerError("Timed out waiting for llama-server to load the model")

    def _stop_locked(self) -> None:
        proc, self._proc, self._key = self._proc, None, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


llama = LlamaServer()
