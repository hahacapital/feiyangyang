# feiyangyang (沸羊羊) — backup-asset finder

Given a primary stock and a hold/flat moving-average rule, **feiyangyang** ranks
candidate "parking" assets to deploy capital into while the primary is flat, and
plots the stitched equity curve. It answers: *when my strategy is out of the
market, where should the cash sit to maximize return with controlled drawdown —
and which backup is genuinely uncorrelated with the primary (antifragile)?*

A daily-return-stitching engine: the portfolio earns the primary's return while a
hold rule is on, and a candidate's (or cash) return while it is off; metrics are
computed on the stitched curve and candidates are ranked. All signals are
lag-correct (decided on `close[t-1]`), so there is **no look-ahead bias**.

This tool was extracted from the `jojo_quant` project; it shares that project's
OHLC cache (see [Data](#data)) but has no other dependency on it.

## Install

```bash
pip install -r requirements.txt   # pandas, numpy, pyarrow, matplotlib
```

## Data

feiyangyang reads a local OHLC parquet cache at `data/ohlc/` (one file per
ticker). The cache is **produced** by the `jojo_quant` project (NASDAQ + NYSE +
commodity futures, refreshed daily) and backed up to S3. feiyangyang is a
read-only **consumer** — pull the latest snapshot from S3:

```bash
bash scripts/pull_cache.sh        # aws s3 sync s3://staking-ledger-bpt/jojo_quant/ohlc/ -> data/ohlc/
```

Requires the AWS CLI with read access to the bucket. `data/` is gitignored.

## Usage

```bash
# Hold while close > MA120; rank backups for the flat periods
python3 src/rebound.py TSLA --rule price_above_ma --ma 120

# Hold while MA5 > MA30
python3 src/rebound.py TSLA --rule ma_cross --fast 5 --slow 30

# Restrict to an investable candidate pool (recommended for short-history primaries)
python3 src/rebound.py HOOD --rule ma_cross --fast 5 --slow 30 \
    --candidates AZO,ORLY,GLD,GC=F,LLY,UNH,KO,PG --min-history 3
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--rule` | (required) | `price_above_ma` or `ma_cross` |
| `--ma` / `--fast` / `--slow` | 120 / 5 / 30 | MA windows |
| `--max-dd` | none | Optional cap: drop candidates whose naked combined max drawdown exceeds this % (default: no cap) |
| `--min-history` | 5 | Drop candidates with less than this many years of common history with the primary |
| `--sort` | `antifragile` | `antifragile` \| `cagr` \| `total_return` \| `calmar` \| `sharpe` |
| `--top` / `--top-k` | 30 / 3 | Table rows / curves overlaid on the plot |
| `--plot-mode` | `naked` | `naked` \| `filtered` (candidate held only when close > MA50, else cash) |
| `--candidates` | — | Comma-separated pool (else the full cache) |
| `--cost-bps` | 0 | One-way cost charged per switch day |
| `--out` | `data/plots/...png` | Equity/drawdown PNG path |
| `--limit` | 0 | First-N candidates (smoke test) |

### Ranking — the antifragile score

The default sort is **`antifragile` = `cagr_n × (1 − corr_off)`**: annualized naked
return scaled by diversification, where `corr_off` is the candidate's return
correlation with the primary over the primary's *off* days. A backup that merely
co-moves with the primary (high `corr_off`) is demoted below a true diversifier —
e.g. for TSLA `ma5>ma30`, AZO/ORLY rank above AMZN despite AMZN's higher raw CAGR.

`--min-history` filters short-history overfit-prone names; `--max-dd` is an
optional drawdown cap (off by default — the naked combined curve inherits the
**primary's own** in-market drawdown, which a backup cannot remove).

## Output

Three things, no report files:

1. **Ranking table** (stdout) — per-candidate naked + filtered metrics, the
   antifragility reads `off_frac` / `park_return` / `corr_off`, and the `afscore`
   ranking value, under the two baselines (primary Buy&Hold, primary + cash).
2. **Current recommendation** (one line) — hold the primary today, or which backup
   to park in if it is flat.
3. **Equity/drawdown PNG** (matplotlib, headless `Agg`) — baselines + top-k combined
   curves, with the rank-1 drawdown panel below.

> Caveat: with a full-market pool the rank-1 backup may simply be a name that
> trended up over the window — read it alongside `corr_off` / `park_return` /
> window length, not in isolation. For short-history primaries, prefer a curated
> `--candidates` pool of investable names.

## Web service (STANDBY CONSOLE)

A personal FastAPI app over the same engine: enter a ticker, pick a strategy, set
params, run a background full-market scan with live progress, and read the ranked
backups plus an interactive ECharts equity/regime/drawdown chart.

### Run locally

Requires **Python ≥ 3.10** (FastAPI 0.138.1 / uvicorn 0.49.0 / sse-starlette 3.4.5).

```bash
pip install -r requirements.txt
# Offline demo (synthetic data, no cache/S3 needed):
cd src && FEIYANG_DEV_FIXTURE=1 python3 -m uvicorn webapp.app:app --port 8000
# Real data (needs the OHLC cache; pulled from S3 at startup, or pre-pull):
bash scripts/pull_cache.sh
cd src && python3 -m uvicorn webapp.app:app --port 8000
```

Open http://localhost:8000. Run the server with `src/` as the working dir (bare
imports), and **a single worker only** — the ~1 GB warm cache and the in-memory
job registry must live in one process.

### Endpoints

`GET /healthz` · `GET /api/status` · `GET /api/universe` · `POST /api/scan` →
`{job_id}` · `GET /api/scan/{id}/events` (SSE progress + result) ·
`GET /api/scan/{id}/result` (poll) · `GET|POST /api/idle-policy`.

### Tests

Requires **Python ≥ 3.10** and both requirements files:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 src/test_webapp.py   # web layer (needs the deps; no cache)
python3 src/test_rebound.py  # engine (no deps beyond pandas/numpy)
```

### Deploy (ECS Fargate, ap-northeast-1, cluster `ff`, ALB ff.theblueprint.xyz)

See `deploy/README.md`. Container syncs the cache from S3 at startup, warms ~1 GB
into RAM (1-3 min cold start), runs 2 vCPU / 8 GB always-on; an opt-in idle
auto-stop toggle drops it to `desiredCount=0` to save cost (wake via
`deploy/wake.sh`).

## Tests

```bash
python3 src/test_rebound.py   # 18 assert-based cases, incl. an explicit no-look-ahead test
```

Tests use synthetic in-memory data and need no cache.

## Layout

| Path | Description |
|------|-------------|
| `src/rebound.py` | The finder: signal layer, daily-return stitching, metrics, ranking, table, plot, CLI |
| `src/data_loader.py` | Read-only OHLC parquet cache reader |
| `src/test_rebound.py` | Assert-based tests (no pytest) |
| `src/webapp/` | FastAPI web service (routes, engine service, job registry, cache sync, idle auto-stop, ECharts SPA) |
| `Dockerfile` | Single-worker container image for ECS Fargate deployment |
| `deploy/` | ECS task definition, IAM policies, deploy/wake scripts |
| `scripts/pull_cache.sh` | Sync the OHLC cache from S3 into `data/ohlc/` |
| `docs/` | Design spec + implementation plan |
| `data/` | OHLC cache + generated plots (gitignored) |

See `docs/2026-06-25-rebound-design.md` for the full design.
