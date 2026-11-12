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
        items, notes = audit_plan.draw(runs, groups, NAMES, 7, assign="spread")
        again, _ = audit_plan.draw(runs, groups, NAMES, 7, assign="spread")
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

    def test_by_problem_gives_whole_problems_and_balances(self):
        runs = fake_runs(["R3 a", "R3 b", "R3 c", "R3 d", "R2 x", "R2 y"])
        groups = audit_plan.parse_groups(audit_plan.DEFAULT_GROUPS)
        items, _ = audit_plan.draw(runs, groups, NAMES, 7, overlap_problems=1)
        self.assertEqual(len(items), 60)
        owners = {}
        for it in items:
            owners.setdefault(it["problem_id"], set()).add(it["labelers"][0])
        self.assertTrue(all(len(v) == 1 for v in owners.values()))              # one owner per problem
        per = {n: {i["problem_id"] for i in items if n in i["labelers"]} for n in NAMES}
        own = {n: {p for p, v in owners.items() if n in v} for n in NAMES}
        for n in NAMES:
            self.assertLessEqual(len(per[n]), len(own[n]) + 1)                  # own problems plus one overlap problem
            self.assertLess(len(per[n]), 10)                                    # nobody needs all 10 problems
        loads = [sum(1 for i in items if n in i["labelers"]) for n in NAMES]
        self.assertLessEqual(max(loads) - min(loads), 12)
        self.assertTrue(any(i["anchor"] for i in items))
        solo = audit_plan.draw(runs, groups, ["Noura"], 7)[0]
        self.assertTrue(all(i["labelers"] == ["Noura"] and not i["anchor"] for i in solo))

    def test_redistribute_keeps_labels_and_whole_problems(self):
        runs = fake_runs(["R3 a", "R3 b", "R2 x"])
        groups = audit_plan.parse_groups(audit_plan.DEFAULT_GROUPS)
        items, _ = audit_plan.draw(runs, groups, NAMES, 3)
        plan = {"labelers": list(NAMES), "items": items}
        first = next(i for i in items if len(i["labelers"]) == 1)
        labels = [{"item_id": first["item_id"], "labeler": first["labelers"][0], "overall_correct": True}]
        res = audit_plan.redistribute(plan, labels, ["Noura", "Zeinab Mohamed"], 0)
        self.assertEqual(res["kept"], 1)
        self.assertEqual(plan["labelers"], ["Noura", "Zeinab Mohamed"])
        kept = next(i for i in plan["items"] if i["item_id"] == first["item_id"])
        self.assertEqual(kept["labelers"], first["labelers"])                   # a labelled item is untouched
        for it in plan["items"]:
            if it["item_id"] != first["item_id"]:
                self.assertTrue(set(it["labelers"]) <= {"Noura", "Zeinab Mohamed"})
        owner = {}
        for it in plan["items"]:
            owner.setdefault(it["problem_id"], set()).add(it["labelers"][0])
        free_owners = {p: o for p, o in owner.items() if p != first["problem_id"]}
        self.assertTrue(all(len(o) == 1 for o in free_owners.values()))

    def test_extend_and_remove(self):
        runs = fake_runs(["R3 a", "R3 b"], per=40)
        items, _ = audit_plan.draw(runs, audit_plan.parse_groups("R3 | split | 6"), NAMES, 1)
        plan = {"labelers": list(NAMES), "items": items}
        before = {i["run_id"] for i in items}
        notes = audit_plan.extend(plan, runs, [], audit_plan.parse_groups("R3 | split | 4"), 2)
        self.assertEqual(len(plan["items"]), 12 + 8)
        self.assertEqual(len({i["item_id"] for i in plan["items"]}), 20)           # new ids do not collide
        self.assertEqual(len(before & {i["run_id"] for i in plan["items"][:0]}), 0)
        self.assertEqual(len({i["run_id"] for i in plan["items"]}), 20)            # no run twice
        keep = plan["items"][0]["item_id"]
        removed, refused = audit_plan.remove_items(plan, [{"item_id": keep, "labeler": "Noura"}], [keep, plan["items"][1]["item_id"]])
        self.assertEqual((len(removed), refused), (1, [keep]))

    def test_second_judge_agreement(self):
        runs = fake_runs(["R3 a", "R2 x"])
        items, _ = audit_plan.draw(runs, audit_plan.parse_groups("R3 | split | 6\nR2 | pooled | 6"), NAMES, 4)
        plan = {"labelers": NAMES, "items": items}
        by = {r["run_id"]: r for r in runs}
        rej = [{"run_id": i["run_id"], "judge_model_id": "openai/x", "success": by[i["run_id"]]["diagnosis_pass"] if k else not by[i["run_id"]]["diagnosis_pass"]}
               for k, i in enumerate(items)]  # flips exactly the first item
        rej.append({"run_id": "not-in-sample", "judge_model_id": "openai/x", "success": True})
        sj = audit_plan.second_judges(plan, runs, rej)
        self.assertEqual(len(sj), 1)
        allrow = sj[0]["rows"][0]
        self.assertEqual((allrow["group"], allrow["n"], allrow["agree"]), ("ALL", 12, 11))
        self.assertEqual(len(sj[0]["disagreements"]), 1)
        self.assertEqual(sj[0]["rejudged"], 12)

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
        self.assertEqual(set(it), {"item_id", "problem_id", "diagnosis_text", "expected", "done", "locked", "mine"})  # blind
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

    def test_edit_actions_are_owner_only_and_delete_hides(self):
        c = self.client
        self.draw()
        edit = lambda **kw: c.post("/api/audit/edit", json={"audit_id": "final-audit", **kw}, headers=self.H)
        self.assertEqual(c.post("/api/audit/edit", json={"audit_id": "final-audit", "action": "delete"}).status_code, 403)
        self.assertEqual(c.get("/api/audit/manage", params={"audit_id": "final-audit"}).status_code, 403)
        m = c.get("/api/audit/manage", params={"audit_id": "final-audit"}, headers=self.H).json()
        self.assertEqual(m["items"], 18)
        self.assertEqual(sum(x["items"] for x in m["labelers"]) >= 18, True)
        r = edit(action="redistribute", labelers=["Noura", "Zeinab Mohamed"], overlap_problems=0).json()
        self.assertEqual(r["re_dealt"], 18)
        m = c.get("/api/audit/manage", params={"audit_id": "final-audit"}, headers=self.H).json()
        self.assertEqual([x["labeler"] for x in m["labelers"]], ["Noura", "Zeinab Mohamed"])
        self.assertEqual(sum(x["items"] for x in m["labelers"]), 18)
        self.assertEqual(edit(action="extend", groups="R3 | split | 2").json()["items"], 22)
        gone = edit(action="remove", item_ids=m["unlabeled"][:2]).json()
        self.assertEqual(len(gone["removed"]), 2)
        self.assertEqual(c.get("/api/audit/manage", params={"audit_id": "final-audit"}, headers=self.H).json()["history"][-1]["action"], "remove")
        self.assertTrue(edit(action="delete").json()["ok"])
        self.assertEqual(c.get("/api/audit/plans").json()["plans"], [])
        self.assertEqual(self.draw().status_code, 409)           # the name of a deleted audit is not reused

    def test_finish_locks_answers_and_shows_the_judge_after(self):
        c = self.client
        self.draw()
        get = lambda n: c.get("/api/audit/mine", params={"audit_id": "final-audit", "labeler": n}).json()
        lab = lambda n, i, **kw: c.post("/api/audit/label", json={"audit_id": "final-audit", "item_id": i, "labeler": n, "d1": True, "d2": True, "d3": True, "confidence": 3, **kw})
        self.assertEqual(c.get("/api/audit/results", params={"audit_id": "final-audit", "labeler": "Noura"}).status_code, 403)   # nothing before finishing
        mine = get("Noura")
        self.assertEqual(c.post("/api/audit/finish", json={"audit_id": "final-audit", "labeler": "Noura"}).status_code, 400)     # items left
        for i in mine["items"]:
            lab("Noura", i["item_id"])
        self.assertFalse(get("Noura")["finished"])
        self.assertTrue(c.post("/api/audit/finish", json={"audit_id": "final-audit", "labeler": "Noura"}).json()["ok"])
        after = get("Noura")
        self.assertTrue(after["finished"] and all(i["locked"] for i in after["items"]))
        self.assertEqual(lab("Noura", mine["items"][0]["item_id"], d2=False, reason="changed my mind").status_code, 409)           # locked
        res = c.get("/api/audit/results", params={"audit_id": "final-audit", "labeler": "Noura"}).json()
        self.assertEqual(res["total"], len(mine["items"]))
        self.assertEqual(set(res["rows"][0]), {"item_id", "problem_id", "judge_pass", "judge_reasoning", "you", "your_reason", "same"})
        self.assertEqual(c.get("/api/audit/results", params={"audit_id": "final-audit", "labeler": "Zeinab Mohamed"}).status_code, 403)  # others still blind

    def test_rejudge_scope_audit_reaches_the_score(self):
        c = self.client
        self.draw()
        import runner
        seen = {}
        def fake(model, items, on_result=None, script=None):
            seen["n"] = len(items)
            for it in items:
                on_result({"run_id": it["run_id"], "judge_model_id": model, "success": True})
                yield "ok\n"
        with mock.patch.object(runner, "stream_rejudge", fake):
            body = c.get("/api/rejudge", params={"judge_model": "openai/x", "scope": "audit", "audit_id": "final-audit"}).text
        self.assertIn("[[DONE]]", body)
        self.assertEqual(seen["n"], 18)
        self.assertEqual(c.get("/api/rejudge", params={"judge_model": "openai/x", "scope": "audit", "audit_id": "nope"}).status_code, 404)
        sc = c.get("/api/audit/score", params={"audit_id": "final-audit"}, headers=self.H).json()
        self.assertEqual(sc["second_judges"][0]["rows"][0]["n"], 18)

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
