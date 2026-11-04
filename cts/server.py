"""The local web server: JSON API + the vanilla HTML/CSS/JS front end."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, ingest, llm as llm_mod, providers, settings as settings_mod, workspace
from .jobs import TERMINAL, get_manager
from .local import catalog as hf_catalog
from .local import models as local_models
from .local import runtime as llama_runtime
from .local.server import LocalServerError, llama
from .paths import WEB_DIR, data_dir, state_file
from .pipeline.runner import RunOptions, media_path
from .pipeline.text import LANGUAGES

DEFAULT_PORT = 8321
CONTEXT_PRESETS = [4096, 8192, 16384, 32768, 65536, 131072, 262144, 1048576]
MAX_UPLOAD_BYTES = 12 * 1024**3
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_server: uvicorn.Server | None = None


# ------------------------------------------------------------------ request bodies
class AttachmentIn(BaseModel):
    label: str = Field(default="Volume", max_length=200)


class JobIn(BaseModel):
    volumes: list[str]
    rating: str = "sfw"
    words_per_page: int = 300
    language: str = "auto"
    carry_bible: bool = True


class InstallIn(BaseModel):
    repo: str
    quant: str


class RuntimeIn(BaseModel):
    update: bool = False
    repair: bool = False


class SelectIn(BaseModel):
    id: str


# ------------------------------------------------------------------------- helpers
def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("CTS_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return _LOCAL_HOSTS | extra


def _host_of(value: str) -> str:
    if value.startswith("["):  # [::1]:8321
        return value.split("]")[0] + "]"
    return value.split(":")[0].lower()


def create_app() -> FastAPI:
    app = FastAPI(title="CTS - Comic To Story", version=__version__, docs_url=None, redoc_url=None)

    # CTS has no login (it is a single-user local app), so it must refuse to be driven by
    # other websites: only local Host headers (DNS-rebinding) and same-origin writes.
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if _host_of(request.headers.get("host", "")) not in _allowed_hosts():
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and _host_of(urlparse(origin).netloc) not in _allowed_hosts():
                return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api") else "no-cache")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    # ------------------------------------------------------------------ general
    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {"name": "CTS", "version": __version__, "data_dir": str(data_dir())}

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return {
            "languages": [{"value": k, "label": v} for k, v in LANGUAGES.items()],
            "context_presets": CONTEXT_PRESETS,
            "providers": providers.catalog(),
        }

    @app.post("/api/shutdown")
    def shutdown() -> dict[str, bool]:
        def stop() -> None:
            time.sleep(0.4)
            llama.stop()
            if _server is not None:
                _server.should_exit = True
            else:  # pragma: no cover - only when not started through serve()
                os._exit(0)

        threading.Thread(target=stop, daemon=True).start()
        return {"ok": True}

    # ----------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_mod.load().public()

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any]) -> dict[str, Any]:
        return settings_mod.update(patch).public()

    @app.post("/api/settings/test")
    async def test_settings() -> dict[str, Any]:
        settings = settings_mod.load()

        def work() -> dict[str, Any]:
            try:
                if settings.provider_type == "local":
                    endpoint = llama.ensure(settings.local.active_model, settings.context_window)
                    spec = llm_mod.resolve_spec(settings, endpoint)
                else:
                    spec = llm_mod.resolve_spec(settings)
                reply = llm_mod.test_connection(spec)
                return {"ok": True, "message": f"Connected to {spec.label}. Model replied: {reply or '(empty)'}"}
            except (llm_mod.ConfigError, LocalServerError) as exc:
                return {"ok": False, "message": str(exc)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": llm_mod.describe_error(exc)[0]}

        return await run_in_threadpool(work)

    # ------------------------------------------------------------- local models
    @app.get("/api/local/state")
    def local_state() -> dict[str, Any]:
        settings = settings_mod.load()
        return {
            "curated": hf_catalog.CURATED,
            "installed": local_models.list_installed(),
            "installs": list(local_models.installs.values()),
            "runtime": local_models.runtime_status(),
            "active": settings.local.active_model,
        }

    @app.get("/api/local/repo")
    async def local_repo(repo: str) -> dict[str, Any]:
        try:
            name = hf_catalog.normalise_repo(repo)
            files = await run_in_threadpool(hf_catalog.list_repo_files, name, settings_mod.load().local.hf_token)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Could not reach Hugging Face: {exc}")
        described = hf_catalog.describe_repo(files)
        if not described["quants"]:
            raise HTTPException(400, "No .gguf files found in that repository.")
        return {"repo": name, **described}

    @app.get("/api/local/search")
    async def local_search(q: str) -> list[dict[str, Any]]:
        try:
            return await run_in_threadpool(hf_catalog.search_models, q, settings_mod.load().local.hf_token)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Could not reach Hugging Face: {exc}")

    @app.post("/api/local/runtime/install")
    def install_runtime(body: RuntimeIn | None = None) -> dict[str, bool]:
        body = body or RuntimeIn()
        local_models.start_runtime_install(update=body.update, repair=body.repair)
        return {"ok": True}

    @app.post("/api/local/runtime/check")
    async def check_runtime() -> dict[str, Any]:
        """Actually run llama-server --version, so a broken install is reported with a fix."""
        return await run_in_threadpool(llama_runtime.check)

    @app.post("/api/local/install")
    def install_model(body: InstallIn) -> dict[str, str]:
        try:
            iid = local_models.start_install(body.repo, body.quant, settings_mod.load().local.hf_token)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"id": iid}

    @app.post("/api/local/install/{iid}/cancel")
    def cancel_install(iid: str) -> dict[str, bool]:
        local_models.cancel_install(iid)
        return {"ok": True}

    @app.post("/api/local/select")
    def select_model(body: SelectIn) -> dict[str, Any]:
        if local_models.get_installed(body.id) is None:
            raise HTTPException(404, "Unknown model")
        return settings_mod.update({"local": {"active_model": body.id}}).public()

    @app.delete("/api/local/models/{mid}")
    def delete_model(mid: str) -> dict[str, bool]:
        llama.stop()
        return {"ok": local_models.delete(mid)}

    # ------------------------------------------------------------------ volumes
    @app.post("/api/volumes/attachments")
    def new_attachment(body: AttachmentIn) -> dict[str, str]:
        return {"id": workspace.create_attachment(body.label)}

    @app.put("/api/volumes/attachments/{aid}/file")
    async def upload_file(aid: str, path: str, request: Request) -> dict[str, Any]:
        try:
            suffix = os.path.splitext(path)[1].lower()
            if suffix not in ingest.IMAGE_EXTS | ingest.ARCHIVE_EXTS:
                return {"skipped": True}
            dest = workspace.attachment_file_path(aid, path)
        except ingest.IngestError as exc:
            raise HTTPException(400, str(exc))
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        size = 0
        with open(part, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    out.close()
                    part.unlink(missing_ok=True)
                    raise HTTPException(413, "File too large")
                out.write(chunk)
        part.replace(dest)
        return {"ok": True, "bytes": size}

    @app.post("/api/volumes/attachments/{aid}/finalize")
    async def finalize(aid: str) -> dict[str, Any]:
        try:
            volumes = await run_in_threadpool(workspace.finalize_attachment, aid)
        except ingest.IngestError as exc:
            raise HTTPException(400, str(exc))
        return {"volumes": volumes}

    @app.get("/api/volumes")
    def volumes() -> list[dict[str, Any]]:
        return workspace.list_volumes()

    @app.delete("/api/volumes/{vid}")
    def delete_volume(vid: str) -> dict[str, bool]:
        busy = {v["id"] for j in get_manager().jobs.values() if j.status in ("running", "queued") for v in j.volumes}
        if vid in busy:
            raise HTTPException(409, "This volume is being processed right now.")
        return {"ok": workspace.delete(vid)}

    @app.get("/api/volumes/{vid}/pages/{index}/thumb")
    async def thumb(vid: str, index: int) -> Response:
        record = workspace.get(vid)
        if record is None:
            raise HTTPException(404)
        data = await run_in_threadpool(workspace.thumbnail, record, index)
        if data is None:
            raise HTTPException(404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # --------------------------------------------------------------------- jobs
    @app.post("/api/jobs")
    def create_job(body: JobIn) -> dict[str, Any]:
        try:
            job = get_manager().create(
                body.volumes,
                RunOptions(body.rating, body.words_per_page, body.language, body.carry_bible),
            )
        except (llm_mod.ConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        return job.summary()

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [j.summary() for j in sorted(get_manager().jobs.values(), key=lambda j: j.created, reverse=True)]

    def _job(jid: str):
        job = get_manager().jobs.get(jid)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job

    @app.get("/api/jobs/{jid}")
    def job_snapshot(jid: str) -> dict[str, Any]:
        return _job(jid).snapshot()

    @app.get("/api/jobs/{jid}/events")
    async def job_events(jid: str, request: Request) -> StreamingResponse:
        job = _job(jid)

        async def stream():
            snapshot = job.snapshot()
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            seq, quiet = snapshot["seq"], 0
            while True:
                if await request.is_disconnected():
                    return
                with job.lock:
                    fresh = job.events[seq:]
                for event in fresh:
                    yield f"data: {json.dumps(event)}\n\n"
                    seq = event["seq"]
                if not fresh:
                    if job.status in TERMINAL:
                        yield "event: end\ndata: {}\n\n"
                        return
                    quiet += 1
                    if quiet % 50 == 0:
                        yield ": keep-alive\n\n"
                else:
                    quiet = 0
                await asyncio.sleep(0.3)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: str) -> dict[str, bool]:
        _job(jid)
        get_manager().cancel(jid)
        return {"ok": True}

    @app.post("/api/jobs/{jid}/resume")
    def resume_job(jid: str) -> dict[str, Any]:
        _job(jid)
        try:
            return get_manager().resume(jid).summary()
        except (llm_mod.ConfigError, ValueError) as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/jobs/{jid}")
    def delete_job(jid: str) -> dict[str, bool]:
        _job(jid)
        try:
            get_manager().delete(jid)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    @app.get("/api/jobs/{jid}/volumes/{index}/download")
    def download(jid: str, index: int, format: str = "pdf") -> FileResponse:
        if format not in ("txt", "docx", "pdf"):
            raise HTTPException(400, "format must be txt, docx or pdf")
        path = get_manager().file_for(jid, index, format)
        if path is None:
            raise HTTPException(404, "That file is not ready yet")
        return FileResponse(path, filename=path.name)

    @app.get("/api/jobs/{jid}/volumes/{index}/pages/{page}/{kind}")
    def page_media(jid: str, index: int, page: int, kind: str) -> FileResponse:
        if kind not in ("thumb", "preview"):
            raise HTTPException(404)
        job = _job(jid)
        if not 0 <= index < len(job.vstate):
            raise HTTPException(404)
        path = media_path(job.volume_dir(index), kind, page)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------------- static
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


app = create_app()


# --------------------------------------------------------------------- entry points
def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def find_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        if not port_in_use(port):
            return port
    raise RuntimeError("No free port found")


def serve(host: str = "127.0.0.1", port: int | None = None, open_browser: bool = True) -> None:
    global _server
    chosen = port or find_port(DEFAULT_PORT)
    url = f"http://localhost:{chosen}"
    config = uvicorn.Config(app, host=host, port=chosen, log_level="warning")
    _server = uvicorn.Server(config)
    state_file().write_text(json.dumps({"pid": os.getpid(), "port": chosen, "url": url}), encoding="utf-8")
    if open_browser:
        threading.Timer(1.2, _open_browser, args=(url,)).start()
    print(f"CTS is running at {url}  (press Ctrl+C to stop)", flush=True)
    try:
        _server.run()
    finally:
        llama.stop()
        state_file().unlink(missing_ok=True)


def _open_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)
