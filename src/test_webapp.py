"""Assert-based tests for the webapp layer (run: python3 src/test_webapp.py).

Mirrors test_rebound.py's hand-rolled runner. Needs the deps from requirements.txt
but NO OHLC cache (synthetic in-memory frames + injected primary loader)."""

import sys

import pandas as pd
from pydantic import ValidationError

from webapp.schemas import ScanRequest


def make_df(closes, start="2018-01-01"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    c = pd.Series(closes, dtype=float).values
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=dates)


def test_scan_request_ma_cross_ok():
    r = ScanRequest(ticker="TSLA", rule="ma_cross", fast=5, slow=30)
    assert r.params() == {"fast": 5, "slow": 30}
    assert r.mode == "naked" and r.sort == "antifragile" and r.min_history == 5.0


def test_scan_request_price_above_ma_ok():
    r = ScanRequest(ticker="NVDA", rule="price_above_ma", ma=120)
    assert r.params() == {"ma": 120}


def test_scan_request_rejects_foreign_keys():
    # ma_cross must not also carry ma (silent-ignore bug guard)
    try:
        ScanRequest(ticker="X", rule="ma_cross", fast=5, slow=30, ma=120)
        assert False, "expected ValidationError for foreign key 'ma'"
    except ValidationError:
        pass
    # price_above_ma must not carry fast/slow
    try:
        ScanRequest(ticker="X", rule="price_above_ma", ma=120, fast=5)
        assert False, "expected ValidationError for foreign key 'fast'"
    except ValidationError:
        pass


def test_scan_request_validation_rules():
    for bad in (
        dict(ticker="X", rule="ma_cross", fast=30, slow=5),   # fast !< slow
        dict(ticker="X", rule="ma_cross", fast=1, slow=30),   # fast < 2
        dict(ticker="X", rule="price_above_ma", ma=1),        # ma < 2
        dict(ticker="X", rule="bogus", ma=10),                # unknown rule
        dict(ticker="X", rule="ma_cross", fast=5, slow=30, sort="nope"),
        dict(ticker="X", rule="ma_cross", fast=5, slow=30, mode="weird"),
    ):
        try:
            ScanRequest(**bad)
            assert False, f"expected ValidationError for {bad}"
        except ValidationError:
            pass


def test_scan_request_conversions():
    r = ScanRequest(ticker="X", rule="ma_cross", fast=5, slow=30, max_dd=25, min_history=3)
    assert abs(r.max_dd_cap() - 0.25) < 1e-12
    assert r.min_history_bars() == int(3 * 252)
    assert ScanRequest(ticker="X", rule="ma_cross", fast=5, slow=30).max_dd_cap() is None


def _fixture():
    import webapp.engine_service as es
    primary = make_df(list(range(1, 400)))                       # long uptrend
    frames = {
        "GLD": make_df([100 + (i % 7) for i in range(400)]),     # choppy, low corr
        "QQQ": make_df([50 + i * 0.5 for i in range(400)]),      # trending
        "SHORT": make_df([1, 2, 3]),                             # thin -> skipped
        "TSLA": make_df(list(range(1, 400))),                    # == primary (must be excluded)
    }
    es.warm_load(frames)
    es.set_primary_loader(lambda t: primary if t == "TSLA" else (_ for _ in ()).throw(
        es.PrimaryNotFound(t)))
    return es


def test_run_scan_excludes_primary_and_loads_on_demand():
    es = _fixture()
    # min_history=0 so the 399-bar fixture candidates survive the history filter
    # (the 5.0-year default = 1260 bars would drop them all -> empty ranked).
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, top=10, top_k=2, min_history=0)
    out = es.run_scan(req)
    tickers = {r["ticker"] for r in out["ranked"]}
    assert "TSLA" not in tickers, "primary must be excluded from candidates"
    assert tickers <= {"GLD", "QQQ"}, "thin/primary candidates dropped"
    assert out["recommendation"]["state"] in {"in-market", "flat"}
    assert "afscore" in out["ranked"][0]                         # afscore attached for the table


def test_run_scan_primary_not_in_cache():
    es = _fixture()
    req = ScanRequest(ticker="NOPE", rule="price_above_ma", ma=20)
    try:
        es.run_scan(req)
        assert False, "expected PrimaryNotFound"
    except es.PrimaryNotFound:
        pass


def test_run_scan_filenotfound_becomes_primary_not_found():
    import webapp.engine_service as es
    es.warm_load({"GLD": make_df([100 + i for i in range(400)])})
    es.set_primary_loader(lambda t: (_ for _ in ()).throw(FileNotFoundError(t)))
    req = ScanRequest(ticker="MISSING", rule="price_above_ma", ma=20)
    try:
        es.run_scan(req)
        assert False, "expected PrimaryNotFound"
    except es.PrimaryNotFound:
        pass


def test_run_scan_progress_and_curves():
    es = _fixture()
    seen = []
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, top_k=2, min_history=0)
    out = es.run_scan(req, progress_cb=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1]                   # final progress hits 100%
    cs = out["curves"]
    assert cs["dates"] and len(cs["primary_buy_hold"]) == len(cs["dates"])
    assert all(s["state"] in {"primary", "candidate", "cash"} for s in cs["regime"])


def test_run_scan_cancellation():
    import threading
    es = _fixture()
    ev = threading.Event(); ev.set()                            # already cancelled
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20)
    try:
        es.run_scan(req, cancel_event=ev)
        assert False, "expected ScanCancelled"
    except es.ScanCancelled:
        pass


def test_dev_fixture_loads():
    import webapp.engine_service as es
    es.load_dev_fixture()
    assert es.warm_count() >= 3 and "SPY" in es.universe()
    out = es.run_scan(ScanRequest(ticker="DEMO", rule="ma_cross", fast=5, slow=20, top_k=2))
    assert out["curves"]["dates"]


def test_jobs_lifecycle():
    import time
    import webapp.engine_service as es
    import webapp.jobs as jobs
    _fixture()
    jid = jobs.submit(ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, top_k=2), "ep1")
    assert jid.startswith("ep1-")
    for _ in range(200):
        if jobs.get(jid).status in {"done", "error"}:
            break
        time.sleep(0.02)
    job = jobs.get(jid)
    assert job.status == "done", f"got {job.status}: {job.error}"
    assert job.total >= 1 and job.done == job.total
    assert job.result["curves"]["dates"]


def test_jobs_unknown_and_epoch():
    import webapp.jobs as jobs
    assert jobs.get("nope") is None
    assert jobs.belongs_to_epoch("ep1-abcd1234", "ep1") is True
    assert jobs.belongs_to_epoch("ep1-abcd1234", "ep2") is False


def test_jobs_error_surfaces():
    import time
    import webapp.jobs as jobs
    _fixture()
    jid = jobs.submit(ScanRequest(ticker="NOPE", rule="price_above_ma", ma=20), "ep1")
    for _ in range(200):
        if jobs.get(jid).status in {"done", "error"}:
            break
        time.sleep(0.02)
    job = jobs.get(jid)
    assert job.status == "error" and "NOPE" in (job.error or "")


def _client(dev_fixture=True):
    import os
    os.environ["FEIYANG_SKIP_WARMUP"] = "1"
    if dev_fixture:
        import webapp.engine_service as es
        es.load_dev_fixture()
    from fastapi.testclient import TestClient
    from webapp.app import app, STATE
    STATE["ready"] = True
    return TestClient(app)


def test_http_status_and_universe():
    with _client() as c:
        assert c.get("/healthz").status_code == 200
        s = c.get("/api/status").json()
        assert s["state"] == "ready" and "server_epoch" in s
        u = c.get("/api/universe").json()
        assert "SPY" in u["tickers"]


def test_http_scan_validation_422():
    with _client() as c:
        r = c.post("/api/scan", json={"ticker": "DEMO", "rule": "ma_cross",
                                      "fast": 30, "slow": 5})
        assert r.status_code == 422


def test_http_scan_poll_result():
    import time
    with _client() as c:
        r = c.post("/api/scan", json={"ticker": "DEMO", "rule": "ma_cross",
                                      "fast": 5, "slow": 20, "top_k": 2})
        assert r.status_code == 200
        jid = r.json()["job_id"]
        out = None
        for _ in range(200):
            rr = c.get(f"/api/scan/{jid}/result")
            if rr.status_code == 200 and rr.json().get("status") == "done":
                out = rr.json()["result"]; break
            time.sleep(0.02)
        assert out and out["curves"]["dates"]


def test_http_unknown_job_410():
    with _client() as c:
        r = c.get("/api/scan/ZZZ-deadbeef/result")
        assert r.status_code == 410
        assert r.json()["status"] == "unknown_job"


def test_local_path_for():
    from webapp.cache_sync import local_path_for
    p = local_path_for("jojo_quant/ohlc/", "jojo_quant/ohlc/stocks/AAPL.parquet", "/tmp/ohlc")
    assert p == "/tmp/ohlc/stocks/AAPL.parquet"


def test_sync_cache_with_fake_client(tmp="/tmp/feiyang_synctest"):
    import os, shutil
    shutil.rmtree(tmp, ignore_errors=True)
    from webapp import cache_sync

    class FakeClient:
        def get_paginator(self, _name):
            class P:
                def paginate(self, **_kw):
                    yield {"Contents": [
                        {"Key": "jojo_quant/ohlc/stocks/AAPL.parquet", "Size": 3},
                        {"Key": "jojo_quant/ohlc/_meta.parquet", "Size": 5},
                        {"Key": "jojo_quant/ohlc/", "Size": 0},          # the prefix "folder" -> skipped
                    ]}
            return P()

        def download_file(self, Bucket, Key, Filename):
            os.makedirs(os.path.dirname(Filename), exist_ok=True)
            with open(Filename, "w") as f:
                f.write("x")

    n = cache_sync.sync_cache(dest_dir=tmp, client=FakeClient())
    assert n == 2
    assert os.path.exists(f"{tmp}/stocks/AAPL.parquet")
    assert os.path.exists(f"{tmp}/_meta.parquet")
    shutil.rmtree(tmp, ignore_errors=True)


def test_idle_monitor_logic():
    from webapp.idle import IdleMonitor
    clock = {"t": 1000.0}

    class FakeEcs:
        def __init__(self): self.calls = []
        def update_service(self, **kw): self.calls.append(kw)

    ecs = FakeEcs()
    m = IdleMonitor(ecs, "ff", "feiyang", now=lambda: clock["t"])
    assert m.should_stop() is False                 # disabled by default
    m.set_minutes(5)
    m.touch()
    clock["t"] += 4 * 60
    assert m.should_stop() is False                 # 4 < 5 min idle
    clock["t"] += 2 * 60
    assert m.should_stop() is True                  # 6 >= 5 min idle
    assert m.maybe_stop() is True
    assert ecs.calls[0]["desiredCount"] == 0
    assert ecs.calls[0]["cluster"] == "ff" and ecs.calls[0]["service"] == "feiyang"


def test_idle_policy_endpoints():
    with _client() as c:
        assert c.get("/api/idle-policy").json()["minutes"] is None
        c.post("/api/idle-policy", json={"minutes": 30})
        assert c.get("/api/idle-policy").json()["minutes"] == 30


def test_single_curve_engine():
    es = _fixture()
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, mode="naked", min_history=0)
    cs = es.single_curve(req, "GLD")
    assert cs["dates"] and len(cs["picks"]) == 1 and cs["picks"][0]["ticker"] == "GLD"
    try:
        es.single_curve(req, "ZZZZ")
        assert False, "expected CandidateNotFound"
    except es.CandidateNotFound:
        pass


def test_http_scan_curve():
    import time
    with _client() as c:
        r = c.post("/api/scan", json={"ticker": "DEMO", "rule": "ma_cross",
                                      "fast": 5, "slow": 20, "top_k": 2, "min_history": 0})
        jid = r.json()["job_id"]
        for _ in range(200):
            if c.get(f"/api/scan/{jid}/result").json().get("status") == "done":
                break
            time.sleep(0.02)
        cv = c.get(f"/api/scan/{jid}/curve", params={"ticker": "GLD"})
        assert cv.status_code == 200
        assert cv.json()["curves"]["picks"][0]["ticker"] == "GLD"
        assert c.get("/api/scan/ZZZ-deadbeef/curve", params={"ticker": "GLD"}).status_code == 410


if __name__ == "__main__":
    tests = [
        ("ScanRequest ma_cross", test_scan_request_ma_cross_ok),
        ("ScanRequest price_above_ma", test_scan_request_price_above_ma_ok),
        ("ScanRequest foreign keys", test_scan_request_rejects_foreign_keys),
        ("ScanRequest validation", test_scan_request_validation_rules),
        ("ScanRequest conversions", test_scan_request_conversions),
        ("run_scan excludes primary", test_run_scan_excludes_primary_and_loads_on_demand),
        ("run_scan primary missing", test_run_scan_primary_not_in_cache),
        ("run_scan FileNotFound->PrimaryNotFound", test_run_scan_filenotfound_becomes_primary_not_found),
        ("run_scan progress/curves", test_run_scan_progress_and_curves),
        ("run_scan cancellation", test_run_scan_cancellation),
        ("dev fixture", test_dev_fixture_loads),
        ("jobs lifecycle", test_jobs_lifecycle),
        ("jobs unknown/epoch", test_jobs_unknown_and_epoch),
        ("jobs error surfaces", test_jobs_error_surfaces),
        ("http status/universe", test_http_status_and_universe),
        ("http scan 422", test_http_scan_validation_422),
        ("http scan poll result", test_http_scan_poll_result),
        ("http unknown job 410", test_http_unknown_job_410),
        ("cache local_path_for", test_local_path_for),
        ("cache sync fake client", test_sync_cache_with_fake_client),
        ("idle monitor logic", test_idle_monitor_logic),
        ("idle policy endpoints", test_idle_policy_endpoints),
        ("single_curve engine", test_single_curve_engine),
        ("http scan curve", test_http_scan_curve),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn(); print(f"  PASS: {name}"); passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}"); failed += 1
    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
