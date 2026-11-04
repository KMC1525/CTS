"""Generation jobs: a queue that processes volumes one at a time and streams progress to the UI."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import llm as llm_mod
from . import settings as settings_mod
from . import workspace
from .local import models as local_models
from .local import runtime as local_runtime
from .local.server import LocalServerError, llama
from .paths import jobs_dir
from .pipeline import budget
from .pipeline.runner import AgeHalt, Cancelled, FatalError, RunOptions, VolumeRunner, _public_record, load_record

log = logging.getLogger("cts.jobs")
TERMINAL = {"done", "failed", "cancelled", "interrupted", "halted"}
STEPS = ("analyze", "write", "polish", "memory")


class Job:
    def __init__(self, jid: str, options: RunOptions, volumes: list[dict[str, Any]], provider_label: str,
                 created: float | None = None):
        self.id = jid
        self.options = options
        self.volumes = volumes  # [{id, name, pages}]
        self.provider_label = provider_label
        self.created = created or time.time()
        self.status = "queued"
        self.error = ""
        self.tokens = 0
        self.started: float | None = None
        self.finished: float | None = None
        self.activity = ""
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self.log: list[dict[str, Any]] = []
        self.vstate: list[dict[str, Any]] = [
            {"index": i, "name": v["name"], "total": v["pages"], "status": "queued", "files": {}, "partial": False,
             "bible": "", "pages": {}}
            for i, v in enumerate(volumes)
        ]

    # ----------------------------------------------------------------- paths
    @property
    def dir(self) -> Path:
        path = jobs_dir() / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def volume_dir(self, index: int) -> Path:
        return self.dir / f"vol_{index + 1:02d}"

    # ---------------------------------------------------------------- events
    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            event = {**event, "seq": len(self.events) + 1, "ts": round(time.time(), 2)}
            self.events.append(event)
            self._apply(event)

    def set_status(self, status: str, error: str = "") -> None:
        with self.lock:
            self.status = status
            self.error = error
            if status == "running" and self.started is None:
                self.started = time.time()
            if status in TERMINAL:
                self.finished = time.time()
                for v in self.vstate:  # nothing is running any more: do not leave pages/volumes "in progress"
                    for page in v["pages"].values():
                        if page["status"] == "running":
                            page["status"] = "stopped"
                            page["steps"] = {k: x for k, x in page["steps"].items() if x == "done"}
                    if v["status"] == "running":
                        v["status"] = "partial"
        self.emit({"type": "job_status", "status": status, "error": error})
        self.save()

    def _page(self, vol: int, page: int) -> dict[str, Any]:
        pages = self.vstate[vol]["pages"]
        return pages.setdefault(page, {"index": page, "status": "pending", "steps": {}, "text": "", "analysis": "",
                                       "notes": [], "words": 0, "rating_used": "", "page_type": "", "seconds": 0})

    def _apply(self, e: dict[str, Any]) -> None:
        kind = e["type"]
        if kind == "job_status":
            return
        vol = e.get("volume")
        if kind == "volume_start":
            v = self.vstate[vol]
            v["status"], v["total"] = "running", e["total"]
        elif kind == "page_start":
            p = self._page(vol, e["page"])
            p["status"], p["steps"] = "running", {}
        elif kind == "step":
            p = self._page(vol, e["page"])
            p["status"] = "running"
            p["steps"][e["step"]] = e["status"]
            self.activity = f"Volume {vol + 1} - page {e['page']}: {_STEP_LABEL[e['step']]}"
        elif kind == "page_done":
            rec = e["record"]
            p = self._page(vol, e["page"])
            p.update(rec)
            if rec.get("status") == "skipped":
                p["steps"] = {k: v for k, v in p["steps"].items() if k == "cover"}
                p["status"] = "skipped"
            else:
                p["steps"] = {s: "done" for s in STEPS}
                p["status"] = "done" if rec.get("status") == "done" else "failed"
            if e.get("tokens") is not None:
                self.tokens = max(self.tokens, e["tokens"])
        elif kind == "page_failed":
            p = self._page(vol, e["page"])
            p["status"], p["error"] = "failed", e["error"]
        elif kind == "bible":
            self.vstate[vol]["bible"] = e["text"]
        elif kind == "volume_files":
            self.vstate[vol]["files"], self.vstate[vol]["partial"] = e["files"], e["partial"]
        elif kind == "volume_done":
            self.vstate[vol]["status"] = "done"
        elif kind == "log":
            self.log.append({"ts": e["ts"], "level": e["level"], "message": e["message"]})
            del self.log[:-300]
        elif kind == "activity":
            self.activity = e["message"]
        elif kind == "age_halt":
            self.log.append({"ts": e["ts"], "level": "error",
                             "message": f"Page {e['page']}: stopped by the age rule - {e['evidence']}"})

    # -------------------------------------------------------------- snapshots
    def summary(self) -> dict[str, Any]:
        with self.lock:
            done = sum(1 for v in self.vstate for p in v["pages"].values() if p["status"] in ("done", "skipped"))
            total = sum(v["total"] for v in self.vstate)
            return {
                "id": self.id, "status": self.status, "error": self.error, "created": self.created,
                "started": self.started, "finished": self.finished, "provider": self.provider_label,
                "rating": self.options.rating, "volumes": [v["name"] for v in self.vstate],
                "pages_done": done, "pages_total": total,
            }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            vols = []
            for v in self.vstate:
                pages = [v["pages"][k] for k in sorted(v["pages"])]
                vols.append({**{k: v[k] for k in ("index", "name", "total", "status", "files", "partial", "bible")},
                             "pages": pages, "done": sum(1 for p in pages if p["status"] in ("done", "skipped"))})
            return {**self.summary(), "options": asdict(self.options), "tokens": self.tokens,
                    "activity": self.activity, "log": list(self.log), "volumes": vols, "seq": len(self.events)}

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        data = {
            "id": self.id, "created": self.created, "status": self.status, "error": self.error,
            "options": asdict(self.options), "volumes": self.volumes, "provider": self.provider_label,
            "tokens": self.tokens, "started": self.started, "finished": self.finished,
        }
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "job.json")

    @classmethod
    def load(cls, path: Path) -> "Job | None":
        try:
            data = json.loads((path / "job.json").read_text(encoding="utf-8"))
            job = cls(data["id"], RunOptions(**data["options"]), data["volumes"], data.get("provider", ""),
                      data.get("created"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        job.status, job.error = data.get("status", "interrupted"), data.get("error", "")
        job.tokens, job.started, job.finished = data.get("tokens", 0), data.get("started"), data.get("finished")
        if job.status in ("running", "queued"):
            job.status, job.error = "interrupted", "CTS was closed while this job was running. Press Resume to continue."
        job._restore_state()
        return job

    def _restore_state(self) -> None:
        for vi, v in enumerate(self.vstate):
            work = self.volume_dir(vi)
            done = 0
            for page in range(1, v["total"] + 1):
                rec = load_record(work, page)
                if not rec:
                    continue
                p = self._page(vi, page)
                p.update(_public_record(rec))
                status = rec.get("status")
                p["steps"] = {s: "done" for s in STEPS} if status == "done" else ({"cover": "done"} if status == "skipped" else {})
                p["status"] = status if status in ("done", "skipped") else "failed"
                v["bible"] = rec.get("bible_after", v["bible"])
                done += p["status"] in ("done", "skipped")
            out = work / "output"
            if out.is_dir():
                files = {}
                for fmt in ("txt", "docx", "pdf"):
                    found = sorted(out.glob(f"*.{fmt}"), key=lambda f: ("_partial" in f.name, f.name))
                    if found:
                        files[fmt] = found[0].relative_to(work).as_posix()
                v["files"], v["partial"] = files, done < v["total"]
            v["status"] = "done" if v["total"] and done == v["total"] else ("partial" if done else "queued")


_STEP_LABEL = {"cover": "checking whether it is a book cover", "analyze": "reading the comic page", "write": "writing the draft",
               "polish": "polishing", "memory": "updating the story bible"}


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        for path in sorted(jobs_dir().iterdir()) if jobs_dir().exists() else []:
            if path.is_dir():
                job = Job.load(path)
                if job:
                    self.jobs[job.id] = job

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="cts-jobs", daemon=True)
                self._worker.start()

    # --------------------------------------------------------------------- API
    def create(self, volume_ids: list[str], options: RunOptions) -> Job:
        settings = settings_mod.load()
        self.check_ready(settings)  # fail early, with a friendly message, before queueing anything
        volumes = []
        for vid in volume_ids:
            record = workspace.get(vid)
            if record is None:
                raise ValueError("One of the volumes no longer exists. Re-attach it.")
            volumes.append({"id": vid, "name": record["name"], "pages": len(record["pages"])})
        if not volumes:
            raise ValueError("Attach at least one comic volume first.")
        job = Job(uuid.uuid4().hex[:10], options.normalised(), volumes, self.describe_provider(settings))
        self.jobs[job.id] = job
        job.save()
        self._enqueue(job)
        return job

    def resume(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.status not in ("cancelled", "failed", "interrupted"):
            raise ValueError("Only stopped or failed jobs can be resumed.")
        self.check_ready(settings_mod.load())
        job.cancel_event.clear()
        job.provider_label = self.describe_provider(settings_mod.load())
        self._enqueue(job)
        return job

    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.cancel_event.set()
        if job.status == "queued":
            job.set_status("cancelled")
        elif job.status == "running" and settings_mod.load().provider_type == "local":
            # A CPU/GPU generation can take minutes: killing the local server aborts the call right away
            # (the next job simply starts it again). Hosted calls cannot be aborted and end on their own.
            threading.Thread(target=llama.stop, daemon=True).start()

    def delete(self, job_id: str) -> None:
        import shutil

        job = self.jobs.get(job_id)
        if job is None:
            return
        if job.status in ("running", "queued"):
            raise ValueError("Stop the job before deleting it.")
        self.jobs.pop(job_id)
        shutil.rmtree(job.dir, ignore_errors=True)

    def _enqueue(self, job: Job) -> None:
        job.set_status("queued")
        self._queue.put(job.id)
        self._ensure_worker()

    @staticmethod
    def describe_provider(settings: settings_mod.Settings) -> str:
        if settings.provider_type == "local":
            model = local_models.get_installed(settings.local.active_model)
            return f"Local / {model['label']}" if model else "Local"
        try:
            return llm_mod.resolve_spec(settings).label
        except llm_mod.ConfigError:
            return settings.frontier.provider

    @staticmethod
    def check_ready(settings: settings_mod.Settings) -> None:
        """Fail early, with a message the user can act on, before anything is queued."""
        if settings.context_window < budget.MIN_WORKING_CONTEXT:
            raise llm_mod.ConfigError(
                f"The context window ({settings.context_window:,} tokens) is too small: CTS needs at least "
                f"{budget.MIN_WORKING_CONTEXT:,} to fit a comic page, the prompts and the story so far. "
                "Raise it in Settings.")
        if settings.provider_type == "local":
            model = local_models.get_installed(settings.local.active_model) if settings.local.active_model else None
            if not model:
                raise llm_mod.ConfigError("Install and select a local model in Settings first.")
            main, projector = local_models.paths_for(model)
            if projector is None:
                raise llm_mod.ConfigError("This local model has no vision projector (mmproj), so it cannot read "
                                          "comic pages. Pick a vision-capable model in Settings.")
            if not main.is_file() or not projector.is_file():
                raise llm_mod.ConfigError("The local model's files are missing. Reinstall it in Settings > Local models.")
            if local_runtime.find_server() is None:
                raise llm_mod.ConfigError("llama.cpp is not installed yet. Open Settings > Local models and press "
                                          "'Install llama.cpp'.")
        else:
            llm_mod.resolve_spec(settings)

    # ------------------------------------------------------------------ worker
    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued" or job.cancel_event.is_set():
                continue
            try:
                self._run(job)
            except Exception:  # noqa: BLE001 - never let the worker die
                log.exception("job crashed")
                job.set_status("failed", "Unexpected error:\n" + traceback.format_exc(limit=3))
            if self._queue.empty():
                llama.stop()  # free the RAM used by the local model once nothing is waiting

    @staticmethod
    def _fit_context(job: Job, spec: llm_mod.LLMSpec) -> None:
        """Use the context the server really has (it can be smaller than what was asked for)."""
        actual = llama.slot_context
        if actual and actual < spec.context_window:
            job.emit({"type": "log", "level": "warn", "message":
                      f"The local server runs with {actual:,} tokens of context (you asked for "
                      f"{spec.context_window:,}); CTS adapts to it."})
            spec.context_window = actual
        if spec.context_window < budget.MIN_WORKING_CONTEXT:
            raise LocalServerError(
                f"The local server only has {spec.context_window:,} tokens of context, but CTS needs at least "
                f"{budget.MIN_WORKING_CONTEXT:,}. Choose a smaller model/quantization or a model with a longer "
                "context, and check the RAM available.")

    def _run(self, job: Job) -> None:
        job.set_status("running")
        settings = settings_mod.load()
        recover = None
        try:
            if settings.provider_type == "local":
                job.emit({"type": "activity", "message": "Starting the local model..."})
                model_id, ctx = settings.local.active_model, settings.context_window
                status = lambda m: job.emit({"type": "activity", "message": m})  # noqa: E731
                endpoint = llama.ensure(model_id, ctx, on_status=status, should_cancel=job.cancel_event.is_set)
                spec = llm_mod.resolve_spec(settings, endpoint)
                self._fit_context(job, spec)

                def recover(_exc: BaseException) -> bool:  # noqa: F811 - the server died mid-job: start it again
                    if job.cancel_event.is_set() or llama.endpoint is not None:
                        return False
                    new_endpoint = llama.ensure(model_id, ctx, on_status=status, should_cancel=job.cancel_event.is_set)
                    spec.base_url = new_endpoint
                    self._fit_context(job, spec)
                    return True
            else:
                spec = llm_mod.resolve_spec(settings)
        except (LocalServerError, llm_mod.ConfigError) as exc:
            job.set_status("failed" if not job.cancel_event.is_set() else "cancelled", str(exc))
            return

        runner = VolumeRunner(spec, job.options, job.emit, job.cancel_event.is_set, recover=recover)
        bible, prior = "", []
        try:
            for vi, vol in enumerate(job.volumes):
                record = workspace.get(vol["id"])
                if record is None:
                    raise FatalError(f"Volume '{vol['name']}' was removed from the workspace.")
                paths = workspace.page_paths(record)
                result = runner.run_volume(
                    vi, vol["name"], paths, job.volume_dir(vi),
                    bible=bible if job.options.carry_bible else "",
                    prior_pages=prior if job.options.carry_bible else [],
                )
                bible = result.bible
                prior = [r["text"] for r in result.records if r.get("status") != "skipped"][-12:]
                job.tokens = runner.tokens
                job.emit({"type": "volume_done", "volume": vi})
            job.set_status("done")
        except Cancelled:
            job.set_status("cancelled")
        except AgeHalt as stop:
            job.tokens = runner.tokens
            job.set_status("halted", str(stop))
        except FatalError as exc:
            job.set_status("failed", str(exc))

    # ------------------------------------------------------------------ files
    def file_for(self, job_id: str, volume: int, fmt: str) -> Path | None:
        job = self.jobs.get(job_id)
        if job is None or not 0 <= volume < len(job.vstate):
            return None
        rel = job.vstate[volume]["files"].get(fmt)
        if not rel:
            return None
        base = job.volume_dir(volume).resolve()
        path = (base / rel).resolve()
        return path if base in path.parents and path.is_file() else None


manager: JobManager | None = None


def get_manager() -> JobManager:
    global manager
    if manager is None:
        manager = JobManager()
    return manager
