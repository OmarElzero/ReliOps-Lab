"""Run: python -m unittest backend.test_stats  (from the repo root) or python backend/test_stats.py"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import stats  # noqa: E402


def run(problem, rep, passed, **cfg):
    base = {"agent_id": "reliops_v1", "model_id": "m", "action_space": "typed", "config_label": "base",
            "judge_model_id": "j", "run_class": "exploration"}
    base.update(cfg)
    return {**base, "problem_id": problem, "repeat_index": rep, "e2e_pass": passed,
            "timestamp_utc": f"2026-10-29T10:00:{rep:02d}Z"}


class WilsonTests(unittest.TestCase):
    def test_known_values(self):
        lo, hi = stats.wilson(0, 10)
        self.assertAlmostEqual(lo, 0.0, places=4)
        self.assertAlmostEqual(hi, 0.2775, places=3)  # textbook Wilson 95% for 0/10
        lo, hi = stats.wilson(10, 10)
        self.assertAlmostEqual(lo, 0.7225, places=3)
        self.assertAlmostEqual(hi, 1.0, places=4)
        lo, hi = stats.wilson(14, 30)
        self.assertTrue(0.30 < lo < 0.31 and 0.63 < hi < 0.65, (lo, hi))

    def test_empty(self):
        self.assertIsNone(stats.wilson(0, 0))


class McNemarTests(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(stats.mcnemar_exact(5, 0), 2 * 0.5 ** 5)
        self.assertAlmostEqual(stats.mcnemar_exact(8, 2), 2 * (1 + 10 + 45) / 1024)
        self.assertEqual(stats.mcnemar_exact(0, 0), 1.0)
        self.assertEqual(stats.mcnemar_exact(5, 5), 1.0)


class SummaryTests(unittest.TestCase):
    def test_incomplete_runs_are_not_failures(self):
        runs = [run("p1", 1, True), run("p2", 1, False), run("p3", 1, None)]
        (g,) = stats.summarize(runs)
        self.assertEqual((g["runs"], g["n"], g["passes"], g["incomplete"]), (3, 2, 1, 1))
        self.assertAlmostEqual(g["rate"], 0.5)

    def test_configs_are_separated(self):
        runs = [run("p1", 1, True), run("p1", 1, True, config_label="F7-on"), run("p1", 1, True, judge_model_id="j2")]
        self.assertEqual(len(stats.summarize(runs)), 3)

    def test_bootstrap_is_deterministic(self):
        pp = {"a": [True, True], "b": [False, False], "c": [True, False], "d": [True, True]}
        self.assertEqual(stats.bootstrap_problem_ci(pp), stats.bootstrap_problem_ci(pp))
        lo, hi = stats.bootstrap_problem_ci(pp)
        self.assertTrue(0 <= lo <= hi <= 1)


class CompareTests(unittest.TestCase):
    def test_pairs_and_flips(self):
        a = [run("p1", 1, True), run("p2", 1, True), run("p3", 1, False), run("p4", 1, False)]
        b = [run("p1", 1, True, config_label="F7"), run("p2", 1, False, config_label="F7"),
             run("p3", 1, True, config_label="F7"), run("p4", 1, True, config_label="F7"),
             run("p9", 1, True, config_label="F7")]  # p9 has no partner and must be ignored
        ka, kb = stats.config_key(a[0]), stats.config_key(b[0])
        res = stats.compare(a + b, ka, kb)
        self.assertEqual((res["pairs"], res["only_a"], res["only_b"], res["both"], res["neither"]), (4, 1, 2, 1, 0))
        self.assertAlmostEqual(res["p_value"], 1.0)  # 1 vs 2 flips: no evidence
        self.assertTrue(res["warnings"])

    def test_rerun_replaces_crashed_attempt(self):
        crashed = run("p1", 1, None)
        crashed["timestamp_utc"] = "2026-10-29T09:00:00Z"
        rerun = run("p1", 1, True)
        rerun["timestamp_utc"] = "2026-10-29T11:00:00Z"
        b = run("p1", 1, False, config_label="F7")
        res = stats.compare([crashed, rerun, b], stats.config_key(rerun), stats.config_key(b))
        self.assertEqual((res["pairs"], res["only_a"]), (1, 1))

    def test_different_judges_warn(self):
        a, b = run("p1", 1, True), run("p1", 1, True, judge_model_id="other")
        res = stats.compare([a, b], stats.config_key(a), stats.config_key(b))
        self.assertTrue(any("judge" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
