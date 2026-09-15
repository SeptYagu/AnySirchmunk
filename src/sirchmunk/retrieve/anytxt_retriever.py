"""AnyTXT Searcher JSON-RPC adapter for Sirchmunk.

This module intentionally uses only the Python standard library.  AnyTXT is a
candidate/fragment service; Sirchmunk remains responsible for reading source
files and building evidence.
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
import logging
import ntpath
import os
import re
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Protocol, Sequence, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from sirchmunk.retrieve.base import BaseRetriever
except ImportError:  # Allows the adapter's contract tests to run standalone.
    class BaseRetriever:  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)
PathLike = Union[str, Path]

#: Two headers are mandatory.  Without both of them the local service answers
#: HTTP 400 with an empty body, so they are part of the compatibility contract.
RPC_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

#: AnyTXT exposes two RPC surfaces from the same process.  ``v1`` is the
#: documented endpoint and stayed healthy through the load that killed the
#: legacy one, so it is the default; ``legacy`` remains for older builds that do
#: not ship the v1 endpoint.
API_MODES: Dict[str, Dict[str, Any]] = {
    "v1": {
        "default_url": "http://127.0.0.1:9924/rpc",
        "search": "anytxt.v1.getResult",
        "fragment": "anytxt.v1.getFragment",
        "nested": False,
    },
    "legacy": {
        "default_url": "http://127.0.0.1:9920",
        "search": "ATRpcServer.Searcher.V1.GetResult",
        "fragment": "ATRpcServer.Searcher.V1.GetFragment",
        "nested": True,
    },
}

#: ``filterDir=""`` is *not* a global search.  AnyTXT resolves the empty value to
#: its own current directory (observed: ``C:``) and returns that drive only.
SERVER_DEFAULT_SCOPE = "anytxt_server_default"
UNVERIFIED_GLOBAL_REASON = "unverified_global_scope"


class AnyTXTError(RuntimeError):
    """Base class for failures which may trigger a bounded rga fallback."""


class AnyTXTBackendError(AnyTXTError):
    """The local service could not complete a valid RPC request."""


class AnyTXTRequestTimeout(AnyTXTBackendError):
    """The local service did not answer before the per-request timeout."""


class AnyTXTUnsupportedQuery(AnyTXTError):
    """The requested semantics are not verified for the configured API."""


class AnyTXTIncompleteResults(AnyTXTError):
    """An exact operation cannot be evaluated over an incomplete result set."""


class AnyTXTBudgetExhausted(AnyTXTError):
    """The retrieve-wide request or time budget was exhausted."""


class ReadOnlyRetriever(Protocol):
    async def retrieve(self, terms: Union[str, List[str]], **kwargs: Any) -> List[Dict[str, Any]]:
        ...

    def merge_results(self, raw_results: List[Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
        ...


class RetrievalEvents(list):
    """List-compatible event stream with non-public diagnostic metadata."""

    def __init__(self, values: Iterable[Dict[str, Any]] = (), *, metadata: Optional[Dict[str, Any]] = None):
        super().__init__(values)
        self.metadata: Dict[str, Any] = metadata or {}


@dataclass(frozen=True)
class AnyTXTConfig:
    #: ``v1`` (documented endpoint, default) or ``legacy`` (builds without it).
    api_mode: str = "v1"
    api_url: str = API_MODES["v1"]["default_url"]
    page_size: int = 300
    request_timeout: float = 15.0
    total_timeout: float = 30.0
    #: Verified safe against AnyTXT 1.3.2477: its RPC service segfaults when
    #: several requests overlap with non-ASCII patterns, which kills the whole
    #: service for the rest of the run.  Concurrency 2 passed 90 consecutive
    #: requests including CJK patterns; higher values stay unverified.
    max_concurrency: int = 2
    max_requests: int = 100
    max_candidates: int = 3000
    max_fragment_chars: int = 100000
    #: Fragment lookups get their own request budget so that snippet retrieval
    #: cannot starve the paging budget that discovers candidate files.
    max_fragment_requests: int = 100
    fallback_to_rga: bool = True
    fallback_roots: tuple[str, ...] = ()
    #: Roots used for "no explicit range" queries.  An empty value means the
    #: unverified server default directory is used and the result is reported as
    #: incomplete instead of pretending to be a global index search.
    global_roots: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AnyTXTConfig":
        def positive(name: str, default: str, cast: Any) -> Any:
            raw = os.getenv(name, default)
            try:
                value = cast(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a positive number") from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive number")
            return value

        roots_raw = os.getenv("ANYTXT_FALLBACK_ROOTS", "[]")
        try:
            roots_value = json.loads(roots_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("ANYTXT_FALLBACK_ROOTS must be a JSON array") from exc
        if not isinstance(roots_value, list) or not all(isinstance(item, str) for item in roots_value):
            raise ValueError("ANYTXT_FALLBACK_ROOTS must be a JSON array of absolute paths")
        roots: List[str] = []
        for item in roots_value:
            if not _is_absolute_path(item):
                raise ValueError(f"ANYTXT_FALLBACK_ROOTS contains a non-absolute path: {item!r}")
            root = _absolute_path(item)
            if not os.path.isdir(root):
                raise ValueError(f"ANYTXT_FALLBACK_ROOTS is not an existing directory: {item!r}")
            roots.append(root)
        global_roots = _env_roots("ANYTXT_GLOBAL_ROOTS", os.getenv("ANYTXT_GLOBAL_ROOTS", "[]"))

        api_mode = os.getenv("ANYTXT_API_MODE", cls.api_mode).strip().lower()
        if api_mode not in API_MODES:
            raise ValueError("ANYTXT_API_MODE must be 'v1' or 'legacy'")
        api_url = os.getenv("ANYTXT_API_URL", API_MODES[api_mode]["default_url"])
        parsed = urlparse(api_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ANYTXT_API_URL must be an HTTP loopback URL")
        fallback = os.getenv("ANYTXT_FALLBACK_TO_RGA", "true").strip().lower()
        if fallback not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("ANYTXT_FALLBACK_TO_RGA must be a boolean")
        return cls(
            api_mode=api_mode,
            api_url=api_url,
            page_size=positive("ANYTXT_SEARCH_LIMIT", "300", int),
            request_timeout=positive("ANYTXT_REQUEST_TIMEOUT", "15", float),
            total_timeout=positive("ANYTXT_TOTAL_TIMEOUT", "30", float),
            max_concurrency=positive("ANYTXT_MAX_CONCURRENCY", "2", int),
            max_requests=positive("ANYTXT_MAX_REQUESTS", "100", int),
            max_candidates=positive("ANYTXT_MAX_CANDIDATES", "3000", int),
            max_fragment_chars=positive("ANYTXT_MAX_FRAGMENT_CHARS", "100000", int),
            max_fragment_requests=positive("ANYTXT_MAX_FRAGMENT_REQUESTS", "100", int),
            fallback_to_rga=fallback in {"true", "1", "yes"},
            fallback_roots=tuple(roots),
            global_roots=tuple(global_roots),
        )


@dataclass(frozen=True)
class SearchPage:
    files: tuple[Dict[str, Any], ...]
    count: Optional[int]


class _Budget:
    def __init__(
        self,
        config: AnyTXTConfig,
        caller_timeout: Optional[float],
        *,
        max_requests: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> None:
        total = min(config.total_timeout, caller_timeout) if caller_timeout else config.total_timeout
        self.deadline = deadline if deadline is not None else time.monotonic() + total
        self.max_requests = max_requests if max_requests is not None else config.max_requests
        self.requests = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def charge(self) -> None:
        if self.remaining <= 0 or self.requests >= self.max_requests:
            raise AnyTXTBudgetExhausted("AnyTXT retrieve budget exhausted")
        self.requests += 1


class _EndpointGate:
    """Process-wide concurrency and method-kind gate for one RPC endpoint.

    AnyTXT 1.3.2477 cannot serve ``GetResult`` and ``GetFragment`` concurrently:
    overlapping calls of the two methods killed the service within a few dozen
    requests.  The gate is synchronous because it must remain held by the worker
    thread even when the awaiting coroutine times out or is cancelled.
    """

    def __init__(self, max_concurrency: int) -> None:
        self._condition = threading.Condition()
        self._max_concurrency = max_concurrency
        self._kind: Optional[str] = None
        self._active = 0

    def tighten(self, max_concurrency: int) -> None:
        """Use the safest limit requested by live clients for this endpoint."""
        with self._condition:
            self._max_concurrency = min(self._max_concurrency, max_concurrency)
            self._condition.notify_all()

    def acquire(self, kind: str, cancelled: Optional[threading.Event]) -> None:
        with self._condition:
            while self._active and (
                self._kind != kind or self._active >= self._max_concurrency
            ):
                if cancelled is not None and cancelled.is_set():
                    raise AnyTXTRequestTimeout("AnyTXT request was cancelled before dispatch")
                self._condition.wait(timeout=0.05)
            if cancelled is not None and cancelled.is_set():
                raise AnyTXTRequestTimeout("AnyTXT request was cancelled before dispatch")
            if self._active == 0:
                self._kind = kind
            self._active += 1

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            if self._active == 0:
                self._kind = None
            self._condition.notify_all()


_ENDPOINT_GATES: "weakref.WeakValueDictionary[str, _EndpointGate]" = weakref.WeakValueDictionary()
_ENDPOINT_GATES_LOCK = threading.Lock()


def _endpoint_gate(url: str, max_concurrency: int) -> _EndpointGate:
    key = url.rstrip("/").lower()
    with _ENDPOINT_GATES_LOCK:
        gate = _ENDPOINT_GATES.get(key)
        if gate is None:
            gate = _EndpointGate(max_concurrency)
            _ENDPOINT_GATES[key] = gate
        else:
            gate.tighten(max_concurrency)
        return gate


class AnyTXTClient:
    #: Legacy method names, kept for callers and docs that reference them.
    SEARCH_METHOD = API_MODES["legacy"]["search"]
    FRAGMENT_METHOD = API_MODES["legacy"]["fragment"]

    def __init__(self, config: AnyTXTConfig) -> None:
        self.config = config
        spec = API_MODES[config.api_mode]
        self.search_method: str = spec["search"]
        self.fragment_method: str = spec["fragment"]
        self._nested_params: bool = bool(spec["nested"])
        self._ids = itertools.count(1)
        self._endpoint_gate = _endpoint_gate(config.api_url, config.max_concurrency)

    def _post(
        self,
        payload: Dict[str, Any],
        timeout: float,
        kind: str,
        cancelled: threading.Event,
    ) -> Dict[str, Any]:
        request = Request(
            self.config.api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(RPC_HEADERS),
            method="POST",
        )
        self._endpoint_gate.acquire(kind, cancelled)
        try:
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = response.read()
            except TimeoutError as exc:
                raise AnyTXTRequestTimeout(f"AnyTXT request timed out: {exc}") from exc
            except HTTPError as exc:
                raise AnyTXTBackendError(f"AnyTXT request failed: {exc}") from exc
            except URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise AnyTXTRequestTimeout(f"AnyTXT request timed out: {exc}") from exc
                raise AnyTXTBackendError(f"AnyTXT request failed: {exc}") from exc
            except OSError as exc:
                raise AnyTXTBackendError(f"AnyTXT request failed: {exc}") from exc
        finally:
            self._endpoint_gate.release()
        try:
            result = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnyTXTBackendError("AnyTXT returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise AnyTXTBackendError("AnyTXT response must be a JSON object")
        if result.get("error"):
            raise AnyTXTBackendError(f"AnyTXT RPC error: {result['error']!r}")
        return result

    async def _rpc(self, method: str, params: Dict[str, Any], budget: _Budget) -> Dict[str, Any]:
        kind = "search" if method == self.search_method else "fragment"
        # AnyTXT answers in milliseconds but was observed to stall for seconds
        # under sustained load.  A stalled response is retried once while the
        # budget allows; connection failures are not retried because they
        # normally mean the service itself is gone.
        attempts = 2
        for attempt in range(attempts):
            budget.charge()
            timeout = min(self.config.request_timeout, budget.remaining)
            if timeout <= 0:
                raise AnyTXTBudgetExhausted("AnyTXT retrieve time budget exhausted")
            payload = {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": method,
                "params": {"input": params} if self._nested_params else params,
            }
            cancelled = threading.Event()
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(self._post, payload, timeout, kind, cancelled),
                    timeout=timeout,
                )
            except asyncio.CancelledError:
                cancelled.set()
                raise
            except (asyncio.TimeoutError, AnyTXTRequestTimeout) as exc:
                cancelled.set()
                can_retry = (
                    attempt + 1 < attempts
                    and budget.remaining > 0
                    and budget.requests < budget.max_requests
                )
                if not can_retry:
                    raise AnyTXTBackendError("AnyTXT request timed out") from exc
                logger.warning("AnyTXT request timed out; retrying once")
        raise AnyTXTBackendError("AnyTXT request timed out")

    async def search(
        self, pattern: str, filter_dir: str, filter_ext: str, offset: int, limit: int, budget: _Budget
    ) -> SearchPage:
        result = await self._rpc(
            self.search_method,
            {
                "pattern": pattern,
                "filterDir": filter_dir,
                "filterExt": filter_ext,
                "lastModifyBegin": 0,
                "lastModifyEnd": 2147483647,
                "limit": limit,
                "offset": offset,
                "order": 0,
            },
            budget,
        )
        output = _rpc_output(result)
        if not isinstance(output, dict):
            raise AnyTXTBackendError("AnyTXT search response is missing output")
        files = _normalise_files(output.get("files"), output.get("field"))
        count = output.get("count")
        if not isinstance(count, int) or count < 0:
            count = None
        return SearchPage(tuple(files), count)

    async def get_fragment(self, fid: Any, pattern: str, budget: _Budget) -> str:
        result = await self._rpc(self.fragment_method, {"fid": fid, "pattern": pattern}, budget)
        output = _rpc_output(result)
        if not isinstance(output, dict):
            raise AnyTXTBackendError("AnyTXT fragment response is missing output")
        text = output.get("text", "")
        if not isinstance(text, str):
            raise AnyTXTBackendError("AnyTXT fragment text must be a string")
        return text

    async def health_check(self) -> Dict[str, Any]:
        budget = _Budget(self.config, self.config.request_timeout)
        page = await self.search("zzzzanysirchmunkhealthcheck", "", "", 0, 1, budget)
        return {"healthy": True, "rpc_method": self.search_method, "structured": isinstance(page.files, tuple)}


class AnyTXTRetriever(BaseRetriever):
    """List-compatible Sirchmunk retriever backed by an AnyTXT index."""

    _REGEX_META = re.compile(r"[.\\+*?\[\](){}|^$]")

    def __init__(
        self,
        work_path: Optional[PathLike] = None,
        *,
        config: Optional[AnyTXTConfig] = None,
        client: Optional[AnyTXTClient] = None,
        fallback: Optional[ReadOnlyRetriever] = None,
        **_: Any,
    ) -> None:
        super().__init__()
        self.work_path = Path(work_path).expanduser().resolve() if work_path else None
        self.config = config or AnyTXTConfig.from_env()
        self.client = client or AnyTXTClient(self.config)
        self.fallback = fallback
        self.last_metadata: Dict[str, Any] = {}

    @staticmethod
    def merge_results(raw_results: List[Dict[str, Any]], limit: int = 50) -> List[Dict[str, Any]]:
        current_path: Optional[str] = None
        current: List[Dict[str, Any]] = []
        current_metadata: Dict[str, Any] = {}
        grouped: List[Dict[str, Any]] = []
        metadata = getattr(raw_results, "metadata", {})
        for item in raw_results:
            kind = item.get("type")
            if kind == "begin":
                current_path = item.get("data", {}).get("path", {}).get("text")
                current = []
                current_metadata = item.get("_retrieval_metadata", metadata)
            elif kind == "match" and current_path:
                current.append(item)
            elif kind == "end" and current_path:
                selected = sorted(current, key=lambda value: value.get("score", 0.0), reverse=True)[:limit]
                grouped.append({
                    "path": current_path,
                    "matches": selected,
                    "lines": [m.get("data", {}).get("lines", {}).get("text", "") for m in selected],
                    "total_matches": len(current),
                    "total_score": sum(float(m.get("score", 0.0)) for m in selected),
                    "_retrieval_metadata": dict(current_metadata),
                })
                current_path = None
                current = []
                current_metadata = {}
        return grouped

    async def retrieve(
        self,
        terms: Union[str, List[str]],
        path: Union[PathLike, Sequence[PathLike], None] = None,
        logic: Literal["and", "or", "not"] = "or",
        *,
        case_sensitive: bool = False,
        whole_word: bool = False,
        literal: bool = False,
        regex: bool = True,
        max_depth: Optional[int] = None,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        file_type: Optional[str] = None,
        invert_match: bool = False,
        count_only: bool = False,
        timeout: float = 30.0,
        **fallback_kwargs: Any,
    ) -> List[Dict[str, Any]]:
        started = time.monotonic()
        caller_timeout = timeout if timeout and timeout > 0 else self.config.total_timeout
        effective_timeout = min(self.config.total_timeout, caller_timeout)
        query_terms = [terms] if isinstance(terms, str) else list(terms)
        query_terms = [term for term in query_terms if isinstance(term, str) and term]
        if not query_terms:
            return RetrievalEvents(metadata=self._metadata(True, False, None, path))
        roots = _normalise_roots(path)
        try:
            self._validate_semantics(query_terms, logic, case_sensitive, whole_word, literal, regex, invert_match, count_only)
            events = await self._retrieve_anytxt(
                query_terms, roots, logic, max_depth, include, exclude, file_type, effective_timeout
            )
            self.last_metadata = dict(events.metadata)
            return events
        except asyncio.CancelledError:
            raise
        except AnyTXTBudgetExhausted:
            logger.warning("AnyTXT retrieve budget exhausted before a candidate was collected")
            metadata = self._metadata(
                False, True, "budget_exhausted",
                roots or list(self.config.global_roots) or SERVER_DEFAULT_SCOPE,
            )
            self.last_metadata = metadata
            return RetrievalEvents(metadata=metadata)
        except (AnyTXTBackendError, AnyTXTUnsupportedQuery, AnyTXTIncompleteResults) as exc:
            remaining = effective_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise AnyTXTBudgetExhausted("AnyTXT retrieve time budget exhausted") from exc
            return await self._fallback_or_raise(
                exc, query_terms, roots, logic, case_sensitive, whole_word, literal, regex,
                max_depth, include, exclude, file_type, invert_match, count_only, remaining,
                fallback_kwargs,
            )

    def _validate_semantics(
        self, terms: List[str], logic: str, case_sensitive: bool, whole_word: bool,
        literal: bool, regex: bool, invert_match: bool, count_only: bool,
    ) -> None:
        if logic not in {"and", "or", "not"}:
            raise AnyTXTUnsupportedQuery(f"unsupported logic: {logic}")
        if case_sensitive or whole_word or invert_match or count_only:
            raise AnyTXTUnsupportedQuery("case/word/invert/count semantics are not verified")
        if regex and not literal:
            raise AnyTXTUnsupportedQuery("RPC regular-expression mode is not verified")
        if literal and any(self._REGEX_META.search(term) for term in terms):
            raise AnyTXTUnsupportedQuery("literal escaping for regex metacharacters is not verified")

    async def _retrieve_anytxt(
        self, terms: List[str], roots: List[str], logic: str, max_depth: Optional[int],
        include: Optional[List[str]], exclude: Optional[List[str]], file_type: Optional[str], timeout: float,
    ) -> RetrievalEvents:
        budget = _Budget(self.config, timeout)
        complete = True
        reasons: List[str] = []
        # "No explicit range" cannot be expressed as an empty filterDir: the
        # service resolves it to its own default directory and returns that
        # drive only, which silently drops every other indexed volume.
        scope_roots = roots or list(self.config.global_roots)
        if scope_roots:
            scopes: List[str] = scope_roots
            effective_scope: Any = scope_roots
        else:
            scopes = [""]
            effective_scope = SERVER_DEFAULT_SCOPE
            complete = False
            reasons.append(UNVERIFIED_GLOBAL_REASON)
            logger.warning(
                "ANYTXT_GLOBAL_ROOTS is not configured. An empty filterDir is resolved by "
                "AnyTXT to its own directory, so this result is not a global index search. "
                'Set ANYTXT_GLOBAL_ROOTS, for example ["C:\\\\", "D:\\\\", "E:\\\\"].'
            )
        per_term: List[Dict[str, Dict[str, Any]]] = []
        for term in terms:
            found: Dict[str, Dict[str, Any]] = {}
            for scope in scopes:
                offset = 0
                seen_pages: set[tuple[Any, ...]] = set()
                while True:
                    try:
                        page = await self.client.search(term, scope, _file_type_glob(file_type), offset, self.config.page_size, budget)
                    except AnyTXTBudgetExhausted:
                        # Exhausting the request budget degrades the result, it must
                        # not throw away the candidates already collected.
                        complete = False
                        reasons.append("budget_exhausted")
                        break
                    signature = tuple(record.get("fid") for record in page.files)
                    if signature and signature in seen_pages:
                        complete = False
                        reasons.append("repeated_page")
                        break
                    seen_pages.add(signature)
                    valid_received = 0
                    for record in page.files:
                        tagged_record = dict(record, _term=term)
                        try:
                            candidate = self._candidate(tagged_record, scope_roots, max_depth, include, exclude)
                        except ValueError:
                            complete = False
                            reasons.append("invalid_record")
                            continue
                        if candidate is None:
                            continue
                        valid_received += 1
                        key = _path_key(candidate["path"])
                        if key not in found:
                            found[key] = candidate
                            if sum(len(group) for group in per_term) + len(found) >= self.config.max_candidates:
                                complete = False
                                reasons.append("candidate_budget")
                                break
                    if not complete and reasons[-1] == "candidate_budget":
                        break
                    received = len(page.files)
                    if received < self.config.page_size:
                        break
                    offset += received
                if not complete and reasons and reasons[-1] in {"repeated_page", "candidate_budget", "budget_exhausted"}:
                    break
            per_term.append(found)
            if "budget_exhausted" in reasons:
                break

        if logic in {"and", "not"} and not complete:
            raise AnyTXTIncompleteResults(f"{logic.upper()} requires complete upstream sets")
        selected = self._combine(per_term, logic)
        events: List[Dict[str, Any]] = []
        fragment_chars = 0
        fragment_cache: Dict[tuple[Any, str], str] = {}
        fragments_exhausted = False
        # Snippets are an optional enhancement: Sirchmunk reads the source file
        # itself.  Giving them a separate budget keeps candidate discovery from
        # being starved by fragment lookups.
        fragment_budget = _Budget(
            self.config,
            timeout,
            max_requests=self.config.max_fragment_requests,
            deadline=budget.deadline,
        )
        for key, candidate in selected.items():
            snippets: List[str] = []
            matched_terms = candidate.pop("_terms", terms[:1])
            for term in matched_terms:
                if fragments_exhausted:
                    break
                remaining = self.config.max_fragment_chars - fragment_chars
                if remaining <= 0:
                    complete = False
                    reasons.append("fragment_budget")
                    fragments_exhausted = True
                    break
                cache_key = (candidate["fid"], term)
                try:
                    if cache_key not in fragment_cache:
                        fragment_cache[cache_key] = await self.client.get_fragment(candidate["fid"], term, fragment_budget)
                    text = fragment_cache[cache_key]
                except AnyTXTBudgetExhausted:
                    # Keep every candidate that was already discovered.
                    complete = False
                    reasons.append("fragment_request_budget")
                    fragments_exhausted = True
                    break
                except AnyTXTBackendError:
                    complete = False
                    reasons.append("fragment_failure")
                    continue
                if len(text) > remaining:
                    complete = False
                    reasons.append("fragment_budget")
                    fragments_exhausted = True
                    text = text[:remaining]
                fragment_chars += len(text)
                if text:
                    snippets.append(text)
            if not snippets:
                snippets = [""]
            path_text = candidate["path"]
            common = {"path": {"text": path_text}}
            events.append({"type": "begin", "data": common, "_search_backend": "anytxt"})
            for snippet in snippets:
                events.append({
                    "type": "match",
                    "data": {"path": {"text": path_text}, "lines": {"text": snippet}},
                    "score": 1.0,
                    "_search_backend": "anytxt",
                    "_anytxt": {name: candidate[name] for name in ("fid", "lastModify", "size") if name in candidate},
                })
            events.append({"type": "end", "data": common, "_search_backend": "anytxt"})
        metadata = self._metadata(complete, not complete, ",".join(dict.fromkeys(reasons)) or None, effective_scope)
        metadata.update({
            "requests": budget.requests + fragment_budget.requests,
            "search_requests": budget.requests,
            "fragment_requests": fragment_budget.requests,
            "candidates": len(selected),
            "fragment_chars": fragment_chars,
        })
        for event in events:
            event["_retrieval_metadata"] = metadata
        return RetrievalEvents(events, metadata=metadata)

    @staticmethod
    def _combine(groups: List[Dict[str, Dict[str, Any]]], logic: str) -> Dict[str, Dict[str, Any]]:
        if not groups:
            return {}
        if logic == "or":
            result: Dict[str, Dict[str, Any]] = {}
            for index, group in enumerate(groups):
                for key, value in group.items():
                    stored = result.setdefault(key, dict(value, _terms=[]))
                    stored["_terms"].append(value.get("_term", str(index)))
            return result
        if logic == "and":
            keys = set(groups[0])
            for group in groups[1:]:
                keys.intersection_update(group)
            return {key: dict(groups[0][key], _terms=[group[key]["_term"] for group in groups]) for key in keys}
        excluded = set().union(*(set(group) for group in groups[1:])) if len(groups) > 1 else set()
        return {key: dict(value, _terms=[value["_term"]]) for key, value in groups[0].items() if key not in excluded}

    def _candidate(
        self, record: Dict[str, Any], roots: List[str], max_depth: Optional[int],
        include: Optional[List[str]], exclude: Optional[List[str]],
    ) -> Optional[Dict[str, Any]]:
        file_path = record.get("file")
        if not isinstance(file_path, str) or not file_path or not _is_absolute_path(file_path):
            return None
        absolute = _absolute_path(file_path)
        matching_root = next((root for root in roots if _is_within(absolute, root)), None) if roots else None
        if roots and matching_root is None:
            return None
        relative = ntpath.relpath(absolute, matching_root) if matching_root else ntpath.basename(absolute)
        if max_depth is not None and matching_root and len([p for p in relative.split(ntpath.sep)[:-1] if p]) > max_depth:
            return None
        if include and not any(_matches_glob(relative, pattern) for pattern in include):
            return None
        if exclude and any(_matches_glob(relative, pattern) for pattern in exclude):
            return None
        fid = record.get("fid")
        if fid is None or fid == "":
            raise ValueError("AnyTXT candidate is missing fid")
        if not os.path.isfile(absolute):
            raise ValueError("AnyTXT candidate file does not exist")
        result = {"path": absolute, "fid": fid, "lastModify": record.get("lastModify"), "size": record.get("size")}
        result["_term"] = record.get("_term", "")
        return result

    async def _fallback_or_raise(
        self, error: AnyTXTError, terms: List[str], roots: List[str], logic: str,
        case_sensitive: bool, whole_word: bool, literal: bool, regex: bool,
        max_depth: Optional[int], include: Optional[List[str]], exclude: Optional[List[str]],
        file_type: Optional[str], invert_match: bool, count_only: bool, timeout: float,
        extra: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not self.config.fallback_to_rga or self.fallback is None:
            raise error
        fallback_roots = roots or list(self.config.fallback_roots)
        if not fallback_roots:
            raise error
        raw = await self.fallback.retrieve(
            terms=terms, path=fallback_roots, logic=logic, case_sensitive=case_sensitive,
            whole_word=whole_word, literal=literal, regex=regex, max_depth=max_depth,
            include=include, exclude=exclude, file_type=file_type, invert_match=invert_match,
            count_only=count_only, timeout=timeout, **extra,
        )
        metadata = {
            "complete": True,
            "truncated": False,
            "reason": None,
            "actual_backend": "rga",
            "effective_scope": fallback_roots,
            "scope_reduced": not bool(roots),
            "fallback_reason": error.__class__.__name__,
        }
        for item in raw:
            item["_search_backend"] = "rga"
            item["_fallback_reason"] = error.__class__.__name__
            item["_retrieval_metadata"] = metadata
        events = RetrievalEvents(raw, metadata=metadata)
        self.last_metadata = metadata
        logger.warning("AnyTXT search fell back to rga: %s", error)
        return events

    def _metadata(self, complete: bool, truncated: bool, reason: Optional[str], scope: Any) -> Dict[str, Any]:
        if isinstance(scope, str):
            effective_scope: Any = scope
        else:
            effective_scope = _normalise_roots(scope) or list(self.config.global_roots) or SERVER_DEFAULT_SCOPE
        return {
            "complete": complete,
            "truncated": truncated,
            "reason": reason,
            "actual_backend": "anytxt",
            "effective_scope": effective_scope,
            "scope_reduced": False,
        }


def _normalise_files(value: Any, fields: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(fields, str):
        names = [part.strip() for part in fields.split(",") if part.strip()]
    elif isinstance(fields, list):
        names = [str(part) for part in fields]
    else:
        names = []
    result: List[Dict[str, Any]] = []
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                result.append(dict(row))
            elif isinstance(row, (list, tuple)) and names:
                result.append(dict(zip(names, row)))
    elif isinstance(value, dict):
        # Some builds return a column-oriented object.
        lengths = [len(column) for column in value.values() if isinstance(column, list)]
        for index in range(max(lengths, default=0)):
            result.append({name: column[index] for name, column in value.items() if isinstance(column, list) and index < len(column)})
    return result


def _rpc_output(response: Dict[str, Any]) -> Any:
    """Extract the AnyTXT payload from its JSON-RPC result envelope."""
    result = response.get("result")
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict) and "output" in data:
            return data["output"]
    # Kept for compatible test doubles and builds observed without an envelope.
    return response.get("output")


def _is_absolute_path(value: str) -> bool:
    return ntpath.isabs(value) or Path(value).is_absolute()


def _absolute_path(value: str) -> str:
    if ntpath.isabs(value):
        if os.name == "nt":
            return str(Path(value).expanduser().resolve(strict=False))
        return ntpath.normpath(value)
    return str(Path(value).expanduser().resolve())


def _env_roots(name: str, raw: str) -> List[str]:
    """Parse a JSON array of existing absolute directories from the environment."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON array") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a JSON array of absolute paths")
    roots: List[str] = []
    for item in value:
        if not _is_absolute_path(item):
            raise ValueError(f"{name} contains a non-absolute path: {item!r}")
        root = _absolute_path(item)
        if not os.path.isdir(root):
            raise ValueError(f"{name} is not an existing directory: {item!r}")
        roots.append(root)
    return roots


def _normalise_roots(path: Any) -> List[str]:
    if path is None or path == "":
        return []
    values = [path] if isinstance(path, (str, Path)) else list(path)
    roots: List[str] = []
    for value in values:
        text = str(value)
        if not _is_absolute_path(text):
            raise ValueError(f"search roots must be absolute: {text!r}")
        absolute = _absolute_path(text)
        if _path_key(absolute) not in {_path_key(root) for root in roots}:
            roots.append(absolute)
    return roots


def _path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _is_within(candidate: str, root: str) -> bool:
    try:
        return ntpath.commonpath([_path_key(candidate), _path_key(root)]) == _path_key(root)
    except ValueError:
        return False


def _matches_glob(relative: str, pattern: str) -> bool:
    value = relative.replace("\\", "/")
    normalised = pattern.replace("\\", "/")
    return fnmatch.fnmatch(value.lower(), normalised.lower()) or fnmatch.fnmatch(ntpath.basename(value).lower(), normalised.lower())


def _file_type_glob(file_type: Optional[str]) -> str:
    if not file_type:
        return ""
    value = file_type.lstrip(".*")
    return f"*.{value}"
