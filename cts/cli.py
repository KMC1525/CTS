"""``cts`` command line: launch the app, run the server, manage the install."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import __version__
from .paths import data_dir, logs_dir, state_file, user_prompts_dir


def _running_server() -> dict | None:
    """Return the state of a live CTS server, or None."""
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
        with httpx.Client(timeout=1.5, trust_env=False) as client:
            info = client.get(f"http://127.0.0.1:{state['port']}/api/info").json()
        if info.get("name") == "CTS":
            return state
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        pass
    return None


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    """Start CTS in the background (if it is not running) and open it in the browser."""
    import webbrowser

    state = _running_server()
    if state is None:
        log = open(logs_dir() / "server.log", "ab")
        kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "cts", "serve", "--no-browser"], **kwargs)
        for _ in range(120):  # up to ~30 s: first start imports CrewAI
            time.sleep(0.25)
            state = _running_server()
            if state:
                break
        if state is None:
            print(f"CTS did not start. See {logs_dir() / 'server.log'}", file=sys.stderr)
            return 1
    url = state["url"]
    print(f"CTS is running at {url}")
    if not args.no_browser:
        webbrowser.open(url)
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    state = _running_server()
    if state is None:
        print("CTS is not running.")
        return 0
    with httpx.Client(timeout=5, trust_env=False) as client:
        try:
            client.post(f"http://127.0.0.1:{state['port']}/api/shutdown", headers={"Origin": state["url"]})
        except httpx.HTTPError:
            pass
    print("CTS stopped.")
    return 0


def cmd_setup_runtime(args: argparse.Namespace) -> int:
    from .local import runtime

    def progress(msg: str, done: int, total: int) -> None:
        print(f"\r{msg} {done * 100 // total}%    " if total else f"\r{msg}    ", end="", flush=True)

    try:
        path = runtime.install_runtime(progress, update=args.update, repair=args.repair, flavor=args.flavor)
        info = runtime.runtime_info()
        ok, detail, _ = runtime.verify(path)
        label = f" {info['tag']} ({info['flavor']})" if info.get("tag") else ""
        print(f"\nllama.cpp{label} ready: {path}" + (f"  [{detail}]" if ok and detail else ""))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not install llama.cpp automatically: {exc}", file=sys.stderr)
        print("You can retry later with `cts setup-runtime --repair`, from Settings > Local models, "
              "or use a hosted AI provider.", file=sys.stderr)
        return 1


# Python libraries CTS needs: (import name, pip name)
PY_REQUIREMENTS = [
    ("crewai", "crewai[anthropic,google-genai]"), ("anthropic", "anthropic"), ("google.genai", "google-genai"),
    ("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("PIL", "pillow"), ("docx", "python-docx"),
    ("reportlab", "reportlab"), ("platformdirs", "platformdirs"), ("httpx", "httpx"),
]


def _missing_python_packages() -> list[tuple[str, str]]:
    import importlib.util

    missing = []
    for module, package in PY_REQUIREMENTS:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((module, package))
    return missing


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check every library CTS needs (Python packages, llama.cpp and its system libraries)."""
    from .local import runtime

    problems = 0
    print(f"CTS {__version__} - checking installation")
    missing = _missing_python_packages()
    if missing and args.fix:
        print(f"  installing missing Python libraries: {', '.join(p for _m, p in missing)}")
        uv = shutil.which("uv")
        cmd = [uv, "pip", "install", "--python", sys.executable] if uv else [sys.executable, "-m", "pip", "install"]
        subprocess.run([*cmd, *[p for _m, p in missing]], check=False)
        missing = _missing_python_packages()
    for module, package in PY_REQUIREMENTS:
        bad = any(m == module for m, _p in missing)
        print(f"  [{'FAIL' if bad else ' ok '}] python: {package}")
        problems += bad
    if missing:
        print("      Re-run the installer to repair them:  curl -fsSL https://raw.githubusercontent.com/KMC1525/CTS/main/install.sh | bash")

    if getattr(args, "no_runtime", False):
        print("  [skip] llama.cpp (--no-runtime)")
        print("Everything looks good." if not problems else f"{problems} problem(s) found.")
        return 0 if not problems else 1

    report = runtime.check()
    if not report["installed"] and args.fix:
        print("  installing llama.cpp ...")
        if cmd_setup_runtime(argparse.Namespace(update=False, repair=False, flavor=None)) == 0:
            report = runtime.check()
    elif report["installed"] and not report["ok"] and args.fix:
        print("  llama.cpp does not start - repairing ...")
        if cmd_setup_runtime(argparse.Namespace(update=False, repair=True, flavor=None)) == 0:
            report = runtime.check()
    if report["ok"]:
        label = f"{report['tag']} {report['flavor']}".strip() or report["origin"]
        print(f"  [ ok ] llama.cpp ({label}): {report['detail']}")
    else:
        problems += 1
        print(f"  [FAIL] llama.cpp: {report['detail']}")
        if report.get("hint"):
            print(f"      {report['hint']}")
        if not args.fix:
            print("      Try:  cts doctor --fix")
    print("Everything looks good." if not problems else f"{problems} problem(s) found.")
    return 0 if not problems else 1


UPDATE_URL = "https://raw.githubusercontent.com/KMC1525/CTS/{ref}/install.{ext}"


def cmd_update(args: argparse.Namespace) -> int:
    """Update CTS: re-run the official installer (new code, every Python library, llama.cpp, menu entry)."""
    ref = args.ref or os.environ.get("CTS_REF", "main")
    windows = os.name == "nt"
    url = UPDATE_URL.format(ref=ref, ext="ps1" if windows else "sh")
    cmd_stop(args)
    if windows:
        # cts.exe cannot replace itself while running: hand over to a new PowerShell window and exit.
        command = f"$env:CTS_REF='{ref}'; irm {url} | iex; Read-Host 'Done - press Enter to close'"
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        print("The updater opened in a new window. CTS will be updated in a moment.")
        return 0
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Could not download the installer from {url}: {exc}", file=sys.stderr)
        return 1
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as tmp:
        tmp.write(resp.text)
    try:
        return subprocess.run(["bash", tmp.name], env={**os.environ, "CTS_REF": ref}).returncode
    finally:
        os.unlink(tmp.name)


def cmd_install_desktop(args: argparse.Namespace) -> int:
    from . import desktop

    command = [args.exec] if args.exec else None
    try:
        created = desktop.install(command)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not add CTS to the applications list: {exc}", file=sys.stderr)
        return 1
    for path in created:
        print(f"  created {path}")
    print("CTS now appears in your applications list.")
    return 0


def cmd_uninstall_desktop(_args: argparse.Namespace) -> int:
    from . import desktop

    for path in desktop.uninstall():
        print(f"  removed {path}")
    return 0


def cmd_info(_args: argparse.Namespace) -> int:
    from .local import runtime

    state = _running_server()
    print(f"CTS {__version__}")
    print(f"  data folder   : {data_dir()}")
    print(f"  custom prompts: {user_prompts_dir()}  (copy a prompt from cts/prompts here to override it)")
    print(f"  llama.cpp     : {runtime.find_server() or 'not installed'}")
    print(f"  server        : {state['url'] if state else 'not running'}")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    if not args.yes:
        print(f"This deletes ALL CTS data (settings, models, uploads, novels) in {data_dir()}. Re-run with --yes.")
        return 1
    shutil.rmtree(data_dir(), ignore_errors=True)
    print("CTS data deleted.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cts", description="CTS - Comic To Story")
    parser.add_argument("--version", action="version", version=f"CTS {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("launch", help="start CTS in the background and open it in your browser (default)")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("serve", help="run the server in this terminal")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("stop", help="stop the background server").set_defaults(func=cmd_stop)
    p = sub.add_parser("setup-runtime", help="install / update the llama.cpp engine for local models")
    p.add_argument("--update", action="store_true", help="move to the newest llama.cpp release")
    p.add_argument("--repair", action="store_true", help="reinstall even if an engine is present")
    p.add_argument("--flavor", help="force a build: cpu, vulkan, cuda ... (default: automatic)")
    p.set_defaults(func=cmd_setup_runtime)
    p = sub.add_parser("doctor", help="check that every library CTS needs is installed and working")
    p.add_argument("--fix", action="store_true", help="install/repair what is missing")
    p.add_argument("--no-runtime", action="store_true", help="do not check/install llama.cpp")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("update", help="update CTS and all its libraries (re-runs the installer)")
    p.add_argument("--ref", help="branch or tag to install (default: main)")
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("install-desktop", help="add CTS to your applications list")
    p.add_argument("--exec", help="path of the launcher to register")
    p.set_defaults(func=cmd_install_desktop)
    sub.add_parser("uninstall-desktop", help="remove CTS from your applications list").set_defaults(func=cmd_uninstall_desktop)
    sub.add_parser("info", help="show where CTS keeps its files").set_defaults(func=cmd_info)
    p = sub.add_parser("purge", help="delete all CTS data (settings, models, novels)")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):  # plain `cts` = launch
        args = build_parser().parse_args(["launch"])
    sys.exit(args.func(args))


def main_gui() -> None:
    """Windowless entry point (Windows ``cts-gui.exe``): there is no console, so log to a file."""
    if sys.stdout is None or sys.stderr is None:
        log = open(logs_dir() / "gui.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    main()
