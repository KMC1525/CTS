"""HTTP API tests: uploads, jobs, SSE, downloads and the localhost-only guard."""
import io
import re
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cts.server import create_app
from mock_llm import ANALYSIS, BIBLE, FRONT_COVER, NOT_COVER, PROSE


@pytest.fixture
def client(cts_home):
    return TestClient(create_app(), base_url="http://localhost")


def zip_bytes(name="Vol 1", pages=3):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(1, pages + 1):
            img = io.BytesIO()
            Image.new("RGB", (200, 300), (i * 40, 80, 120)).save(img, "JPEG")
            zf.writestr(f"{name}/{i:03d}.jpg", img.getvalue())
    return buf.getvalue()


def upload(client, filename="Vol 1.zip", data=None, label=None):
    aid = client.post("/api/volumes/attachments", json={"label": label or filename}).json()["id"]
    r = client.put(f"/api/volumes/attachments/{aid}/file", params={"path": filename}, content=data or zip_bytes())
    assert r.status_code == 200
    fin = client.post(f"/api/volumes/attachments/{aid}/finalize")
    assert fin.status_code == 200, fin.text
    return fin.json()["volumes"]


def configure(client, mock):
    r = client.put("/api/settings", json={"provider_type": "frontier", "context_window": 16384, "frontier": {
        "provider": "custom", "models": {"custom": "mock-vl"}, "base_urls": {"custom": mock.base_url}}})
    assert r.status_code == 200


def wait_for(client, jid, states=("done", "failed", "cancelled"), timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        snap = client.get(f"/api/jobs/{jid}").json()
        if snap["status"] in states:
            return snap
        time.sleep(0.2)
    raise AssertionError(f"job did not reach {states}: {snap['status']}")


# ------------------------------------------------------------------------ security
def test_rejects_foreign_hosts_and_cross_origin_writes(client):
    assert client.get("/api/info").status_code == 200
    assert client.get("/api/info", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/info", headers={"host": "127.0.0.1:8321"}).status_code == 200
    assert client.post("/api/shutdown", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/settings", json={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.put("/api/settings", json={}, headers={"origin": "http://localhost:8321"}).status_code == 200


def test_settings_never_return_secrets(client):
    client.put("/api/settings", json={"frontier": {"provider": "openai", "api_keys": {"openai": "sk-very-secret"}}})
    body = client.get("/api/settings").text
    assert "sk-very-secret" not in body and json.loads(body)["frontier"]["api_keys_set"] == {"openai": True}


def test_options_endpoint(client):
    data = client.get("/api/options").json()
    assert {p["id"] for p in data["providers"]} >= {"anthropic", "openai", "xai", "gemini", "openrouter", "custom"}
    assert 8192 in data["context_presets"] and data["languages"][0]["value"] == "auto"


# -------------------------------------------------------------------------- uploads
def test_zip_upload_creates_a_volume_with_thumbnails(client):
    [vol] = upload(client)
    assert vol["name"] == "Vol 1" and vol["pages"] == 3
    r = client.get(f"/api/volumes/{vol['id']}/pages/0/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/volumes/{vol['id']}/pages/99/thumb").status_code == 404
    assert [v["id"] for v in client.get("/api/volumes").json()] == [vol["id"]]
    assert client.delete(f"/api/volumes/{vol['id']}").json() == {"ok": True}
    assert client.get("/api/volumes").json() == []


def test_folder_upload_with_relative_paths(client):
    aid = client.post("/api/volumes/attachments", json={"label": "My Folder"}).json()["id"]
    for name in ("My Folder/2.jpg", "My Folder/10.jpg", "My Folder/1.jpg"):
        client.put(f"/api/volumes/attachments/{aid}/file", params={"path": name}, content=zip_bytes(pages=1)[:0] or _jpeg())
    [vol] = client.post(f"/api/volumes/attachments/{aid}/finalize").json()["volumes"]
    assert vol["name"] == "My Folder" and vol["pages"] == 3


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (50, 80)).save(buf, "JPEG")
    return buf.getvalue()


def test_upload_rejects_path_traversal_and_skips_unknown_types(client):
    aid = client.post("/api/volumes/attachments", json={"label": "x"}).json()["id"]
    assert client.put(f"/api/volumes/attachments/{aid}/file", params={"path": "../../etc/x.png"}, content=b"x").status_code == 400
    assert client.put(f"/api/volumes/attachments/{aid}/file", params={"path": "notes.txt"}, content=b"x").json() == {"skipped": True}
    assert client.post(f"/api/volumes/attachments/{aid}/finalize").status_code == 400  # nothing usable uploaded
    assert client.put("/api/volumes/attachments/not-an-id/file", params={"path": "a.png"}, content=b"x").status_code == 400


def test_multi_volume_zip_is_split(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for v in ("Volume 2", "Volume 1"):
            zf.writestr(f"{v}/1.jpg", _jpeg())
    vols = upload(client, "series.zip", buf.getvalue())
    assert [v["name"] for v in vols] == ["Volume 1", "Volume 2"]


# ---------------------------------------------------------------------------- jobs
def test_job_without_configuration_gives_an_actionable_error(client):
    [vol] = upload(client)
    r = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]]})
    assert r.status_code == 400 and "API key" in r.json()["detail"]
    assert client.post("/api/jobs", json={"final_review": False, "volumes": []}).status_code in (400, 422)


def test_batch_generation_end_to_end_with_sse_and_downloads(client, mock_llm):
    mock = mock_llm()
    configure(client, mock)
    v2 = upload(client, "Vol 2.zip", zip_bytes("Vol 2", 2))[0]
    v1 = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 3))[0]
    job = client.post("/api/jobs", json={"volumes": [v1["id"], v2["id"]], "rating": "sfw", "words_per_page": 120, "skip_covers": False, "final_review": False}).json()
    jid = job["id"]

    events, snapshot = [], None
    with client.stream("GET", f"/api/jobs/{jid}/events") as stream:
        kind = None
        for line in stream.iter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[5:])
                if kind == "snapshot":
                    snapshot = payload
                elif kind == "end":
                    break
                else:
                    events.append(payload)
            if line == "":
                kind = None if kind != "end" else kind
    assert snapshot is not None and snapshot["volumes"][0]["name"] == "Vol 1"
    types = [e["type"] for e in events]
    assert types.count("volume_start") == 2 and types.count("page_done") >= 1 and types[-1] == "job_status"
    assert [e["volume"] for e in events if e["type"] == "volume_start"] == [0, 1]  # volume 1, then volume 2

    snap = wait_for(client, jid)
    assert snap["status"] == "done" and snap["pages_done"] == 5 and snap["pages_total"] == 5
    assert [v["done"] for v in snap["volumes"]] == [3, 2] and snap["volumes"][0]["pages"][0]["text"] == PROSE
    assert len(mock.requests) == 5 * 4 and mock.images_seen() == 5

    for fmt, magic in (("txt", b"VOL 1"), ("docx", b"PK"), ("pdf", b"%PDF")):
        r = client.get(f"/api/jobs/{jid}/volumes/0/download", params={"format": fmt})
        assert r.status_code == 200 and magic in r.content[:200], fmt
    assert client.get(f"/api/jobs/{jid}/volumes/0/download", params={"format": "exe"}).status_code == 400
    assert client.get(f"/api/jobs/{jid}/volumes/7/download", params={"format": "pdf"}).status_code == 404
    assert client.get(f"/api/jobs/{jid}/volumes/0/pages/1/thumb").status_code == 200
    assert client.get(f"/api/jobs/{jid}/volumes/0/pages/1/preview").status_code == 200
    assert client.get(f"/api/jobs/{jid}/volumes/0/pages/1/secret").status_code == 404
    assert jid in [j["id"] for j in client.get("/api/jobs").json()]


def test_carry_story_bible_between_volumes(client, mock_llm):
    mock = mock_llm()
    configure(client, mock)
    v1 = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 1))[0]
    v2 = upload(client, "Vol 2.zip", zip_bytes("Vol 2", 1))[0]
    jid = client.post("/api/jobs", json={"final_review": False, "volumes": [v1["id"], v2["id"]], "carry_bible": True, "words_per_page": 120}).json()["id"]
    wait_for(client, jid)
    novelists = [q for q in mock.requests if mock.role_of(q["messages"]) == "novelist"]
    second_volume_prompt = json.dumps(novelists[1]["messages"])
    assert "# Story Bible" in second_volume_prompt and "Mira: black hair" in second_volume_prompt
    first_prompt = json.dumps(novelists[0]["messages"])
    assert "Mira: black hair" not in first_prompt


def test_stop_then_resume_continues_where_it_left_off(client, mock_llm):
    mock = mock_llm()
    mock.delay = 0.25
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 4))
    jid = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]], "words_per_page": 120}).json()["id"]
    end = time.time() + 30
    while time.time() < end and client.get(f"/api/jobs/{jid}").json()["pages_done"] < 1:
        time.sleep(0.1)
    assert client.delete(f"/api/volumes/{vol['id']}").status_code == 409  # busy volumes cannot be removed
    assert client.delete(f"/api/jobs/{jid}").status_code == 409
    client.post(f"/api/jobs/{jid}/cancel")
    snap = wait_for(client, jid, ("cancelled", "done"))
    assert snap["status"] == "cancelled" and 1 <= snap["pages_done"] < 4
    assert snap["volumes"][0]["partial"] and "pdf" in snap["volumes"][0]["files"]  # partial export is downloadable
    assert client.get(f"/api/jobs/{jid}/volumes/0/download", params={"format": "txt"}).status_code == 200

    mock.delay = 0
    assert client.post(f"/api/jobs/{jid}/resume").status_code == 200
    done = wait_for(client, jid, ("done",))
    assert done["pages_done"] == 4 and not done["volumes"][0]["partial"]
    assert client.delete(f"/api/jobs/{jid}").json() == {"ok": True} and client.get(f"/api/jobs/{jid}").status_code == 404


def test_jobs_survive_a_restart_as_interrupted(client, mock_llm, cts_home):
    mock = mock_llm()
    mock.delay = 0.4
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 3))
    jid = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]], "words_per_page": 120}).json()["id"]
    end = time.time() + 30
    while time.time() < end and client.get(f"/api/jobs/{jid}").json()["pages_done"] < 1:
        time.sleep(0.1)
    # "restart": a brand-new manager reads the same data folder while the old job is mid-flight
    from cts import jobs

    reborn = jobs.JobManager()
    job = reborn.jobs[jid]
    assert job.status == "interrupted" and "Resume" in job.error and job.vstate[0]["status"] in ("partial", "done")
    client.post(f"/api/jobs/{jid}/cancel")
    wait_for(client, jid, ("cancelled", "done"))


def test_test_connection_reports_success_and_failure(client, mock_llm):
    mock = mock_llm()
    configure(client, mock)
    ok = client.post("/api/settings/test").json()
    assert ok["ok"] and "Connected" in ok["message"]
    client.put("/api/settings", json={"frontier": {"provider": "anthropic"}})
    bad = client.post("/api/settings/test").json()
    assert bad["ok"] is False and "API key" in bad["message"]


def test_local_endpoints_without_network(client):
    state = client.get("/api/local/state").json()
    assert state["installed"] == [] and len(state["curated"]) >= 4 and state["runtime"]["installed"] is False
    assert client.get("/api/local/repo", params={"repo": "not a repo"}).status_code == 400
    assert client.post("/api/local/select", json={"id": "nope"}).status_code == 404


# ------------------------------------------------------------------ llama.cpp engine API (v0.2)
def test_runtime_install_endpoint_passes_update_and_repair(client, monkeypatch):
    from cts.local import models, runtime

    seen = []
    monkeypatch.setattr(runtime, "install_runtime", lambda progress=None, **kw: seen.append(kw) or None)
    for body in (None, {"update": True}, {"repair": True}):
        models.runtime_install.update(status="idle")
        r = client.post("/api/local/runtime/install", json=body) if body is not None else client.post("/api/local/runtime/install")
        assert r.status_code == 200
        end = time.time() + 5
        while time.time() < end and models.runtime_install["status"] == "running":
            time.sleep(0.05)
    assert seen == [{"update": False, "repair": False}, {"update": True, "repair": False}, {"update": False, "repair": True}]
    assert client.get("/api/local/state").json()["runtime"]["install"]["status"] == "done"


def test_runtime_install_errors_reach_the_ui(client, monkeypatch):
    from cts.local import models, runtime

    def fail(progress=None, **kw):
        raise RuntimeError("llama.cpp could not be installed:\n  - x: it does not start")

    monkeypatch.setattr(runtime, "install_runtime", fail)
    models.runtime_install.update(status="idle")
    client.post("/api/local/runtime/install", json={})
    end = time.time() + 5
    while time.time() < end and models.runtime_install["status"] == "running":
        time.sleep(0.05)
    inst = client.get("/api/local/state").json()["runtime"]["install"]
    assert inst["status"] == "error" and "does not start" in inst["message"]


def test_runtime_check_endpoint(client, tmp_path, monkeypatch):
    assert client.post("/api/local/runtime/check").json()["ok"] is False
    good = tmp_path / "llama-server"
    good.write_text(f'#!{__import__("sys").executable}\nimport sys\nsys.stderr.write("version: 9 (x)\\n")\n')
    good.chmod(0o755)
    monkeypatch.setenv("CTS_LLAMA_SERVER", str(good))
    res = client.post("/api/local/runtime/check").json()
    assert res["ok"] and res["origin"] == "env" and "version: 9" in res["detail"]
    state = client.get("/api/local/state").json()["runtime"]
    assert state["installed"] and state["origin"] == "env"


# ========================================== v0.3: age rule, covers, preflight ==========================================
def minor_dossier():
    return ANALYSIS.replace("sexual_content: none", "sexual_content: explicit").replace(
        "minor_involved: no", "minor_involved: yes\nminor_evidence: Mira - the caption says she is 15").replace(
        "- Mira: short black hair, red coat, adult woman", "- Mira: short black hair; APPARENT AGE: teen; STATED AGE: 15")


def test_nsfw_job_halts_when_the_comic_shows_a_minor_and_cannot_be_resumed(client, mock_llm):
    mock = mock_llm(lambda role, msgs: {"analyst": minor_dossier(), "keeper": BIBLE, "cover": NOT_COVER}.get(role, PROSE))
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 3))
    jid = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]], "rating": "nsfw", "skip_covers": False}).json()["id"]
    snap = wait_for(client, jid, ("halted", "done", "failed"))
    assert snap["status"] == "halted" and "under 18" in snap["error"] and "page 1" in snap["error"]
    assert "SFW edition" in snap["error"] and snap["pages_done"] == 0
    assert any("age rule" in entry["message"] for entry in snap["log"])
    assert not any(mock.role_of(q["messages"]) in ("novelist", "editor") for q in mock.requests)
    assert client.post(f"/api/jobs/{jid}/resume").status_code == 400  # a halted job is final
    with client.stream("GET", f"/api/jobs/{jid}/events") as stream:  # the event stream ends instead of hanging
        assert any(line.startswith("event: end") for line in stream.iter_lines())
    # the same comic in the SFW edition is simply written
    sfw = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]], "rating": "sfw", "skip_covers": False, "words_per_page": 120}).json()["id"]
    assert wait_for(client, sfw)["status"] == "done"
    assert client.delete(f"/api/jobs/{jid}").json() == {"ok": True}


def test_a_halted_job_survives_a_restart_as_halted(client, mock_llm):
    from cts import jobs

    mock = mock_llm(lambda role, msgs: {"analyst": minor_dossier(), "keeper": BIBLE}.get(role, PROSE))
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 1))
    jid = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]], "rating": "nsfw", "skip_covers": False}).json()["id"]
    wait_for(client, jid, ("halted",))
    reborn = jobs.JobManager()
    assert reborn.jobs[jid].status == "halted" and "under 18" in reborn.jobs[jid].error


def test_covers_are_skipped_counted_as_finished_and_left_out_of_the_novel(client, mock_llm):
    def handler(role, msgs):
        if role == "cover":
            number = int(re.search(r"Is page (\d+) of", " ".join(m["content"] if isinstance(m["content"], str)
                                  else " ".join(p.get("text", "") for p in m["content"]) for m in msgs)).group(1))
            return FRONT_COVER if number == 1 else NOT_COVER
        return {"analyst": ANALYSIS, "keeper": BIBLE}.get(role, PROSE)

    mock = mock_llm(handler)
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 3))
    job = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]], "words_per_page": 120}).json()
    assert client.get(f"/api/jobs/{job['id']}").json()["options"]["skip_covers"] is True  # on by default
    snap = wait_for(client, job["id"])
    assert snap["status"] == "done" and snap["pages_done"] == 3 and snap["pages_total"] == 3
    pages = {p["index"]: p for p in snap["volumes"][0]["pages"]}
    assert pages[1]["status"] == "skipped" and pages[1]["cover"]["title"] == "Midnight Rooftop" and pages[2]["status"] == "done"
    assert snap["volumes"][0]["done"] == 3 and snap["volumes"][0]["status"] == "done"
    txt = client.get(f"/api/jobs/{job['id']}/volumes/0/download", params={"format": "txt"}).text
    assert txt.count("--- Page ") == 2 and txt.startswith("MIDNIGHT ROOFTOP")
    from cts import jobs

    reloaded = jobs.JobManager().jobs[job["id"]]  # skipped pages are restored from disk too
    assert reloaded.vstate[0]["pages"][1]["status"] == "skipped" and reloaded.summary()["pages_done"] == 3


def test_skip_covers_can_be_turned_off(client, mock_llm):
    mock = mock_llm()
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 2))
    jid = client.post("/api/jobs", json={"volumes": [vol["id"]], "words_per_page": 120, "skip_covers": False, "final_review": False}).json()["id"]
    wait_for(client, jid)
    assert not any(mock.role_of(q["messages"]) == "cover" for q in mock.requests)


def test_a_context_window_below_the_minimum_is_refused_with_advice(client, mock_llm):
    mock = mock_llm()
    configure(client, mock)
    client.put("/api/settings", json={"context_window": 4096})
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 1))
    r = client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]]})
    assert r.status_code == 400 and "too small" in r.json()["detail"] and "8,192" in r.json()["detail"]
    assert 4096 not in client.get("/api/options").json()["context_presets"]


def test_local_job_is_refused_up_front_when_the_engine_or_model_is_not_ready(client, cts_home, monkeypatch):
    from test_local import install_fake_model

    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 1))
    client.put("/api/settings", json={"provider_type": "local", "context_window": 16384})
    assert "Install and select a local model" in client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]]}).json()["detail"]
    entry = install_fake_model(cts_home)
    client.put("/api/settings", json={"local": {"active_model": entry["id"]}})
    assert "llama.cpp is not installed" in client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]]}).json()["detail"]
    (cts_home / "models" / "o__m" / "m-Q4_K_M.gguf").unlink()
    assert "files are missing" in client.post("/api/jobs", json={"final_review": False, "volumes": [vol["id"]]}).json()["detail"]


def test_stop_kills_the_local_engine_so_a_long_generation_ends_at_once(cts_home, monkeypatch):
    from cts import jobs

    stopped = []
    monkeypatch.setattr(jobs, "llama", type("L", (), {"stop": staticmethod(lambda: stopped.append(1)), "endpoint": None})())
    from cts import settings as settings_mod

    manager = jobs.JobManager()
    job = jobs.Job("abc1234567", jobs.RunOptions(), [{"id": "v", "name": "V", "pages": 1}], "Local")
    job.status = "running"
    manager.jobs[job.id] = job
    settings_mod.update({"provider_type": "frontier"})
    manager.cancel(job.id)  # hosted providers cannot be aborted: nothing is killed
    time.sleep(0.2)
    assert stopped == [] and job.cancel_event.is_set()
    settings_mod.update({"provider_type": "local"})
    job.cancel_event.clear()
    manager.cancel(job.id)
    time.sleep(0.5)
    assert stopped == [1]


def test_the_context_the_server_really_has_is_what_cts_plans_with(cts_home, monkeypatch):
    from cts import jobs, llm

    events = []
    job = jobs.Job("abc1234567", jobs.RunOptions(), [{"id": "v", "name": "V", "pages": 1}], "Local")
    job.emit = events.append
    spec = llm.LLMSpec("local", "local", "local", "k", "http://127.0.0.1:1/v1", 32768, 1024)
    monkeypatch.setattr(jobs, "llama", type("L", (), {"slot_context": 12288})())
    jobs.JobManager._fit_context(job, spec)
    assert spec.context_window == 12288 and "12,288" in events[0]["message"]
    monkeypatch.setattr(jobs, "llama", type("L", (), {"slot_context": 4096})())
    with pytest.raises(jobs.LocalServerError, match="needs at least 8,192"):
        jobs.JobManager._fit_context(job, llm.LLMSpec("local", "local", "local", "k", "http://127.0.0.1:1/v1", 32768, 1024))


# ========================================== v0.4: final review + system prompt ==========================================
def review_api_handler(reviewer, writer=None):
    from test_pipeline import page_text, revised_text

    def handler(role, msgs):
        task = " ".join(m["content"] if isinstance(m["content"], str) else " ".join(p.get("text", "") for p in m["content"])
                        for m in msgs[-1:])
        if role == "revisor":
            return reviewer(msgs)
        if role == "analyst":
            return ANALYSIS
        if role == "keeper":
            return BIBLE
        if role == "cover":
            return NOT_COVER
        found = re.search(r"novel page (\d+)", task)
        n = int(found.group(1)) if found else 1
        return revised_text(n) if "REVISION NOTES" in task else page_text(n)

    return handler


def test_system_prompt_and_review_options_flow_through_the_api_and_are_remembered(client, mock_llm):
    from test_pipeline import Reviewer

    mock = mock_llm(review_api_handler(Reviewer()))
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 2))
    direction = "Write like a hard-boiled noir. First person, present tense."
    job = client.post("/api/jobs", json={"volumes": [vol["id"]], "words_per_page": 120, "skip_covers": False,
                                         "custom_prompt": f"  {direction}  "}).json()
    snap = wait_for(client, job["id"])
    assert snap["status"] == "done"
    assert snap["options"]["custom_prompt"] == direction and snap["options"]["final_review"] is True  # review is on by default
    review = snap["volumes"][0]["review"]
    assert review["status"] == "passed" and review["rounds"][0]["verdict"] == "coherent"
    novelist = next(q for q in mock.requests if mock.role_of(q["messages"]) == "novelist")
    assert direction in novelist["messages"][0]["content"]
    # the review is persisted with the job and survives a restart
    from cts import jobs

    reborn = jobs.JobManager().jobs[job["id"]]
    assert reborn.vstate[0]["review"]["status"] == "passed" and reborn.options.custom_prompt == direction


def test_the_system_prompt_is_a_remembered_default(client):
    r = client.put("/api/settings", json={"defaults": {"custom_prompt": "Make it a ghost story.", "final_review": False}})
    assert r.json()["defaults"]["custom_prompt"] == "Make it a ghost story." and r.json()["defaults"]["final_review"] is False
    assert client.get("/api/settings").json()["defaults"]["custom_prompt"] == "Make it a ghost story."
    client.put("/api/settings", json={"defaults": {"custom_prompt": ""}})
    assert client.get("/api/settings").json()["defaults"]["custom_prompt"] == ""


def test_an_absurdly_long_system_prompt_is_rejected_and_a_long_one_is_capped(client, mock_llm):
    from test_pipeline import Reviewer

    mock = mock_llm(review_api_handler(Reviewer()))
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 1))
    assert client.post("/api/jobs", json={"volumes": [vol["id"]], "custom_prompt": "x" * 20001}).status_code == 422
    job = client.post("/api/jobs", json={"volumes": [vol["id"]], "words_per_page": 120, "custom_prompt": "y" * 9000}).json()
    assert len(client.get(f"/api/jobs/{job['id']}").json()["options"]["custom_prompt"]) == 6000
    wait_for(client, job["id"])


def test_pages_rewritten_by_the_review_are_visible_in_the_job_and_streamed(client, mock_llm):
    from test_pipeline import FIX_PAGE_2, Reviewer, text_coherent

    reviewer = Reviewer(window_issues=[{2: "her coat is blue but the bible says red"}, {}], verdicts=[FIX_PAGE_2, text_coherent()])
    mock = mock_llm(review_api_handler(reviewer))
    configure(client, mock)
    [vol] = upload(client, "Vol 1.zip", zip_bytes("Vol 1", 3))
    jid = client.post("/api/jobs", json={"volumes": [vol["id"]], "words_per_page": 120, "skip_covers": False}).json()["id"]
    events = []
    with client.stream("GET", f"/api/jobs/{jid}/events") as stream:
        kind = None
        for line in stream.iter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and kind not in ("snapshot", "end"):
                events.append(json.loads(line[5:]))
            elif line == "":
                if kind == "end":
                    break
                kind = None  # an SSE event without an "event:" line is a plain message
    types = [e["type"] for e in events]
    assert "review_start" in types and "review_result" in types and types.index("review_start") < types.index("volume_files")
    assert [e["step"] for e in events if e["type"] == "review_step"][:2] == ["windows", "global"]
    assert any(e["type"] == "review_step" and e["step"] == "fix" and e["page"] == 2 for e in events)
    snap = wait_for(client, jid)
    pages = {p["index"]: p for p in snap["volumes"][0]["pages"]}
    assert pages[2]["revisions"] == 1 and not pages[1].get("revisions") and pages[2]["status"] == "done"
    assert snap["volumes"][0]["review"]["status"] == "passed" and [r["action"] for r in snap["volumes"][0]["review"]["rounds"]] == ["fix-pages", "pass"]
    assert snap["pages_done"] == 3 and snap["volumes"][0]["done"] == 3  # a rewrite does not change the progress count
    txt = client.get(f"/api/jobs/{jid}/volumes/0/download", params={"format": "txt"}).text
    assert "Her red coat stayed red" in txt
    assert client.get(f"/api/jobs/{jid}").json()["activity"]  # the activity line mentioned the review while it ran
