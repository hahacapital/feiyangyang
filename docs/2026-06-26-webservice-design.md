# feiyangyang Web Service — Design Spec

**Date:** 2026-06-26
**Status:** Approved; deployment decisions locked (§11); implementation plan next
**Topic:** Wrap the existing `rebound.py` backup-finder engine in a personal web service
(input a ticker, pick a strategy, set params, run → ranked backups + an interactive equity curve),
deployed to AWS ECS Fargate.

This spec was written after verifying every third-party fact against current official docs
(FastAPI 0.138.1, uvicorn 0.49.0, sse-starlette 3.4.5, Apache ECharts 6.1.0, AWS ECS Fargate docs,
fetched 2026-06-26) and after four adversarial design reviews (engine-reuse correctness,
memory/performance realism, ECS deployment correctness, frontend distinctiveness/accessibility).
See **§13 Sources** for citations and **§12 What the research changed** for deltas vs the first
draft.

---

## 1. Goal

A single-user, no-auth web tool that exposes the `rebound` engine:

1. User enters a **primary ticker**, picks a **rule** (`price_above_ma` or `ma_cross`), sets its
   **params**, optionally tunes filters (max drawdown cap, min history, sort key, top-N, mode).
2. The server runs a **full-market backup scan** as a background job, streaming progress.
3. The browser shows the **ranked backups table**, a **live recommendation** ("what to do now"),
   and an **interactive equity/regime/drawdown chart** — replacing the CLI's static PNG.

## 2. Non-goals (YAGNI)

No authentication / multi-user isolation, no historical-run persistence (beyond optional result
caching for restart recovery), no user-uploaded candidate universes, no mobile app, no real-time
quotes (still the daily parquet snapshot). The engine (`rebound.py`, `data_loader.py`) is **not
modified in behavior** — only an additive `curve_series()` helper is added (§7).

## 3. Architecture

FastAPI monolith: one process serves the JSON API **and** the static single-page frontend. The
engine's pure functions are called directly over a **warm in-memory frame store**, bypassing
`scan()`'s per-request disk re-read.

```
src/
  rebound.py            # engine — UNCHANGED except additive curve_series() (§7)
  data_loader.py        # parquet cache reader — UNCHANGED
  webapp/
    __init__.py
    app.py              # FastAPI app: lifespan warmup, routes, static mount
    engine_service.py   # warm frame store + scan-over-warm-frames + result/curve assembly
    jobs.py             # in-memory job registry, threadpool runner, progress, cancellation
    schemas.py          # pydantic request/response models + server-side validation
    static/
      index.html
      styles.css
      app.js            # ECharts 6.1.0 + fetch/SSE client
      vendor/echarts.min.js
Dockerfile
deploy/
  task-definition.json  # parameterized Fargate task def
  iam/                  # task-role + execution-role policy JSON
  deploy.sh             # build → ECR push → register task def → update service
  README.md             # required inputs + step-by-step
requirements.txt        # + fastapi, uvicorn[standard], sse-starlette, boto3
src/test_webapp.py      # service-layer tests (synthetic in-memory frames; no cache needed)
```

**Import model.** `webapp/` must import the engine (`rebound`, `data_loader`) which live in `src/`.
The repo's convention is that `src/` is on `sys.path[0]` because scripts are run as
`python3 src/<file>.py`. For the web app we run uvicorn with `src/` as the import root:
`uvicorn webapp.app:app` launched with working dir `src/` (or `PYTHONPATH=src`). `app.py` also
inserts its parent dir on `sys.path` defensively so `from rebound import ...` resolves. The run
command is documented in README and baked into the Dockerfile `CMD`.

## 4. Engine-reuse contract (correctness-critical)

`scan()` does three things beyond loading frames that the web layer **must replicate** — adversarial
review flagged the first two as blockers:

1. **Exclude the requested primary from candidates.** `scan()` filters `[t for t in pool if t !=
   primary]` (`rebound.py:411,414`); `evaluate_all`/`evaluate_candidate` do **not**. `engine_service`
   must build `candidate_frames = {t: f for t, f in WARM.items() if t != primary_ticker}` before
   calling `evaluate_all`. Otherwise the primary ranks against itself and pollutes the table.

2. **Load the primary on demand.** The warm store holds only `list_universe(min_bars=756)`. A
   user's primary may have `<756` bars or be a futures `=` ticker not in that set, so the primary
   frame is loaded via `data_loader.load_ohlc(primary)` per request (not assumed warm). Surface a
   clean 404-style error if the primary is absent from the cache entirely.

3. **Build the rule-specific params dict exactly as `main()`.** `primary_held` is keyword-only
   `(*, ma=120, fast=5, slow=30)` and **silently ignores keys that don't apply** (verified:
   `primary_held(df,'ma_cross',ma=120)` does not raise — it uses default fast/slow). So the API
   must branch: `price_above_ma → {"ma": int}`, `ma_cross → {"fast": int, "slow": int}`, and must
   **reject a superset / foreign keys** rather than forward a merged dict. Validation:
   `ma >= 2`; `ma_cross` requires `2 <= fast < slow`.

**Other replicated conversions** (these live in `main()`, not in `rank_candidates`):
- `max_dd_cap = max_dd_percent / 100.0` (percent → fraction) or `None`.
- `min_history_bars = int(min_history_years * 252)`; **default `min_history = 5.0` years (1260
  bars)** to match the CLI's user-facing default — *not* `rank_candidates`' raw `0`.
- `min_overlap=252`, `health_ma=50` passed explicitly to match `scan()`.
- Validate `sort ∈ {antifragile, cagr, total_return, calmar, sharpe}` before calling
  `rank_candidates` (`SORT_KEYS[bad]` raises `KeyError`).

**Invariants preserved (verified by review).** No-lookahead (`primary_held`/`candidate_healthy` end
in `.shift(1, fill_value=False)`) is untouched by the warm path. Metrics stay fractions; the API
emits raw row dicts and the frontend formats to percent (mirroring `render_table`). Ranking stays on
`_n` keys with `antifragile = cagr_n·(1−corr_off)` default. **Concurrency is safe without copies:**
`daily_returns`, `candidate_healthy`, `reindex`, `stitch` were empirically verified non-mutating, so
the read-only warm frames can be shared across concurrent jobs.

## 5. Backend

### 5.1 Startup warmup (lifespan)

Use the **`lifespan` async context manager** (`@asynccontextmanager` + `FastAPI(lifespan=...)`);
`@app.on_event` is deprecated. Because the app does not serve requests until lifespan *returns*, the
heavy work runs on a **background thread** kicked off inside lifespan; a module-level state object
flips `warming → ready` and tracks `loaded/total`:

1. Pull the cache: `aws s3 sync`-equivalent via **boto3** (list+download under
   `s3://staking-ledger-bpt/jojo_quant/ohlc/` using the ECS container credential provider) into
   `data/ohlc/` (reuses `data_loader`'s on-disk layout). Raise S3 download concurrency, or fetch a
   single packed snapshot, to keep the ~10.8k-file sync within the warmup budget.
2. Read `_meta.parquet`, compute `list_universe(min_bars=756)`, load each frame into
   `WARM: dict[str, DataFrame]`. Measured: ~1.36 ms/file → ~9–11 s parse; the 1–3 min budget is
   dominated by S3 network transfer.
3. Set `READY`, record `server_epoch` (a boot id, §10.4) and `cache_date`.

### 5.2 Job model

In-memory `jobs.py`: a dict `job_id → JobState{status, done, total, result, error, cancel_event}`.
Scans run via `anyio.to_thread.run_sync` / `run_in_threadpool` (one worker process; **single scan at
a time** — a lock/queue, surfaced as `queued` in status). The blocking scan calls a thin generator
wrapper around `evaluate_all` that `yield`s `(ticker, row_or_None, error)` so the job can:
- update `done` and stream **batched** progress (every N≈50–100 candidates, not per-candidate —
  6500 per-candidate events in 30 s is wasteful),
- distinguish *skipped-thin* vs *errored* candidates (the engine currently swallows both),
- poll `cancel_event` each iteration so an abandoned job stops.

The GIL fear is empirically unfounded: a 14.2 s threadpool scan kept event-loop tick gaps at
mean 50.3 ms / p95 50.8 ms (pandas/NumPy release the GIL in their C routines; CPython's 5 ms switch
interval covers the Python glue). SSE and concurrent requests stay responsive. No process pool.

### 5.3 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Cheap liveness — 200 as soon as uvicorn is up (for ALB/container health check). |
| `GET` | `/api/status` | `{state: warming\|ready, loaded, total, cache_date, server_epoch}`. |
| `GET` | `/api/universe` | Sorted list of valid tickers (frontend validation / autocomplete). |
| `POST` | `/api/scan` | Validate body → create job → `{job_id, server_epoch}`; kicks off background scan. |
| `GET` | `/api/scan/{id}/events` | **SSE** (`sse-starlette` `EventSourceResponse`): `progress` events `{done,total}`, terminal `result` event, `error` event. Heartbeat ping (default 15 s); `id:`/Last-Event-ID for reconnect. |
| `GET` | `/api/scan/{id}/result` | Poll fallback for the full result payload; **410 + `{status:"unknown_job", server_epoch}`** for unknown/expired ids (so a post-restart UI re-runs instead of hanging). |
| `GET`/`POST` | `/api/idle-policy` | Read/set the idle auto-stop window in minutes (default off). Enabling it lets the idle timer call `ecs:UpdateService desiredCount=0` (§10.6). |
| `GET` | `/`, `/static/*` | SPA via `StaticFiles(directory=..., html=True)` mounted **after** all `/api` routes. |

### 5.4 Request / response schemas (pydantic)

`POST /api/scan` body:
```jsonc
{
  "ticker": "TSLA",                 // required; validated against /api/universe-or-cache
  "rule": "ma_cross",               // price_above_ma | ma_cross
  "ma": 120,                        // required iff price_above_ma; >=2
  "fast": 5, "slow": 30,            // required iff ma_cross; 2 <= fast < slow
  "mode": "naked",                  // naked | filtered (chart + recommendation; ranking always _n)
  "sort": "antifragile",            // antifragile|cagr|total_return|calmar|sharpe
  "max_dd": null,                   // percent or null; -> /100 internally
  "min_history": 5.0,               // years; -> *252 bars; default 5.0
  "top": 30,                        // table rows
  "top_k": 3,                       // curves drawn
  "cost_bps": 0.0
}
```
Server-side validation rejects foreign param keys per §4.3 and returns 422 with a clear message.

`result` payload:
```jsonc
{
  "server_epoch": "...",
  "as_of": "2026-06-24",
  "recommendation": { "state": "flat", "top": "GLD" },   // state: in-market|flat
  "baselines": { "buy_hold": {...fractions...}, "primary_cash": {...} },
  "ranked": [ { "ticker": "GLD", "n_bars": 3000, "off_frac": 0.41,
                "cagr_n": 0.18, "max_dd_n": 0.33, "corr_off": -0.12,
                "park_return": 0.22, "calmar_n": 0.5, "sharpe_n": 0.9,
                "cagr_f": ..., "max_dd_f": ..., "calmar_f": ..., "afscore": 0.20 } ],
  "curves": { /* §7 */ }
}
```
Metrics are fractions; the frontend formats to percent (`afscore` precomputed = `antifragile_score`).

## 6. Frontend — "STANDBY CONSOLE / 待命台"

**Subject thesis.** A quant's *standby instrument panel*: capital is either **LIVE** (riding the
primary) or **PARKED** (in a backup), and the tool picks the best thing to be parked in. A two-state
**semantic** signaling system organizes the whole page — color encodes real model state, not decor.

The first draft's "near-black + mint accent" risked the generic dark-dashboard default and **failed
colorblind accessibility**. The revised direction commits to a *literal instrument panel* and a
**blue/orange** signal pair (the canonical colorblind-safe duo), with redundant non-color channels.

### 6.1 Palette (verified WCAG; redundant encoding mandatory)

| Token | Hex | Role | Notes |
|---|---|---|---|
| `ink` | `#12161C` | base (warm near-black) | faint phosphor/brushed texture + engraved hairline labels to escape the SaaS-flat slab |
| `panel` | `#232B38` | raised panels | ≥1.6:1 delta vs ink so panels actually read |
| `edge` | `#38424F` | hairlines / engraved labels | |
| `live` | `#2BB8E6` | **IN-MARKET** signal (instrument cyan) | colder/bluer than web-mint; blue/orange is colorblind-safe vs amber |
| `standby` | `#F2A93B` | **PARKED** signal (amber) | |
| `cash` | `#8A93A3` | CASH / secondary text | passes AA on ink |
| `drawdown` | `#E8806B` | drawdown line/fill + small numerals | lightened from `#D9624B` so small text clears 4.5:1; deep clay reserved for line/fill (3:1) only |
| `paper` | `#ECEFF3` | primary text | 15.4:1 on ink |

**Colorblind blocker fix — redundant channels everywhere a signal appears** (hue alone is banned):
- **Regime ribbon:** IN-MARKET = solid cyan block · PARKED = amber block with diagonal hatch ·
  CASH = empty/grey dotted. Every segment carries a hover label.
- **Equity curve:** baselines vs picks differ by **line style** (solid / dashed / dotted) + weight,
  not color alone; rank-1 pick gets the heaviest stroke.
- **State plate:** always a text word (`IN-MARKET` / `PARKED → GLD` / `CASH`) + a lamp glyph; never
  color-only.

### 6.2 Typography

- **Display / state plate:** **Archivo Expanded** — instrument-panel signage authority (the hero
  deserves a face, not the plain free-grotesque default).
- **Data / numerals:** a characterful tabular monospace (e.g. **Spline Sans Mono**) — escapes the
  reflexive IBM-Plex-Mono "we look technical" default while keeping tabular alignment for the table.
- **Body / UI:** Archivo. (All faces verified available before build.)

### 6.3 Signature & layout

Signature = the **regime ribbon as the sole, load-bearing home of regime state** + the **state plate
that "powers on."** Per "remove one accessory," the **markArea background shading is cut from the
equity curve** (it duplicated the ribbon); the curve stays clean (rebased series + 2 baselines),
optionally tinting the *line* by regime rather than flooding the background.

```
┌───────────────────────────────────────────────────────┐
│ 沸羊羊 · STANDBY CONSOLE              [cache READY ●]   │
├──────────────┬────────────────────────────────────────┤
│ CONSOLE      │  STATE PLATE  (powers on)               │
│ ticker [▢]   │  ┌──────────────────────────────────┐   │
│ strategy[▾]  │  │ ⬤ PARKED → GLD                    │   │  hero = live verdict
│ params …     │  │ as of 2026-06-24                  │   │
│ [ RUN SCAN ] │  └──────────────────────────────────┘   │
│              │  afscore · corr_off · park_return       │
├──────────────┴────────────────────────────────────────┤
│ EQUITY (log, rebased≈1.0)            [naked|filtered]  │  ECharts grid panel 1 (log y)
│ ▓▓░░▓▓▓░░  ← REGIME RIBBON (cyan/amber-hatch/grey)     │  panel 2 (signature)
│ ╲__╱ rank-1 DRAWDOWN (clay, %)                         │  panel 3 (linear y)
├────────────────────────────────────────────────────────┤
│ BACKUPS (ranked)                                        │
│ #  TICKER  off%  afscore  cagr_n  maxdd_n  corr  park%  │  Spline Sans Mono table
└────────────────────────────────────────────────────────┘
```
Mobile: CONSOLE stacks above the state plate; table scrolls horizontally.

### 6.4 Motion & copy

Minimal, `prefers-reduced-motion` respected (gate ECharts `animation` on
`matchMedia('(prefers-reduced-motion: reduce)')`, re-apply on its `change` event). Only deliberate
moments: state plate filament-warm flicker on result arrival; equity line draws left→right with the
ribbon filling in sync; scan progress is an instrument readout `SCANNING 4,210 / 10,802` + thin bar
(real SSE), not a spinner. Aesthetic risk: the literal armed-relay indicator-lamp state plate +
phosphor-textured panel.

Copy (end-user voice, active): button `RUN SCAN`; `LIVE — holding TSLA` / `STANDBY — park in GLD` /
`CASH`; empty `Pick a ticker and run a scan.`; warming `Warming the cache — 4,210 / 8,000 loaded`;
errors `TSLA isn't in the cache. Try another symbol.`, `Service restarted — please re-run.`

## 7. `curve_series()` (added into `rebound.py`)

To keep the JSON and the PNG from drifting, add `curve_series(...)` **into `rebound.py` beside
`plot_curves`**, and have `plot_curves` delegate to it for its series math. It must mirror
`plot_curves` (`rebound.py:343–384`) exactly:

- **Shared top-k window** `common = held.index` then `∩` each of the `top_k` picks' frame index
  (`rebound.py:345–347`) — *not* `evaluate_candidate`'s pairwise window.
- Recompute on that window: `h = held.reindex(common).fillna(False)`,
  `pr = primary_ret.reindex(common).fillna(0.0)`; per pick `cr = daily_returns(frame).reindex(...)`,
  `ch = candidate_healthy(frame).reindex(...)`, `s = stitch(h, pr, cr, mode, cand_healthy=ch)`,
  `eq = equity_curve(s["ret"])`.
- **Emit `equity_curve` verbatim — no re-normalization.** The window rarely starts at a global first
  bar, so the rebased first point is genuinely ~1.0044, not 1.0 (verified). Do **not** divide by
  `series[0]` or prepend 1.0; `rebased≈1.0` is only an axis label.
- Series emitted (matching the PNG): `primary_buy_hold = equity_curve(pr)`,
  `primary_cash = equity_curve(pr.where(h, 0.0))`, `picks: [{ticker, mode, equity[]}]`, and
  `rank1_drawdown` = from **picks[0] only**, `dd = (eq/eq.cummax() − 1)·100` (percent, negative —
  distinct from the metrics' positive `max_drawdown` fraction).

**Payload shape (perf-tuned).** One **shared x-axis** `dates[]` (`YYYY-MM-DD` or epoch-day ints)
plus **value-only** parallel arrays per series (ECharts category axis indexes into shared dates — no
per-series `[x,y]` tuples). **Regime as run-length segments**, not a per-point boolean:
`regime: [{start, end, state}]` where `state ∈ {primary, candidate, cash}` for the rank-1 pick,
computed from `stitch()["active"]` via `(active != active.shift()).cumsum()` grouping. Measured
~195 KB uncompressed / ~49 KB gzipped at 2500 pts — **gzip the response; no downsampling**.

## 8. ECharts wiring (6.1.0)

Pin **ECharts 6.1.0** (vendored `static/vendor/echarts.min.js`; offline-safe in ECS). Three stacked
panels via a `grid` array sharing one time x-axis (each panel its own `xAxis`/`yAxis` bound by
`gridIndex`):
1. **Equity** — `yAxis.type:'log'` (proportional moves). **Never** put drawdown (≤0) on this axis;
   6.1 silently drops non-positive points on a log axis.
2. **Regime ribbon** — dedicated thin panel; 3-state piecewise series (cyan / amber-hatch / grey)
   driven by the run-length `regime` segments.
3. **Drawdown** — its own **linear** panel (clay), from `rank1_drawdown`.

Crosshair across panels: top-level `axisPointer.link:[{xAxisIndex:'all'}]` +
`tooltip:{trigger:'axis', axisPointer:{type:'cross'}}`. Zoom: `dataZoom` `inside` + `slider`, each
with `xAxisIndex:[0,1,2]` so all panels move together (slider in bottom-grid padding). Tree-shake or
vendor the full UMD build; CanvasRenderer (default). Flat-period emphasis on the curve uses
**line-color-by-regime**, not `markArea` (cut per §6.3).

## 9. Performance & sizing (measured)

- **Resident RAM ≈ 1 GB** for ~6500 warm frames (measured 602 MB RSS; ratio ~1.0 — data lives in
  NumPy blocks, no Python-object bloat). The first draft's "2–3 GB" was wrong; treat an OOM as a
  real leak. On-disk parquet for the full 10.8k cache is **<1 GB**.
- **Cold start 1–3 min**, network-bound (parse ~9–11 s).
- **Scan ≈ 4.58 ms/candidate → ~30 s** for 6500 single-process. Batch SSE progress.
- **Payload ~49 KB gzipped**; ECharts handles 2500 pts × ~6 series trivially.

## 10. Deployment (ECS Fargate, ap-northeast-1, cluster `ff`)

### 10.1 Dockerfile
`FROM python:3.12-slim` (all deps need ≥3.10), non-root `USER`, copy `requirements.txt` first for
layer caching, exec-form `CMD ["uvicorn","webapp.app:app","--host","0.0.0.0","--port","8080",
"--proxy-headers"]` (working dir `src/`). **Single worker** — no `--workers`, `WEB_CONCURRENCY`
unset (each worker would duplicate the ~1 GB cache + own its own job registry, breaking SSE
lookups). Do **not** use the deprecated `tiangolo/uvicorn-gunicorn` base image; do not add gunicorn.

### 10.2 Task definition (`deploy/task-definition.json`)
`requiresCompatibilities:["FARGATE"]`, `networkMode:"awsvpc"`, **`cpu:"2048"`, `memory:"8192"`**
(2 vCPU / 8 GB — a *valid* Fargate pair; `1–2 vCPU/8GB` as written is not registerable; 2 vCPU for
scan throughput, 8 GB is generous over ~1 GB resident). `runtimePlatform.cpuArchitecture` matching
the build (ARM64 or X86_64). Keep **default 20 GiB ephemeral storage** (cache <1 GB on disk).
`portMappings` 8080. `logConfiguration` `awslogs` → ap-northeast-1. Container `healthCheck` on
`/healthz` with `startPeriod` near its 300 s max.

### 10.3 IAM (two distinct roles)
- **executionRoleArn:** AWS-managed `AmazonECSTaskExecutionRolePolicy` (ECR pull + CloudWatch Logs).
- **taskRoleArn (app identity):** least-privilege inline policy —
  `s3:GetObject` on `arn:aws:s3:::staking-ledger-bpt/jojo_quant/ohlc/*`;
  `s3:ListBucket` (+ optional `s3:GetBucketLocation`) on `arn:aws:s3:::staking-ledger-bpt` with
  `Condition StringLike s3:prefix = jojo_quant/ohlc/*`; add `Condition aws:ResourceAccount =
  <account-id>` to block bucket-takeover. **App S3 access goes only on the task role.** For the idle
  auto-stop toggle (§10.6) the task role also needs `ecs:UpdateService` + `ecs:DescribeServices`
  scoped to this service's ARN only.

### 10.4 Slow-warmup survival & restart observability
- The service is fronted by the **existing ALB for `ff.theblueprint.xyz`**. The ALB target-group
  health check (and the container health check) point at **`/healthz`** (cheap liveness), *not*
  `/api/status` (which is 503 while warming), so the LB keeps a slow-warming task alive. Set service
  `healthCheckGracePeriodSeconds ≈ 600`. `startTimeout`/`stopTimeout` max out at 120 s and won't
  cover warmup — use `startPeriod` + grace period instead. Set `stopTimeout` generously so in-flight
  scans/SSE drain on SIGTERM (uvicorn graceful shutdown via exec-form CMD). The ECS service
  registers tasks into the target group via `loadBalancers` (`containerName`/`containerPort 8080`);
  the host-based listener rule for `ff.theblueprint.xyz` routes to that target group (HTTPS via the
  domain's ACM cert).
- **`server_epoch`** (boot id) is returned in `/api/status` and every `job_id`. On a task
  replacement (deploy, OOM, AZ event) in-memory jobs are lost and a fresh warmup runs; unknown ids
  return **410** so the UI shows "Service restarted — please re-run" instead of hanging. Single task:
  `minimumHealthyPercent:0, maximumPercent:100` (no rolling), brief downtime accepted.

### 10.5 deploy.sh & required inputs
`deploy.sh`: `docker build` → ECR login + push → `register-task-definition` → `update-service` (or
`create-service` first time). Parameterized via a `deploy/.env`; **inputs the user must supply:**
AWS account id, ECR repo URI, region (`ap-northeast-1`), cluster (`ff`), subnet ids,
security-group id (must allow the ALB's SG inbound on 8080), the two role ARNs, service name; and
for ingress — the **ALB target-group ARN for `ff.theblueprint.xyz`** (or the ALB listener ARN if we
add the host rule + a new target group), and the ACM cert / HTTPS listener already terminating the
domain. Image pull: private subnet + NAT or ECR/S3/logs VPC endpoints (the ALB faces the internet,
tasks need not be public). Unverifiable from docs and to confirm before deploy: cluster `ff` and the
`ff.theblueprint.xyz` ALB/listener/target-group exist in ap-northeast-1, bucket name, account id.

### 10.6 Cost (decided — §11)
24/7 **2 vCPU / 8 GB ≈ ~$85/mo** in Tokyo + the existing ALB. **Decision: always-on
`desiredCount=1`** (matches the "keep warm" choice) — and additionally **ship an idle auto-stop
toggle** (default **off**): an in-app idle timer (resets on each `/api/*` request) that, once
enabled via `POST /api/idle-policy {minutes}`, calls `ecs:UpdateService desiredCount=0` after N idle
minutes, dropping to ~$5–10/mo at the cost of a 1–3 min cold start next session.

**Honest limitation — the app can stop itself but cannot wake itself.** Once `desiredCount=0` there
is no running task, so the ALB returns 503 (no healthy targets) and the SPA can't be served. Waking
is therefore out-of-band: ship `deploy/wake.sh` (`aws ecs update-service --desired-count 1`) and
document an optional one-click path (a tiny always-on Lambda behind a dedicated ALB listener
rule/path that flips desired to 1). The idle timer only ever performs the **stop** (it is up when it
fires). `server_epoch` (§10.4) makes the subsequent cold restart observable so the UI re-runs
cleanly. Fargate Spot (~70 % off) remains an available knob since jobs are ephemeral, not enabled by
default.

This requires the **task role** to also hold `ecs:UpdateService` + `ecs:DescribeServices` scoped to
this one service ARN (§10.3) and `POST /api/idle-policy` / `GET /api/idle-policy` endpoints (§5.3).

## 11. Deployment decisions (resolved)

1. **Cost mode → always-on `desiredCount=1`, plus an idle auto-stop toggle** the user can flip on to
   reach scale-to-zero economics (§10.6).
2. **Ingress → the existing ALB for `ff.theblueprint.xyz`** (stable DNS/TLS); the ECS service
   registers into its target group, health check on `/healthz`, grace period ≈600 s (§10.4).
3. **Task size → 2 vCPU / 8 GB** (`cpu=2048, memory=8192`); revisit 4 vCPU / 16 GB only if 30 s
   full-market scans feel slow (§10.2).

## 12. What the research changed (vs first draft)

- RAM **2–3 GB → ~1 GB** resident (measured) → task memory is generous, not tight; OOM = a leak.
- Task size **"1–2 vCPU/8GB" (invalid) → 2 vCPU / 8 GB** (registerable pair).
- GIL/event-loop stall fear **disproven** → simple threadpool, no process pool / chunked yields.
- SSE **bare StreamingResponse → sse-starlette `EventSourceResponse`** (heartbeat, reconnect).
- Warmup **on_event → lifespan** (+ background thread so `/api/status` stays responsive).
- Palette **mint-teal+amber (colorblind blocker, 1.02:1) → blue/orange + redundant channels**;
  commit to the literal instrument-panel metaphor; panel/ink delta raised; clay lightened for text.
- **Cut markArea shading** — regime ribbon becomes the sole signature; curve stays clean.
- Type **Archivo+IBM Plex Mono → Archivo Expanded (hero) + Spline Sans Mono (data)**.
- Payload **per-point held mask → run-length regime segments + shared dates axis + gzip**.
- Deployment hardening: `/healthz` vs `/api/status`, `server_epoch` restart recovery, two-role IAM
  scoping, default 20 GiB ephemeral, **scale-to-zero cost option**.
- Engine-reuse **blockers surfaced:** exclude primary from candidates, load primary on demand,
  strict rule-specific params (silent-ignore bug), replicate CLI unit conversions, add
  `curve_series()` into `rebound.py` so JSON/PNG can't drift.

## 13. Testing

`src/test_webapp.py` — synthetic in-memory frames (no cache needed, matching `test_rebound.py`'s
philosophy), via FastAPI `TestClient`:
- param validation: rule-specific params, foreign-key rejection, `fast<slow`, `ma>=2`, bad sort →
  422; unit conversions (`max_dd/100`, `years*252`).
- engine-service: primary excluded from candidates; primary loaded on demand; `_n` ranking + default
  `antifragile` sort preserved.
- `curve_series`: shared-window length, **no re-normalization** (first point ≠ forced 1.0),
  `rank1_drawdown` from picks[0], regime run-length segments tile the window with no gaps/overlaps.
- job lifecycle: submit → batched progress → result; cancellation stops the loop; unknown id → 410.
The engine's 18 tests stay green and untouched.

## 14. Documentation updates (doc-sync rule)

Same-change updates to **README.md** (run the web server, endpoints, Docker build/run, ECS deploy
steps + required inputs) and **CLAUDE.md** (new `webapp/` module map, `curve_series` addition, the
run-as-`uvicorn webapp.app:app` import note, single-worker/warm-cache gotcha).

## 15. Sources (fetched 2026-06-26)

- FastAPI lifespan / events: https://fastapi.tiangolo.com/advanced/events/
- FastAPI async & threadpool: https://fastapi.tiangolo.com/async/
- FastAPI StaticFiles: https://fastapi.tiangolo.com/reference/staticfiles/
- FastAPI deployment (docker / workers / memory-per-process):
  https://fastapi.tiangolo.com/deployment/docker/ · /deployment/concepts/ · /deployment/server-workers/
- sse-starlette: https://github.com/sysid/sse-starlette (3.4.5)
- Pins (PyPI): fastapi 0.138.1, uvicorn 0.49.0, sse-starlette 3.4.5 (Python ≥3.10; let starlette
  resolve ≥0.49.1 for sse-starlette).
- ECharts 6.1.0: changelog https://echarts.apache.org/en/changelog.html · option ref
  https://echarts.apache.org/en/option.html · import handbook
  https://echarts.apache.org/handbook/en/basics/import/
- ECS task def params: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html
- Fargate sizing/storage: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html
  · /fargate-task-storage.html
- ECS IAM roles: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html
  · /task_execution_IAM_role.html
- ECS health-check grace period:
  https://docs.aws.amazon.com/help-panel/AmazonECS/latest/console/hp-service-update-loadbalancing-healthcheckgraceperiod.html
