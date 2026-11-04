"""Starts and supervises a local ``llama-server`` for the selected GGUF model.

Everything that can go wrong between CTS and the engine is handled here:

* proxies never see the loopback traffic (health checks use ``trust_env=False``; the LLM client gets
  ``NO_PROXY``, see ``llm.bypass_proxy_for_loopback``);
* the model/projector files are checked (GGUF magic) before the engine is started;
* the engine is told to size itself to the machine (``--fit``) and not to "think" (``--reasoning off``)
  when this build supports it, so thinking models cannot burn the whole answer budget;
* after start-up ``/props`` confirms that vision is really loaded and reports the context the server
  actually has, which CTS then uses for its budgets;
* ``stop()`` never waits for a model that is still loading, and a pid file lets the next start remove an
  engine orphaned by a crash;
* a dead engine is restarted by calling ``ensure`` again (the job runner does that on failures).
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ..paths import logs_dir, runtime_dir
from . import deps, models, runtime


class LocalServerError(RuntimeError):
    pass


class _PortBusy(Exception):
    """The chosen port was taken between picking it and the engine binding it."""


def _pid_file() -> Path:
    return runtime_dir() / "llama.pid"


def _process_is_llama(pid: int) -> bool:
    """Is ``pid`` a running llama-server? (never signal a pid that merely got reused by another program)"""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return "llama-server" in out.lower()
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.exists():
            return "llama-server" in cmdline.read_bytes().decode(errors="replace")
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10).stdout
        return "llama-server" in out
    except (OSError, subprocess.SubprocessError):
        return False


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            time.sleep(0.25)
            try:
                os.kill(pid, 0)
            except OSError:
                return
        os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def cleanup_orphan() -> bool:
    """Remove a llama-server left behind by a CTS that was killed. Returns True if one was stopped."""
    try:
        info = json.loads(_pid_file().read_text(encoding="utf-8"))
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    stopped = False
    if pid != os.getpid() and _process_is_llama(pid):
        _kill_pid(pid)
        stopped = True
    _pid_file().unlink(missing_ok=True)
    return stopped


def check_gguf(path: Path, what: str) -> None:
    if not models.is_gguf(path):
        raise LocalServerError(f"The {what} file is damaged or incomplete ({path.name} is not a valid GGUF file). "
                               "Delete the model in Settings > Local models and install it again.")


class LlamaServer:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._key: tuple | None = None
        self._port = 0
        self._info: dict[str, Any] = {}
        self._lock = threading.Lock()  # serialises ensure(); stop() deliberately does NOT take it
        self._abort = threading.Event()
        atexit.register(self.stop)

    # ----------------------------------------------------------------------- state
    @property
    def endpoint(self) -> str | None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return f"http://127.0.0.1:{self._port}/v1"
        return None

    @property
    def slot_context(self) -> int | None:
        """Context window the running server really has (from /props), if known."""
        value = self._info.get("n_ctx")
        return int(value) if value else None

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    # ----------------------------------------------------------------------- start
    def ensure(self, model_id: str, context_window: int, on_status: Callable[[str], None] = lambda _m: None,
               should_cancel: Callable[[], bool] = lambda: False) -> str:
        """Start (or reuse) a server for ``model_id``; returns the OpenAI-compatible endpoint."""
        with self._lock:
            self._abort.clear()
            model = models.get_installed(model_id)
            if model is None:
                raise LocalServerError("No local model is installed/selected. Open Settings > Local models.")
            main, mmproj = models.paths_for(model)
            if not main.is_file():
                raise LocalServerError("The model file is missing. Reinstall it in Settings.")
            if mmproj is None:
                raise LocalServerError("This model has no vision projector (mmproj), so it cannot read comic pages. "
                                       "Pick a vision-capable model.")
            if not mmproj.is_file():
                raise LocalServerError("The model's vision projector (mmproj) file is missing. Reinstall the model "
                                       "in Settings > Local models.")
            check_gguf(main, "model")
            check_gguf(mmproj, "vision projector")
            binary = runtime.find_server()
            if binary is None:
                raise LocalServerError("llama.cpp is not installed yet. Open Settings > Local models and press "
                                       "'Install llama.cpp'. " + runtime.MANUAL_HELP)
            key = (model_id, context_window, str(binary))
            if self.endpoint and self._key == key:
                return self.endpoint
            self._stop_locked()
            cleanup_orphan()

            last_error: Exception | None = None
            for _attempt in range(3):  # a port can be taken between choosing it and the engine binding it
                try:
                    return self._start(binary, main, mmproj, context_window, key, on_status, should_cancel)
                except _PortBusy as busy:
                    last_error = busy
                    self._stop_locked()
            raise LocalServerError(f"Could not find a free port for the local server ({last_error}).")

    def command(self, binary: Path, main: Path, mmproj: Path, context_window: int, port: int) -> list[str]:
        flags = runtime.supported_flags(binary)
        cmd = [str(binary), "-m", str(main), "--mmproj", str(mmproj), "-c", str(context_window), "-np", "1",
               "--host", "127.0.0.1", "--port", str(port), "--jinja"]
        if "--fit" not in flags:
            # Old builds: offload everything. Newer builds adjust the offload to the memory available when
            # -ngl is NOT given - a fixed -ngl 99 would turn off that safety net.
            cmd += ["-ngl", "99"]
        # Comics -> prose does not benefit from "thinking", and a thinking model can spend the whole answer
        # budget on it and return an empty page.
        if "--reasoning" in flags:
            cmd += ["--reasoning", "off"]
        elif "--reasoning-budget" in flags:
            cmd += ["--reasoning-budget", "0"]
        return cmd

    def _start(self, binary: Path, main: Path, mmproj: Path, context_window: int, key: tuple,
               on_status: Callable[[str], None], should_cancel: Callable[[], bool]) -> str:
        self._port = self._free_port()
        cmd = self.command(binary, main, mmproj, context_window, self._port)
        env = runtime.runtime_env(binary)
        log_path = logs_dir() / "llama-server.log"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        on_status("Loading the local model (this can take a minute)...")
        with open(log_path, "wb") as log:
            self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                          cwd=str(Path(binary).parent), creationflags=flags)
        self._key = key
        try:
            _pid_file().write_text(json.dumps({"pid": self._proc.pid, "port": self._port}), encoding="utf-8")
        except OSError:
            pass
        try:
            self._wait_ready(log_path, should_cancel, on_status)
            self._info = self._read_props()
            if self._info.get("vision") is False:
                raise LocalServerError(
                    "The local model started WITHOUT image support, so it cannot read comic pages. Make sure the "
                    "model is a vision model and that its vision projector (mmproj) matches it.")
        except BaseException:
            self._stop_locked()
            raise
        got = self.slot_context
        note = f" ({got:,} tokens of context)" if got else ""
        on_status(f"Local model ready{note}")
        return self.endpoint or ""

    def _wait_ready(self, log_path: Path, should_cancel: Callable[[], bool], on_status: Callable[[str], None],
                    timeout: float = 900.0) -> None:
        started = time.monotonic()
        deadline = started + timeout
        url = f"http://127.0.0.1:{self._port}/health"
        last_tick = 0.0
        # trust_env=False: a corporate HTTP_PROXY must never be used to reach this computer
        with httpx.Client(timeout=3, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self._abort.is_set() or should_cancel():
                    raise LocalServerError("Stopped while the model was loading")
                proc = self._proc
                if proc is None or proc.poll() is not None:
                    if self._abort.is_set():
                        raise LocalServerError("Stopped while the model was loading")
                    tail = log_path.read_text(errors="replace")[-1500:] if log_path.exists() else ""
                    if time.monotonic() - started < 10 and any(
                            m in tail.lower() for m in ("address already in use", "couldn't bind", "failed to bind",
                                                         "cannot bind")):
                        raise _PortBusy(str(self._port))
                    code = proc.returncode if proc is not None else None
                    hint = deps.hint_for(tail, code) or "Is there enough free RAM/VRAM for this model and context size?"
                    raise LocalServerError(f"llama-server stopped while loading the model. {hint}\n{tail}")
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                now = time.monotonic()
                if now - last_tick >= 5:
                    last_tick = now
                    on_status(f"Loading the local model... {int(now - started)}s")
                time.sleep(0.4)
        raise LocalServerError("Timed out waiting for llama-server to load the model (15 minutes)")

    def _read_props(self) -> dict[str, Any]:
        """Ask the running server what it really loaded: vision support and the context per slot."""
        info: dict[str, Any] = {}
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                data = client.get(f"http://127.0.0.1:{self._port}/props").json()
            ctx = (data.get("default_generation_settings") or {}).get("n_ctx")
            if ctx:
                info["n_ctx"] = int(ctx)
            vision = (data.get("modalities") or {}).get("vision")
            if vision is not None:
                info["vision"] = bool(vision)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass  # older builds or a stripped-down server: nothing to learn, carry on
        return info

    # ------------------------------------------------------------------------ stop
    @staticmethod
    def _terminate(proc: subprocess.Popen | None) -> None:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _stop_locked(self) -> None:
        proc, self._proc, self._key, self._info = self._proc, None, None, {}
        self._terminate(proc)
        _pid_file().unlink(missing_ok=True)

    def stop(self) -> None:
        """Stop the engine now - even while ``ensure`` is still waiting for the model to load."""
        self._abort.set()
        self._stop_locked()


llama = LlamaServer()
