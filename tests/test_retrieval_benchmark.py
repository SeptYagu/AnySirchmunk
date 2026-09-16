import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "retrieval_benchmark.py"
SPEC = importlib.util.spec_from_file_location("retrieval_benchmark", MODULE_PATH)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkContractTests(unittest.TestCase):
    def test_fixed_manifest_contains_exactly_thirty_queries(self):
        manifest = benchmark.load_manifest(Path(__file__).parents[1] / "benchmarks" / "jsbach-30.json")
        self.assertEqual(len(manifest["queries"]), 30)
        self.assertEqual(len({query["id"] for query in manifest["queries"]}), 30)

    def test_scores_are_case_insensitive_and_negative_controls_are_explicit(self):
        recall, precision = benchmark.retrieval_scores(["A.PDF", "noise.pdf"], ["a.pdf"])
        self.assertEqual(recall, 1.0)
        self.assertEqual(precision, 0.5)
        self.assertEqual(benchmark.retrieval_scores([], []), (None, None))

    def test_percentile_uses_linear_interpolation(self):
        self.assertEqual(benchmark.percentile([1, 2, 3], 0.5), 2)
        self.assertEqual(benchmark.percentile([0, 10], 0.95), 9.5)

    def test_manifest_rejects_missing_ground_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps({"queries": [{"term": "x"}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected"):
                benchmark.load_manifest(path)


if __name__ == "__main__":
    unittest.main()
