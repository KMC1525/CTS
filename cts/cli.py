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


def cmd_setup_runtime(_args: argparse.Namespace) -> int:
    from .local import runtime

    try:
        path = runtime.install_runtime(lambda msg, done, total: print(
            f"\r{msg} {done * 100 // total}%" if total else f"\r{msg}", end="", flush=True))
        print(f"\nllama.cpp ready: {path}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not install llama.cpp automatically: {exc}", file=sys.stderr)
        print("You can retry later from Settings > Local models, or use a hosted AI provider.", file=sys.stderr)
        return 1


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
    sub.add_parser("setup-runtime", help="download the llama.cpp engine for local models").set_defaults(func=cmd_setup_runtime)
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
