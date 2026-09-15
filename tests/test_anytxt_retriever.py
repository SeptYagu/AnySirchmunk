import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


MODULE_PATH = Path(__file__).parents[1] / "src" / "sirchmunk" / "retrieve" / "anytxt_retriever.py"
SPEC = importlib.util.spec_from_file_location("anytxt_retriever", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class FakeClient:
    def __init__(self, pages, fragments=None, totals=None, ready=True, errnos=None):
        self.pages = pages
        self.fragments = fragments or {}
        #: ``(pattern, filter_dir) -> total rows`` or an explicit SearchTotal.
        #: Missing keys report "unknown", which keeps the paging heuristic in use.
        self.totals = totals or {}
        #: ``(pattern, filter_dir) -> errno`` for *paging* requests; a non-zero
        #: value models a scope AnyTXT cannot search (measured: an unindexed
        #: drive answers errno 1 with an empty payload).
        self.errnos = errnos or {}
        self.ready = ready
        self.search_calls = []
        self.fragment_calls = []
        self.count_calls = []
        self.status_calls = 0

    async def search(self, pattern, filter_dir, filter_ext, offset, limit, budget):
        budget.charge()
        self.search_calls.append((pattern, filter_dir, filter_ext, offset, limit))
        rows, count = self.pages.get((pattern, filter_dir, offset), ([], 0))
        errno = self.errnos.get((pattern, filter_dir), mod.ERRNO_OK)
        return mod.SearchPage(tuple(rows), count, errno)

    async def count(self, pattern, filter_dir, filter_ext, budget):
        budget.charge()
        self.count_calls.append((pattern, filter_dir, filter_ext))
        value = self.totals.get((pattern, filter_dir), mod.SearchTotal(None, mod.ERRNO_OK))
        if isinstance(value, int):
            return mod.SearchTotal(value, mod.ERRNO_OK)
        return value

    async def status(self, budget):
        budget.charge()
        self.status_calls += 1
        return self.ready

    async def get_fragment(self, fid, pattern, budget):
        budget.charge()
        self.fragment_calls.append((fid, pattern))
        value = self.fragments.get((fid, pattern), f"fragment:{pattern}")
        if isinstance(value, Exception):
            raise value
        return value


class FakeFallback:
    def __init__(self):
        self.calls = []

    async def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        path = str(kwargs["path"][0])
        return [
            {"type": "begin", "data": {"path": {"text": path}}},
            {"type": "match", "data": {"path": {"text": path}, "lines": {"text": "fallback"}}},
            {"type": "end", "data": {"path": {"text": path}}},
        ]


def config(**kwargs):
    values = dict(api_url="http://127.0.0.1:9924/rpc",
                  page_size=2, request_timeout=1, total_timeout=10, max_concurrency=2,
                  max_requests=30, max_candidates=30, max_fragment_chars=1000,
                  fallback_to_rga=True, fallback_roots=(), global_roots=())
    values.update(kwargs)
    return mod.AnyTXTConfig(**values)


class ConfigTests(unittest.TestCase):
    def test_rejects_non_loopback_url_and_relative_fallback_root(self):
        with patch.dict(os.environ, {"ANYTXT_API_URL": "http://example.com:9920"}, clear=True):
            with self.assertRaisesRegex(ValueError, "loopback"):
                mod.AnyTXTConfig.from_env()
        with patch.dict(os.environ, {"ANYTXT_FALLBACK_ROOTS": '["relative"]'}, clear=True):
            with self.assertRaisesRegex(ValueError, "non-absolute"):
                mod.AnyTXTConfig.from_env()

    def test_rejects_non_positive_budget(self):
        with patch.dict(os.environ, {"ANYTXT_MAX_REQUESTS": "0"}, clear=True):
            with self.assertRaisesRegex(ValueError, "positive"):
                mod.AnyTXTConfig.from_env()

    def test_default_concurrency_is_the_verified_safe_value(self):
        # AnyTXT 1.3.2477 segfaults on overlapping requests carrying CJK
        # patterns; concurrency 2 is the highest value verified safe.
        self.assertEqual(mod.AnyTXTConfig().max_concurrency, 2)

    def test_environment_defaults_target_the_v1_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            parsed = mod.AnyTXTConfig.from_env()
        self.assertEqual(parsed.api_url, "http://127.0.0.1:9924/rpc")

    def test_legacy_mode_is_rejected_instead_of_silently_ignored(self):
        # A leftover ANYTXT_API_MODE=legacy in an operator's .env must fail
        # loudly: answering through a different API than the configured one is
        # exactly the kind of silent semantic change this project forbids.
        with patch.dict(os.environ, {"ANYTXT_API_MODE": "legacy"}, clear=True):
            with self.assertRaisesRegex(ValueError, "no longer supported"):
                mod.AnyTXTConfig.from_env()
        with patch.dict(os.environ, {"ANYTXT_API_MODE": "v1"}, clear=True):
            self.assertEqual(mod.AnyTXTConfig.from_env().api_url, "http://127.0.0.1:9924/rpc")

    def test_page_size_outside_the_documented_v1_range_is_rejected(self):
        # AnyTXT v1 answers -32602 for limit outside [1, 300]; catching it at
        # configuration time turns a mid-query protocol error into a start-up
        # message.
        for value in ("0", "301"):
            with self.subTest(limit=value):
                with patch.dict(os.environ, {"ANYTXT_SEARCH_LIMIT": value}, clear=True):
                    with self.assertRaisesRegex(ValueError, "ANYTXT_SEARCH_LIMIT"):
                        mod.AnyTXTConfig.from_env()
        with patch.dict(os.environ, {"ANYTXT_SEARCH_LIMIT": "300"}, clear=True):
            self.assertEqual(mod.AnyTXTConfig.from_env().page_size, 300)

    def test_accepts_existing_absolute_fallback_root(self):
        with tempfile.TemporaryDirectory() as directory:
            encoded = __import__("json").dumps([directory])
            with patch.dict(os.environ, {"ANYTXT_FALLBACK_ROOTS": encoded}, clear=True):
                parsed = mod.AnyTXTConfig.from_env()
            self.assertEqual(parsed.fallback_roots, (mod._absolute_path(directory),))


class ResponseTests(unittest.TestCase):
    def test_normalises_row_and_column_responses(self):
        rows = mod._normalise_files([["f1", 1, 2, r"D:\\a.pdf"]], "fid,lastModify,size,file")
        self.assertEqual(rows[0]["file"], r"D:\\a.pdf")
        columns = mod._normalise_files({"fid": ["a"], "file": [r"D:\\b.pdf"]}, None)
        self.assertEqual(columns, [{"fid": "a", "file": r"D:\\b.pdf"}])

    def test_extracts_json_rpc_envelope(self):
        payload = {"result": {"data": {"output": {"count": 1}}}}
        self.assertEqual(mod._rpc_output(payload), {"count": 1})


class RetrieverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Contract-test paths are synthetic Windows paths. Individual tests can
        # override this when exercising stale index records.
        self._isfile_patch = patch.object(mod.os.path, "isfile", return_value=True)
        self._isfile_patch.start()
        self.addCleanup(self._isfile_patch.stop)

    async def test_global_search_uses_empty_filter_and_emits_contract(self):
        client = FakeClient({("alpha", "", 0): ([{"fid": "1", "file": r"D:\\docs\\a.pdf", "size": 12}], 1)})
        retriever = mod.AnyTXTRetriever(config=config(), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual([event["type"] for event in events], ["begin", "match", "end"])
        self.assertEqual(events[1]["data"]["path"]["text"], r"D:\docs\a.pdf")
        self.assertEqual(events[1]["data"]["lines"]["text"], "fragment:alpha")
        self.assertEqual(client.search_calls[0][1], "")
        # An empty filterDir is resolved by AnyTXT to its own directory, so the
        # result must not be advertised as a complete global index search.
        self.assertEqual(events.metadata["effective_scope"], mod.SERVER_DEFAULT_SCOPE)
        self.assertFalse(events.metadata["complete"])
        self.assertIn(mod.UNVERIFIED_GLOBAL_REASON, events.metadata["reason"])
        merged = retriever.merge_results(events)
        self.assertEqual(merged[0]["_retrieval_metadata"]["actual_backend"], "anytxt")

    async def test_paginates_past_fully_filtered_first_page(self):
        root = r"D:\wanted"
        pages = {
            ("alpha", root, 0): ([
                {"fid": "x", "file": r"D:\outside\x.pdf"},
                {"fid": "y", "file": r"D:\outside\y.pdf"},
            ], 3),
            ("alpha", root, 2): ([{"fid": "z", "file": r"D:\wanted\z.pdf"}], 3),
        }
        client = FakeClient(pages)
        retriever = mod.AnyTXTRetriever(config=config(), client=client)
        events = await retriever.retrieve("alpha", path=[root], literal=True, regex=False)
        self.assertEqual(len([event for event in events if event["type"] == "match"]), 1)
        self.assertEqual([call[3] for call in client.search_calls], [0, 2])

    async def test_count_is_treated_as_page_count_not_total(self):
        pages = {
            ("alpha", "", 0): ([{"fid": "1", "file": r"D:\a.pdf"}, {"fid": "2", "file": r"D:\b.pdf"}], 2),
            ("alpha", "", 2): ([{"fid": "3", "file": r"D:\c.pdf"}], 1),
        }
        client = FakeClient(pages)
        retriever = mod.AnyTXTRetriever(config=config(), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(len([event for event in events if event["type"] == "begin"]), 3)
        self.assertEqual([call[3] for call in client.search_calls], [0, 2])

    async def test_deduplicates_multi_directory_results_by_windows_path(self):
        roots = [r"D:\docs", r"D:\docs\sub"]
        row1 = {"fid": "1", "file": r"D:\docs\sub\A.pdf"}
        row2 = {"fid": "1", "file": r"d:\DOCS\sub\a.pdf"}
        client = FakeClient({("alpha", roots[0], 0): ([row1], 1), ("alpha", roots[1], 0): ([row2], 1)})
        retriever = mod.AnyTXTRetriever(config=config(), client=client)
        events = await retriever.retrieve("alpha", path=roots, literal=True, regex=False)
        self.assertEqual(len([event for event in events if event["type"] == "begin"]), 1)

    async def test_and_and_not_are_file_set_operations(self):
        root = r"D:\lib"
        pages = {
            ("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}, {"fid": "2", "file": r"D:\lib\b.pdf"}], 2),
            ("beta", root, 0): ([{"fid": "2", "file": r"D:\lib\b.pdf"}], 1),
        }
        and_retriever = mod.AnyTXTRetriever(config=config(page_size=3, global_roots=(root,)), client=FakeClient(pages))
        and_events = await and_retriever.retrieve(["alpha", "beta"], path=None, logic="and", literal=True, regex=False)
        self.assertEqual(
            [e["data"]["path"]["text"] for e in and_events if e["type"] == "begin"], [r"D:\lib\b.pdf"]
        )
        not_retriever = mod.AnyTXTRetriever(config=config(page_size=3, global_roots=(root,)), client=FakeClient(pages))
        not_events = await not_retriever.retrieve(["alpha", "beta"], path=None, logic="not", literal=True, regex=False)
        self.assertEqual(
            [e["data"]["path"]["text"] for e in not_events if e["type"] == "begin"], [r"D:\lib\a.pdf"]
        )

    async def test_repeated_page_makes_or_incomplete_and_exact_and_falls_back(self):
        repeated = [{"fid": "1", "file": r"D:\\docs\\a.pdf"}, {"fid": "2", "file": r"D:\\docs\\b.pdf"}]
        pages = {("alpha", r"D:\docs", 0): (repeated, 10), ("alpha", r"D:\docs", 2): (repeated, 10)}
        or_retriever = mod.AnyTXTRetriever(config=config(), client=FakeClient(pages))
        events = await or_retriever.retrieve("alpha", path=r"D:\docs", literal=True, regex=False)
        self.assertFalse(events.metadata["complete"])
        self.assertIn("repeated_page", events.metadata["reason"])

        fallback = FakeFallback()
        exact = mod.AnyTXTRetriever(config=config(), client=FakeClient(pages), fallback=fallback)
        result = await exact.retrieve(["alpha", "beta"], path=r"D:\docs", logic="and", literal=True, regex=False)
        self.assertEqual(result.metadata["actual_backend"], "rga")
        self.assertEqual(len(fallback.calls), 1)

    async def test_global_failure_only_falls_back_to_configured_roots(self):
        class Broken(FakeClient):
            async def search(self, *args, **kwargs):
                raise mod.AnyTXTBackendError("offline")

        fallback = FakeFallback()
        retriever = mod.AnyTXTRetriever(
            config=config(fallback_roots=(r"D:\\fallback",)), client=Broken({}), fallback=fallback
        )
        result = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(fallback.calls[0]["path"], [r"D:\\fallback"])
        self.assertTrue(result.metadata["scope_reduced"])

    async def test_global_failure_without_roots_does_not_scan_cwd(self):
        class Broken(FakeClient):
            async def search(self, *args, **kwargs):
                raise mod.AnyTXTBackendError("offline")

        fallback = FakeFallback()
        retriever = mod.AnyTXTRetriever(config=config(), client=Broken({}), fallback=fallback)
        with self.assertRaises(mod.AnyTXTBackendError):
            await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(fallback.calls, [])

    async def test_unverified_regex_uses_at_most_one_fallback(self):
        fallback = FakeFallback()
        retriever = mod.AnyTXTRetriever(config=config(), client=FakeClient({}), fallback=fallback)
        result = await retriever.retrieve("a.*b", path=r"D:\\docs", literal=False, regex=True)
        self.assertEqual(result.metadata["fallback_reason"], "AnyTXTUnsupportedQuery")
        self.assertEqual(len(fallback.calls), 1)

    async def test_cancellation_never_falls_back(self):
        class Cancelled(FakeClient):
            async def search(self, *args, **kwargs):
                raise asyncio.CancelledError

        fallback = FakeFallback()
        retriever = mod.AnyTXTRetriever(config=config(), client=Cancelled({}), fallback=fallback)
        with self.assertRaises(asyncio.CancelledError):
            await retriever.retrieve("alpha", path=r"D:\\docs", literal=True, regex=False)
        self.assertEqual(fallback.calls, [])

    async def test_configured_global_roots_are_queried_per_root(self):
        roots = [r"D:\lib", r"E:\lib"]
        pages = {
            ("alpha", roots[0], 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1),
            ("alpha", roots[1], 0): ([
                {"fid": "1", "file": r"d:\LIB\A.pdf"},
                {"fid": "2", "file": r"E:\lib\b.pdf"},
            ], 2),
        }
        client = FakeClient(pages)
        retriever = mod.AnyTXTRetriever(config=config(page_size=3, global_roots=tuple(roots)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual([call[1] for call in client.search_calls], roots)
        self.assertEqual(
            [event["data"]["path"]["text"] for event in events if event["type"] == "begin"],
            [r"D:\lib\a.pdf", r"E:\lib\b.pdf"],
        )
        self.assertEqual(events.metadata["effective_scope"], roots)
        self.assertTrue(events.metadata["complete"])

    async def test_unverified_global_scope_never_claims_exact_set_operations(self):
        pages = {("alpha", "", 0): ([{"fid": "1", "file": r"C:\a.pdf"}], 1)}
        fallback = FakeFallback()
        retriever = mod.AnyTXTRetriever(
            config=config(fallback_roots=(r"D:\lib",)), client=FakeClient(pages), fallback=fallback
        )
        result = await retriever.retrieve(["alpha", "beta"], path=None, logic="and", literal=True, regex=False)
        self.assertEqual(result.metadata["fallback_reason"], "AnyTXTIncompleteResults")
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(fallback.calls[0]["path"], [r"D:\lib"])

    def test_global_roots_are_read_from_env_and_validated(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            encoded = json.dumps([first, second])
            with patch.dict(os.environ, {"ANYTXT_GLOBAL_ROOTS": encoded}, clear=True):
                parsed = mod.AnyTXTConfig.from_env()
            self.assertEqual(parsed.global_roots, (mod._absolute_path(first), mod._absolute_path(second)))
            self.assertEqual(parsed.fallback_roots, ())
        with patch.dict(os.environ, {"ANYTXT_GLOBAL_ROOTS": '["relative"]'}, clear=True):
            with self.assertRaisesRegex(ValueError, "non-absolute"):
                mod.AnyTXTConfig.from_env()
        with patch.dict(os.environ, {"ANYTXT_GLOBAL_ROOTS": "{}"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be a JSON array"):
                mod.AnyTXTConfig.from_env()

    async def test_search_budget_exhaustion_keeps_collected_candidates(self):
        root = r"D:\lib"
        pages = {
            ("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}, {"fid": "2", "file": r"D:\lib\b.pdf"}], 2),
            ("alpha", root, 2): ([{"fid": "3", "file": r"D:\lib\c.pdf"}], 1),
        }
        # Three requests fit (readiness, the exact count and the first page); the
        # second page does not.
        retriever = mod.AnyTXTRetriever(
            config=config(page_size=2, max_requests=3, global_roots=(root,)), client=FakeClient(pages)
        )
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertFalse(events.metadata["complete"])
        self.assertIn("budget_exhausted", events.metadata["reason"])
        self.assertEqual(
            [event["data"]["path"]["text"] for event in events if event["type"] == "begin"],
            [r"D:\lib\a.pdf", r"D:\lib\b.pdf"],
        )

    async def test_fragment_budget_exhaustion_keeps_every_candidate(self):
        root = r"D:\lib"
        pages = {("alpha", root, 0): ([
            {"fid": "1", "file": r"D:\lib\a.pdf"},
            {"fid": "2", "file": r"D:\lib\b.pdf"},
        ], 2)}
        retriever = mod.AnyTXTRetriever(
            config=config(page_size=3, max_fragment_requests=1, global_roots=(root,)),
            client=FakeClient(pages),
        )
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertFalse(events.metadata["complete"])
        self.assertIn("fragment_request_budget", events.metadata["reason"])
        self.assertEqual(
            [event["data"]["path"]["text"] for event in events if event["type"] == "begin"],
            [r"D:\lib\a.pdf", r"D:\lib\b.pdf"],
        )
        # The candidate without a snippet is still reported, just without text.
        self.assertEqual(
            [event["data"]["lines"]["text"] for event in events if event["type"] == "match"],
            ["fragment:alpha", ""],
        )

    async def test_fragment_character_budget_stops_requests_and_marks_truncation(self):
        root = r"D:\lib"
        rows = [
            {"fid": "1", "file": r"D:\lib\a.pdf"},
            {"fid": "2", "file": r"D:\lib\b.pdf"},
        ]
        client = FakeClient({("alpha", root, 0): (rows, 2)}, fragments={("1", "alpha"): "abcdef"})
        retriever = mod.AnyTXTRetriever(
            config=config(page_size=3, max_fragment_chars=3, global_roots=(root,)),
            client=client,
        )
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertFalse(events.metadata["complete"])
        self.assertIn("fragment_budget", events.metadata["reason"])
        self.assertEqual(client.fragment_calls, [("1", "alpha")])
        self.assertEqual(events.metadata["fragment_requests"], 1)
        self.assertEqual(events.metadata["fragment_chars"], 3)
        self.assertEqual(
            [event["data"]["lines"]["text"] for event in events if event["type"] == "match"],
            ["abc", ""],
        )

    async def test_invalid_candidate_is_skipped_and_marks_results_incomplete(self):
        root = r"D:\lib"
        rows = [
            {"file": r"D:\lib\missing-fid.pdf"},
            {"fid": "2", "file": r"D:\lib\missing-file.pdf"},
        ]
        client = FakeClient({("alpha", root, 0): (rows, 2)})
        with patch.object(mod.os.path, "isfile", return_value=False):
            retriever = mod.AnyTXTRetriever(
                config=config(page_size=3, global_roots=(root,)), client=client
            )
            events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(list(events), [])
        self.assertFalse(events.metadata["complete"])
        self.assertIn("invalid_record", events.metadata["reason"])
        self.assertEqual(client.fragment_calls, [])

    async def test_budget_exhaustion_before_first_request_returns_metadata(self):
        retriever = mod.AnyTXTRetriever(
            config=config(max_requests=0, global_roots=(r"D:\lib",)), client=FakeClient({})
        )
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(list(events), [])
        self.assertFalse(events.metadata["complete"])
        self.assertIn("budget_exhausted", events.metadata["reason"])


    async def test_unsearchable_scope_is_reported_and_other_roots_survive(self):
        # Measured on 1.3.3541: filterDir pointing at an unindexed volume answers
        # errno 1 with an empty payload.  Treating that as "no match" would
        # silently drop a whole drive while still claiming completeness.
        good, bad = r"D:\lib", r"Z:\nowhere"
        pages = {
            ("alpha", good, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1),
        }
        client = FakeClient(pages, errnos={("alpha", bad): 1})
        retriever = mod.AnyTXTRetriever(
            config=config(page_size=3, global_roots=(good, bad)), client=client
        )
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(
            [event["data"]["path"]["text"] for event in events if event["type"] == "begin"],
            [r"D:\lib\a.pdf"],
        )
        self.assertFalse(events.metadata["complete"])
        self.assertIn("scope_errno_1", events.metadata["reason"])

    async def test_count_errno_skips_the_scope_without_paging_it(self):
        bad = r"Z:\nowhere"
        client = FakeClient({}, totals={("alpha", bad): mod.SearchTotal(None, 1)})
        retriever = mod.AnyTXTRetriever(config=config(page_size=3, global_roots=(bad,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(list(events), [])
        self.assertEqual(client.search_calls, [])
        self.assertIn("scope_errno_1", events.metadata["reason"])

    async def test_exact_count_ends_paging_once_every_row_is_delivered(self):
        root = r"D:\lib"
        rows = [{"fid": "1", "file": r"D:\lib\a.pdf"}, {"fid": "2", "file": r"D:\lib\b.pdf"}]
        pages = {
            ("alpha", root, 0): (rows, 2),
            # The server would happily serve another page; the exact count says
            # there is nothing left, so that request must never be sent.
            ("alpha", root, 2): ([{"fid": "3", "file": r"D:\lib\c.pdf"}], 1),
        }
        client = FakeClient(pages, totals={("alpha", root): 2})
        retriever = mod.AnyTXTRetriever(config=config(page_size=2, global_roots=(root,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual([call[3] for call in client.search_calls], [0])
        self.assertEqual(len([event for event in events if event["type"] == "begin"]), 2)
        self.assertTrue(events.metadata["complete"])
        self.assertIsNone(events.metadata["reason"])

    async def test_short_page_below_the_exact_count_is_marked_incomplete(self):
        root = r"D:\lib"
        pages = {("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1)}
        client = FakeClient(pages, totals={("alpha", root): 5})
        retriever = mod.AnyTXTRetriever(config=config(page_size=2, global_roots=(root,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertFalse(events.metadata["complete"])
        self.assertIn("incomplete_enumeration", events.metadata["reason"])

    async def test_unknown_total_falls_back_to_the_paging_heuristic(self):
        # Without a count the adapter must keep working, and must not invent a
        # completeness verdict it cannot support.
        root = r"D:\lib"
        pages = {
            ("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}, {"fid": "2", "file": r"D:\lib\b.pdf"}], 2),
            ("alpha", root, 2): ([{"fid": "3", "file": r"D:\lib\c.pdf"}], 1),
        }
        client = FakeClient(pages)
        retriever = mod.AnyTXTRetriever(config=config(page_size=2, global_roots=(root,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual([call[3] for call in client.search_calls], [0, 2])
        self.assertEqual(len([event for event in events if event["type"] == "begin"]), 3)
        self.assertTrue(events.metadata["complete"])

    async def test_unavailable_fragment_keeps_the_candidate(self):
        root = r"D:\lib"
        pages = {("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1)}
        client = FakeClient(pages, fragments={("1", "alpha"): None})
        retriever = mod.AnyTXTRetriever(config=config(page_size=2, global_roots=(root,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual([event["data"]["path"]["text"] for event in events if event["type"] == "begin"],
                         [r"D:\lib\a.pdf"])
        self.assertEqual([event["data"]["lines"]["text"] for event in events if event["type"] == "match"], [""])
        self.assertFalse(events.metadata["complete"])
        self.assertIn("fragment_unavailable", events.metadata["reason"])

    async def test_engine_not_ready_is_reported_without_dropping_candidates(self):
        root = r"D:\lib"
        pages = {("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1)}
        client = FakeClient(pages, totals={("alpha", root): 1}, ready=False)
        retriever = mod.AnyTXTRetriever(config=config(page_size=2, global_roots=(root,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(len([event for event in events if event["type"] == "begin"]), 1)
        self.assertFalse(events.metadata["complete"])
        self.assertIn(mod.ENGINE_NOT_READY_REASON, events.metadata["reason"])

    async def test_readiness_failure_never_fails_the_query(self):
        class BrokenStatus(FakeClient):
            async def status(self, budget):
                raise mod.AnyTXTBackendError("status endpoint is missing")

        root = r"D:\lib"
        pages = {("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1)}
        retriever = mod.AnyTXTRetriever(
            config=config(page_size=2, global_roots=(root,)),
            client=BrokenStatus(pages, totals={("alpha", root): 1}),
        )
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        self.assertEqual(len([event for event in events if event["type"] == "begin"]), 1)
        self.assertTrue(events.metadata["complete"])

    async def test_highlight_markers_never_reach_the_evidence(self):
        root = r"D:\lib"
        pages = {("alpha", root, 0): ([{"fid": "1", "file": r"D:\lib\a.pdf"}], 1)}
        client = FakeClient(pages, fragments={("1", "alpha"): "see *<<*alpha*>>* here"},
                            totals={("alpha", root): 1})
        retriever = mod.AnyTXTRetriever(config=config(page_size=2, global_roots=(root,)), client=client)
        events = await retriever.retrieve("alpha", path=None, literal=True, regex=False)
        snippets = [event["data"]["lines"]["text"] for event in events if event["type"] == "match"]
        self.assertEqual(snippets[0], "see alpha here")

    def test_highlight_stripping_leaves_plain_text_alone(self):
        self.assertEqual(mod.strip_highlight_markers("no markers"), "no markers")
        self.assertEqual(mod.strip_highlight_markers(""), "")
        self.assertEqual(mod.strip_highlight_markers("*<<*a*>>*b"), "ab")

    async def test_page_order_is_configurable_and_defaults_to_path_ascending(self):
        self.assertEqual(mod.AnyTXTConfig().page_order, 3)
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"result": {"errno": 0, "data": {"output": {"count": 0, "field": [], "files": []}}}}'

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return _Response()

        client = mod.AnyTXTClient(config(page_order=4))
        with patch.object(mod, "urlopen", fake_urlopen):
            await client.search("alpha", "", "", 0, 5, mod._Budget(config(), 5))
        self.assertEqual(captured["payload"]["params"]["order"], 4)
        # A count must not carry paging parameters at all.
        with patch.object(mod, "urlopen", fake_urlopen):
            await client.count("alpha", "", "", mod._Budget(config(), 5))
        self.assertNotIn("order", captured["payload"]["params"])


class RpcContractTests(unittest.IsolatedAsyncioTestCase):
    """The local service rejects requests which miss either required header."""

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    async def test_search_sends_jsonrpc_envelope_and_required_headers(self):
        body = b'{"result": {"errno": 0, "data": {"output": {"count": 0, "field": [], "files": []}}}}'
        captured, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config(page_size=5))
        with patch.object(mod, "urlopen", fake_urlopen):
            await client.search("alpha", "", "", 0, 5, mod._Budget(config(), 5))

        request = captured["request"]
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers.get("accept"), "application/json")
        self.assertEqual(headers.get("content-type"), "application/json")

        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertIn("id", payload)
        self.assertEqual(payload["method"], mod.SEARCH_METHOD)
        # v1 takes the parameters directly in params; wrapping them in "input"
        # is the removed legacy shape.
        self.assertNotIn("input", payload["params"])
        self.assertEqual(payload["params"]["pattern"], "alpha")
        self.assertEqual(payload["params"]["filterDir"], "")
        self.assertEqual(payload["params"]["order"], 3)
        self.assertEqual(payload["params"]["offset"], 0)
        self.assertEqual(payload["params"]["limit"], 5)

    async def test_count_uses_the_documented_method_and_filters(self):
        body = b'{"result": {"errno": 0, "data": {"output": {"count": 77}}}}'
        captured, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", fake_urlopen):
            total = await client.count("alpha", r"D:\\docs", "*.pdf", mod._Budget(config(), 5))

        self.assertEqual(total.total, 77)
        self.assertEqual(total.errno, mod.ERRNO_OK)
        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["method"], mod.COUNT_METHOD)
        self.assertEqual(payload["params"]["filterDir"], r"D:\\docs")
        self.assertEqual(payload["params"]["filterExt"], "*.pdf")
        # A count must describe the exact same enumeration as paging, so it may
        # not carry paging-only parameters.
        self.assertNotIn("limit", payload["params"])
        self.assertNotIn("offset", payload["params"])

    async def test_fragment_returns_the_wire_text_unchanged(self):
        # Stripping the ``*<<*`` hit markers happens where the evidence is built,
        # not here: this layer only decodes the wire format.
        body = '{"result": {"errno": 0, "data": {"output": {"text": "a *<<*hit*>>* here"}}}}'.encode()
        captured, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", fake_urlopen):
            text = await client.get_fragment("fid-1", "alpha", mod._Budget(config(), 5))

        self.assertEqual(text, "a *<<*hit*>>* here")
        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["method"], mod.FRAGMENT_METHOD)
        self.assertEqual(payload["params"], {"fid": "fid-1", "pattern": "alpha"})

    async def test_unresolvable_fragment_reports_none_instead_of_an_error(self):
        # Measured on 1.3.3541: an unresolvable fid answers errno 1 with
        # text: null, so "no snippet" must not look like a transport failure.
        for body in (
            b'{"result": {"errno": 1, "data": {"output": {"text": null}}}}',
            b'{"result": {"errno": 0, "data": {"output": {"text": null}}}}',
        ):
            with self.subTest(body=body):
                client = mod.AnyTXTClient(config())
                _, fake_urlopen = self._capture(body)
                with patch.object(mod, "urlopen", fake_urlopen):
                    self.assertIsNone(await client.get_fragment("1", "alpha", mod._Budget(config(), 5)))

    async def test_json_rpc_error_codes_are_classified(self):
        for code, expected in ((-32601, "anytxt.v1 method"), (-32602, "parameter")):
            with self.subTest(code=code):
                body = json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "error": {"code": code, "message": "nope"}}).encode()
                client = mod.AnyTXTClient(config())
                _, fake_urlopen = self._capture(body)
                with patch.object(mod, "urlopen", fake_urlopen):
                    with self.assertRaises(mod.AnyTXTProtocolError) as caught:
                        await client.search("alpha", "", "", 0, 5, mod._Budget(config(), 5))
                self.assertIn(expected, str(caught.exception))
                self.assertIn(str(code), str(caught.exception))

    async def test_unreachable_service_names_the_required_version_and_endpoint(self):
        def refused(request, timeout=None):
            raise URLError(ConnectionRefusedError(10061, "connection refused"))

        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", refused):
            with self.assertRaises(mod.AnyTXTBackendError) as caught:
                await client.search("alpha", "", "", 0, 5, mod._Budget(config(), 5))
        message = str(caught.exception)
        self.assertIn("1.3.3541", message)
        self.assertIn("9924/rpc", message)

    def _capture(self, body: bytes):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return self._Response(body)

        return captured, fake_urlopen


class ConcurrencyGateTests(unittest.IsolatedAsyncioTestCase):
    """AnyTXT 1.3.2477 dies when GetResult and GetFragment overlap."""

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    SEARCH_BODY = b'{"result": {"data": {"output": {"count": 0, "field": [], "files": []}}}}'
    FRAGMENT_BODY = b'{"result": {"data": {"output": {"text": "snippet"}}}}'

    def _tracking_urlopen(self, overlaps, peak):
        import threading
        import time as _time
        lock = threading.Lock()
        inflight = {"search": 0, "fragment": 0}

        def fake_urlopen(request, timeout=None):
            payload = json.loads(request.data.decode("utf-8"))
            kind = "search" if payload["method"].endswith("GetResult") else "fragment"
            with lock:
                inflight[kind] += 1
                if inflight["search"] and inflight["fragment"]:
                    overlaps.append(dict(inflight))
                peak[0] = max(peak[0], inflight[kind])
            _time.sleep(0.02)
            with lock:
                inflight[kind] -= 1
            return self._Response(self.SEARCH_BODY if kind == "search" else self.FRAGMENT_BODY)

        return fake_urlopen

    async def test_search_and_fragment_requests_never_overlap(self):
        overlaps = []
        peak = [0]
        # Concurrency must be large enough that both kinds are admitted at the
        # same time; with a smaller limit the semaphore alone would batch them
        # into "all searches, then all fragments" and the test would pass even
        # without the gate.
        client = mod.AnyTXTClient(config(max_concurrency=8))
        with patch.object(mod, "urlopen", self._tracking_urlopen(overlaps, peak)):
            await asyncio.gather(
                *[client.search("alpha", "", "", 0, 5, mod._Budget(config(), 5)) for _ in range(4)],
                *[client.get_fragment("fid", "alpha", mod._Budget(config(), 5)) for _ in range(4)],
            )
        self.assertEqual(overlaps, [], f"GetResult and GetFragment were in flight together: {overlaps}")

    async def test_different_clients_share_the_endpoint_gate(self):
        overlaps = []
        peak = [0]
        first = mod.AnyTXTClient(config(max_concurrency=8))
        second = mod.AnyTXTClient(config(max_concurrency=8))
        with patch.object(mod, "urlopen", self._tracking_urlopen(overlaps, peak)):
            await asyncio.gather(
                first.search("alpha", "", "", 0, 5, mod._Budget(config(), 5)),
                second.get_fragment("fid", "alpha", mod._Budget(config(), 5)),
            )
        self.assertEqual(overlaps, [], f"separate clients bypassed the endpoint gate: {overlaps}")

    async def test_timed_out_worker_keeps_cross_kind_gate_until_transport_finishes(self):
        import threading
        import time as _time

        overlaps = []
        lock = threading.Lock()
        inflight = {"search": 0, "fragment": 0}
        search_calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            payload = json.loads(request.data.decode("utf-8"))
            kind = "search" if payload["method"].endswith("GetResult") else "fragment"
            with lock:
                inflight[kind] += 1
                if inflight["search"] and inflight["fragment"]:
                    overlaps.append(dict(inflight))
                if kind == "search":
                    search_calls["n"] += 1
                    call_number = search_calls["n"]
                else:
                    call_number = 0
            _time.sleep(0.3 if kind == "search" and call_number == 1 else 0.005)
            with lock:
                inflight[kind] -= 1
            return self._Response(self.SEARCH_BODY if kind == "search" else self.FRAGMENT_BODY)

        fast_timeout = config(request_timeout=0.08, total_timeout=1, max_concurrency=2)
        patient_timeout = config(request_timeout=0.8, total_timeout=1, max_concurrency=2)
        first = mod.AnyTXTClient(fast_timeout)
        second = mod.AnyTXTClient(patient_timeout)
        with patch.object(mod, "urlopen", fake_urlopen):
            await first.search("alpha", "", "", 0, 5, mod._Budget(fast_timeout, 1))
            await second.get_fragment("fid", "alpha", mod._Budget(patient_timeout, 1))
        self.assertEqual(overlaps, [], f"orphaned timeout worker bypassed the gate: {overlaps}")

    async def test_same_kind_requests_still_run_in_parallel(self):
        overlaps = []
        peak = [0]
        client = mod.AnyTXTClient(config(max_concurrency=4))
        with patch.object(mod, "urlopen", self._tracking_urlopen(overlaps, peak)):
            await asyncio.gather(
                *[client.get_fragment(f"fid-{index}", "alpha", mod._Budget(config(), 5)) for index in range(4)]
            )
        # The gate must not degrade same-kind calls into a serial stream.
        self.assertGreaterEqual(peak[0], 2)


class V1EndpointTests(unittest.IsolatedAsyncioTestCase):
    """Only the documented v1 surface is supported."""

    SEARCH_BODY = b'{"jsonrpc": "2.0", "result": {"errno": 0, "data": {"output": {"count": 0, "field": [], "files": []}}}}'

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    def _capture(self, body: bytes = None):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return self._Response(body if body is not None else self.SEARCH_BODY)

        return captured, fake_urlopen

    def test_default_client_uses_the_v1_endpoint_and_methods(self):
        parsed = mod.AnyTXTConfig()
        self.assertEqual(parsed.api_url, "http://127.0.0.1:9924/rpc")
        payload = mod._search_params("alpha", "", "")
        self.assertEqual(mod.SEARCH_METHOD, "anytxt.v1.getResult")
        self.assertEqual(mod.COUNT_METHOD, "anytxt.v1.search")
        self.assertEqual(mod.FRAGMENT_METHOD, "anytxt.v1.getFragment")
        self.assertEqual(mod.STATUS_METHOD, "anytxt.v1.status")
        self.assertNotIn("input", payload)

    async def test_requests_go_to_the_configured_v1_url(self):
        captured, fake_urlopen = self._capture()
        client = mod.AnyTXTClient(config(api_url="http://127.0.0.1:9924/rpc"))
        with patch.object(mod, "urlopen", fake_urlopen):
            await client.search("alpha", "C:\\", "", 0, 5, mod._Budget(config(), 5))
        self.assertEqual(captured["request"].full_url, "http://127.0.0.1:9924/rpc")
        self.assertEqual(json.loads(captured["request"].data.decode("utf-8"))["method"], "anytxt.v1.getResult")

    async def test_health_check_reads_the_engine_state_from_status(self):
        body = b'{"jsonrpc": "2.0", "id": 1, "result": {"errno": 0, "data": {"input": {}, "output": {"return": true}}}}'
        captured, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", fake_urlopen):
            health = await client.health_check()
        self.assertEqual(
            json.loads(captured["request"].data.decode("utf-8"))["method"], mod.STATUS_METHOD
        )
        self.assertEqual(health["rpc_method"], mod.STATUS_METHOD)
        self.assertTrue(health["engine_ready"])
        self.assertEqual(health["api_url"], "http://127.0.0.1:9924/rpc")

    async def test_status_false_is_reported_as_not_ready(self):
        body = b'{"jsonrpc": "2.0", "id": 1, "result": {"errno": 0, "data": {"input": {}, "output": {"return": false}}}}'
        _, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", fake_urlopen):
            health = await client.health_check()
        self.assertTrue(health["healthy"])
        self.assertFalse(health["engine_ready"])


class TimeoutRetryTests(unittest.IsolatedAsyncioTestCase):
    """A single stalled response must not fail the whole query."""

    SEARCH_BODY = b'{"jsonrpc": "2.0", "result": {"data": {"output": {"count": 0, "field": [], "files": []}}}}'

    class _Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def read(self) -> bytes:
            return self._body

    def _client_and_budget(self):
        settings = config(request_timeout=0.05, total_timeout=10)
        return mod.AnyTXTClient(settings), mod._Budget(settings, 10)

    async def test_timed_out_request_is_retried_once(self):
        import time as _time
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                _time.sleep(0.4)  # far beyond the 0.05s request timeout
            return self._Response(self.SEARCH_BODY)

        client, budget = self._client_and_budget()
        with patch.object(mod, "urlopen", fake_urlopen):
            page = await client.search("alpha", "", "", 0, 5, budget)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(page.files, ())

    async def test_transport_timeout_is_retried_once(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("socket timed out")
            return self._Response(self.SEARCH_BODY)

        client, budget = self._client_and_budget()
        with patch.object(mod, "urlopen", fake_urlopen):
            page = await client.search("alpha", "", "", 0, 5, budget)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(page.files, ())

    async def test_persistently_stalling_request_fails_after_one_retry(self):
        import time as _time
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            _time.sleep(0.4)
            return self._Response(self.SEARCH_BODY)

        client, budget = self._client_and_budget()
        with patch.object(mod, "urlopen", fake_urlopen):
            with self.assertRaises(mod.AnyTXTBackendError):
                await client.search("alpha", "", "", 0, 5, budget)
        self.assertEqual(calls["n"], 2)


class DeliveryArtifactTests(unittest.TestCase):
    """The shipped patch is what an operator actually installs."""

    PATCH = MODULE_PATH.parents[3] / "patches" / "sirchmunk-3c7ee54-anytxt.patch"

    def test_patch_templates_carry_the_v1_defaults_and_no_legacy_surface(self):
        patch_text = self.PATCH.read_text(encoding="utf-8")
        # Both delivered templates (config/env.example and the CLI's .env writer)
        # must agree; a count of 2 is what makes that checkable.
        self.assertEqual(patch_text.count("+ANYTXT_API_URL=http://127.0.0.1:9924/rpc"), 2)
        self.assertEqual(patch_text.count("+ANYTXT_MAX_CONCURRENCY=2"), 2)
        self.assertEqual(patch_text.count("+ANYTXT_MAX_FRAGMENT_REQUESTS=100"), 2)
        self.assertEqual(patch_text.count("+ANYTXT_PAGE_ORDER=3"), 2)
        self.assertEqual(patch_text.count("+ANYTXT_EXACT_COUNT=true"), 2)
        # No delivered template may offer the removed knobs or point at the
        # removed endpoint.  Leading "+" keeps this about the added lines only,
        # so the adapter explaining the removal is still allowed.
        for removed in ("+ANYTXT_API_MODE", "+ANYTXT_MAX_CONCURRENCY=4",
                        "+ANYTXT_API_URL=http://127.0.0.1:9920",
                        "ATRpcServer.Searcher.V1.GetResult", "ATRpcServer.Searcher.V1.GetFragment"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, patch_text)

    def test_patch_installs_the_v1_only_adapter(self):
        patch_text = self.PATCH.read_text(encoding="utf-8")
        self.assertIn("+SEARCH_METHOD = \"anytxt.v1.getResult\"", patch_text)
        self.assertNotIn("+API_MODES", patch_text)
        for path in ("config/env.example", "src/sirchmunk/agentic/tools.py", "src/sirchmunk/cli/cli.py",
                     "src/sirchmunk/retrieve/anytxt_retriever.py", "src/sirchmunk/search.py"):
            with self.subTest(path=path):
                self.assertIn(f"diff --git a/{path}", patch_text)


if __name__ == "__main__":
    unittest.main()
