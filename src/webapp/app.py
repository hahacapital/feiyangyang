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
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
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
}


def _warmup_prod() -> None:
    """S3 sync -> disk -> warm store. Runs on a background thread."""
    from webapp import cache_sync
    import data_loader as dl
    try:
        cache_sync.sync_cache()
        tickers = dl.list_universe(min_bars=756)
        frames = {}
        for i, t in enumerate(tickers, 1):
            try:
                frames[t] = dl.load_ohlc(t)
            except Exception:
                continue
            STATE["loaded"] = i
            STATE["total"] = len(tickers)
        engine_service.warm_load(frames)
        manifest = dl.read_manifest()
        STATE["cache_date"] = manifest.get("last_update")
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
    yield


app = FastAPI(title="feiyangyang · STANDBY CONSOLE", lifespan=lifespan)
# Compress JSON responses (the ~49 KB curve payload from /api/scan/{id}/result).
# Tiny SSE progress events are below minimum_size, so the live stream is unaffected.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/healthz")
def healthz() -> Response:
    return Response(content="ok", media_type="text/plain")


@app.get("/api/status")
def status() -> dict:
    return {"state": "ready" if STATE["ready"] else "warming",
            "loaded": STATE["loaded"], "total": STATE["total"],
            "cache_date": STATE["cache_date"], "server_epoch": STATE["server_epoch"]}


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


@app.get("/api/scan/{job_id}/events")
async def scan_events(job_id: str):
    async def gen():
        job = jobs.get(job_id)
        if job is None or not jobs.belongs_to_epoch(job_id, STATE["server_epoch"]):
            yield {"event": "error", "data": json.dumps(
                {"status": "unknown_job", "server_epoch": STATE["server_epoch"]})}
            return
        last = -1
        while True:
            if job.done != last:
                last = job.done
                yield {"event": "progress",
                       "data": json.dumps({"done": job.done, "total": job.total})}
            if job.status in {"done", "error", "cancelled"}:
                # Signal completion only; the SPA fetches the (gzip-eligible) full
                # result from GET /api/scan/{id}/result so the ~49 KB curve payload
                # is compressed and the SSE stream stays small.
                yield {"event": "result",
                       "data": json.dumps({"status": job.status, "error": job.error})}
                return
            await asyncio.sleep(0.5)
    return EventSourceResponse(gen())


# Static SPA — mounted LAST so /api/* wins. html=True serves index.html at "/".
_STATIC = _HERE / "static"
if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
