# Load test results

Hardware: Apple M2, 8 cores (4 performance), 8 GB RAM, macOS 15.1, Python 3.12.12. Everything ran on this one machine (client, server, worker, PostgreSQL).
Config: 1 web process, SEARCH_CONCURRENCY=2, SEARCH_QUEUE=16, WORKER_THREADS=2.
Run: 2026-09-24 11:20:12 to 2026-09-24 11:38:22 (18.2 min). Mac slept during the run: no. Status-poll connection retries: 0.

## Upload and indexing

| Photos | MB | Upload s | Until searchable s | Photos/s | Faces | Failed | Avg ms/photo | Worker CPU avg | Worker RSS max MB |
|---|---|---|---|---|---|---|---|---|---|
| 500 | 184 | 1.3 | 63.1 | 7.92 | 1260 | 0 | 247 | 364% | 938 |
| 2000 | 728 | 15.3 | 641.1 | 3.12 | 5003 | 0 | 628 | 188% | 740 |

## Search latency (closed loop, no think time)

| Event photos | Visitors | Duration s | Requests | Req/s | p50 ms | p95 ms | p99 ms | Errors | Status counts | Web CPU avg | Web RSS max MB | Worker RSS max MB | PostgreSQL RSS max MB | Mac free RAM min GB | Swap max GB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 500 | 1 | 60.0 | 800 | 13.3 | 68.5 | 113.1 | 184.1 | 0.0% | {'200': 800} | 322% | 723 | 15 | 80 | 1.08 | 12.7 |
| 500 | 10 | 61.0 | 883 | 14.5 | 603.5 | 1145.8 | 1581.2 | 0.0% | {'200': 883} | 350% | 769 | 18 | 71 | 1.08 | 12.7 |
| 2000 | 1 | 60.0 | 769 | 12.8 | 65.1 | 154.0 | 309.6 | 0.0% | {'200': 769} | 315% | 772 | 15 | 78 | 1.07 | 13.8 |
| 2000 | 10 | 60.4 | 1111 | 18.4 | 494.3 | 754.5 | 1603.2 | 0.0% | {'200': 1111} | 440% | 918 | 44 | 107 | 1.09 | 12.7 |
| 2000 | 25 | 60.8 | 26626 | 437.7 | 11.3 | 71.9 | 1060.1 | 96.13% | {'503': 25595, '200': 1031} | 441% | 961 | 15 | 115 | 1.08 | 12.7 |
| 2000 | 50 | 60.9 | 10117 | 166.0 | 120.2 | 1170.5 | 1800.0 | 88.89% | {'503': 8993, '200': 1124} | 456% | 955 | 14 | 127 | 1.1 | 12.8 |

## Searching while 2000 photos were being indexed

10 visitors, 641.6 s: 4244 searches, p50 1385.3 ms, p95 2479.0 ms, errors 0.0% {'200': 4244}.
