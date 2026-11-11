import json
import random
import unittest
from unittest import mock

import audit_plan
from test_v2 import TmpRepoCase

NAMES = ["Omar Betawy", "Noura", "Zeinab Mohamed"]


def fake_runs(configs, per=30, seed=1):
    rng = random.Random(seed)
    runs = []
    for c in configs:
        for i in range(per):
            runs.append({"run_id": f"{c}-{i}", "problem_id": f"p{i % 10}", "config_label": c, "judge_model_id": "j",
                         "diagnosis_pass": rng.random() < .4, "diagnosis_text": f"diag {c} {i}"})
    return runs


class PureTests(unittest.TestCase):
    def test_parse_groups(self):
        g = audit_plan.parse_groups("R3 | split | 12\nR2 | pooled | 12")
        self.assertEqual([x["split"] for x in g], [True, False])
        for bad in ("R3 | whatever | 12", "R3 | split", "", "R3 | split | x"):
            with self.assertRaises(ValueError):
                audit_plan.parse_groups(bad)

    def test_draw_shape_and_determinism(self):
        runs = fake_runs(["R3 a", "R3 b", "R3 c", "R3 d", "R2 x", "R2 y"]) + [
            {"run_id": "f", "problem_id": "p1", "config_label": "R3 dumb-baseline", "judge_model_id": "j", "diagnosis_pass": False, "diagnosis_text": "t"}]
        groups = audit_plan.parse_groups(audit_plan.DEFAULT_GROUPS)
        items, notes = audit_plan.draw(runs, groups, NAMES, 7)
        again, _ = audit_plan.draw(runs, groups, NAMES, 7)
        self.assertEqual(items, again)
        self.assertEqual(len(items), 60)
        self.assertEqual(sum(i["anchor"] for i in items), 15)
        self.assertNotIn("f", {i["run_id"] for i in items})
        for n in NAMES:
            self.assertEqual(sum(1 for i in items if n in i["labelers"]), 30)
        by = {r["run_id"]: r for r in runs}
        for st in {i["stratum"] for i in items}:
            rows = [i for i in items if i["stratum"] == st]
            self.assertEqual(len(rows), 12)
            self.assertEqual(sum(by[i["run_id"]]["diagnosis_pass"] for i in rows), 6)
            probs = [by[i["run_id"]]["problem_id"] for i in rows]
            self.assertLessEqual(max(probs.count(p) for p in set(probs)), 2)

    def test_validate_label(self):
        item = {"item_id": "I001", "labelers": ["Noura"]}
        ok = {"labeler": "noura", "d1": True, "d2": True, "d3": False, "confidence": 3, "reason": ""}
        rec, err = audit_plan.validate_label(ok, item)
        self.assertIsNone(err)
        self.assertEqual((rec["labeler"], rec["overall_correct"]), ("Noura", True))
        self.assertIsNotNone(audit_plan.validate_label({**ok, "labeler": "Omar Betawy"}, item)[1])
        self.assertIsNotNone(audit_plan.validate_label({**ok, "d1": None}, item)[1])
        self.assertIsNotNone(audit_plan.validate_label({**ok, "d2": False}, item)[1])           # incorrect needs a reason
        self.assertIsNotNone(audit_plan.validate_label({**ok, "confidence": 1}, item)[1])       # unsure needs a reason
        rec, err = audit_plan.validate_label({**ok, "d2": False, "reason": "names the pod only"}, item)
        self.assertEqual((err, rec["overall_correct"]), (None, False))


class ApiTests(TmpRepoCase):
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
        rng, rows = random.Random(3), []
        for c in ("R3 a", "R3 b", "R2 x"):
            for i in range(30):
                rows.append({"run_id": f"{c}-{i}", "timestamp_utc": f"2026-11-10T10:{i:02d}:00Z", "problem_id": f"p{i % 10}", "agent_id": "reliops_v2",
                             "model_id": "m", "judge_model_id": "j", "action_space": "typed", "config_label": c, "run_class": "exploration",
                             "repeat_index": 1, "repeat_n": 1, "diagnosis_text": f"diag {c} {i}",
                             "row": {"Diagnosis.success": str(rng.random() < .5), "Mitigation.success": "True"}})
        (self.tmp / "results" / "gui_runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        p = mock.patch.object(app_module, "_owner_password", return_value="pw")
        p.start()
        self.addCleanup(p.stop)
        self.H = {"x-owner-password": "pw"}

    def draw(self, **kw):
        body = {"name": "Final audit", "labelers": NAMES, "groups": "R3 | split | 6\nR2 | pooled | 6", "anchors_per_stratum": 2, **kw}
        return self.client.post("/api/audit/draw", json=body, headers=self.H)

    def test_draw_needs_owner_and_freezes_after_labels(self):
        c = self.client
        self.assertEqual(c.post("/api/audit/draw", json={"name": "x", "labelers": NAMES}).status_code, 403)
        self.assertEqual(self.draw(preview=True).json()["items"], 18)
        self.assertEqual(len(c.get("/api/audit/plans").json()["plans"]), 0)
        r = self.draw().json()
        self.assertEqual((r["ok"], r["items"]), (True, 18))
        mine = c.get("/api/audit/mine", params={"audit_id": "final-audit", "labeler": "noura"}).json()
        self.assertGreater(mine["total"], 0)
        it = mine["items"][0]
        self.assertEqual(set(it), {"item_id", "problem_id", "diagnosis_text", "expected", "done", "mine"})  # blind
        self.assertEqual(c.get("/api/audit/mine", params={"audit_id": "final-audit", "labeler": "Someone"}).status_code, 403)
        ok = c.post("/api/audit/label", json={"audit_id": "final-audit", "item_id": it["item_id"], "labeler": "Noura", "d1": True, "d2": True, "d3": True, "confidence": 3})
        self.assertTrue(ok.json()["overall_correct"])
        self.assertEqual(self.draw().status_code, 409)
        story = c.get("/api/audit/story", params={"audit_id": "final-audit", "labeler": "Noura", "item_id": it["item_id"]})
        self.assertNotEqual(story.status_code, 403)  # allowed; the fake run has no transcript (and its id is not a UUID), so 400 or 404
        theirs = next((i for i in c.get("/api/audit/mine", params={"audit_id": "final-audit", "labeler": "Omar Betawy"}).json()["items"]
                       if i["item_id"] not in {x["item_id"] for x in mine["items"]}), None)
        self.assertEqual(c.get("/api/audit/story", params={"audit_id": "final-audit", "labeler": "Noura", "item_id": theirs["item_id"]}).status_code, 403)
        c.post("/api/audit/label", json={"audit_id": "final-audit", "item_id": it["item_id"], "labeler": "Noura", "d1": True, "d2": True, "d3": True, "confidence": 3, "used_story": True})
        sc = c.get("/api/audit/score", params={"audit_id": "final-audit"}, headers=self.H).json()
        self.assertEqual(sc["story_used"], 1)

    def test_cannot_label_someone_elses_item_and_score_is_owner_only(self):
        c = self.client
        self.draw()
        plan = c.get("/api/audit/plans").json()["plans"][0]
        self.assertEqual(plan["items"], 18)
        mine = {n: c.get("/api/audit/mine", params={"audit_id": "final-audit", "labeler": n}).json() for n in NAMES}
        theirs = next(i for i in mine["Omar Betawy"]["items"] if i["item_id"] not in {x["item_id"] for x in mine["Noura"]["items"]})
        bad = c.post("/api/audit/label", json={"audit_id": "final-audit", "item_id": theirs["item_id"], "labeler": "Noura", "d1": True, "d2": True, "d3": True, "confidence": 3})
        self.assertEqual(bad.status_code, 400)
        for n in NAMES:  # everybody labels everything, agreeing with the judge's verdict most of the time
            for i in mine[n]["items"]:
                c.post("/api/audit/label", json={"audit_id": "final-audit", "item_id": i["item_id"], "labeler": n, "d1": True, "d2": True, "d3": True, "confidence": 3})
        self.assertEqual(c.get("/api/audit/score", params={"audit_id": "final-audit"}).status_code, 403)
        sc = c.get("/api/audit/score", params={"audit_id": "final-audit"}, headers=self.H).json()
        self.assertEqual(sc["judge_vs_human"][0]["group"], "ALL")
        self.assertEqual(sc["judge_vs_human"][0]["n"], 18)
        self.assertEqual(len(sc["labeler_pairs"]), 3)
        self.assertTrue(all(p["done"] == p["total"] for p in sc["progress"]))


if __name__ == "__main__":
    unittest.main()
