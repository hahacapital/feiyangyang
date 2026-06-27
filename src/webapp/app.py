"""FastAPI app: lifespan warmup, scan API with SSE progress, static SPA.

Single worker only (the warm cache + job registry are process-local). Warmup runs
on a background thread so /api/status stays responsive during the 1-3 min cold
start; /healthz is a cheap liveness for the ALB while warming."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.gzip import GZipMiddleware

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from webapp import engine_service, jobs  # noqa: E402
from webapp.schemas import ScanRequest    # noqa: E402

STATE = {
    "ready": False,
    "loaded": 0,
    "total": 0,
    "cache_date": None,
    "server_epoch": uuid.uuid4().hex[:8],
    "warmup_error": None,
}


class IdlePolicy(BaseModel):
    minutes: Optional[float] = None


_MONITOR = {"obj": None}  # set in lifespan (prod) or lazily for tests
_IDLE_FALLBACK = {"minutes": None}


def _warmup_prod() -> None:
    """S3 sync -> disk -> warm store. Runs on a background thread."""
    from webapp import cache_sync
    import data_loader as dl
    try:
        cache_sync.sync_cache()
        tickers = dl.list_universe(min_bars=756)
        frames = {}
        STATE["total"] = len(tickers)
        for i, t in enumerate(tickers, 1):
            try:
                frames[t] = dl.load_ohlc(t)
            except Exception:
                continue
            STATE["loaded"] = i
        engine_service.warm_load(frames)
        manifest = dl.read_manifest()
        STATE["cache_date"] = manifest.get("last_update")
    except Exception as exc:               # noqa: BLE001 - surface warmup failure
        STATE["warmup_error"] = str(exc)
    finally:
        STATE["ready"] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("FEIYANG_DEV_FIXTURE") == "1":
        engine_service.load_dev_fixture()
        STATE.update(ready=True, loaded=engine_service.warm_count(),
                     total=engine_service.warm_count(), cache_date="dev-fixture")
    elif os.environ.get("FEIYANG_SKIP_WARMUP") == "1":
        pass  # tests inject WARM and set STATE["ready"]
    else:
        threading.Thread(target=_warmup_prod, name="warmup", daemon=True).start()
        import boto3
        from webapp.idle import IdleMonitor
        cluster = os.environ.get("ECS_CLUSTER", "ff")
        service = os.environ.get("ECS_SERVICE", "feiyangyang")
        _MONITOR["obj"] = IdleMonitor(boto3.client("ecs"), cluster, service)

        def _idle_loop():
            while True:
                time.sleep(60)
                try:
                    if _MONITOR["obj"] and _MONITOR["obj"].maybe_stop():
                        return
                except Exception:
                    pass
        threading.Thread(target=_idle_loop, name="idle", daemon=True).start()
    yield


app = FastAPI(title="feiyangyang · STANDBY CONSOLE", lifespan=lifespan)
# Compress JSON responses (the ~49 KB curve payload from /api/scan/{id}/result).
# Starlette's GZipMiddleware excludes text/event-stream by content-type, so the
# SSE progress stream is never buffered/compressed.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def _touch_idle(request: Request, call_next):
    if _MONITOR["obj"] is not None and request.url.path.startswith("/api/"):
        _MONITOR["obj"].touch()
    return await call_next(request)


# SPA assets change on every deploy; without this browsers serve a stale cached
# index.html/app.js/styles.css. no-cache forces revalidation (ETag -> 304 when
# unchanged, so it stays cheap) instead of blind caching.
_NO_CACHE_PATHS = {"/", "/index.html", "/app.js", "/styles.css"}


@app.middleware("http")
async def _spa_no_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path in _NO_CACHE_PATHS:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/healthz")
def healthz() -> Response:
    return Response(content="ok", media_type="text/plain")


@app.get("/api/status")
def status() -> dict:
    return {"state": "ready" if STATE["ready"] else "warming",
            "loaded": STATE["loaded"], "total": STATE["total"],
            "cache_date": STATE["cache_date"], "server_epoch": STATE["server_epoch"],
            "warmup_error": STATE["warmup_error"]}


@app.get("/api/universe")
def universe() -> dict:
    return {"tickers": engine_service.universe()}


@app.post("/api/scan")
def scan(req: ScanRequest) -> dict:
    if not STATE["ready"]:
        return JSONResponse(status_code=503, content={"error": "Warming the cache — try again shortly."})
    job_id = jobs.submit(req, STATE["server_epoch"])
    return {"job_id": job_id, "server_epoch": STATE["server_epoch"]}


@app.get("/api/scan/{job_id}/result")
def scan_result(job_id: str):
    job = jobs.get(job_id)
    if job is None or not jobs.belongs_to_epoch(job_id, STATE["server_epoch"]):
        return JSONResponse(status_code=410,
                            content={"status": "unknown_job",
                                     "server_epoch": STATE["server_epoch"]})
    return {"status": job.status, "done": job.done, "total": job.total,
            "error": job.error, "result": job.result}


@app.get("/api/scan/{job_id}/curve")
def scan_curve(job_id: str, ticker: str):
    """On-demand combined equity curve for one ranked backup (click-to-view)."""
    job = jobs.get(job_id)
    if job is None or not jobs.belongs_to_epoch(job_id, STATE["server_epoch"]):
        return JSONResponse(status_code=410,
                            content={"status": "unknown_job",
                                     "server_epoch": STATE["server_epoch"]})
    if job.req is None:
        return JSONResponse(status_code=409, content={"error": "job has no request context"})
    try:
        curves = engine_service.single_curve(job.req, ticker.upper())
    except engine_service.CandidateNotFound:
        return JSONResponse(status_code=404, content={"error": f"{ticker} not in the universe"})
    except engine_service.PrimaryNotFound:
        return JSONResponse(status_code=404, content={"error": "primary not in cache"})
    return {"curves": curves}


@app.get("/api/scan/{job_id}/events")
async def scan_events(job_id: str):
    async def gen():
        job = jobs.get(job_id)
        if job is None or not jobs.belongs_to_epoch(job_id, STATE["server_epoch"]):
            yield {"event": "error", "data": json.dumps(
                {"status": "unknown_job", "server_epoch": STATE["server_epoch"]})}
            return
        last = -1
        completed = False
        try:
            while True:
                if job.done != last:
                    last = job.done
                    yield {"event": "progress",
                           "data": json.dumps({"done": job.done, "total": job.total})}
                if job.status in {"done", "error", "cancelled"}:
                    # Completion signal only; the SPA fetches the full result from
                    # GET /api/scan/{id}/result (gzip-eligible) so the SSE stream stays small.
                    yield {"event": "result",
                           "data": json.dumps({"status": job.status, "error": job.error})}
                    completed = True
                    return
                await asyncio.sleep(0.5)
        finally:
            # Client disconnected before completion -> stop the abandoned scan so it
            # doesn't hold the single-thread executor and block the next scan.
            if not completed and job.status in {"queued", "running"}:
                jobs.cancel(job_id)
    return EventSourceResponse(gen())


@app.get("/api/idle-policy")
def get_idle_policy() -> dict:
    m = _MONITOR["obj"]
    return {"minutes": m.get_minutes() if m else _IDLE_FALLBACK["minutes"]}


@app.post("/api/idle-policy")
def set_idle_policy(policy: IdlePolicy) -> dict:
    m = _MONITOR["obj"]
    if m:
        m.set_minutes(policy.minutes)
    else:
        _IDLE_FALLBACK["minutes"] = policy.minutes  # no ECS client (dev/test)
    return {"minutes": policy.minutes}


# Static SPA — mounted LAST so /api/* wins. html=True serves index.html at "/".
_STATIC = _HERE / "static"
if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
