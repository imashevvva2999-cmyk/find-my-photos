"""Load test against an ISOLATED copy of the site (never the real site or real photos).

A separate web server (port 8100) and worker run with their own database
(findmyphotos_load) and a temporary data folder, on the project's local PostgreSQL.
Data: synthetic collections from tests/load/make_dataset.py (permitted test photos).

    .venv/bin/python tests/load/run_load.py                     # defaults below
    .venv/bin/python tests/load/run_load.py --levels 1 10 --duration 30

Measures: upload + indexing time, search latency (p50/p95/p99) at several numbers of
simultaneous visitors, searches while indexing, error rate, CPU and memory.
Results: test_data/load/results.json and test_data/load/results.md
"""
import argparse
import asyncio
import json
import os
import platform
import random
import re
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import psutil
import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.passwords import hash_password  # noqa: E402

PORT = 8100
BASE = f"http://127.0.0.1:{PORT}"
LOAD_DB = "findmyphotos_load"
OUT = ROOT / "test_data" / "load"
SELFIES = [ROOT / "test_data/search/search2_koch_2018_portrait.jpg", ROOT / "test_data/search/search1_exact_copy.jpg"] + \
          [ROOT / f"test_data/collection_40/photo_{n}.jpg" for n in ("01", "02", "11", "12", "18", "22", "23", "29", "32", "35", "39")]


def env_file() -> dict:
    return dict(line.split("=", 1) for line in (ROOT / ".env").read_text().split() if "=" in line)


def hardware() -> dict:
    def sysctl(key):
        return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True).stdout.strip()
    return {"cpu": sysctl("machdep.cpu.brand_string"), "cores": int(sysctl("hw.ncpu")),
            "performance_cores": int(sysctl("hw.perflevel0.physicalcpu") or 0),
            "memory_gb": round(int(sysctl("hw.memsize")) / 2**30), "macos": platform.mac_ver()[0],
            "python": platform.python_version()}


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


class Sampler(threading.Thread):
    """CPU% and memory of the web server, the worker and PostgreSQL, once per second."""

    def __init__(self, procs: dict[str, psutil.Process]):
        super().__init__(daemon=True)
        self.procs = procs
        self.samples: list[dict] = []
        self.stop_flag = threading.Event()
        for p in self._all():
            try:
                p.cpu_percent(None)
            except psutil.Error:  # short-lived PostgreSQL helper processes may already be gone
                pass

    def _all(self):
        for name, proc in self.procs.items():
            group = [proc] + (proc.children(recursive=True) if name == "postgres" else [])
            for p in group:
                yield p

    def run(self):
        while not self.stop_flag.wait(1.0):
            row = {"t": time.time()}
            for name, proc in self.procs.items():
                group = [proc] + (proc.children(recursive=True) if name == "postgres" else [])
                cpu = mem = 0.0
                for p in group:
                    try:
                        cpu += p.cpu_percent(None)
                        mem += p.memory_info().rss
                    except psutil.Error:
                        pass
                row[name] = {"cpu": cpu, "rss_mb": mem / 2**20}
            vm = psutil.virtual_memory()
            row["system"] = {"available_gb": vm.available / 2**30, "swap_gb": psutil.swap_memory().used / 2**30}
            self.samples.append(row)

    def window(self, start: float, end: float) -> dict:
        rows = [r for r in self.samples if start <= r["t"] <= end]
        out = {}
        for name in self.procs:
            cpus = [r[name]["cpu"] for r in rows]
            mems = [r[name]["rss_mb"] for r in rows]
            out[name] = {"cpu_avg_pct": round(statistics.mean(cpus)) if cpus else None,
                         "cpu_max_pct": round(max(cpus)) if cpus else None,
                         "rss_max_mb": round(max(mems)) if mems else None}
        avail = [r["system"]["available_gb"] for r in rows]
        swap = [r["system"]["swap_gb"] for r in rows]
        out["system"] = {"available_min_gb": round(min(avail), 2) if avail else None,
                         "swap_max_gb": round(max(swap), 1) if swap else None}
        return out


def sleep_events(start: float, end: float) -> list[str]:
    """Mac sleep/wake entries from the power log between start and end (a sleeping Mac invalidates a run)."""
    out = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True).stdout.splitlines()
    t0, t1 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end))
    return [line[:120] for line in out if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line) and t0 <= line[:19] <= t1
            and re.search(r"\s(Sleep|DarkWake|Wake)\s", line)]


class Site:
    """Starts and stops the isolated web server + worker."""

    def __init__(self, worker_threads: int, search_concurrency: int, search_queue: int):
        env = env_file()
        m = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+):(\d+)/", env["DATABASE_URL"])
        self.user, pw, host, port = m.groups()
        superuser = (ROOT / "data/.pg-superuser").read_text().strip()
        self.super_url = f"postgresql://postgres:{superuser}@{host}:{port}/postgres"
        self.url = f"postgresql://{self.user}:{pw}@{host}:{port}/{LOAD_DB}"
        self.data_dir = Path(tempfile.mkdtemp(prefix="fmp-load-"))
        self.password = secrets.token_urlsafe(12)
        self.env = dict(os.environ, DATABASE_URL=self.url, DATA_DIR=str(self.data_dir), LOG_LEVEL="WARNING",
                        SECRET_KEY=secrets.token_hex(32), ADMIN_PASSWORD_HASH=hash_password(self.password),
                        SEARCHES_PER_10_MIN="100000", WORKER_THREADS=str(worker_threads),
                        SEARCH_CONCURRENCY=str(search_concurrency), SEARCH_QUEUE=str(search_queue),
                        MIN_FREE_DISK_MB="2048")
        pid_file = ROOT / "data/postgres/postmaster.pid"
        self.postgres = psutil.Process(int(pid_file.read_text().split()[0]))

    def __enter__(self):
        with psycopg.connect(self.super_url, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {LOAD_DB} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {LOAD_DB} OWNER {self.user}")
        subprocess.run([sys.executable, "-m", "app.migrate"], cwd=ROOT, env=self.env, check=True, capture_output=True)
        self.web = subprocess.Popen([str(ROOT / ".venv/bin/uvicorn"), "app.main:app", "--port", str(PORT),
                                     "--no-access-log", "--log-level", "warning"], cwd=ROOT, env=self.env)
        self.worker = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=self.env)
        for _ in range(80):
            try:
                if httpx.get(f"{BASE}/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        for name, proc in (("web", self.web), ("worker", self.worker)):
            if proc.poll() is not None:
                raise RuntimeError(f"{name} process exited early with code {proc.returncode}")
        self.sampler = Sampler({"web": psutil.Process(self.web.pid), "worker": psutil.Process(self.worker.pid),
                                "postgres": self.postgres})
        self.sampler.start()
        return self

    def __exit__(self, *exc):
        self.sampler.stop_flag.set()
        if exc[0] is not None:  # explain a failure: are the processes alive, how much memory did they use?
            print(f"FAILURE DIAGNOSTICS: web exit code={self.web.poll()} worker exit code={self.worker.poll()}", flush=True)
            for row in self.sampler.samples[-5:]:
                print("  last samples:", {k: v for k, v in row.items() if k != "t"}, flush=True)
            print("  system memory:", psutil.virtual_memory()._asdict(), flush=True)
        for proc in (self.web, self.worker):
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        with psycopg.connect(self.super_url, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {LOAD_DB} WITH (FORCE)")
        shutil.rmtree(self.data_dir, ignore_errors=True)


async def admin_client(site: Site) -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=BASE, headers={"Origin": BASE}, timeout=300,
                               limits=httpx.Limits(max_connections=20))
    r = await client.post("/admin/login", data={"password": site.password})
    assert r.status_code in (200, 303), r.text
    return client


async def create_event(admin: httpx.AsyncClient, name: str) -> tuple[int, str]:
    r = await admin.post("/admin/events", data={"name": name})
    event_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = (await admin.get(f"/admin/events/{event_id}")).text
    return event_id, re.search(r'/e/([A-Za-z0-9_-]{20,})"', page).group(1)


async def upload_collection(admin, event_id: int, files: list[Path], parallel: int = 4) -> dict:
    batches = [files[i:i + 5] for i in range(0, len(files), 5)]
    queue: asyncio.Queue = asyncio.Queue()
    for b in batches:
        queue.put_nowait(b)
    result = {"accepted": 0, "rejected": 0, "http_errors": 0}

    async def uploader():
        while not queue.empty():
            batch = queue.get_nowait()
            parts = [("files", (p.name, p.read_bytes(), "image/jpeg")) for p in batch]
            r = await admin.post(f"/admin/api/events/{event_id}/photos", files=parts)
            if r.status_code != 200:
                result["http_errors"] += 1
                continue
            body = r.json()
            result["accepted"] += len(body["accepted"])
            result["rejected"] += len(body["rejected"])

    await asyncio.gather(*(uploader() for _ in range(parallel)))
    return result


POLL_RETRIES = {"count": 0}


async def wait_indexed(admin, event_id: int, site: "Site") -> dict:
    """Poll progress. A dropped keep-alive connection is retried (and counted in the report);
    a dead server process is a failure."""
    failures = 0
    while True:
        try:
            s = (await admin.get(f"/admin/api/events/{event_id}/status?since=9999-01-01T00:00:00%2B00:00|0")).json()
            failures = 0
        except httpx.TransportError:
            POLL_RETRIES["count"] += 1
            failures += 1
            if site.web.poll() is not None or failures > 5:
                raise
            await asyncio.sleep(1)
            continue
        if s["pending"] + s["processing"] == 0:
            return s
        await asyncio.sleep(0.5)


async def visitors(token: str, count: int, duration: float, selfie_bytes: list[bytes], stop: asyncio.Event | None = None) -> dict:
    latencies, statuses, server_ms, match_ms = [], {}, [], []
    deadline = time.perf_counter() + duration
    async with httpx.AsyncClient(base_url=BASE, timeout=120, limits=httpx.Limits(max_connections=count + 5)) as client:
        async def visitor(i):
            rng = random.Random(i)
            while time.perf_counter() < deadline and not (stop and stop.is_set()):
                body = rng.choice(selfie_bytes)
                t = time.perf_counter()
                try:
                    r = await client.post(f"/e/{token}/search", content=body, headers={"content-type": "image/jpeg"})
                    code = r.status_code
                except httpx.HTTPError as exc:
                    code = type(exc).__name__
                latencies.append((time.perf_counter() - t) * 1000)
                statuses[code] = statuses.get(code, 0) + 1
                if code == 200:
                    timing = r.json()["timing"]
                    server_ms.append(timing["server_ms"])
                    match_ms.append(timing["match_ms"])
        started = time.perf_counter()
        await asyncio.gather(*(visitor(i) for i in range(count)))
        elapsed = time.perf_counter() - started
    total = sum(statuses.values())
    ok = statuses.get(200, 0)
    ok_lat = latencies  # all requests, including errors, are part of the latency distribution
    return {"visitors": count, "duration_s": round(elapsed, 1), "requests": total, "ok": ok,
            "errors": total - ok, "error_rate_pct": round(100 * (total - ok) / total, 2) if total else None,
            "status_counts": {str(k): v for k, v in statuses.items()},
            "throughput_per_s": round(total / elapsed, 1) if elapsed else None,
            "latency_ms": {"p50": pct(ok_lat, 50), "p95": pct(ok_lat, 95), "p99": pct(ok_lat, 99),
                           "mean": round(statistics.mean(ok_lat), 1) if ok_lat else None, "max": round(max(ok_lat), 1) if ok_lat else None},
            "server_ms_p50": pct(server_ms, 50), "match_ms_p50": pct(match_ms, 50)}


async def main_async(args) -> dict:
    selfie_bytes = [p.read_bytes() for p in SELFIES]
    report = {"hardware": hardware(), "config": vars(args), "indexing": [], "search": [], "search_during_indexing": None}
    with Site(args.worker_threads, args.search_concurrency, args.search_queue) as site:
        admin = await admin_client(site)
        token_by_size = {}
        for size in args.sizes:
            files = sorted((OUT / f"set_{size}").glob("*.jpg"))[:size]
            event_id, token = await create_event(admin, f"Load {size}")
            token_by_size[size] = token
            t0 = time.time()
            during = None
            upload_task = asyncio.create_task(upload_collection(admin, event_id, files))
            if size == max(args.sizes):  # a visitor does not have to wait for indexing
                stop = asyncio.Event()
                search_task = asyncio.create_task(visitors(token, 10, 10_000, selfie_bytes, stop))
            uploaded = await upload_task
            t_upload = time.time()
            status = await wait_indexed(admin, event_id, site)
            t_done = time.time()
            if size == max(args.sizes):
                stop.set()
                during = await search_task
                during["event_photos"] = size
                report["search_during_indexing"] = during
            mb = sum(f.stat().st_size for f in files) / 1e6
            report["indexing"].append({
                "photos": size, "megabytes": round(mb), **uploaded,
                "upload_s": round(t_upload - t0, 1), "until_searchable_s": round(t_done - t0, 1),
                "photos_per_s": round(size / (t_done - t0), 2), "faces": status["faces"], "failed": status["error"],
                "server_processing_s": status["processing_seconds"],
                "avg_processing_ms_per_photo": round(1000 * status["processing_seconds"] / max(status["timed_photos"], 1)),
                "resources": site.sampler.window(t0, t_done),
                "searches_ran_during_indexing": bool(during)})
            print(f"indexed {size}: {report['indexing'][-1]['until_searchable_s']} s, faces={status['faces']}", flush=True)

        for size in args.sizes:
            levels = args.levels if size == max(args.sizes) else [1, 10]
            for level in levels:
                await visitors(token_by_size[size], min(level, 4), 3, selfie_bytes)  # warm-up (fills the cache)
                t0 = time.time()
                result = await visitors(token_by_size[size], level, args.duration, selfie_bytes)
                result["event_photos"] = size
                result["resources"] = site.sampler.window(t0, time.time())
                report["search"].append(result)
                print(f"search {size} photos, {level} visitors: p50={result['latency_ms']['p50']} ms "
                      f"p95={result['latency_ms']['p95']} ms errors={result['error_rate_pct']}%", flush=True)
        await admin.aclose()
    return report


def write_markdown(r: dict) -> str:
    hw = r["hardware"]
    lines = ["# Load test results", "",
             f"Hardware: {hw['cpu']}, {hw['cores']} cores ({hw['performance_cores']} performance), {hw['memory_gb']} GB RAM, "
             f"macOS {hw['macos']}, Python {hw['python']}. Everything ran on this one machine (client, server, worker, PostgreSQL).",
             f"Config: 1 web process, SEARCH_CONCURRENCY={r['config']['search_concurrency']}, "
             f"SEARCH_QUEUE={r['config']['search_queue']}, WORKER_THREADS={r['config']['worker_threads']}.",
             f"Run: {r['run']['started']} to {r['run']['ended']} ({r['run']['total_minutes']} min). "
             f"Mac slept during the run: {'YES - RESULTS INVALID' if r['run']['mac_slept_during_test'] else 'no'}. "
             f"Status-poll connection retries: {r['run']['status_poll_retries']}.", "",
             "## Upload and indexing", "",
             "| Photos | MB | Upload s | Until searchable s | Photos/s | Faces | Failed | Avg ms/photo | Worker CPU avg | Worker RSS max MB |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i in r["indexing"]:
        lines.append(f"| {i['photos']} | {i['megabytes']} | {i['upload_s']} | {i['until_searchable_s']} | {i['photos_per_s']} | "
                     f"{i['faces']} | {i['failed'] + i['rejected']} | {i['avg_processing_ms_per_photo']} | "
                     f"{i['resources']['worker']['cpu_avg_pct']}% | {i['resources']['worker']['rss_max_mb']} |")
    lines += ["", "## Search latency (closed loop, no think time)", "",
              "| Event photos | Visitors | Duration s | Requests | Req/s | p50 ms | p95 ms | p99 ms | Errors | Status counts | Web CPU avg | Web RSS max MB | Worker RSS max MB | PostgreSQL RSS max MB | Mac free RAM min GB | Swap max GB |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in r["search"]:
        res = s['resources']
        lines.append(f"| {s['event_photos']} | {s['visitors']} | {s['duration_s']} | {s['requests']} | {s['throughput_per_s']} | "
                     f"{s['latency_ms']['p50']} | {s['latency_ms']['p95']} | {s['latency_ms']['p99']} | {s['error_rate_pct']}% | "
                     f"{s['status_counts']} | {res['web']['cpu_avg_pct']}% | {res['web']['rss_max_mb']} | {res['worker']['rss_max_mb']} | "
                     f"{res['postgres']['rss_max_mb']} | {res['system']['available_min_gb']} | {res['system']['swap_max_gb']} |")
    d = r["search_during_indexing"]
    if d:
        lines += ["", f"## Searching while {d['event_photos']} photos were being indexed", "",
                  f"10 visitors, {d['duration_s']} s: {d['requests']} searches, p50 {d['latency_ms']['p50']} ms, "
                  f"p95 {d['latency_ms']['p95']} ms, errors {d['error_rate_pct']}% {d['status_counts']}."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[500, 2000])
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 10, 25, 50])
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--worker-threads", type=int, default=2)
    parser.add_argument("--search-concurrency", type=int, default=2)
    parser.add_argument("--search-queue", type=int, default=64)
    args = parser.parse_args()
    started = time.time()
    report = asyncio.run(main_async(args))
    ended = time.time()
    events = sleep_events(started, ended)
    report["run"] = {"started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
                     "ended": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended)),
                     "total_minutes": round((ended - started) / 60, 1),
                     "mac_slept_during_test": bool(events), "sleep_events": events,
                     "status_poll_retries": POLL_RETRIES["count"]}
    (OUT / "results.json").write_text(json.dumps(report, indent=2))
    (OUT / "results.md").write_text(write_markdown(report))
    print(write_markdown(report))


if __name__ == "__main__":
    main()
