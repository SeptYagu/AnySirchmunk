import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "src" / "sirchmunk" / "retrieve" / "anytxt_retriever.py"
SPEC = importlib.util.spec_from_file_location("anytxt_retriever", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


class FakeClient:
    def __init__(self, pages, fragments=None):
        self.pages = pages
        self.fragments = fragments or {}
        self.search_calls = []
        self.fragment_calls = []

    async def search(self, pattern, filter_dir, filter_ext, offset, limit, budget):
        budget.charge()
        self.search_calls.append((pattern, filter_dir, filter_ext, offset, limit))
        rows, count = self.pages.get((pattern, filter_dir, offset), ([], 0))
        return mod.SearchPage(tuple(rows), count)

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
    values = dict(page_size=2, request_timeout=1, total_timeout=10, max_concurrency=2,
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

    def _capture(self, body: bytes):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return self._Response(body)

        return captured, fake_urlopen

    async def test_search_sends_jsonrpc_envelope_and_required_headers(self):
        body = b'{"result": {"data": {"output": {"count": 0, "field": [], "files": []}}}}'
        captured, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", fake_urlopen):
            await client.search("alpha", "", "", 0, 5, mod._Budget(config(), 5))

        request = captured["request"]
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers.get("accept"), "application/json")
        self.assertEqual(headers.get("content-type"), "application/json")

        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertIn("id", payload)
        self.assertEqual(payload["method"], mod.AnyTXTClient.SEARCH_METHOD)
        self.assertEqual(payload["params"]["input"]["pattern"], "alpha")
        self.assertEqual(payload["params"]["input"]["filterDir"], "")

    async def test_fragment_uses_jsonrpc_envelope(self):
        body = b'{"result": {"data": {"output": {"text": "snippet"}}}}'
        captured, fake_urlopen = self._capture(body)
        client = mod.AnyTXTClient(config())
        with patch.object(mod, "urlopen", fake_urlopen):
            text = await client.get_fragment("fid-1", "alpha", mod._Budget(config(), 5))

        self.assertEqual(text, "snippet")
        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(payload["method"], mod.AnyTXTClient.FRAGMENT_METHOD)
        self.assertEqual(payload["params"]["input"], {"fid": "fid-1", "pattern": "alpha"})


if __name__ == "__main__":
    unittest.main()
