#!/usr/bin/env python3
"""AnyTXT RPC stability probe.

Replays the request mix the adapter produces (candidate search + fragment
lookup) against a running AnyTXT Searcher and reports where, if anywhere, the
service stops answering.  It exists because the 1.3.2477 Beta RPC service was
observed to segfault under sustained load, and we need a reproducible way to
compare behaviour before and after an AnyTXT upgrade.

Standard library only.  On Windows it also samples the ATGUI.exe working set so
a memory leak shows up as a trend rather than a surprise.

Exit codes:
    0  every request succeeded and the service stayed up
    2  the service stopped answering (crash / restart window)
    3  requests failed but the service recovered
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence

SEARCH_METHOD = "ATRpcServer.Searcher.V1.GetResult"
FRAGMENT_METHOD = "ATRpcServer.Searcher.V1.GetFragment"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9920)
    parser.add_argument("--patterns", default="partimento,counterpoint,历史起源",
                        help="comma separated search patterns; non-ASCII ones exercise the known crash trigger")
    parser.add_argument("--drives", default="C:\\,D:\\",
                        help="comma separated filterDir values used for candidate discovery")
    parser.add_argument("--requests", type=int, default=600, help="total RPC requests to send")
    parser.add_argument("--concurrency", type=int, default=2,
                        help="in-flight requests; the adapter default is 2")
    parser.add_argument("--fragments-per-search", type=int, default=3,
                        help="fragment lookups issued for every search request")
    parser.add_argument("--mode", choices=("mixed", "search-only", "fragment-only"), default="mixed",
                        help="request mix; the single-kind modes isolate whether concurrent GetResult "
                             "and GetFragment calls are what kills the service")
    parser.add_argument("--report-every", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--settle-seconds", type=float, default=0.0,
                        help="sleep between requests; 0 means as fast as possible")
    return parser.parse_args(argv)


def service_alive(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


def atgui_memory_mb() -> Optional[float]:
    """Working set of ATGUI.exe in MiB, or None when it is not running."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ATGUI.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception:
        return None
    if "ATGUI" not in out:
        return None
    fields = [part.strip('"') for part in out.splitlines()[0].split('","')]
    for field in reversed(fields):
        digits = "".join(ch for ch in field if ch.isdigit())
        if digits and "," in field:
            return int(digits) / 1024.0
    return None


def call(host: str, port: int, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"http://{host}:{port}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def search_payload(pattern: str, drive: str, limit: int = 3) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0", "id": 1, "method": SEARCH_METHOD,
        "params": {"input": {"pattern": pattern, "filterDir": drive, "filterExt": "",
                             "lastModifyBegin": 0, "lastModifyEnd": 2147483647,
                             "limit": limit, "offset": 0, "order": 0}},
    }


def fragment_payload(fid: Any, pattern: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": FRAGMENT_METHOD,
            "params": {"input": {"fid": fid, "pattern": pattern}}}


def collect_fids(host: str, port: int, pattern: str, drive: str, timeout: float) -> List[str]:
    try:
        response = call(host, port, search_payload(pattern, drive, limit=30), timeout)
    except Exception:
        return []
    output = response.get("result", {}).get("data", {}).get("output", {}) or {}
    fields = output.get("field") or []
    rows = output.get("files") or []
    fids: List[str] = []
    for row in rows:
        record = dict(zip(fields, row)) if isinstance(row, list) else row
        if record.get("fid"):
            fids.append(record["fid"])
    return fids


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    drives = [d.strip() for d in args.drives.split(",") if d.strip()]

    if not service_alive(args.host, args.port):
        print(f"service is not listening on {args.host}:{args.port}")
        return 2

    fid_pool: List[tuple[Any, str]] = []
    for index, pattern in enumerate(patterns):
        fids = collect_fids(args.host, args.port, pattern, drives[index % len(drives)], args.timeout)
        fid_pool.extend((fid, pattern) for fid in fids)
    print(f"fid pool: {len(fid_pool)} entries from {len(patterns)} patterns")
    if not fid_pool:
        print("no candidates returned; cannot probe fragment lookups")
        return 3

    ok = failed = 0
    jobs: List[tuple[str, Any, str]] = []
    index = 0
    while len(jobs) < args.requests:
        pattern = patterns[index % len(patterns)]
        drive = drives[index % len(drives)]
        if args.mode != "fragment-only":
            jobs.append(("search", pattern, drive))
        if args.mode != "search-only":
            fid, fragment_pattern = fid_pool[index % len(fid_pool)]
            repeats = 1 if args.mode == "fragment-only" else args.fragments_per_search
            for _ in range(repeats):
                if len(jobs) >= args.requests:
                    break
                jobs.append(("fragment", fid, fragment_pattern))
        index += 1
    print(f"sending {len(jobs)} requests at concurrency {args.concurrency} "
          f"(~{args.fragments_per_search} fragment lookups per search)")

    memory_samples: List[tuple[int, Optional[float]]] = []
    started = time.monotonic()
    first_failure_at: Optional[int] = None
    sent = 0

    def run(job: tuple[str, Any, str]) -> bool:
        kind, value, pattern = job
        payload = search_payload(value, pattern) if kind == "search" else fragment_payload(value, pattern)
        try:
            call(args.host, args.port, payload, args.timeout)
            return True
        except Exception:
            return False

    chunk = max(args.report_every, args.concurrency)
    aborted = False
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for start in range(0, len(jobs), chunk):
            for result in pool.map(run, jobs[start:start + chunk]):
                sent += 1
                if result:
                    ok += 1
                else:
                    failed += 1
                    if first_failure_at is None:
                        first_failure_at = sent
                if args.settle_seconds:
                    time.sleep(args.settle_seconds)
            rss = atgui_memory_mb()
            up = service_alive(args.host, args.port)
            memory_samples.append((sent, rss))
            print(f"  sent={sent:4d} ok={ok:4d} fail={failed:3d} atgui_rss="
                  f"{'n/a' if rss is None else format(rss, '.0f') + 'MB'} alive={up} "
                  f"{time.monotonic() - started:.1f}s")
            if not up:
                aborted = True
                break

    alive_after = service_alive(args.host, args.port)
    print(f"\nsummary: ok={ok} fail={failed} alive_after={alive_after} "
          f"elapsed={time.monotonic() - started:.1f}s")
    if first_failure_at is not None:
        print(f"first failure at request #{first_failure_at}")
    if memory_samples:
        first_rss = next((r for _, r in memory_samples if r is not None), None)
        last_rss = next((r for _, r in reversed(memory_samples) if r is not None), None)
        if first_rss is not None and last_rss is not None:
            print(f"ATGUI working set: {first_rss:.0f}MB -> {last_rss:.0f}MB")

    if not alive_after:
        return 2
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
