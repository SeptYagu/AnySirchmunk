"""AnyTXT Searcher JSON-RPC adapter for Sirchmunk.

This module intentionally uses only the Python standard library.  AnyTXT is a
candidate/fragment service; Sirchmunk remains responsible for reading source
files and building evidence.

Only the documented AnyTXT **v1** API (``anytxt.v1.*`` on
``http://127.0.0.1:9924/rpc``, AnyTXT 1.3.3541 or newer) is supported.  The
legacy ``ATRpcServer.Searcher.V1.*`` service on port 9920 is not: it cannot
serve a full DEEP query (it exits mid-run under that load) and it uses a
different parameter envelope, so supporting both only widened the failure
surface.
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

#: The documented AnyTXT v1 surface.  API versions are carried by the JSON-RPC
#: method name, so the URL itself has no version segment.
DEFAULT_API_URL = "http://127.0.0.1:9924/rpc"
SEARCH_METHOD = "anytxt.v1.getResult"
COUNT_METHOD = "anytxt.v1.search"
FRAGMENT_METHOD = "anytxt.v1.getFragment"
TEXT_METHOD = "anytxt.v1.getText"
STATUS_METHOD = "anytxt.v1.status"

#: ``result.errno`` is AnyTXT's business status and is *not* a transport
#: failure.  Measured on 1.3.3541: ``filterDir`` pointing at an unindexed
#: volume and an unresolvable ``fid`` both answer ``errno = 1`` with an empty
#: payload, while malformed requests use the JSON-RPC ``error`` member
#: (``-32602``).  Ignoring ``errno`` makes an unsearchable scope look exactly
#: like a scope with no matches.
ERRNO_OK = 0

#: AnyTXT's fragment text wraps every hit in these markers.  They are a GUI
#: artefact; the evidence handed to Sirchmunk must be the document text.
HIGHLIGHT_OPEN = "*<<*"
HIGHLIGHT_CLOSE = "*>>*"

#: v1 rejects ``limit`` outside this range with ``-32602``.
PAGE_SIZE_MIN = 1
PAGE_SIZE_MAX = 300

#: ``filterDir=""`` is *not* a global search.  AnyTXT resolves the empty value to
#: its own current directory (observed: ``C:``) and returns that drive only.
SERVER_DEFAULT_SCOPE = "anytxt_server_default"
UNVERIFIED_GLOBAL_REASON = "unverified_global_scope"
ENGINE_NOT_READY_REASON = "engine_not_ready"

#: A deliberately impossible-looking literal used only to distinguish an
#: indexed scope (``errno=0, count=0``) from an unindexed one (``errno=1``).
#: The probe asks AnyTXT's index; it never walks the drive or changes the index.
INDEX_SCOPE_PROBE_PATTERN = "__anysirchmunk_index_scope_probe_7d63f94c__"


class AnyTXTError(RuntimeError):
    """Base class for failures which may trigger a bounded rga fallback."""


class AnyTXTBackendError(AnyTXTError):
    """The local service could not complete a valid RPC request."""


class AnyTXTRequestTimeout(AnyTXTBackendError):
    """The local service did not answer before the per-request timeout."""


class AnyTXTProtocolError(AnyTXTBackendError):
    """The service rejected the request at the JSON-RPC layer.

    Codes worth naming (AnyTXT v1 reference): ``-32601`` method not found,
    ``-32602`` missing or invalid parameter.  Both mean the adapter asked for
    something this build cannot do, so they must not be reported as a generic
    transport failure.
    """


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
    #: Loopback URL of the AnyTXT v1 JSON-RPC service.
    api_url: str = DEFAULT_API_URL
    page_size: int = 300
    request_timeout: float = 15.0
    total_timeout: float = 30.0
    #: Verified safe against AnyTXT 1.3.2477: its RPC service segfaults when
    #: several requests overlap with non-ASCII patterns, which kills the whole
    #: service for the rest of the run.  Concurrency 2 passed 90 consecutive
    #: requests including CJK patterns; higher values stay unverified.
    #: 1.3.3541 survived 600 mixed requests at this value.
    max_concurrency: int = 2
    #: Deterministic page ordering.  AnyTXT's default order (0) is not
    #: guaranteed stable across pages, which is what makes a long enumeration
    #: risk skipping or repeating a file.  3 = path ascending.
    page_order: int = 3
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
    #: When no global roots are configured, enumerate local fixed-drive roots
    #: and retain only scopes accepted by AnyTXT.  Explicit configuration
    #: always wins, and removable/network drives are never probed.
    auto_discover_roots: bool = True
    #: Ask ``anytxt.v1.search`` for the exact total before paging.  One extra
    #: request per term and scope turns "did we see every match" from a
    #: heuristic into an answer.
    exact_count: bool = True

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

        # The legacy mode was removed together with its endpoint.  Failing loudly
        # beats silently answering through a different API than the one the
        # operator configured.
        api_mode = os.getenv("ANYTXT_API_MODE", "v1").strip().lower()
        if api_mode != "v1":
            raise ValueError(
                "ANYTXT_API_MODE=%r is no longer supported: AnySirchmunk now requires the AnyTXT "
                "1.3.3541+ v1 API (anytxt.v1.* on http://127.0.0.1:9924/rpc)" % api_mode
            )
        api_url = os.getenv("ANYTXT_API_URL", cls.api_url)
        parsed = urlparse(api_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("ANYTXT_API_URL must be an HTTP loopback URL")
        page_size = positive("ANYTXT_SEARCH_LIMIT", "300", int)
        if not PAGE_SIZE_MIN <= page_size <= PAGE_SIZE_MAX:
            raise ValueError(
                f"ANYTXT_SEARCH_LIMIT must be between {PAGE_SIZE_MIN} and {PAGE_SIZE_MAX}; "
                "AnyTXT v1 rejects any other limit with -32602"
            )
        fallback = os.getenv("ANYTXT_FALLBACK_TO_RGA", "true").strip().lower()
        if fallback not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("ANYTXT_FALLBACK_TO_RGA must be a boolean")
        exact = os.getenv("ANYTXT_EXACT_COUNT", "true").strip().lower()
        if exact not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("ANYTXT_EXACT_COUNT must be a boolean")
        auto_discover = os.getenv("ANYTXT_AUTO_DISCOVER_ROOTS", "true").strip().lower()
        if auto_discover not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("ANYTXT_AUTO_DISCOVER_ROOTS must be a boolean")
        return cls(
            api_url=api_url,
            page_size=page_size,
            request_timeout=positive("ANYTXT_REQUEST_TIMEOUT", "15", float),
            total_timeout=positive("ANYTXT_TOTAL_TIMEOUT", "30", float),
            max_concurrency=positive("ANYTXT_MAX_CONCURRENCY", "2", int),
            page_order=positive("ANYTXT_PAGE_ORDER", "3", int),
            max_requests=positive("ANYTXT_MAX_REQUESTS", "100", int),
            max_candidates=positive("ANYTXT_MAX_CANDIDATES", "3000", int),
            max_fragment_chars=positive("ANYTXT_MAX_FRAGMENT_CHARS", "100000", int),
            max_fragment_requests=positive("ANYTXT_MAX_FRAGMENT_REQUESTS", "100", int),
            fallback_to_rga=fallback in {"true", "1", "yes"},
            fallback_roots=tuple(roots),
            global_roots=tuple(global_roots),
            auto_discover_roots=auto_discover in {"true", "1", "yes"},
            exact_count=exact in {"true", "1", "yes"},
        )


@dataclass(frozen=True)
class SearchPage:
    """One page of matching file metadata.

    ``count`` is the number of rows in *this* page: ``getResult`` does not
    report a total.  Use :class:`SearchTotal` for that.
    """

    files: tuple[Dict[str, Any], ...]
    count: Optional[int]
    errno: int = ERRNO_OK


@dataclass(frozen=True)
class SearchTotal:
    """Answer of ``anytxt.v1.search``: the exact number of matching files.

    Measured against ``filterExt`` as well, so it is the authoritative total for
    the same (pattern, filterDir, filterExt) enumeration that paging walks.
    ``errno`` other than 0 means the scope itself could not be searched.
    """

    total: Optional[int]
    errno: int = ERRNO_OK


@dataclass(frozen=True)
class IndexedText:
    """Indexed text returned for literal candidate verification."""

    text: str
    truncated: bool = False


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
    def __init__(self, config: AnyTXTConfig) -> None:
        self.config = config
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
                raise AnyTXTBackendError(f"AnyTXT request failed: {exc}{_http_hint(exc)}") from exc
            except URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise AnyTXTRequestTimeout(f"AnyTXT request timed out: {exc}") from exc
                if _is_connection_refused(exc.reason) or _is_connection_refused(exc):
                    raise AnyTXTBackendError(_unreachable_message(self.config.api_url)) from exc
                raise AnyTXTBackendError(f"AnyTXT request failed: {exc}") from exc
            except OSError as exc:
                if _is_connection_refused(exc):
                    raise AnyTXTBackendError(_unreachable_message(self.config.api_url)) from exc
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
            raise AnyTXTProtocolError(_protocol_error_message(result["error"]))
        return result

    async def _rpc(self, method: str, params: Dict[str, Any], budget: _Budget) -> Dict[str, Any]:
        # Only fragment lookups are gated separately: AnyTXT used to die when a
        # GetResult and a GetFragment were in flight together.  Counting and
        # readiness queries belong to the same search family.
        kind = "fragment" if method == FRAGMENT_METHOD else "search"
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
                "params": params,
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
            SEARCH_METHOD,
            _search_params(pattern, filter_dir, filter_ext, order=self.config.page_order)
            | {"limit": limit, "offset": offset},
            budget,
        )
        output = _rpc_output(result)
        if not isinstance(output, dict):
            raise AnyTXTBackendError("AnyTXT search response is missing output")
        files = _normalise_files(output.get("files"), output.get("field"))
        count = output.get("count")
        if not isinstance(count, int) or count < 0:
            count = None
        return SearchPage(tuple(files), count, _rpc_errno(result))

    async def count(self, pattern: str, filter_dir: str, filter_ext: str, budget: _Budget) -> SearchTotal:
        """Exact number of indexed files matching the same filters as paging."""
        result = await self._rpc(
            COUNT_METHOD,
            _search_params(pattern, filter_dir, filter_ext),
            budget,
        )
        errno = _rpc_errno(result)
        if errno != ERRNO_OK:
            return SearchTotal(None, errno)
        output = _rpc_output(result)
        if not isinstance(output, dict):
            raise AnyTXTBackendError("AnyTXT count response is missing output")
        total = output.get("count")
        if not isinstance(total, int) or total < 0:
            total = None
        return SearchTotal(total, errno)

    async def get_fragment(self, fid: Any, pattern: str, budget: _Budget) -> Optional[str]:
        """Return the matching snippet exactly as AnyTXT sends it, or ``None``.

        An unresolvable ``fid`` answers ``errno = 1`` with ``text: null`` rather
        than a protocol error, so "no snippet" must not be mistaken for a
        transport failure.  The returned text still carries AnyTXT's ``*<<*``
        hit markers: decoding the wire format is this layer's job, but removing
        presentation markup belongs to whoever builds the evidence.
        """
        result = await self._rpc(FRAGMENT_METHOD, {"fid": fid, "pattern": pattern}, budget)
        if _rpc_errno(result) != ERRNO_OK:
            return None
        output = _rpc_output(result)
        if not isinstance(output, dict):
            raise AnyTXTBackendError("AnyTXT fragment response is missing output")
        text = output.get("text")
        if text is None:
            return None
        if not isinstance(text, str):
            raise AnyTXTBackendError("AnyTXT fragment text must be a string")
        return text

    async def get_text(self, fid: Any, budget: _Budget) -> Optional[IndexedText]:
        """Return indexed text for verification, never as final source evidence."""
        result = await self._rpc(TEXT_METHOD, {"fid": fid}, budget)
        if _rpc_errno(result) != ERRNO_OK:
            return None
        output = _rpc_output(result)
        if not isinstance(output, dict):
            raise AnyTXTBackendError("AnyTXT indexed-text response is missing output")
        text = output.get("text")
        if not isinstance(text, str):
            raise AnyTXTBackendError("AnyTXT indexed text must be a string")
        return IndexedText(text=text, truncated=output.get("truncated") is True)

    async def status(self, budget: _Budget) -> Optional[bool]:
        """Whether the search engine finished loading, or ``None`` if unknown."""
        result = await self._rpc(STATUS_METHOD, {}, budget)
        if _rpc_errno(result) != ERRNO_OK:
            return None
        output = _rpc_output(result)
        if not isinstance(output, dict):
            return None
        ready = output.get("return")
        return ready if isinstance(ready, bool) else None

    async def health_check(self) -> Dict[str, Any]:
        budget = _Budget(self.config, self.config.request_timeout)
        ready = await self.status(budget)
        return {
            "healthy": True,
            "rpc_method": STATUS_METHOD,
            "engine_ready": ready,
            "api_url": self.config.api_url,
        }


class AnyTXTRetriever(BaseRetriever):
    """List-compatible Sirchmunk retriever backed by an AnyTXT index."""

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
        self._discovered_global_roots: Optional[tuple[str, ...]] = None

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
        if any(re.search(r'[&|!()"]', term) for term in terms):
            # These characters are operators in AnyTXT's expression grammar.
            # The v1 reference does not document a general escape sequence, so
            # fail closed instead of allowing a literal to become an expression.
            raise AnyTXTUnsupportedQuery("literal escaping for AnyTXT expression operators is not verified")

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
        scope_source = "explicit" if roots else "configured"
        scope_roots = roots or list(self.config.global_roots)
        if not scope_roots and self.config.auto_discover_roots:
            scope_roots = list(await self._discover_global_roots(budget))
            scope_source = "autodiscovered"
        if scope_roots:
            scopes: List[str] = scope_roots
            effective_scope: Any = scope_roots
        else:
            scope_source = "server_default"
            scopes = [""]
            effective_scope = SERVER_DEFAULT_SCOPE
            complete = False
            reasons.append(UNVERIFIED_GLOBAL_REASON)
            logger.warning(
                "No indexed fixed-drive roots were configured or discovered. An empty "
                "filterDir is resolved by AnyTXT to its own directory, so this result is "
                "not a global index search. Set ANYTXT_GLOBAL_ROOTS to override discovery."
            )
        try:
            ready = await self.client.status(budget)
        except AnyTXTError as exc:
            # Readiness is diagnostics: it must never be a reason to fail a query.
            logger.warning("AnyTXT readiness check failed: %s", exc)
            ready = None
        if ready is False:
            # The service is up but the search engine has not finished loading,
            # so an empty answer here would be indistinguishable from "no match".
            complete = False
            reasons.append(ENGINE_NOT_READY_REASON)
            logger.warning("AnyTXT reports that its search engine has not finished loading")
        per_term: List[Dict[str, Dict[str, Any]]] = []
        candidate_keys: set[str] = set()
        text_validation_requests = 0
        text_validation_chars = 0
        for term in terms:
            wire_pattern = _literal_candidate_expression(term)
            found: Dict[str, Dict[str, Any]] = {}
            filter_ext = _file_type_glob(file_type)
            for scope in scopes:
                expected: Optional[int] = None
                if self.config.exact_count:
                    try:
                        total = await self.client.count(wire_pattern, scope, filter_ext, budget)
                    except AnyTXTBudgetExhausted:
                        # Counting is an optimisation; paging still works without it.
                        total = None
                    if total is not None:
                        if total.errno != ERRNO_OK:
                            # The scope itself cannot be searched (measured: an
                            # unindexed drive answers errno 1 with an empty
                            # payload).  Keep what other scopes delivered, but
                            # never let it pass as "this scope had no match".
                            complete = False
                            reasons.append(f"scope_errno_{total.errno}")
                            continue
                        expected = total.total
                offset = 0
                unique_received_total = 0
                halt_scopes = False
                seen_pages: set[tuple[Any, ...]] = set()
                seen_fids: set[str] = set()
                if expected == 0:
                    continue
                while True:
                    try:
                        page = await self.client.search(
                            wire_pattern, scope, filter_ext, offset, self.config.page_size, budget
                        )
                    except AnyTXTBudgetExhausted:
                        # Exhausting the request budget degrades the result, it must
                        # not throw away the candidates already collected.
                        complete = False
                        reasons.append("budget_exhausted")
                        halt_scopes = True
                        break
                    if page.errno != ERRNO_OK:
                        complete = False
                        reasons.append(f"scope_errno_{page.errno}")
                        break
                    signature = tuple(_record_signature(record) for record in page.files)
                    if signature and signature in seen_pages:
                        complete = False
                        reasons.append("repeated_page")
                        halt_scopes = True
                        break
                    seen_pages.add(signature)
                    for record in page.files:
                        fid = record.get("fid")
                        if not isinstance(fid, str) or not fid:
                            continue
                        if fid in seen_fids:
                            complete = False
                            reasons.append("overlapping_page")
                            continue
                        seen_fids.add(fid)
                        unique_received_total += 1
                    candidate_budget_hit = False
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
                        key = _path_key(candidate["path"])
                        if key not in found:
                            if key not in candidate_keys and len(candidate_keys) >= self.config.max_candidates:
                                complete = False
                                reasons.append("candidate_budget")
                                candidate_budget_hit = True
                                break
                            found[key] = candidate
                            candidate_keys.add(key)
                    if candidate_budget_hit:
                        halt_scopes = True
                        break
                    if expected is not None:
                        if unique_received_total == expected:
                            # Every distinct record the count promised has been delivered.
                            break
                        if unique_received_total > expected:
                            complete = False
                            reasons.append("count_mismatch")
                            break
                    if len(page.files) < self.config.page_size:
                        if expected is not None and unique_received_total != expected:
                            # The server promised more records than it served.
                            complete = False
                            reasons.append("incomplete_enumeration")
                        break
                    offset += len(page.files)
                if halt_scopes:
                    break
            if _needs_text_validation(term) and found:
                verified: Dict[str, Dict[str, Any]] = {}
                for key, candidate in found.items():
                    try:
                        text_validation_requests += 1
                        indexed = await self.client.get_text(candidate["fid"], budget)
                    except AnyTXTBudgetExhausted:
                        complete = False
                        reasons.append("text_validation_budget")
                        break
                    except AnyTXTBackendError:
                        complete = False
                        reasons.append("text_validation_failure")
                        continue
                    if indexed is None:
                        complete = False
                        reasons.append("text_validation_unavailable")
                        continue
                    text_validation_chars += len(indexed.text)
                    snippet = _literal_snippet(indexed.text, term)
                    if snippet is not None:
                        candidate.setdefault("_validated_snippets", {})[term] = snippet
                        verified[key] = candidate
                    elif indexed.truncated:
                        complete = False
                        reasons.append("text_validation_truncated")
                found = verified
            per_term.append(found)
            if "budget_exhausted" in reasons:
                break

        exact_logic_incomplete = logic in {"and", "not"} and not complete
        budget_limited = any(reason in {"budget_exhausted", "candidate_budget"} for reason in reasons)
        if exact_logic_incomplete and not budget_limited:
            raise AnyTXTIncompleteResults(f"{logic.upper()} requires complete upstream sets")
        if exact_logic_incomplete:
            # A budget stop is terminal and must not start an expensive fallback.
            # For AND, only the intersection of every collected term group is a
            # sound subset; if a term was never searched there is no sound result.
            # For NOT, an incomplete exclusion set cannot prove any candidate safe.
            reasons.append("exact_logic_incomplete")
            selected = (
                self._combine(per_term, logic)
                if logic == "and" and len(per_term) == len(terms)
                else {}
            )
        else:
            selected = self._combine(per_term, logic)
        events: List[Dict[str, Any]] = []
        fragment_chars = 0
        fragment_cache: Dict[tuple[Any, str], Optional[str]] = {}
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
                validated = candidate.get("_validated_snippets", {}).get(term)
                raw_text = validated if isinstance(validated, str) else None
                try:
                    if raw_text is None and cache_key not in fragment_cache:
                        fragment_cache[cache_key] = await self.client.get_fragment(
                            candidate["fid"], _literal_candidate_expression(term), fragment_budget
                        )
                    if raw_text is None:
                        raw_text = fragment_cache[cache_key]
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
                if raw_text is None:
                    # The index cannot resolve this record even though the file
                    # exists (measured: errno 1 with ``text: null``).  The
                    # candidate is still valid, only its snippet is lost.
                    complete = False
                    reasons.append("fragment_unavailable")
                    continue
                # AnyTXT marks hits with ``*<<*``/``*>>*``; that is presentation
                # markup and must not become part of the evidence.
                text = strip_highlight_markers(raw_text)
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
            "scope_source": scope_source,
            "discovered_roots": list(self._discovered_global_roots or ()),
            "text_validation_requests": text_validation_requests,
            "text_validation_chars": text_validation_chars,
            "requests": budget.requests + fragment_budget.requests,
            "search_requests": budget.requests,
            "fragment_requests": fragment_budget.requests,
            "candidates": len(selected),
            "fragment_chars": fragment_chars,
        })
        for event in events:
            event["_retrieval_metadata"] = metadata
        return RetrievalEvents(events, metadata=metadata)

    async def _discover_global_roots(self, budget: _Budget) -> tuple[str, ...]:
        """Return fixed-drive roots that the running AnyTXT instance accepts.

        AnyTXT v1 has no index-list method.  On Windows, however, its exact-count
        endpoint returns ``errno=0`` for an indexed root even when a literal has
        no matches, and ``errno=1`` for an unindexed root.  Cache the result for
        this retriever so discovery costs at most one lightweight request per
        local fixed drive during the process lifetime.
        """
        if self._discovered_global_roots is not None:
            return self._discovered_global_roots
        discovered: List[str] = []
        for root in _fixed_drive_roots():
            total = await self.client.count(INDEX_SCOPE_PROBE_PATTERN, root, "*", budget)
            if total.errno == ERRNO_OK:
                discovered.append(root)
        self._discovered_global_roots = tuple(discovered)
        logger.info("AnyTXT indexed fixed-drive roots discovered: %s", discovered)
        return self._discovered_global_roots

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
                    stored.setdefault("_validated_snippets", {}).update(
                        value.get("_validated_snippets", {})
                    )
            return result
        if logic == "and":
            keys = set(groups[0])
            for group in groups[1:]:
                keys.intersection_update(group)
            result: Dict[str, Dict[str, Any]] = {}
            for key in keys:
                value = dict(groups[0][key], _terms=[group[key]["_term"] for group in groups])
                snippets: Dict[str, str] = {}
                for group in groups:
                    snippets.update(group[key].get("_validated_snippets", {}))
                value["_validated_snippets"] = snippets
                result[key] = value
            return result
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
        if not isinstance(fid, str) or not fid:
            raise ValueError("AnyTXT candidate fid must be a non-empty string")
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


def _record_signature(record: Dict[str, Any]) -> tuple[str, str]:
    """Return a hashable page identity even for malformed service records."""
    fid = record.get("fid")
    file_path = record.get("file")
    return (
        fid if isinstance(fid, str) else repr(fid),
        file_path if isinstance(file_path, str) else repr(file_path),
    )


def _fixed_drive_roots() -> List[str]:
    """Enumerate existing Windows fixed-drive roots without scanning them."""
    if os.name != "nt":
        return []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = int(kernel32.GetLogicalDrives())
        roots = []
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            # DRIVE_FIXED = 3.  Skip removable, optical, RAM and network roots.
            if int(kernel32.GetDriveTypeW(root)) == 3 and os.path.isdir(root):
                roots.append(_absolute_path(root))
        return roots
    except (AttributeError, OSError, TypeError, ValueError):
        logger.warning("Unable to enumerate Windows fixed drives for AnyTXT scope discovery")
        return []


def _needs_text_validation(term: str) -> bool:
    return len(term.split()) > 1


def _literal_candidate_expression(term: str) -> str:
    """Build a recall-oriented candidate expression for a literal substring."""
    if re.search(r'[&|!()"]', term):
        raise AnyTXTUnsupportedQuery("literal escaping for AnyTXT expression operators is not verified")
    tokens = term.split()
    return " | ".join(tokens) if len(tokens) > 1 else term


def _literal_snippet(text: str, term: str, radius: int = 300) -> Optional[str]:
    """Return context around a case-insensitive exact substring, if present."""
    index = text.casefold().find(term.casefold())
    if index < 0:
        return None
    start = max(0, index - radius)
    end = min(len(text), index + len(term) + radius)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def _search_params(
    pattern: str, filter_dir: str, filter_ext: str, *, order: Optional[int] = None
) -> Dict[str, Any]:
    """Shared parameter block of ``anytxt.v1.search``/``getResult``.

    v1 takes the parameters directly in ``params`` (no ``input`` wrapper) and
    ``lastModifyEnd: 0`` means "no upper bound".
    """
    params: Dict[str, Any] = {
        "pattern": pattern,
        "filterDir": filter_dir,
        "filterExt": filter_ext,
        "lastModifyBegin": 0,
        "lastModifyEnd": 0,
    }
    if order is not None:
        params["order"] = order
    return params


def _rpc_errno(response: Dict[str, Any]) -> int:
    """AnyTXT business status; a missing value is treated as success."""
    result = response.get("result")
    if isinstance(result, dict):
        errno = result.get("errno")
        if isinstance(errno, int):
            return errno
    return ERRNO_OK


def strip_highlight_markers(text: str) -> str:
    """Remove AnyTXT's ``*<<*``/``*>>*`` hit markers from a fragment."""
    if HIGHLIGHT_OPEN not in text and HIGHLIGHT_CLOSE not in text:
        return text
    return text.replace(HIGHLIGHT_OPEN, "").replace(HIGHLIGHT_CLOSE, "")


def _is_connection_refused(exc: Any) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConnectionRefusedError):
        return True
    return getattr(exc, "winerror", None) == 10061


def _unreachable_message(api_url: str) -> str:
    return (
        f"AnyTXT is not accepting connections at {api_url}. AnySirchmunk requires "
        "AnyTXT Searcher 1.3.3541 or newer, running, with the local API enabled "
        "(the legacy 9920 interface is no longer supported)."
    )


def _http_hint(exc: HTTPError) -> str:
    hints = {
        400: " (the request headers were rejected; Accept and Content-Type: application/json are both required)",
        403: " (AnyTXT rejects requests with a non-loopback Host or Origin header)",
        404: " (the RPC path is wrong; the v1 endpoint is http://127.0.0.1:9924/rpc)",
    }
    return hints.get(getattr(exc, "code", None), "")


def _protocol_error_message(error: Any) -> str:
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    detail = f"{code}: {message}" if code is not None else repr(error)
    if code == -32601:
        return f"AnyTXT RPC error {detail} — this build does not expose the anytxt.v1 method (1.3.3541+ required)"
    if code == -32602:
        return f"AnyTXT RPC error {detail} — the adapter sent a parameter this AnyTXT build rejects"
    return f"AnyTXT RPC error {detail}"


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
