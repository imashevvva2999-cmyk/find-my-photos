# Load test results

Hardware: Apple M2, 8 cores (4 performance), 8 GB RAM, macOS 15.1, Python 3.12.12. Everything ran on this one machine (client, server, worker, PostgreSQL).
Config: 1 web process, SEARCH_CONCURRENCY=2, SEARCH_QUEUE=64, WORKER_THREADS=2.
Run: 2026-09-24 11:41:55 to 2026-09-24 11:52:14 (10.3 min). Mac slept during the run: no. Status-poll connection retries: 0.

## Upload and indexing

| Photos | MB | Upload s | Until searchable s | Photos/s | Faces | Failed | Avg ms/photo | Worker CPU avg | Worker RSS max MB |
|---|---|---|---|---|---|---|---|---|---|
| 2000 | 728 | 14.6 | 482.8 | 4.14 | 5003 | 0 | 471 | 229% | 1005 |

## Search latency (closed loop, no think time)

| Event photos | Visitors | Duration s | Requests | Req/s | p50 ms | p95 ms | p99 ms | Errors | Status counts | Web CPU avg | Web RSS max MB | Worker RSS max MB | PostgreSQL RSS max MB | Mac free RAM min GB | Swap max GB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2000 | 25 | 61.8 | 684 | 11.1 | 1871.0 | 4410.9 | 5420.6 | 0.0% | {'200': 684} | 287% | 776 | 19 | 108 | 1.07 | 13.2 |
| 2000 | 50 | 63.2 | 844 | 13.3 | 3413.4 | 6852.0 | 7736.8 | 0.0% | {'200': 844} | 331% | 869 | 16 | 153 | 1.07 | 13.3 |

## Searching while 2000 photos were being indexed

10 visitors, 483.3 s: 4423 searches, p50 1049.0 ms, p95 1517.7 ms, errors 0.0% {'200': 4423}.
