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
    mdir.mkdir(parents=True)
    (mdir / "m-Q4_K_M.gguf").write_bytes(b"gguf")
    (mdir / "mmproj-F16.gguf").write_bytes(b"gguf")
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
