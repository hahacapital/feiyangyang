"""In-memory background-job registry for scans.

Single-thread executor (one scan at a time) so the warm cache and CPU are not
oversubscribed. Job ids are prefixed with the server epoch so a client can detect
a process restart (the new process won't recognise an old id)."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from webapp import engine_service


class JobState:
    def __init__(self) -> None:
        self.status = "queued"          # queued|running|done|error|cancelled
        self.done = 0
        self.total = 0
        self.result: dict | None = None
        self.error: str | None = None
        self.req = None                 # the ScanRequest, for on-demand single curves
        self.cancel_event = threading.Event()


_jobs: dict[str, JobState] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan")


def submit(req, server_epoch: str) -> str:
    job_id = f"{server_epoch}-{uuid.uuid4().hex[:8]}"
    job = JobState()
    job.req = req
    with _lock:
        _jobs[job_id] = job
    _executor.submit(_run, job_id, req)
    return job_id


def _run(job_id: str, req) -> None:
    job = _jobs[job_id]
    job.status = "running"

    def progress(done: int, total: int) -> None:
        job.done, job.total = done, total

    try:
        job.result = engine_service.run_scan(
            req, progress_cb=progress, cancel_event=job.cancel_event)
        job.status = "done"
    except engine_service.ScanCancelled:
        job.status = "cancelled"
    except engine_service.PrimaryNotFound as exc:
        job.status = "error"
        job.error = f"{exc} isn't in the cache. Try another symbol."
    except Exception as exc:  # noqa: BLE001 - surface any engine error to the client
        job.status = "error"
        job.error = str(exc)


def get(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def cancel(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job is None:
        return False
    job.cancel_event.set()
    return True


def belongs_to_epoch(job_id: str, server_epoch: str) -> bool:
    return job_id.startswith(f"{server_epoch}-")
