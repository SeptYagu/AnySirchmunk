#!/usr/bin/env python3
"""Run the fixed-query AnyTXT versus rga retrieval benchmark.

The benchmark calls retrievers directly, so it consumes no LLM tokens.  A
manifest supplies a public corpus identifier, literal queries, and manually
checked expected basenames; the machine-specific corpus root is a command-line
argument and is deliberately omitted from the result artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def unique_basenames(events: list[dict[str, Any]], limit: int) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.get("type") != "begin":
            continue
        path = event.get("data", {}).get("path", {}).get("text")
        if not isinstance(path, str):
            continue
        name = os.path.basename(path)
        key = name.casefold()
        if key not in seen:
            names.append(name)
            seen.add(key)
        if len(names) >= limit:
            break
    return names


def retrieval_scores(returned: list[str], expected: list[str]) -> tuple[float | None, float | None]:
    returned_set = {name.casefold() for name in returned}
    expected_set = {name.casefold() for name in expected}
    if not expected_set:
        return None, None
    hits = len(returned_set & expected_set)
    recall = hits / len(expected_set)
    precision = hits / len(returned_set) if returned_set else 0.0
    return recall, precision


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in runs if not run.get("error")]
    expected = [run for run in successful if run.get("recall_at_k") is not None]
    negatives = [run for run in successful if run.get("negative_control")]
    latencies = [run["latency_ms"] for run in successful]
    return {
        "runs": len(runs),
        "successful_runs": len(successful),
        "latency_ms_p50": percentile(latencies, 0.50),
        "latency_ms_p95": percentile(latencies, 0.95),
        "mean_recall_at_k": (
            sum(run["recall_at_k"] for run in expected) / len(expected) if expected else None
        ),
        "mean_precision_at_k": (
            sum(run["precision_at_k"] for run in expected) / len(expected) if expected else None
        ),
        "negative_zero_result_rate": (
            sum(run["result_count"] == 0 for run in negatives) / len(negatives) if negatives else None
        ),
        "complete_rate": (
            sum(bool(run.get("complete")) for run in successful) / len(successful) if successful else 0.0
        ),
        "fallback_rate": (
            sum(bool(run.get("fallback")) for run in successful) / len(successful) if successful else 0.0
        ),
        "requests": sum(int(run.get("requests", 0)) for run in successful),
        "fragment_chars": sum(int(run.get("fragment_chars", 0)) for run in successful),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("queries"), list):
        raise ValueError("benchmark manifest must be an object with a queries array")
    for index, query in enumerate(value["queries"], 1):
        if not isinstance(query, dict) or not isinstance(query.get("term"), str):
            raise ValueError(f"query {index} must contain a string term")
        if not isinstance(query.get("expected"), list) or not all(
            isinstance(item, str) for item in query["expected"]
        ):
            raise ValueError(f"query {index} expected must be an array of basenames")
    return value


def git_head(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


async def timed_retrieve(
    backend: str,
    retriever: Any,
    query: dict[str, Any],
    root: Path,
    k: int,
    timeout: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        kwargs: dict[str, Any] = {
            "terms": query["term"],
            "path": str(root),
            "literal": True,
            "regex": False,
            "timeout": timeout,
        }
        if backend == "rga":
            kwargs["rank"] = False
        events = await retriever.retrieve(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000
        returned = unique_basenames(events, k)
        expected = query["expected"]
        recall, precision = retrieval_scores(returned, expected)
        metadata = dict(getattr(events, "metadata", {}) or {})
        if backend == "rga":
            fragment_chars = sum(
                len(str(event.get("data", {}).get("lines", {}).get("text", "")))
                for event in events
                if event.get("type") == "match"
            )
            requests = 1
            complete = True
            actual_backend = next(
                (event.get("_search_backend") for event in events if event.get("_search_backend")),
                "rga",
            )
        else:
            fragment_chars = int(metadata.get("fragment_chars", 0))
            requests = int(metadata.get("requests", 0))
            complete = bool(metadata.get("complete"))
            actual_backend = metadata.get("actual_backend", "anytxt")
        return {
            "id": query.get("id", query["term"]),
            "term": query["term"],
            "latency_ms": round(latency_ms, 3),
            "result_count": len(returned),
            "returned_at_k": returned,
            "expected": expected,
            "negative_control": not bool(expected),
            "recall_at_k": recall,
            "precision_at_k": precision,
            "complete": complete,
            "reason": metadata.get("reason"),
            "actual_backend": actual_backend,
            "fallback": actual_backend != backend,
            "requests": requests,
            "fragment_chars": fragment_chars,
        }
    except Exception as exc:  # Keep the full benchmark auditable after one failure.
        return {
            "id": query.get("id", query["term"]),
            "term": query["term"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "result_count": 0,
            "returned_at_k": [],
            "expected": query["expected"],
            "negative_control": not bool(query["expected"]),
            "recall_at_k": None,
            "precision_at_k": None,
            "complete": False,
            "fallback": False,
            "requests": 0,
            "fragment_chars": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    root = args.root.resolve()
    sirchmunk = args.sirchmunk_path.resolve()
    if not root.is_dir():
        raise ValueError(f"corpus root is not a directory: {root}")
    source = sirchmunk / "src"
    if not source.is_dir():
        raise ValueError(f"Sirchmunk source directory is missing: {source}")
    sys.path.insert(0, str(source))
    from sirchmunk.retrieve.anytxt_retriever import AnyTXTConfig, AnyTXTRetriever
    from sirchmunk.retrieve.text_retriever import GrepRetriever

    anytxt = AnyTXTRetriever(
        work_path=args.work_path,
        config=AnyTXTConfig(
            api_url=args.api_url,
            request_timeout=min(15.0, args.timeout),
            total_timeout=args.timeout,
            max_requests=200,
            max_candidates=3000,
            max_fragment_requests=100,
            max_fragment_chars=100000,
            fallback_to_rga=False,
            global_roots=(str(root),),
        ),
    )
    rga = GrepRetriever(work_path=args.work_path)
    retrievers = {"anytxt": anytxt, "rga": rga}
    details: dict[str, dict[str, list[dict[str, Any]]]] = {
        "cold": {"anytxt": [], "rga": []},
        "warm": {"anytxt": [], "rga": []},
    }
    total_cycles = 1 + args.warm_runs
    for cycle in range(total_cycles):
        phase = "cold" if cycle == 0 else "warm"
        for index, query in enumerate(manifest["queries"]):
            order = ("anytxt", "rga") if (index + cycle) % 2 == 0 else ("rga", "anytxt")
            for backend in order:
                print(
                    f"[{phase} {cycle + 1}/{total_cycles}] {index + 1}/{len(manifest['queries'])} "
                    f"{backend}: {query['term']}",
                    flush=True,
                )
                result = await timed_retrieve(
                    backend, retrievers[backend], query, root, args.k, args.timeout
                )
                details[phase][backend].append(result)

    aggregates = {
        phase: {backend: aggregate(details[phase][backend]) for backend in ("anytxt", "rga")}
        for phase in ("cold", "warm")
    }
    anytxt_warm = aggregates["warm"]["anytxt"]
    rga_warm = aggregates["warm"]["rga"]
    recall_pass = (
        anytxt_warm["mean_recall_at_k"] is not None
        and rga_warm["mean_recall_at_k"] is not None
        and anytxt_warm["mean_recall_at_k"] >= rga_warm["mean_recall_at_k"]
    )
    latency_pass = (
        anytxt_warm["latency_ms_p95"] is not None
        and rga_warm["latency_ms_p95"] is not None
        and anytxt_warm["latency_ms_p95"] <= 0.8 * rga_warm["latency_ms_p95"]
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {
            "id": manifest.get("corpus_id", args.manifest.stem),
            "root_redacted": True,
            "file_count": sum(path.is_file() for path in root.iterdir()),
        },
        "query_count": len(manifest["queries"]),
        "k": args.k,
        "cold_runs_per_query": 1,
        "warm_runs_per_query": args.warm_runs,
        "cold_definition": "first harness run; operating-system and AnyTXT service caches were not flushed",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sirchmunk_commit": git_head(sirchmunk),
            "anytxt_api_url": args.api_url,
        },
        "aggregates": aggregates,
        "thresholds": {
            "recall": {
                "rule": "AnyTXT mean recall@K >= rga mean recall@K on positive queries",
                "passed": recall_pass,
            },
            "warm_latency": {
                "rule": "AnyTXT warm P95 <= 80% of rga warm P95",
                "passed": latency_pass,
            },
            "overall_passed": recall_pass and latency_pass,
        },
        "details": details,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True, help="machine-local corpus root")
    parser.add_argument("--sirchmunk-path", type=Path, required=True)
    parser.add_argument("--work-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:9924/rpc")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if args.k <= 0 or args.warm_runs <= 0 or args.timeout <= 0:
        parser.error("--k, --warm-runs and --timeout must be positive")
    return args


def main() -> int:
    args = parse_args()
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["aggregates"], ensure_ascii=False, indent=2))
    print(f"result: {args.output}")
    return 0 if result["thresholds"]["overall_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
