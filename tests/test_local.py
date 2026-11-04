import json
import stat
import sys

import httpx
import pytest

from cts.local import catalog, download, models, runtime
from cts.local.catalog import RepoFile

# Real listings (trimmed) from huggingface.co - the naming patterns CTS must understand.
GEMMA_4B = [
    RepoFile("README.md", 21856), RepoFile("gemma-3-4b-it-Q4_K_M.gguf", 2489757856),
    RepoFile("gemma-3-4b-it-Q8_0.gguf", 4130226336), RepoFile("gemma-3-4b-it-f16.gguf", 7767474336),
    RepoFile("mmproj-model-f16.gguf", 851251104),
]
QWEN3_VL = [
    RepoFile("Qwen3-VL-8B-Instruct-Q4_K_M.gguf", 5027785568), RepoFile("Qwen3-VL-8B-Instruct-Q8_0.gguf", 8709520224),
    RepoFile("Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf", 5148699488), RepoFile("Qwen3-VL-8B-Instruct-IQ4_XS.gguf", 4581288800),
    RepoFile("Qwen3-VL-8B-Instruct-BF16.gguf", 16388045408), RepoFile("imatrix_unsloth.gguf_file", 5316790),
    RepoFile("mmproj-BF16.gguf", 1162569280), RepoFile("mmproj-F16.gguf", 1159030336), RepoFile("mmproj-F32.gguf", 2305574464),
]


def test_quantizations_are_discovered_and_sorted():
    info = catalog.describe_repo(GEMMA_4B)
    assert [q["label"] for q in info["quants"]] == ["Q4_K_M", "Q8_0", "F16"]
    assert info["recommended"] == "Q4_K_M" and info["vision"]
    assert info["default_mmproj"] == "mmproj-model-f16.gguf"
    assert info["quants"][0]["size"] == 2489757856 and "balanced" in info["quants"][0]["hint"]


def test_unsloth_style_names_and_projector_choice():
    info = catalog.describe_repo(QWEN3_VL)
    labels = [q["label"] for q in info["quants"]]
    assert {"Q4_K_M", "IQ4_XS", "UD-Q4_K_XL", "Q8_0", "BF16"} <= set(labels)
    assert not any("imatrix" in f for q in info["quants"] for f in q["files"])
    assert info["default_mmproj"] == "mmproj-F16.gguf"  # f16, not bf16/f32


def test_split_shards_are_grouped():
    files = [RepoFile(f"BF16/Model-BF16-0000{i}-of-00003.gguf", 10) for i in (1, 2, 3)] + [RepoFile("mmproj-F16.gguf", 5)]
    [q] = catalog.describe_repo(files)["quants"]
    assert q["label"] == "BF16" and len(q["files"]) == 3 and q["size"] == 30


def test_repo_without_projector_is_flagged():
    info = catalog.describe_repo([RepoFile("m-Q4_K_M.gguf", 1)])
    assert info["vision"] is False and info["default_mmproj"] is None


def test_normalise_repo():
    assert catalog.normalise_repo("https://huggingface.co/ggml-org/gemma-3-4b-it-GGUF/tree/main") == "ggml-org/gemma-3-4b-it-GGUF"
    assert catalog.normalise_repo(" a/b ") == "a/b"
    with pytest.raises(ValueError):
        catalog.normalise_repo("not a repo")


def test_list_repo_files_uses_lfs_size_and_reports_gated():
    def handler(request):
        if "gated" in str(request.url):
            return httpx.Response(401)
        return httpx.Response(200, json=[{"type": "file", "path": "a.gguf", "size": 1, "lfs": {"size": 99}},
                                         {"type": "directory", "path": "x"}])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert catalog.list_repo_files("o/n", client=client) == [RepoFile("a.gguf", 99)]
    with pytest.raises(ValueError, match="token"):
        catalog.list_repo_files("o/gated", client=client)


# --------------------------------------------------------------------------- downloads
def test_download_resumes_a_partial_file(tmp_path):
    payload = bytes(range(256)) * 40
    seen = []

    def handler(request):
        rng = request.headers.get("range")
        seen.append(rng)
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return httpx.Response(206, content=payload[start:])
        return httpx.Response(200, content=payload)

    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(payload[:1000])
    progress = []
    download.download_file("http://x/m", dest, expected_size=len(payload), on_progress=progress.append,
                           client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert dest.read_bytes() == payload and seen == ["bytes=1000-"] and progress[-1] == len(payload)
    assert not (tmp_path / "m.gguf.part").exists()


def test_download_cancel_keeps_part_file(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 10)))
    with pytest.raises(download.DownloadCancelled):
        download.download_file("http://x/m", tmp_path / "m", is_cancelled=lambda: True, client=client)
    assert (tmp_path / "m.part").exists()


def test_download_detects_truncation(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"abc")))
    with pytest.raises(RuntimeError, match="incomplete"):
        download.download_file("http://x/m", tmp_path / "m", expected_size=10, client=client)


# ---------------------------------------------------------------- the server lifecycle
FAKE_SERVER = """#!{python}
import sys, http.server
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{{"status":"ok"}}')
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def install_fake_model(cts_home):
    mdir = cts_home / "models" / "o__m"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "m-Q4_K_M.gguf").write_bytes(b"GGUF\x03\x00\x00\x00")
    (mdir / "mmproj-F16.gguf").write_bytes(b"GGUF\x03\x00\x00\x00")
    entry = {"id": "o__m__Q4_K_M", "repo": "o/m", "quant": "Q4_K_M", "label": "m - Q4_K_M", "files": ["o__m/m-Q4_K_M.gguf"],
             "mmproj": "o__m/mmproj-F16.gguf", "size": 8, "installed_at": 1}
    (cts_home / "models" / "registry.json").write_text(json.dumps({entry["id"]: entry}))
    return entry


def test_llama_server_starts_waits_for_health_and_stops(cts_home, tmp_path, monkeypatch):
    from cts.local.server import LlamaServer, LocalServerError

    install_fake_model(cts_home)
    binary = tmp_path / "llama-server"
    binary.write_text(FAKE_SERVER.format(python=sys.executable))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(binary))
    srv = LlamaServer()
    try:
        endpoint = srv.ensure("o__m__Q4_K_M", 8192)
        assert endpoint.startswith("http://127.0.0.1:") and httpx.get(endpoint[:-3] + "/health", trust_env=False).status_code == 200
        assert srv.ensure("o__m__Q4_K_M", 8192) == endpoint  # same config: reused, not restarted
    finally:
        srv.stop()
    assert srv.endpoint is None
    with pytest.raises(LocalServerError, match="No local model"):
        srv.ensure("missing", 8192)


def test_llama_server_requires_vision_projector(cts_home):
    from cts.local.server import LlamaServer, LocalServerError

    entry = install_fake_model(cts_home)
    entry["mmproj"] = None
    (cts_home / "models" / "registry.json").write_text(json.dumps({entry["id"]: entry}))
    with pytest.raises(LocalServerError, match="vision"):
        LlamaServer().ensure(entry["id"], 8192)


def test_delete_model_removes_files_and_selection(cts_home):
    from cts import settings

    entry = install_fake_model(cts_home)
    settings.update({"local": {"active_model": entry["id"]}})
    assert models.delete(entry["id"])
    assert not (cts_home / "models" / "o__m" / "m-Q4_K_M.gguf").exists()
    assert settings.load().local.active_model == ""


def test_a_server_that_dies_at_startup_explains_the_missing_library(cts_home, tmp_path, monkeypatch):
    from cts.local.server import LlamaServer, LocalServerError

    install_fake_model(cts_home)
    binary = tmp_path / "llama-server"
    binary.write_text(f"#!{sys.executable}\nimport sys\n"
                      "sys.stderr.write('llama-server: error while loading shared libraries: libgomp.so.1: cannot open shared object file\\n')\n"
                      "sys.exit(127)\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(binary))
    with pytest.raises(LocalServerError, match="libgomp"):
        LlamaServer().ensure("o__m__Q4_K_M", 8192)


# ============================== v0.3: the connection between CTS and the local engine ==============================
import threading
import time


def make_binary(tmp_path, body: str, name="llama-server"):
    binary = tmp_path / name
    binary.write_text(f"#!{sys.executable}\n{body}")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


SERVER_BODY = '''
import sys, json, http.server
if "--help" in sys.argv:
    sys.stdout.write(HELP); sys.exit(0)
port = int(sys.argv[sys.argv.index("--port") + 1])
open(LOG, "a").write(json.dumps(sys.argv[1:]) + "\\n")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PROPS if self.path == "/props" else '{"status":"ok"}'
        self.send_response(200); self.end_headers(); self.wfile.write(body.encode())
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def server_binary(tmp_path, help_text="", props='{"modalities": {"vision": true}, "default_generation_settings": {"n_ctx": 8192}}'):
    log = tmp_path / "argv.log"
    body = f"HELP = {help_text!r}\nPROPS = {props!r}\nLOG = {str(log)!r}\n" + SERVER_BODY
    return make_binary(tmp_path, body), log


def test_command_line_adapts_to_what_the_build_supports(tmp_path):
    from cts.local.server import LlamaServer

    new, _ = server_binary(tmp_path, "  -fit, --fit [on|off]\n  --reasoning [on|off|auto]\n  -ngl N\n")
    cmd = LlamaServer().command(new, Path("m.gguf"), Path("p.gguf"), 16384, 9999)
    assert "-ngl" not in cmd  # an explicit -ngl would switch off llama.cpp's automatic fit to memory
    assert cmd[cmd.index("--reasoning") + 1] == "off" and cmd[cmd.index("-c") + 1] == "16384"
    assert {"-np", "--jinja", "--mmproj", "--host"} <= set(cmd) and cmd[cmd.index("--host") + 1] == "127.0.0.1"

    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old, _ = server_binary(old_dir, "  -ngl N\n  --reasoning-budget N\n")
    cmd = LlamaServer().command(old, Path("m.gguf"), Path("p.gguf"), 8192, 9999)
    assert cmd[cmd.index("-ngl") + 1] == "99" and "--fit" not in cmd and "--reasoning" not in cmd
    assert cmd[cmd.index("--reasoning-budget") + 1] == "0"

    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    bare, _ = server_binary(bare_dir, "")
    cmd = LlamaServer().command(bare, Path("m.gguf"), Path("p.gguf"), 8192, 9999)
    assert "-ngl" in cmd and "--reasoning" not in cmd and "--reasoning-budget" not in cmd  # unknown flags are never passed


from pathlib import Path  # noqa: E402


def test_started_server_reports_its_real_context_and_the_flags_reach_the_engine(cts_home, tmp_path, monkeypatch):
    from cts.local.server import LlamaServer

    install_fake_model(cts_home)
    binary, log = server_binary(tmp_path, "--fit --reasoning", props='{"modalities": {"vision": true}, "default_generation_settings": {"n_ctx": 12288}}')
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(binary))
    srv = LlamaServer()
    try:
        srv.ensure("o__m__Q4_K_M", 16384)
        assert srv.slot_context == 12288  # what the server really has, not what was asked for
        argv = json.loads(log.read_text().splitlines()[-1])
        assert argv[argv.index("--reasoning") + 1] == "off" and "-ngl" not in argv
        assert (cts_home / "runtime" / "llama.pid").exists()
    finally:
        srv.stop()
    assert not (cts_home / "runtime" / "llama.pid").exists() and srv.slot_context is None


def test_a_model_loaded_without_vision_is_rejected_at_start(cts_home, tmp_path, monkeypatch):
    from cts.local.server import LlamaServer, LocalServerError

    install_fake_model(cts_home)
    binary, _ = server_binary(tmp_path, props='{"modalities": {"vision": false}, "default_generation_settings": {"n_ctx": 8192}}')
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(binary))
    srv = LlamaServer()
    with pytest.raises(LocalServerError, match="WITHOUT image support"):
        srv.ensure("o__m__Q4_K_M", 8192)
    assert srv.endpoint is None  # and it was shut down again


def test_damaged_model_files_are_caught_before_starting(cts_home, tmp_path, monkeypatch):
    from cts.local.server import LlamaServer, LocalServerError

    install_fake_model(cts_home)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(server_binary(tmp_path)[0]))
    (cts_home / "models" / "o__m" / "m-Q4_K_M.gguf").write_text("<html>429 Too Many Requests</html>")
    with pytest.raises(LocalServerError, match="damaged or incomplete"):
        LlamaServer().ensure("o__m__Q4_K_M", 8192)
    install_fake_model(cts_home)
    (cts_home / "models" / "o__m" / "mmproj-F16.gguf").unlink()
    with pytest.raises(LocalServerError, match="projector"):
        LlamaServer().ensure("o__m__Q4_K_M", 8192)


def test_health_checks_ignore_http_proxies(cts_home, tmp_path, monkeypatch):
    """Reproduces: on a network with HTTP_PROXY (and no NO_PROXY) the engine on 127.0.0.1 looked dead."""
    from cts.local.server import LlamaServer

    install_fake_model(cts_home)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(server_binary(tmp_path)[0]))
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # nothing listens there
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    srv = LlamaServer()
    outcome = {}

    def start():
        try:
            outcome["endpoint"] = srv.ensure("o__m__Q4_K_M", 8192)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    thread.join(timeout=40)  # without the fix the health check never sees the engine and this never finishes
    srv.stop()  # also unblocks a start that is stuck
    thread.join(timeout=10)
    assert outcome.get("endpoint", "").startswith("http://127.0.0.1:"), outcome


def test_stop_does_not_wait_for_a_model_that_is_still_loading(cts_home, tmp_path, monkeypatch):
    """Reproduces: Quit / delete-model blocked for up to 15 minutes while the engine was loading."""
    from cts.local.server import LlamaServer, LocalServerError

    install_fake_model(cts_home)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(make_binary(
        tmp_path, "import sys, time\nif '--help' in sys.argv: sys.exit(0)\ntime.sleep(300)\n")))  # never becomes healthy
    srv = LlamaServer()
    outcome = {}

    def start():
        try:
            srv.ensure("o__m__Q4_K_M", 8192)
        except LocalServerError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    time.sleep(1.5)
    began = time.monotonic()
    srv.stop()
    assert time.monotonic() - began < 5, "stop() must not wait for the load to finish"
    thread.join(timeout=10)
    assert not thread.is_alive() and "Stopped while the model was loading" in outcome["error"]
    assert srv.endpoint is None


def test_a_port_taken_at_the_last_moment_is_retried_with_another(cts_home, tmp_path, monkeypatch):
    from cts.local.server import LlamaServer

    install_fake_model(cts_home)
    counter = tmp_path / "tries"
    body = f"""
import sys, os, http.server
if "--help" in sys.argv: sys.exit(0)
c = {str(counter)!r}
n = int(open(c).read()) if os.path.exists(c) else 0
open(c, "w").write(str(n + 1))
if n == 0:
    sys.stderr.write("couldn't bind HTTP server socket, address already in use\\n"); sys.exit(1)
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{{}}")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(make_binary(tmp_path, body)))
    srv = LlamaServer()
    try:
        srv.ensure("o__m__Q4_K_M", 8192)
        assert int(counter.read_text()) == 2
    finally:
        srv.stop()


def test_a_leftover_engine_from_a_crashed_cts_is_removed_but_other_programs_are_not(cts_home, tmp_path):
    import subprocess

    from cts.local import server as srvmod

    leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "llama-server"])
    innocent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (cts_home / "runtime").mkdir(parents=True, exist_ok=True)
        pid_file = cts_home / "runtime" / "llama.pid"
        pid_file.write_text(json.dumps({"pid": leftover.pid, "port": 1}))
        assert srvmod.cleanup_orphan() is True
        leftover.wait(timeout=10)
        assert leftover.poll() is not None and not pid_file.exists()
        pid_file.write_text(json.dumps({"pid": innocent.pid, "port": 1}))  # a reused pid: some other program
        assert srvmod.cleanup_orphan() is False and innocent.poll() is None and not pid_file.exists()
        assert srvmod.cleanup_orphan() is False  # nothing recorded
    finally:
        for proc in (leftover, innocent):
            proc.kill()


def test_downloaded_files_must_be_real_gguf(tmp_path):
    good, bad = tmp_path / "a.gguf", tmp_path / "b.gguf"
    good.write_bytes(b"GGUF\x03\x00")
    bad.write_text("<html>error</html>")
    assert models.is_gguf(good) and not models.is_gguf(bad) and not models.is_gguf(tmp_path / "missing")
