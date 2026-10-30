"""Run: python3 -I backend/test_dashboard.py  (from the repo root; the API tests need fastapi + httpx)"""

import csv
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import campaign  # noqa: E402
import dashboard  # noqa: E402
import pins  # noqa: E402
import store  # noqa: E402
from test_v2 import TmpRepoCase  # noqa: E402


def write_set(tmp, name, rows):
    exp = tmp / "experiments"
    exp.mkdir(exist_ok=True)
    with (exp / name).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "problem_id", "type", "app"])
        for i, (pid, typ) in enumerate(rows, start=1):
            w.writerow([i, pid, typ, "hotel"])


def pin(tmp, frozen, dev, bad_hash=False):
    h = lambda ids: campaign.problems_hash(ids)  # noqa: E731
    (tmp / "experiments" / "pins.json").write_text(json.dumps({
        "_about": "x",
        "frozen-30": {"file": "first-30.csv", "role": "frozen", "problems_hash": "0" * 16 if bad_hash else h(frozen), "pinned_on": "d"},
        "dev-12": {"file": "dev-12.csv", "role": "dev", "problems_hash": h(dev), "pinned_on": "d"},
    }))


def run_rec(rid, problem, ok, **kw):
    r = {"run_id": rid, "timestamp_utc": "2026-10-30T10:00:00Z", "problem_id": problem, "agent_id": "reliops_v1",
         "model_id": "m", "judge_model_id": "j", "action_space": "typed", "config_label": "base", "run_class": "exploration",
         "repeat_index": 1, "run_by": "omar", "row": {}}
    r.update(kw)
    if ok is None:
        r["row"] = {"run_status": "incomplete"}
    else:
        r["row"] = {"Diagnosis.success": ok, "Mitigation.success": ok}
    return r


class PinTests(TmpRepoCase):
    def test_pin_ok_changed_and_roles(self):
        write_set(self.tmp, "first-30.csv", [("a", "Net"), ("b", "Sec")])
        write_set(self.tmp, "dev-12.csv", [("c", "Net")])
        pin(self.tmp, ["a", "b"], ["c"])
        ps = pins.load()
        self.assertEqual({p["name"]: p["status"] for p in ps}, {"frozen-30": "ok", "dev-12": "ok"})
        self.assertEqual(pins.roles(ps), {"a": "frozen", "b": "frozen", "c": "dev"})
        write_set(self.tmp, "first-30.csv", [("a", "Net"), ("b", "Sec"), ("z", "Sec")])
        self.assertEqual({p["name"]: p["status"] for p in pins.load()}["frozen-30"], "changed")

    def test_overlap_is_reported_and_not_assigned(self):
        write_set(self.tmp, "first-30.csv", [("a", "Net")])
        write_set(self.tmp, "dev-12.csv", [("a", "Net"), ("c", "Net")])
        pin(self.tmp, ["a"], ["a", "c"])
        self.assertEqual(pins.overlap(), ["a"])
        self.assertEqual(pins.roles(), {"c": "dev"})

    def test_no_pins_file_is_fine(self):
        self.assertEqual(pins.load(), [])


class CategoryTests(unittest.TestCase):
    def test_labels_that_differ_only_by_suffix_group_together(self):
        import runner
        n = runner.normalize_category
        self.assertEqual(n("Misconfiguration Failure"), n("Misconfiguration [our label]"))
        self.assertEqual(n("Network Failure (OS level)"), "Network")
        self.assertEqual(n("Cloud Management System Failure"), n("Cloud Management (RBAC) [our label]"))
        self.assertEqual(n(""), "Uncategorized")


class PurposeTests(unittest.TestCase):
    def test_purpose_rules(self):
        roles = {"f": "frozen", "d": "dev"}
        p = dashboard.purpose_of
        self.assertEqual(p({"problem_id": "d"}, None, roles), ("dev", "auto"))
        self.assertEqual(p({"problem_id": "f", "run_class": "frozen"}, None, roles), ("final", "auto"))
        # a frozen-set problem outside a frozen campaign is not reportable
        self.assertEqual(p({"problem_id": "f", "run_class": "exploration"}, None, roles), ("experiment", "auto"))
        self.assertEqual(p({"problem_id": "x"}, None, roles), ("experiment", "auto"))
        self.assertEqual(p({"problem_id": "f", "run_class": "frozen"}, {"purpose": "dev"}, roles), ("dev", "marked"))


class FilterTests(unittest.TestCase):
    RUNS = [
        {"run_id": "1", "problem_id": "a", "category": "Net", "purpose": "dev", "e2e_pass": True, "model_id": "m1", "timestamp_utc": "2026-10-28T1"},
        {"run_id": "2", "problem_id": "b", "category": "Sec", "purpose": "dev", "e2e_pass": False, "failure_mode_tag": "wrong-target", "model_id": "m1", "timestamp_utc": "2026-10-29T1"},
        {"run_id": "3", "problem_id": "c", "category": "Sec", "purpose": "experiment", "e2e_pass": None, "model_id": "m2", "timestamp_utc": "2026-10-30T1"},
    ]

    def ids(self, f):
        return [r["run_id"] for r in dashboard.apply_filters(self.RUNS, f)]

    def test_filters(self):
        self.assertEqual(self.ids({}), ["1", "2", "3"])
        self.assertEqual(self.ids({"category": ["Sec"]}), ["2", "3"])
        self.assertEqual(self.ids({"category": ["Sec"], "purpose": ["dev"]}), ["2"])
        self.assertEqual(self.ids({"outcome": ["incomplete"]}), ["3"])
        self.assertEqual(self.ids({"outcome": ["fail"], "failure_tag": ["wrong-target"]}), ["2"])
        self.assertEqual(self.ids({"date_from": "2026-10-29"}), ["2", "3"])
        self.assertEqual(self.ids({"q": "WRONG"}), ["2"])
        self.assertEqual(self.ids({"model_id": ["m1", "m2"]}), ["1", "2", "3"])


class DashboardApiTests(TmpRepoCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.client = TestClient(app_module.app)
        write_set(self.tmp, "first-30.csv", [("f1", "Net"), ("f2", "Sec")])
        write_set(self.tmp, "dev-12.csv", [("d1", "Net"), ("d2", "Sec")])
        pin(self.tmp, ["f1", "f2"], ["d1", "d2"])
        (self.tmp / "results").mkdir()
        recs = [run_rec("r1", "d1", True), run_rec("r2", "d2", False),
                run_rec("r3", "f1", True), run_rec("r4", "f2", None),
                run_rec("r5", "d1", True, model_id="m2", timestamp_utc="2026-10-29T10:00:00Z")]
        with (self.tmp / "results" / "gui_runs.jsonl").open("w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        store.append("reviews", {"run_id": "r2", "failure_mode_tag": "x"})

    def dash(self, **filters):
        r = self.client.post("/api/dashboard", json={"filters": filters})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_config_marks_pinned_sets(self):
        cfg = self.client.get("/api/config").json()
        by = {s["id"]: s for s in cfg["problem_sets"]}
        self.assertEqual(by["first-30"]["pin"]["role"], "frozen")
        self.assertIn("pinned frozen set", by["first-30"]["label"])
        self.assertEqual(by["dev-12"]["pin"]["role"], "dev")
        self.assertEqual(cfg["pin_overlap"], [])

    def test_changed_pin_is_flagged_in_label(self):
        write_set(self.tmp, "first-30.csv", [("f1", "Net")])
        by = {s["id"]: s for s in self.client.get("/api/config").json()["problem_sets"]}
        self.assertIn("CHANGED since pinned", by["first-30"]["label"])

    def test_runs_get_purpose_and_category(self):
        d = self.dash()
        by = {r["run_id"]: r for r in d["runs"]}
        self.assertEqual((by["r1"]["purpose"], by["r1"]["purpose_source"], by["r1"]["category"]), ("dev", "auto", "Net"))
        # frozen-set problem run as exploration is an experiment, not a reportable result
        self.assertEqual(by["r3"]["purpose"], "experiment")
        self.assertEqual(d["kpis"]["runs"], 5)
        self.assertEqual(d["kpis"]["incomplete"], 1)

    def test_filter_to_one_purpose_computes_from_that_subset(self):
        d = self.dash(purpose=["dev"], model_id=["m"])
        self.assertEqual(d["kpis"]["runs"], 2)
        self.assertEqual((d["groups"][0]["passes"], d["groups"][0]["n"]), (1, 2))
        self.assertEqual({c["category"] for c in d["categories"]}, {"Net", "Sec"})
        self.assertEqual(d["tags"], [{"tag": "x", "n": 1}])
        self.assertTrue(any("judge" in n.lower() for n in d["notes"]))   # no hand labels yet
        self.assertFalse(any("mix purposes" in n for n in d["notes"]))

    def test_mixed_purposes_warn_and_incomplete_never_fails(self):
        d = self.dash()
        self.assertTrue(any("mix purposes" in n for n in d["notes"]))
        self.assertEqual(d["kpis"]["failures"], 1)

    def test_mark_overrides_and_clears(self):
        c = self.client
        r = c.post("/api/marks", json={"run_ids": ["r3"], "purpose": "dev", "marked_by": "omar"})
        self.assertEqual(r.json()["marked"], 1)
        by = {x["run_id"]: x for x in self.dash()["runs"]}
        self.assertEqual((by["r3"]["purpose"], by["r3"]["purpose_source"]), ("dev", "marked"))
        self.assertEqual(self.dash(purpose=["dev"])["kpis"]["runs"], 4)
        c.post("/api/marks", json={"run_ids": ["r3"], "purpose": ""})
        by = {x["run_id"]: x for x in self.dash()["runs"]}
        self.assertEqual((by["r3"]["purpose"], by["r3"]["purpose_source"]), ("experiment", "auto"))
        self.assertEqual(c.post("/api/marks", json={"run_ids": ["r3"], "purpose": "nonsense"}).status_code, 400)
        self.assertEqual(c.post("/api/marks", json={"run_ids": []}).status_code, 400)

    def test_report_markdown_states_basis(self):
        r = self.client.post("/api/report.md", json={"filters": {"purpose": ["dev"]}})
        self.assertEqual(r.status_code, 200)
        t = r.text
        for needle in ("What these numbers are based on", "purpose = dev", "Summary per configuration", "By fault category"):
            self.assertIn(needle, t)

    def test_people_panel_and_filter(self):
        d = self.dash()
        self.assertEqual([(p["name"], p["runs"]) for p in d["people"]], [("omar", 5)])
        self.assertEqual(self.dash(run_by=["nobody"])["people"], [])

    def test_one_run_has_full_record(self):
        r = self.client.get("/api/run/r1").json()
        self.assertEqual((r["problem_id"], r["purpose"], r["category"]), ("d1", "dev", "Net"))
        self.assertEqual(self.client.get("/api/run/nope").status_code, 404)

    def test_bad_metric_is_a_400(self):
        self.assertEqual(self.client.post("/api/dashboard", json={"metric": "nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
