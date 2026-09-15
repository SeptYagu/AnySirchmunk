#!/usr/bin/env python3
"""AnyTXT RPC stability probe.

Replays the request mix the adapter produces (candidate search + fragment
lookup) against a running AnyTXT Searcher and reports where, if anywhere, the
service stops answering.  It exists because the AnyTXT Beta RPC service was
observed to die under sustained load, and we need a reproducible way to compare
builds and settings.

Only the documented v1 interface is probed:

  v1  http://127.0.0.1:9924/rpc  anytxt.v1.*

The legacy ``ATRpcServer.Searcher.V1.*`` service on port 9920 is no longer
supported by this project; the measurements taken against it on 1.3.2477 and
1.3.3541 are kept in docs/anytxt-capabilities.md as history.

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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

ENDPOINT: Dict[str, Any] = {
    "port": 9924,
    "path": "/rpc",
    "search": "anytxt.v1.getResult",
    "fragment": "anytxt.v1.getFragment",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="defaults to the v1 endpoint's port (9924)")
    parser.add_argument("--patterns", default="partimento,counterpoint,历史起源",
                        help="comma separated search patterns; non-ASCII ones exercise the known crash trigger")
    parser.add_argument("--drives", default="C:\\,D:\\",
                        help="comma separated filterDir values used for candidate discovery")
    parser.add_argument("--limit", type=int, default=30,
                        help="search page size; DEEP extraction asks for up to 300, which makes each "
                             "response far heavier than the default")
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
            capture_output=True, text=True, timeout=15, errors="replace",
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


def call(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def search_payload(spec: Dict[str, Any], pattern: str, drive: str, limit: int,
                   offset: int = 0) -> Dict[str, Any]:
    # Mirrors the adapter's request shape (v1 parameters go directly in params,
    # page order 3 = path ascending, no upper modification-time bound).
    params = {"pattern": pattern, "filterDir": drive, "filterExt": "",
              "lastModifyBegin": 0, "lastModifyEnd": 0,
              "limit": limit, "offset": offset, "order": 3}
    return {"jsonrpc": "2.0", "id": 1, "method": spec["search"], "params": params}


def fragment_payload(spec: Dict[str, Any], fid: Any, pattern: str) -> Dict[str, Any]:
    params = {"fid": fid, "pattern": pattern}
    return {"jsonrpc": "2.0", "id": 1, "method": spec["fragment"], "params": params}


def collect_fids(url: str, spec: Dict[str, Any], pattern: str, drive: str,
                 limit: int, timeout: float) -> List[str]:
    try:
        response = call(url, search_payload(spec, pattern, drive, limit), timeout)
    except Exception as exc:  # noqa: BLE001
        # Never fail silently: an empty pool used to mean "no candidates", which
        # is indistinguishable from "the request itself was broken".
        print(f"candidate discovery for {pattern!r} in {drive!r} failed: {type(exc).__name__}: {exc}")
        return []
    error = response.get("error")
    if error:
        print(f"candidate discovery for {pattern!r} in {drive!r} was rejected: {error}")
        return []
    errno = (response.get("result") or {}).get("errno")
    if errno:
        print(f"candidate discovery for {pattern!r} in {drive!r} answered errno {errno}")
        return []
    output = response.get("result", {}).get("data", {}).get("output", {}) or {}
    fields = output.get("field") or []
    rows = output.get("files") or []
    fids: List[str] = []
    for row in rows:
        record = dict(zip(fields, row)) if isinstance(row, list) else row
        if record.get("fid"):
            fids.append(str(record["fid"]))
    return fids


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    spec = ENDPOINT
    port = args.port or spec["port"]
    url = f"http://{args.host}:{port}{spec['path']}"
    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    drives = [d.strip() for d in args.drives.split(",") if d.strip()]

    if not service_alive(args.host, port):
        print(f"service is not listening on {args.host}:{port} (AnyTXT v1 API, 1.3.3541+)")
        return 2

    fid_pool: List[Tuple[str, str]] = []
    for index, pattern in enumerate(patterns):
        fids = collect_fids(url, spec, pattern, drives[index % len(drives)], args.limit, args.timeout)
        fid_pool.extend((fid, pattern) for fid in fids)
    print(f"endpoint: {url}")
    print(f"fid pool: {len(fid_pool)} entries from {len(patterns)} patterns")
    if not fid_pool:
        print("no candidates returned; cannot probe fragment lookups")
        return 3

    jobs: List[Tuple[str, str, str]] = []
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
    print(f"sending {len(jobs)} requests at concurrency {args.concurrency}, page size {args.limit} "
          f"(~{args.fragments_per_search} fragment lookups per search)")

    def run(job: Tuple[str, str, str]) -> bool:
        kind, value, pattern = job
        payload = (search_payload(spec, value, pattern, args.limit) if kind == "search"
                   else fragment_payload(spec, value, pattern))
        try:
            call(url, payload, args.timeout)
            return True
        except Exception:
            return False

    ok = failed = sent = 0
    first_failure_at: Optional[int] = None
    memory_samples: List[Tuple[int, Optional[float]]] = []
    chunk = max(args.report_every, args.concurrency)
    started = time.monotonic()

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
            up = service_alive(args.host, port)
            memory_samples.append((sent, rss))
            print(f"  sent={sent:4d} ok={ok:4d} fail={failed:3d} atgui_rss="
                  f"{'n/a' if rss is None else format(rss, '.0f') + 'MB'} alive={up} "
                  f"{time.monotonic() - started:.1f}s")
            if not up:
                break

    alive_after = service_alive(args.host, port)
    print(f"\nsummary: endpoint={url} ok={ok} fail={failed} alive_after={alive_after} "
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
