"""Run: python3 -I backend/test_screening.py  (from the repo root)"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import screening  # noqa: E402
from test_v2 import TmpRepoCase  # noqa: E402

PROBS = [f"p{i}" for i in range(1, 13)]
PAIRS = [(p, r) for p in PROBS for r in (1, 2, 3)]          # 36 pairs, like dev-12 x 3 repeats


def runs_for(label, passing=(), failing=None, none=(), commit="aaaa1111", who="Omar", versions="helm v4"):
    """One run per pair: pass for `passing`, fail for the rest (or only `failing`), no result for `none`."""
    out = []
    for i, (prob, rep) in enumerate(PAIRS):
        if (prob, rep) in set(none):
            v = None
        elif (prob, rep) in set(passing):
            v = True
        elif failing is None or (prob, rep) in set(failing):
            v = False
        else:
            continue
        out.append({"config_label": label, "problem_id": prob, "repeat_index": rep, "e2e_pass": v, "timestamp_utc": f"2026-11-05T10:{i:02d}:00Z",
                    "run_by": who, "git_commit": commit, "machine_versions": versions, "agent_id": "a", "model_id": "m",
                    "action_space": "s", "judge_model_id": "j"})
    return out


def ladder(sw=("F1", "F2"), win=10, **kw):
    """F1 helps on the first `win` pairs both as one-on and one-off; F2 changes nothing."""
    gain = PAIRS[:win]
    runs = runs_for("R2 B2") + runs_for("R2 B2 +F1", gain) + runs_for("R2 B2 +F2") + runs_for("R2 ALL", gain) \
        + runs_for("R2 ALL -F1") + runs_for("R2 ALL -F2", gain)
    return runs


class MathTests(unittest.TestCase):
    def test_holm_by_hand(self):
        # sorted 0.005, 0.01, 0.03, 0.04 -> x4, x3, x2, x1 = 0.02, 0.03, 0.06, 0.04 -> running max 0.02, 0.03, 0.06, 0.06
        self.assertEqual([round(x, 6) for x in screening.holm([0.01, 0.04, 0.03, 0.005])], [0.03, 0.06, 0.06, 0.02])
        self.assertEqual(screening.holm([0.9, 0.8]), [1.0, 1.0])               # 0.8 x 2 = 1.6 -> 1; 0.9 x 1 stays below the running max
        self.assertEqual(screening.holm([]), [])
        self.assertTrue(all(0 <= x <= 1 for x in screening.holm([0.5, 0.7, 0.9, 1.0])))

    def test_the_rule_thresholds_of_plan_15_section_8(self):
        # 16 contrasts: one has k flipped pairs all one way, the rest cannot be tested yet (p = 1).
        def adj(k):
            p = 2 / 2 ** k
            return screening.holm([p] + [1.0] * 15)[0]
        self.assertGreater(adj(5), 0.05)                 # 5 all one way: p = 0.0625 even before correction
        self.assertAlmostEqual(2 / 2 ** 6, 0.03125)      # 6 all one way: p = 0.031, the "without correction" threshold
        self.assertLess(adj(10), 0.05)                   # 10 all one way passes with Holm over 16 (16 x 0.00195 = 0.031)
        self.assertGreater(adj(9), 0.05)                 # 9 does not (16 x 0.0039 = 0.0625)


class ScreenTests(unittest.TestCase):
    def test_ten_flipped_pairs_for_f1_is_a_finding_and_f2_shows_nothing(self):
        r = screening.screen(ladder(), expected=set(PAIRS))
        self.assertEqual(r["switches"], ["F1", "F2"])
        c = {x["id"]: x for x in r["contrasts"]}
        self.assertEqual((c["F1:on"]["treated_only"], c["F1:on"]["control_only"], c["F1:on"]["pairs"]), (10, 0, 36))
        self.assertAlmostEqual(c["F1:on"]["p"], 2 / 1024)
        self.assertAlmostEqual(c["F1:on"]["p_holm"], 4 * 2 / 1024)                    # family of 4 here: 2 switches x on/off
        self.assertTrue(c["F1:on"]["passes_holm"] and c["F1:off"]["passes_holm"])
        self.assertEqual((c["F2:on"]["diff"], c["F2:on"]["p"]), (0, 1.0))
        # F2 as one-off: ALL passes the 10 pairs, ALL -F2 passes them too -> nothing flipped
        self.assertEqual(c["F2:off"]["diff"], 0)
        v = {x["switch"]: x["verdict"] for x in r["verdicts"]}
        self.assertEqual(v, {"F1": "finding: helps", "F2": "no evidence"})

    def test_six_flipped_pairs_is_a_candidate_only_when_the_family_is_16(self):
        gain = PAIRS[:6]
        sw = [f"S{i}" for i in range(8)]
        runs = runs_for("R2 B2") + runs_for("R2 ALL", gain)
        for f in sw:
            runs += runs_for(f"R2 B2 +{f}", gain if f == "S0" else ()) + runs_for(f"R2 ALL -{f}")
        r = screening.screen(runs, expected=set(PAIRS))
        self.assertEqual(len(r["contrasts"]), 16)
        c = {x["id"]: x for x in r["contrasts"]}
        self.assertAlmostEqual(c["S0:on"]["p"], 2 / 64)
        self.assertTrue(c["S0:on"]["passes_raw"])
        self.assertFalse(c["S0:on"]["passes_holm"])                                    # 16 x 0.031 = 0.5
        self.assertEqual({x["switch"]: x["verdict"] for x in r["verdicts"]}["S0"], "candidate: helps")

    def test_family_stays_16_when_some_contrasts_cannot_be_tested_yet(self):
        sw = [f"S{i}" for i in range(8)]
        gain = PAIRS[:10]
        runs = runs_for("R2 B2") + runs_for("R2 B2 +S0", gain)                         # only one contrast has data
        r = screening.screen(runs, switches=sw, expected=set(PAIRS))
        c = {x["id"]: x for x in r["contrasts"]}
        self.assertAlmostEqual(c["S0:on"]["p_holm"], 16 * 2 / 1024)                    # 0.03125, not 1 x p
        self.assertIsNone(c["S1:on"]["p"])
        self.assertIsNone(c["S1:on"]["p_holm"])

    def test_missing_cells_are_not_failures_and_the_worst_case_is_shown(self):
        none = PAIRS[:6]
        runs = runs_for("R2 B2", passing=PAIRS[6:9], none=none)          # 6 incomplete, 3 pass, 27 fail
        r = screening.screen(runs, expected=set(PAIRS), switches=[])
        c = r["configs"][0]
        self.assertEqual((c["cells"], c["passes"], c["missing"]), (30, 3, 6))
        self.assertAlmostEqual(c["rate"], 3 / 30)                                       # incomplete runs do not lower the rate
        self.assertAlmostEqual(c["worst_low"], 3 / 36)
        self.assertAlmostEqual(c["worst_high"], 9 / 36)

    def test_pass_all_repeats_per_problem(self):
        # p1 passes 3 of 3, p2 passes 2 of 3, the other problems fail
        passing = [("p1", 1), ("p1", 2), ("p1", 3), ("p2", 1), ("p2", 2)]
        r = screening.screen(runs_for("R2 B2", passing), expected=set(PAIRS), switches=[])
        c = r["configs"][0]
        self.assertEqual((c["passes"], c["problems_full"], c["pass_all"], c["repeats"]), (5, 12, 1, 3))

    def test_a_newer_run_of_the_same_pair_replaces_the_older_one(self):
        runs = runs_for("R2 B2", [("p1", 1)])
        again = dict(runs[0], e2e_pass=False, timestamp_utc="2026-11-06T00:00:00Z")      # a re-run after the first
        r = screening.screen(runs + [again], expected=set(PAIRS), switches=[])
        self.assertEqual(r["configs"][0]["passes"], 0)

    def test_excluded_problems_and_commits(self):
        runs = runs_for("R2 B2", [("p1", 1)]) + runs_for("R2 B2", [("p1", 2)], commit="old00000")
        r = screening.screen(runs, expected=set(PAIRS), switches=[], excluded_problems={"p1"})
        self.assertEqual(r["pairs_expected"], 33)
        self.assertEqual(r["excluded_problems"], ["p1"])
        r2 = screening.screen(runs, expected=set(PAIRS), switches=[], exclude_commits={"old00000"})
        self.assertTrue(any("set aside" in w for w in r2["warnings"]))
        r3 = screening.screen(runs, expected=set(PAIRS), switches=[])
        self.assertTrue(any("more than one commit" in w for w in r3["warnings"]))                  # mixed agent code is flagged

    def test_different_tool_versions_are_flagged(self):
        runs = runs_for("R2 B2", versions="helm v3.16") + runs_for("R2 B2", versions="helm v4.0.2")
        r = screening.screen(runs, expected=set(PAIRS), switches=[])
        self.assertTrue(any("different tool versions" in w for w in r["warnings"]))

    def test_candidate_by_doc_15_section_7(self):
        r = screening.screen(ladder(), expected=set(PAIRS))
        # ALL beats B2 by 10 pairs; F1 effects (+10, +10) stay, F2 (0, 0) is dropped -> B2 + F1, which has already run
        self.assertEqual(r["all_vs_base"]["diff"], 10)
        self.assertEqual(r["candidate"]["switches"], ["F1"])
        self.assertEqual(r["candidate"]["dropped"], ["F2"])
        self.assertIn("already run", r["candidate"]["note"])
        # ALL does not beat B2: B2 plus the switches with a positive one-on effect
        gain = PAIRS[:4]
        runs = runs_for("R2 B2") + runs_for("R2 B2 +F1", gain) + runs_for("R2 B2 +F2") + runs_for("R2 ALL") + runs_for("R2 ALL -F1") + runs_for("R2 ALL -F2")
        r2 = screening.screen(runs, expected=set(PAIRS))
        self.assertEqual(r2["candidate"]["rule"], "ALL does not beat B2")
        self.assertEqual(r2["candidate"]["switches"], ["F1"])

    def test_one_on_and_one_off_must_point_the_same_way(self):
        gain = PAIRS[:10]
        runs = runs_for("R2 B2") + runs_for("R2 B2 +F1", gain) + runs_for("R2 ALL", gain) + runs_for("R2 ALL -F1", gain + PAIRS[10:12])   # one-off: ALL is worse
        r = screening.screen(runs, expected=set(PAIRS))
        c = {x["id"]: x for x in r["contrasts"]}
        self.assertEqual(c["F1:on"]["direction"], "up")
        self.assertEqual(c["F1:off"]["direction"], "down")
        self.assertEqual(r["verdicts"][0]["verdict"], "mixed")

    def test_exports_have_every_config_and_contrast(self):
        r = screening.screen(ladder(), expected=set(PAIRS))
        self.assertEqual(len(screening.configs_csv(r).strip().splitlines()), 1 + 6)
        self.assertEqual(len(screening.contrasts_csv(r).strip().splitlines()), 1 + 4 + 1)
        md = screening.to_markdown(r, "Test")
        for needle in ("## Configurations", "## Contrasts", "finding: helps", "outside the 16", "## Rule"):
            self.assertIn(needle, md)

    def test_thin_data_is_said_and_the_candidate_is_not_called_evidence(self):
        r = screening.screen(runs_for("R2 B2", PAIRS[:5], failing=PAIRS[5:8]) + runs_for("R2 ALL", PAIRS[:2], failing=PAIRS[2:8]), expected=set(PAIRS), switches=[])
        self.assertTrue(any("Only 16 of 72 cells" in w for w in r["warnings"]))
        r = screening.screen(ladder(win=3), expected=set(PAIRS))
        self.assertFalse(r["candidate"]["evidence"])
        self.assertIn("not evidence", r["candidate"]["note"])
        self.assertTrue(screening.screen(ladder(), expected=set(PAIRS))["candidate"]["evidence"])

    def test_nothing_run_yet(self):
        r = screening.screen([], switches=[])
        self.assertEqual(r["configs"], [])
        r = screening.screen(runs_for("R2 B2"), expected=set(PAIRS))
        self.assertEqual(r["verdicts"], [])


class ScreeningApiTests(TmpRepoCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.app_module = app_module
        self.client = TestClient(app_module.app)
        (self.tmp / "results").mkdir()

    def test_endpoint_uses_only_the_current_round_and_never_quick_runs_and_leaves_out_null_passed_problems(self):
        runs = []
        for r in ladder():
            runs.append(dict(r, experiment="Ex", origin="queue"))
        runs.append(dict(runs_for("R2 B2", PAIRS[:1])[0], experiment="Ex", origin="quick", timestamp_utc="2026-11-07T00:00:00Z"))     # ignored
        runs.append(dict(runs_for("R2 B2", PAIRS[:1])[0], experiment="Ex (restart 2)", origin="queue", e2e_pass=True, timestamp_utc="2026-11-08T00:00:00Z"))
        c = self.client
        with mock.patch.object(self.app_module, "_collect_runs", return_value=("local", runs)):
            self.assertEqual(c.get("/api/screening").status_code, 400)                  # needs the experiment name
            r = c.get("/api/screening", params={"experiment": "Ex"})                    # the newest round is "Ex (restart 2)": one run
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["experiment"], "Ex (restart 2)")
            self.assertEqual(r.json()["runs_used"], 1)
            # the first round, named exactly, is the full ladder; a null run that passed p1 takes p1 out of every number
            runs2 = [dict(x, experiment="Ex") for x in runs[:-2]]
            runs2.append(dict(runs_for("R2 NULL", PAIRS[:1])[0], agent_switches="NULL_AGENT", experiment="Other", e2e_pass=True))
        with mock.patch.object(self.app_module, "_collect_runs", return_value=("local", runs2)):
            r = c.get("/api/screening", params={"experiment": "Ex"}).json()
            self.assertEqual(r["excluded_problems"], ["p1"])
            self.assertEqual(r["pairs_expected"], 33)
            r = c.get("/api/screening", params={"experiment": "Ex", "include_null": "true"}).json()
            self.assertEqual(r["excluded_problems"], [])
            self.assertEqual(r["pairs_expected"], 36)
            self.assertEqual({v["switch"]: v["verdict"] for v in r["verdicts"]}, {"F1": "finding: helps", "F2": "no evidence"})
            md = c.get("/api/screening", params={"experiment": "Ex", "include_null": "true", "format": "md"})
            self.assertIn("text/markdown", md.headers["content-type"])
            self.assertIn("## Verdict per switch", md.text)
            self.assertEqual(len(c.get("/api/screening", params={"experiment": "Ex", "format": "csv-contrasts"}).text.strip().splitlines()), 6)
            self.assertIn("label,cells", c.get("/api/screening", params={"experiment": "Ex", "format": "csv"}).text)
            self.assertEqual(c.get("/api/screening", params={"experiment": "Ex", "metric": "nope"}).status_code, 400)

    def test_queue_gives_the_expected_pairs_and_applies_restart_my_share(self):
        q = self.client.post("/api/queue", json={"name": "L", "steps": [
            {"agent_id": "reliops_v2", "agent_label": "x", "action_space": "typed+shell", "model_id": "m", "judge_model_id": "j",
             "config_label": "R2 B2", "problems": PROBS, "repeat_n": 3}]}).json()["queue"]
        runs = [dict(r, experiment="Ex", origin="queue") for r in runs_for("R2 B2", PAIRS[:2], failing=PAIRS[2:6])]      # 6 attempts
        with mock.patch.object(self.app_module, "_collect_runs", return_value=("local", runs)):
            r = self.client.get("/api/screening", params={"experiment": "Ex", "queue_id": q["queue_id"]}).json()
        self.assertEqual(r["pairs_expected"], 36)                                      # from the queue, not from what was run
        self.assertEqual((r["configs"][0]["cells"], r["configs"][0]["missing"]), (6, 30))


if __name__ == "__main__":
    unittest.main()
