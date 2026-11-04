import os
import sys

import pytest

from cts import cli
from cts.local import runtime

PY = sys.executable


def run(*argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    return exc.value.code


def test_doctor_passes_when_python_libraries_are_present(capsys):
    assert run("doctor", "--no-runtime") == 0
    out = capsys.readouterr().out
    assert "python: crewai" in out and "python: pillow" in out and "FAIL" not in out and "Everything looks good" in out


def test_doctor_flags_a_missing_library(monkeypatch, capsys):
    monkeypatch.setattr(cli, "PY_REQUIREMENTS", [*cli.PY_REQUIREMENTS, ("definitely_not_installed_mod", "some-package")])
    assert run("doctor", "--no-runtime") == 1
    out = capsys.readouterr().out
    assert "[FAIL] python: some-package" in out and "Re-run the installer" in out


def test_doctor_fix_installs_missing_python_libraries(monkeypatch, capsys):
    installed = []
    monkeypatch.setattr(cli, "PY_REQUIREMENTS", [("definitely_not_installed_mod", "some-package")])
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: installed.append(cmd))
    assert run("doctor", "--fix", "--no-runtime") == 1  # still missing afterwards (the fake installer did nothing)
    assert installed and installed[0][-1] == "some-package" and "--python" in installed[0] or "pip" in installed[0]


def test_doctor_reports_a_broken_engine_with_the_fix(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "llama-server"
    bad.write_text(f'#!{PY}\nimport sys\nsys.stderr.write("error while loading shared libraries: libgomp.so.1: cannot open shared object file\\n"); sys.exit(127)\n')
    bad.chmod(0o755)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(bad))
    assert run("doctor") == 1
    out = capsys.readouterr().out
    assert "[FAIL] llama.cpp" in out and "libgomp" in out and "cts doctor --fix" in out


def test_doctor_fix_repairs_a_broken_engine(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_install(progress=None, **kw):
        calls.append(kw)
        return tmp_path / "x"

    state = {"ok": False}
    monkeypatch.setattr(runtime, "install_runtime", fake_install)
    monkeypatch.setattr(runtime, "check", lambda: {"installed": True, "ok": state["ok"], "detail": "version: 1", "hint": "h",
                                                   "origin": "cts", "tag": "b1", "flavor": "cpu", "path": ""})
    monkeypatch.setattr(runtime, "verify", lambda p: (True, "version: 1", 0))
    monkeypatch.setattr(runtime, "runtime_info", lambda: {"installed": True, "tag": "b1", "flavor": "cpu"})
    orig = fake_install

    def flip(progress=None, **kw):
        state["ok"] = True
        return orig(progress, **kw)

    monkeypatch.setattr(runtime, "install_runtime", flip)
    assert run("doctor", "--fix") == 0 and calls == [{"update": False, "repair": True, "flavor": None}]


def test_setup_runtime_passes_flags_and_reports_failure(monkeypatch, capsys):
    seen = {}

    def fail(progress=None, **kw):
        seen.update(kw)
        raise RuntimeError("nope")

    monkeypatch.setattr(runtime, "install_runtime", fail)
    assert run("setup-runtime", "--update", "--flavor", "cpu") == 1
    assert seen == {"update": True, "repair": False, "flavor": "cpu"}
    assert "Could not install llama.cpp" in capsys.readouterr().err


def test_update_runs_the_installer_from_the_requested_ref(monkeypatch):
    import httpx

    ran = {}
    monkeypatch.setattr(cli, "cmd_stop", lambda args: 0)

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url):
            ran["url"] = url
            return httpx.Response(200, text="#!/bin/bash\necho updated\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(cli.httpx, "Client", FakeClient)
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, env=None, **kw: ran.update(cmd=cmd, ref=env["CTS_REF"]) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(cli.os, "name", "posix")
    assert run("update", "--ref", "v0.2.0") == 0
    assert ran["url"].endswith("/v0.2.0/install.sh") and ran["ref"] == "v0.2.0" and ran["cmd"][0] == "bash"


def test_update_on_windows_hands_over_to_a_new_powershell(monkeypatch):
    spawned = {}
    monkeypatch.setattr(cli, "cmd_stop", lambda args: 0)
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(cli.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda cmd, **kw: spawned.update(cmd=cmd, kw=kw))
    assert run("update") == 0
    assert spawned["cmd"][0] == "powershell" and "install.ps1 | iex" in spawned["cmd"][-1] and "CTS_REF='main'" in spawned["cmd"][-1]
