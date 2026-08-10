"""Small concurrency smoke test for a running ServiceSlate host."""
from __future__ import annotations

import argparse
import concurrent.futures
import time
import urllib.request


def hit(url: str) -> tuple[int, float]:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, (time.perf_counter() - start) * 1000
    except Exception:
        return 0, (time.perf_counter() - start) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="http://127.0.0.1:8787/api/health")
    ap.add_argument("--requests", type=int, default=100)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda _: hit(args.url), range(args.requests)))
    failures = sum(1 for status, _ in results if status != 200)
    timings = sorted(ms for _, ms in results)
    p95 = timings[min(len(timings) - 1, int(len(timings) * .95))]
    print(f"requests={len(results)} failures={failures} p95_ms={p95:.1f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
