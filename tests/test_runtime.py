"""llama.cpp engine installer: asset selection (real release names), verification, fallback, update."""
import io
import json
import os
import stat
import sys
import tarfile
import zipfile

import httpx
import pytest

from cts.local import deps, runtime

# The conftest fixture replaces these with fakes in every test; keep the real ones for the tests of deps itself.
REAL_INSTALL_PACKAGES = deps.install_system_packages
REAL_INSTALL_VC_REDIST = deps.install_vc_redist

PY = sys.executable
OK_SCRIPT = f'#!{PY}\nimport sys\nif "--version" in sys.argv:\n    sys.stderr.write("version: {{v}} (abc1234)\\n"); sys.exit(0)\nsys.exit(0)\n'
LIB_ERROR = ('error while loading shared libraries: libvulkan.so.1: cannot open shared object file: '
             'No such file or directory')
BAD_SCRIPT = f'#!{PY}\nimport sys\nsys.stderr.write("llama-server: {LIB_ERROR}\\n"); sys.exit(127)\n'

# Names copied from llama.cpp's release.yml / release-publish.yml (note: cudart on Windows has no tag).
REAL_NAMES = [
    "llama-b9000-bin-macos-arm64.tar.gz", "llama-b9000-bin-macos-x64.tar.gz", "llama-b9000-xcframework.zip",
    "llama-b9000-bin-ubuntu-x64.tar.gz", "llama-b9000-bin-ubuntu-arm64.tar.gz", "llama-b9000-bin-ubuntu-s390x.tar.gz",
    "llama-b9000-bin-ubuntu-26.04-s390x.tar.gz", "llama-b9000-bin-ubuntu-zdnn-s390x.tar.gz",
    "llama-b9000-bin-ubuntu-vulkan-x64.tar.gz", "llama-b9000-bin-ubuntu-vulkan-arm64.tar.gz",
    "llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz", "cudart-llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "llama-b9000-bin-ubuntu-rocm-10.0-x64.tar.gz", "llama-b9000-bin-ubuntu-openvino-2026.4.1-x64.tar.gz",
    "llama-b9000-bin-ubuntu-sycl-fp16-x64.tar.gz", "llama-b9000-bin-linux-arm64-snapdragon.tar.gz",
    "llama-b9000-bin-android-arm64.tar.gz", "llama-b9000-ui.tar.gz",
    "llama-b9000-bin-win-cpu-x64.zip", "llama-b9000-bin-win-cpu-arm64.zip", "llama-b9000-bin-win-vulkan-x64.zip",
    "llama-b9000-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "llama-b9000-bin-win-opencl-adreno-arm64.zip", "llama-b9000-bin-win-openvino-2026.4.1-x64.zip",
]
ASSETS = [{"name": n, "size": 1, "browser_download_url": f"https://dl.test/{n}"} for n in REAL_NAMES]


def names(assets):
    return [a["name"] for a in assets]


# ------------------------------------------------------------------------ choosing assets
@pytest.mark.parametrize("system,arch,flavor,expected", [
    ("linux", "x64", "", "llama-b9000-bin-ubuntu-vulkan-x64.tar.gz"),
    ("linux", "x64", "cpu", "llama-b9000-bin-ubuntu-x64.tar.gz"),
    ("linux", "x64", "cuda", "llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz"),
    ("linux", "arm64", "", "llama-b9000-bin-ubuntu-vulkan-arm64.tar.gz"),
    ("macos", "arm64", "", "llama-b9000-bin-macos-arm64.tar.gz"),
    ("macos", "x64", "", "llama-b9000-bin-macos-x64.tar.gz"),
    ("windows", "x64", "", "llama-b9000-bin-win-vulkan-x64.zip"),
    ("windows", "x64", "cuda", "llama-b9000-bin-win-cuda-12.4-x64.zip"),
    ("windows", "x64", "cpu", "llama-b9000-bin-win-cpu-x64.zip"),
    ("windows", "arm64", "", "llama-b9000-bin-win-cpu-arm64.zip"),
])
def test_pick_asset_with_real_release_names(system, arch, flavor, expected):
    assert runtime.pick_asset(ASSETS, system, arch, flavor)["name"] == expected


def test_exotic_and_foreign_builds_are_never_chosen():
    for system, arch in (("linux", "x64"), ("linux", "arm64"), ("macos", "arm64"), ("windows", "x64"), ("windows", "arm64")):
        chosen = names(runtime.rank_assets(ASSETS, system, arch))
        assert not any(bad in n for n in chosen for bad in ("s390x", "snapdragon", "android", "ui.tar", "xcframework",
                                                              "cudart", "openvino", "sycl", "rocm", "adreno"))
        assert chosen, (system, arch)


def test_cpu_build_is_always_the_last_resort():
    assert names(runtime.rank_assets(ASSETS, "linux", "x64")) == [
        "llama-b9000-bin-ubuntu-vulkan-x64.tar.gz", "llama-b9000-bin-ubuntu-x64.tar.gz"]
    assert names(runtime.rank_assets(ASSETS, "windows", "x64", "cuda"))[-1] == "llama-b9000-bin-win-cpu-x64.zip"
    assert runtime.pick_asset([{"name": "llama-b1-bin-macos-arm64.tar.gz"}], "linux", "x64") is None


def test_cuda_builds_come_with_their_cudart_archive():
    win = next(a for a in ASSETS if a["name"].endswith("win-cuda-12.4-x64.zip"))
    assert names(runtime.companion_assets(ASSETS, win)) == ["cudart-llama-bin-win-cuda-12.4-x64.zip"]
    lin = next(a for a in ASSETS if "ubuntu-cuda-12.8-x64" in a["name"] and a["name"].startswith("llama-"))
    assert names(runtime.companion_assets(ASSETS, lin)) == ["cudart-llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz"]
    assert runtime.companion_assets(ASSETS, ASSETS[0]) == []


@pytest.mark.parametrize("system,arch", [("linux", "x64"), ("linux", "arm64"), ("macos", "arm64"), ("macos", "x64"),
                                         ("windows", "x64"), ("windows", "arm64")])
def test_synthesized_names_match_the_real_release_names(system, arch):
    """When the GitHub API is unavailable, names derived from the naming scheme must exist in a real release."""
    real = set(REAL_NAMES)
    synthetic = runtime.synthesize_assets("b9000", system, arch)
    assert synthetic and all(a["name"] in real for a in synthetic), names(synthetic)
    assert all(a["browser_download_url"].startswith("https://github.com/ggml-org/llama.cpp/releases/download/b9000/")
               for a in synthetic)
    assert runtime.rank_assets(synthetic, system, arch)  # and they pass our own filter


# ------------------------------------------------------------------------ release lookup
def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_latest_release_from_api():
    c = client_for(lambda r: httpx.Response(200, json={"tag_name": "b9000", "assets": ASSETS}))
    rel = runtime.latest_release(c)
    assert rel["tag_name"] == "b9000" and rel["source"] == "api" and len(rel["assets"]) == len(ASSETS)


def test_latest_release_falls_back_to_the_website_when_the_api_is_rate_limited():
    def handler(request):
        if "api.github.com" in str(request.url):
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        return httpx.Response(302, headers={"location": "https://github.com/ggml-org/llama.cpp/releases/tag/b9123"})

    rel = runtime.latest_release(client_for(handler))
    assert (rel["tag_name"], rel["source"], rel["assets"]) == ("b9123", "redirect", [])


def test_latest_release_reports_when_everything_fails():
    with pytest.raises(RuntimeError, match="Could not reach GitHub"):
        runtime.latest_release(client_for(lambda r: httpx.Response(500)))


def test_github_token_is_sent_when_available(monkeypatch):
    seen = {}
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"tag_name": "b1", "assets": []})

    runtime.latest_release(client_for(handler))
    assert seen["auth"] == "Bearer tok"


# ------------------------------------------------------------------------------ installing
def tar_gz(files: dict[str, tuple[str, int]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, (content, mode) in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    """Serves a release whose Vulkan build is broken and whose CPU build works."""

    def __init__(self, tag="b9000", vulkan_ok=False, cpu_ok=True, api_ok=True):
        self.tag, self.api_ok, self.requests = tag, api_ok, []
        good, bad = OK_SCRIPT.format(v=tag.lstrip("b")), BAD_SCRIPT
        self.files = {
            f"llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz": tar_gz({f"llama-{tag}/llama-server": (good if vulkan_ok else bad, 0o755),
                                                                 f"llama-{tag}/libggml.so": ("lib", 0o644)}),
            f"llama-{tag}-bin-ubuntu-x64.tar.gz": tar_gz({f"llama-{tag}/llama-server": (good if cpu_ok else bad, 0o755)}),
        }

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True)

    def handle(self, request):
        url = str(request.url)
        self.requests.append(url)
        if "api.github.com" in url:
            if not self.api_ok:
                return httpx.Response(403, json={"message": "rate limited"})
            assets = [{"name": n, "size": len(b), "browser_download_url": f"https://dl.test/{n}"} for n, b in self.files.items()]
            return httpx.Response(200, json={"tag_name": self.tag, "assets": assets})
        if url.endswith("/releases/latest"):
            return httpx.Response(302, headers={"location": f"https://github.com/ggml-org/llama.cpp/releases/tag/{self.tag}"})
        name = url.rsplit("/", 1)[-1].split("?")[0]
        if name in self.files:
            return httpx.Response(200, content=self.files[name])
        return httpx.Response(404)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(runtime, "system_arch", lambda: ("linux", "x64"))
    monkeypatch.delenv("CTS_LLAMA_FLAVOR", raising=False)


def test_broken_gpu_build_falls_back_to_cpu_and_state_is_recorded(linux, cts_home, no_system_changes):
    gh = FakeGitHub(vulkan_ok=False)
    progress = []
    server = runtime.install_runtime(lambda m, d, t: progress.append(m), client=gh.client())
    info = runtime.managed_info()
    assert info["tag"] == "b9000" and info["flavor"] == "cpu" and server.is_file() and os.access(server, os.X_OK)
    assert runtime.runtime_info()["origin"] == "cts" and runtime.find_server() == server
    assert any("trying another" in m for m in progress)
    assert ("pkgs", ["libvulkan.so.1"]) in no_system_changes  # it tried to install the missing system library first
    assert runtime.check()["ok"]
    # nothing half-installed is left behind
    assert not list((cts_home / "runtime").glob("*.tmp")) and not (cts_home / "runtime" / "downloads").exists()


def test_missing_system_library_is_repaired_then_gpu_build_is_kept(linux, cts_home, monkeypatch):
    gh = FakeGitHub(vulkan_ok=False)
    good = OK_SCRIPT.format(v="9000")

    def fix(detail, code=None, binary=None, runner=None):
        assert "libvulkan.so.1" in detail
        open(binary, "w").write(good)  # as if `apt install libvulkan1` made the binary start
        return True, "Installed system libraries: libvulkan1"

    monkeypatch.setattr(deps, "try_fix", fix)
    runtime.install_runtime(client=gh.client())
    assert runtime.managed_info()["flavor"] == "vulkan"


def test_all_builds_failing_gives_every_reason_and_changes_nothing(linux, cts_home):
    gh = FakeGitHub(vulkan_ok=False, cpu_ok=False)
    with pytest.raises(RuntimeError) as exc:
        runtime.install_runtime(client=gh.client())
    text = str(exc.value)
    assert "ubuntu-vulkan-x64" in text and "ubuntu-x64" in text and "libvulkan.so.1" in text and "brew install llama.cpp" in text
    assert runtime.managed_info() is None and runtime.find_server() is None
    assert not list((cts_home / "runtime").glob("*.tmp"))


def test_api_blocked_still_installs_using_the_redirect_and_known_names(linux, cts_home):
    gh = FakeGitHub(vulkan_ok=True, api_ok=False, tag="b9100")
    server = runtime.install_runtime(client=gh.client())
    assert runtime.managed_info()["tag"] == "b9100" and runtime.managed_info()["flavor"] == "vulkan" and server.is_file()
    assert any("releases/download/b9100/llama-b9100-bin-ubuntu-vulkan-x64.tar.gz" in u for u in gh.requests)


def test_corrupt_download_is_discarded_and_next_build_is_tried(linux, cts_home):
    gh = FakeGitHub(vulkan_ok=True)
    gh.files["llama-b9000-bin-ubuntu-vulkan-x64.tar.gz"] = b"this is not a tarball"
    runtime.install_runtime(client=gh.client())
    assert runtime.managed_info()["flavor"] == "cpu"
    downloads = cts_home / "runtime" / "downloads"
    assert not downloads.exists() or not list(downloads.glob("*vulkan*"))


def test_flavor_override_forces_the_cpu_build(linux, cts_home, monkeypatch):
    monkeypatch.setenv("CTS_LLAMA_FLAVOR", "cpu")
    gh = FakeGitHub(vulkan_ok=True)
    runtime.install_runtime(client=gh.client())
    assert runtime.managed_info()["flavor"] == "cpu"
    assert not any("vulkan" in u for u in gh.requests)


# ----------------------------------------------------------------------------- updating
def install_version(tag, cts_home, linux_patch=None):
    gh = FakeGitHub(tag=tag, vulkan_ok=True)
    return gh, runtime.install_runtime(client=gh.client(), repair=True)


def test_default_install_keeps_a_working_engine_without_touching_the_network(linux, cts_home):
    _gh, server = install_version("b9000", cts_home)

    def boom(request):
        raise AssertionError("no network expected")

    assert runtime.install_runtime(client=client_for(boom)) == server


def test_update_installs_the_newer_release_and_removes_the_old_one(linux, cts_home):
    _gh, old = install_version("b9000", cts_home)
    gh2 = FakeGitHub(tag="b9100", vulkan_ok=True)
    new = runtime.install_runtime(update=True, client=gh2.client())
    assert runtime.managed_info()["tag"] == "b9100" and new.is_file() and not old.exists()
    assert "9100" in runtime.check()["detail"]
    assert sorted(p.name for p in (cts_home / "runtime").iterdir()) == ["b9100-vulkan", "current.json"]


def test_update_is_a_no_op_when_already_on_the_newest_release(linux, cts_home):
    _gh, server = install_version("b9000", cts_home)
    gh = FakeGitHub(tag="b9000", vulkan_ok=True)
    assert runtime.install_runtime(update=True, client=gh.client()) == server
    assert not any(u.endswith(".tar.gz") for u in gh.requests)  # asked GitHub for the tag, downloaded nothing


def test_update_never_downgrades(linux, cts_home):
    _gh, server = install_version("b9100", cts_home)
    gh = FakeGitHub(tag="b9000", vulkan_ok=True)
    assert runtime.install_runtime(update=True, client=gh.client()) == server and runtime.managed_info()["tag"] == "b9100"


def test_repair_reinstalls_a_broken_engine(linux, cts_home):
    _gh, server = install_version("b9000", cts_home)
    server.write_text(BAD_SCRIPT)
    broken = runtime.check()
    assert broken["ok"] is False and "libvulkan.so.1" in broken["hint"]
    fixed = runtime.install_runtime(client=FakeGitHub(vulkan_ok=True).client())  # broken => reinstalled even w/o --repair
    assert runtime.check()["ok"] and fixed.is_file()


def test_engine_installed_by_cts_0_1_is_updated(linux, cts_home):
    legacy = cts_home / "runtime" / "b6000" / "llama-b6000"
    legacy.mkdir(parents=True)
    (legacy / "llama-server").write_text(OK_SCRIPT.format(v="6000"))
    (legacy / "llama-server").chmod(0o755)
    assert runtime.runtime_info()["origin"] == "cts" and runtime.find_server() == legacy / "llama-server"
    gh = FakeGitHub(tag="b9000", vulkan_ok=True)
    runtime.install_runtime(update=True, client=gh.client())
    assert runtime.managed_info()["tag"] == "b9000" and not (cts_home / "runtime" / "b6000").exists()


def test_an_engine_the_user_installed_is_used_and_never_replaced(linux, cts_home, tmp_path, monkeypatch):
    mine = tmp_path / "bin" / "llama-server"
    mine.parent.mkdir()
    mine.write_text(OK_SCRIPT.format(v="123"))
    mine.chmod(0o755)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(mine))

    def boom(request):
        raise AssertionError("no network expected")

    for flags in ({}, {"update": True}):
        assert runtime.install_runtime(client=client_for(boom), **flags) == mine
    assert runtime.runtime_info()["origin"] == "env"


def test_install_survives_a_symlinked_data_folder(linux, tmp_path, monkeypatch):
    """macOS-style /var -> /private/var: cleanup must not delete the engine it just installed."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("CTS_HOME", str(link))
    server = runtime.install_runtime(client=FakeGitHub(vulkan_ok=True).client())
    assert server.is_file() and runtime.check()["ok"]
    runtime.install_runtime(update=True, client=FakeGitHub(tag="b9200", vulkan_ok=True).client())
    assert runtime.check()["ok"] and "9200" in runtime.check()["detail"]


# ---------------------------------------------------------------------------- unpacking
def test_zip_without_unix_modes_gets_executable_bits(cts_home, tmp_path):
    archive = tmp_path / "llama-b9000-bin-win-cpu-x64.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("llama-server", OK_SCRIPT.format(v="9000"))
        zf.writestr("ggml-cpu.dll", "x")
    dest = tmp_path / "out"
    runtime._extract(archive, dest)
    runtime._make_executable(dest)
    assert os.access(dest / "llama-server", os.X_OK) and runtime.verify(dest / "llama-server")[0]


def test_extract_blocks_path_traversal_and_handles_symlinks(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escape.txt", "x")
    with pytest.raises(RuntimeError, match="Unsafe"):
        runtime._extract(evil, tmp_path / "out")
    buf = tmp_path / "mac.tar.gz"
    with tarfile.open(buf, "w:gz") as tf:
        data = b"dylib"
        info = tarfile.TarInfo("llama-b9000/libllama.0.dylib")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("llama-b9000/libllama.dylib")
        link.type, link.linkname = tarfile.SYMTYPE, "libllama.0.dylib"
        tf.addfile(link)
    runtime._extract(buf, tmp_path / "mac")
    assert (tmp_path / "mac" / "llama-b9000" / "libllama.dylib").read_bytes() == b"dylib"


def test_verify_reports_exit_code_and_missing_libraries(tmp_path):
    bad = tmp_path / "llama-server"
    bad.write_text(BAD_SCRIPT)
    bad.chmod(0o755)
    ok, detail, code = runtime.verify(bad)
    assert not ok and code == 127 and "libvulkan.so.1" in detail
    assert "libvulkan" in deps.hint_for(detail, code)
    assert runtime.verify(tmp_path / "missing")[0] is False


def test_runtime_env_exposes_bundled_libraries(tmp_path, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/old")
    env = runtime.runtime_env(tmp_path / "llama-server")
    assert env["LD_LIBRARY_PATH"].split(os.pathsep) == [str(tmp_path), "/old"]


# --------------------------------------------------------------------- system libraries
def test_loader_messages_are_parsed():
    assert deps.missing_libs_from_text(LIB_ERROR) == ["libvulkan.so.1"]
    ldd = "\tlibgomp.so.1 => not found\n\tlibm.so.6 => /lib/x86_64-linux-gnu/libm.so.6\n\tlibstdc++.so.6 => not found\n"
    assert deps.missing_libs_from_text(ldd) == ["libgomp.so.1", "libstdc++.so.6"]
    assert deps.missing_libs_from_text("all fine") == []


def test_package_names_per_distro():
    libs = ["libgomp.so.1", "libvulkan.so.1", "libunknown.so.9"]
    assert deps.packages_for(libs, "apt") == ["libgomp1", "libvulkan1"]
    assert deps.packages_for(libs, "dnf") == ["libgomp", "vulkan-loader"]
    assert deps.packages_for(libs, "pacman") == ["gcc-libs", "vulkan-icd-loader"]
    assert deps.install_command("apt", ["libgomp1"]) == ["apt-get", "install", "-y", "libgomp1"]
    assert deps.detect_package_manager(lambda b: "/usr/bin/x" if b == "dnf" else None) == "dnf"
    assert deps.detect_package_manager(lambda b: None) is None


class Runner:
    def __init__(self, sudo_ok=True, rc=0):
        self.calls, self.sudo_ok, self.rc = [], sudo_ok, rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:3] == ["sudo", "-n", "true"]:
            return subprocess_result(0 if self.sudo_ok else 1)
        return subprocess_result(self.rc)


def subprocess_result(rc):
    import subprocess

    return subprocess.CompletedProcess([], rc, "", "boom" if rc else "")


def test_system_packages_installed_as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    r = Runner()
    ok, msg = REAL_INSTALL_PACKAGES(["libgomp.so.1"], r, manager="apt")
    assert ok and r.calls == [["apt-get", "install", "-y", "libgomp1"]] and "libgomp1" in msg


def test_system_packages_use_passwordless_sudo(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    r = Runner(sudo_ok=True)
    ok, _ = REAL_INSTALL_PACKAGES(["libvulkan.so.1"], r, manager="dnf")
    assert ok and r.calls[-1] == ["sudo", "-n", "dnf", "install", "-y", "vulkan-loader"]


def test_without_privileges_the_exact_command_is_printed_not_run(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    r = Runner(sudo_ok=False)
    ok, msg = REAL_INSTALL_PACKAGES(["libvulkan.so.1"], r, manager="apt")
    assert not ok and "sudo apt-get install -y libvulkan1" in msg
    assert r.calls == [["sudo", "-n", "true"]]  # only probed for sudo; never ran the install


def test_package_manager_failure_is_reported_with_the_command(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    ok, msg = REAL_INSTALL_PACKAGES(["libgomp.so.1"], Runner(rc=100), manager="apt")
    assert not ok and "apt-get install -y libgomp1" in msg


def test_unknown_library_falls_back_to_a_generic_message():
    ok, msg = REAL_INSTALL_PACKAGES(["libweird.so.7"], Runner(), manager="apt")
    assert not ok and "libweird.so.7" in msg


def test_windows_missing_dll_installs_the_vc_runtime(monkeypatch):
    r = Runner(rc=0x8A15002B)  # winget: "already installed" also counts as success
    ok, _msg = REAL_INSTALL_VC_REDIST(r, which=lambda n: "winget")
    assert ok and r.calls[0][:3] == ["winget", "install", "-e"] and "VCRedist" in r.calls[0][4]
    ok, msg = REAL_INSTALL_VC_REDIST(Runner(), which=lambda n: None)
    assert not ok and "vc_redist" in msg
    assert "Visual C++" in deps.hint_for("", 3221225781)


def test_try_fix_routes_by_platform(monkeypatch):
    calls = []
    monkeypatch.setattr(deps, "install_system_packages", lambda libs, *a, **k: calls.append(("pkgs", libs)) or (True, "ok"))
    monkeypatch.setattr(deps, "install_vc_redist", lambda *a, **k: calls.append(("vc",)) or (True, "ok"))
    monkeypatch.setattr(deps.platform, "system", lambda: "Linux")
    assert deps.try_fix(LIB_ERROR, 127, None) == (True, "ok") and calls == [("pkgs", ["libvulkan.so.1"])]
    monkeypatch.setattr(deps.platform, "system", lambda: "Windows")
    assert deps.try_fix("", 3221225781) == (True, "ok") and calls[-1] == ("vc",)
    monkeypatch.setattr(deps.platform, "system", lambda: "Darwin")
    assert deps.try_fix("anything", 1) == (False, "")


def test_hint_covers_cpu_mismatch():
    assert "architecture" in deps.hint_for("cannot execute binary file: Exec format error")
    assert deps.hint_for("some random failure") == ""
