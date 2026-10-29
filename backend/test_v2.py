"""Run: python3 -I backend/test_v2.py  (from the repo root; the API tests need fastapi + httpx)"""

import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import audit  # noqa: E402
import campaign  # noqa: E402
import export  # noqa: E402
import firebase_sync  # noqa: E402
import runner  # noqa: E402
import stats  # noqa: E402
import store  # noqa: E402


def run(i, problem, rep, passed, label="base", cls="exploration", judge="j", **kw):
    r = {"run_id": f"r{i}", "timestamp_utc": f"2026-10-29T10:00:{i % 60:02d}Z", "problem_id": problem,
         "repeat_index": rep, "agent_id": "reliops_v1", "model_id": "m", "action_space": "typed",
         "config_label": label, "run_class": cls, "judge_model_id": judge, "e2e_pass": passed,
         "diagnosis_pass": passed, "diagnosis_text": f"text {i}"}
    r.update(kw)
    return r


class KappaTests(unittest.TestCase):
    def test_values(self):
        self.assertAlmostEqual(stats.cohen_kappa([True, True, False, False], [True, False, False, False]), 0.5)
        self.assertAlmostEqual(stats.cohen_kappa([True, False, True], [True, False, True]), 1.0)
        self.assertIsNone(stats.cohen_kappa([True, True], [True, True]))  # chance agreement is 1: undefined
        self.assertIsNone(stats.cohen_kappa([], []))

    def test_summary_failure_tags_and_cost(self):
        runs = [run(1, "p1", 1, False, failure_mode_tag="wrong-mechanism", total_tokens=100, num_llm_calls=4),
                run(2, "p2", 1, False, total_tokens=300, num_llm_calls=6),
                run(3, "p3", 1, True, total_tokens=200, num_llm_calls=5)]
        (g,) = stats.summarize(runs)
        self.assertEqual(g["failure_tags"], {"wrong-mechanism": 1})
        self.assertEqual(g["failures_untagged"], 1)
        self.assertAlmostEqual(g["mean_tokens"], 200)
        self.assertAlmostEqual(g["tokens_per_resolved"], 600)  # all tokens spent / 1 pass


class JudgeAuditTests(unittest.TestCase):
    def test_agreement_kappa_and_pairs(self):
        runs = [run(i, f"p{i}", 1, v) for i, v in enumerate([True, True, False, False], start=1)]
        labels = [{"run_id": f"r{i}", "correct": v} for i, v in enumerate([True, False, False, False], start=1)]
        rejudges = [{"run_id": f"r{i}", "judge_model_id": "strong", "success": v}
                    for i, v in enumerate([True, False, False, False], start=1)]
        res = audit.judge_audit(runs, labels, rejudges)
        by = {j["judge"]: j for j in res["judges"]}
        self.assertEqual(by["strong"]["agree"], 4)
        self.assertAlmostEqual(by["strong"]["kappa"], 1.0)
        self.assertEqual((by["j"]["tp"], by["j"]["fp"], by["j"]["tn"], by["j"]["fn"]), (1, 1, 2, 0))
        self.assertAlmostEqual(by["j"]["kappa"], 0.5)
        self.assertEqual(res["pairs"][0]["n"], 4)
        self.assertTrue(res["warnings"])  # fewer than 30 labels

    def test_label_queue_hides_verdict_and_skips_labelled(self):
        runs = [run(1, "p1", 1, True), run(2, "p2", 1, False), run(3, "p3", 1, True, diagnosis_text="  ")]
        q = audit.label_queue(runs, [{"run_id": "r1", "correct": True}])
        self.assertEqual([i["run_id"] for i in q], ["r2"])
        self.assertNotIn("diagnosis_pass", q[0])
        self.assertNotIn("e2e_pass", q[0])


class StandingAuditTests(unittest.TestCase):
    def test_floor_comparison_and_controls(self):
        floor = [run(i, f"p{i % 5}", 1, i < 3, label="dumb-baseline") for i in range(10)]   # 30%
        strong = [run(100 + i, f"p{i}", 1, True, label="F7") for i in range(20)]            # 100%
        weak = [run(200 + i, f"p{i % 5}", 1, i < 4, label="F2") for i in range(10)]         # 40%
        control = [run(300, "no_fault_hotel", 1, True, label="F7")]
        res = audit.standing_audit(floor + strong + weak + control)
        by = {r["config_label"]: r for r in res["rows"]}
        self.assertEqual(by["F7"]["floor_status"], "clearly above floor")
        self.assertEqual(by["F2"]["floor_status"], "NOT clearly above floor")
        self.assertEqual((by["F7"]["control_runs"], by["F7"]["control_passes"]), (1, 1))
        self.assertNotIn("dumb-baseline", by)

    def test_no_floor_is_not_a_pass(self):
        res = audit.standing_audit([run(1, "p1", 1, True, label="F7")])
        self.assertEqual(res["rows"][0]["floor_status"], "no floor runs yet")
        self.assertTrue(any("No no-fault control" in n for n in res["notes"]))

    def test_control_detection(self):
        self.assertTrue(audit.is_control("hotel_no_fault_control"))
        self.assertFalse(audit.is_control("configmap_drift_hotel_reservation"))


class CampaignTests(unittest.TestCase):
    MANIFEST = {"manifest_id": "m1", "problems": ["a", "b"], "repeat_n": 2, "agent_id": "reliops_v1", "model_id": "m",
                "action_space": "typed", "judge_model_id": "j", "config_label": "base", "git_commit": "abc12345"}

    def test_grid_statuses(self):
        runs = [run(1, "a", 1, True, manifest_id="m1"), run(2, "a", 2, False, manifest_id="m1"),
                run(3, "b", 1, None, manifest_id="m1"), run(4, "b", 2, True, manifest_id="other")]
        g = campaign.grid(self.MANIFEST, runs)
        st = {(c["problem"], c["repeat"]): c["status"] for c in g["cells"]}
        self.assertEqual(st, {("a", 1): "pass", ("a", 2): "fail", ("b", 1): "incomplete", ("b", 2): "missing"})
        self.assertEqual(set(g["todo"]), {("b", 1), ("b", 2)})

    def test_slices_partition_the_cells(self):
        cells = campaign.all_cells({"problems": ["a", "b", "c"], "repeat_n": 3})
        parts = [campaign.slice_cells(cells, k, 4) for k in range(1, 5)]
        flat = [c for p in parts for c in p]
        self.assertEqual(sorted(flat), sorted(cells))
        self.assertEqual(len(flat), len(set(flat)))
        with self.assertRaises(ValueError):
            campaign.slice_cells(cells, 5, 4)

    def test_verify_run_flags_mismatches(self):
        with mock.patch.object(campaign, "git_commit", return_value="abc12345"), \
             mock.patch.object(campaign, "agent_dirty", return_value=False):
            ok = campaign.verify_run(self.MANIFEST, agent_id="reliops_v1", model_id="m", action_space="typed",
                                     judge_model_id="j", config_label="base", problems=["a"])
            self.assertEqual(ok, [])
            bad = campaign.verify_run(self.MANIFEST, agent_id="reliops_v1", model_id="m", action_space="typed",
                                      judge_model_id="other-judge", config_label="base", problems=["a", "zzz"])
            self.assertEqual(len(bad), 2)
        with mock.patch.object(campaign, "git_commit", return_value="ffff0000"), \
             mock.patch.object(campaign, "agent_dirty", return_value=True):
            bad = campaign.verify_run(self.MANIFEST, agent_id="reliops_v1", model_id="m", action_space="typed",
                                      judge_model_id="j", config_label="base", problems=["a"])
            self.assertEqual(len(bad), 2)  # different commit, and a dirty tree

    def test_create_manifest_rules(self):
        kw = dict(name="", agent_id="x", model_id="m", action_space="typed", judge_model_id="j", repeat_n=3,
                  created_by="o", problems=["b", "a", "a"])
        with mock.patch.object(campaign, "agent_dirty", return_value=True):
            with self.assertRaises(ValueError):
                campaign.create_manifest(config_label="base", **kw)
        with mock.patch.object(campaign, "agent_dirty", return_value=False), \
             mock.patch.object(campaign, "git_commit", return_value="abc"):
            with self.assertRaises(ValueError):
                campaign.create_manifest(config_label="  ", **kw)
            m = campaign.create_manifest(config_label="base", **kw)
        self.assertEqual(m["problems"], ["a", "b"])
        self.assertEqual(m["problems_hash"], campaign.problems_hash(["a", "b"]))
        self.assertEqual(campaign.problems_hash(["a", "b"]), campaign.problems_hash(["b", "a"]))


class TmpRepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for target in ("reliops_dir",):
            p = mock.patch.object(runner, target, return_value=self.tmp)
            p.start()
            self.addCleanup(p.stop)
        for name, ret in (("list_docs", None), ("write_doc", "not written: test"), ("read_story", None),
                          ("write_story", "not written: test"), ("list_runs", None)):
            p = mock.patch.object(firebase_sync, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)


class StoreTests(TmpRepoCase):
    def test_latest_wins_and_history_is_kept(self):
        store.append("labels", {"run_id": "r1", "correct": True})
        store.append("labels", {"run_id": "r1", "correct": False})
        store.append("labels", {"run_id": "r2", "correct": True})
        latest = {r["run_id"]: r["correct"] for r in store.read("labels")}
        self.assertEqual(latest, {"r1": False, "r2": True})
        self.assertEqual(len((self.tmp / "results" / "gui_labels.jsonl").read_text().splitlines()), 3)

    def test_rejudge_key_is_run_and_judge(self):
        store.append("rejudges", {"run_id": "r1", "judge_model_id": "a", "success": True})
        store.append("rejudges", {"run_id": "r1", "judge_model_id": "b", "success": False})
        self.assertEqual(len(store.read("rejudges")), 2)


class CardTests(TmpRepoCase):
    def test_cards_list_read_and_section(self):
        d = self.tmp / "experiments" / "problem-cards"
        d.mkdir(parents=True)
        (d / "TEMPLATE.md").write_text("# t")
        (d / "p1.md").write_text("# card\n\nStatus: **draft**. x\n\n## 3. Mechanics\nm\n\n## 4. What a correct diagnosis must say\n- D1 thing\n\n## 5. Fix\nf\n")
        self.assertEqual(runner.list_cards(), [{"problem_id": "p1", "status": "draft"}])
        card = runner.read_card("p1")
        self.assertIn("D1 thing", runner.card_section(card, 4))
        self.assertNotIn("Fix", runner.card_section(card, 4).replace("What a correct", ""))
        self.assertIsNone(runner.read_card("../etc/passwd"))
        self.assertIsNone(runner.read_card("nope"))


class CostTests(TmpRepoCase):
    def test_transcript_stats(self):
        t = self.tmp / "external" / "envs" / "sregym" / "x.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text("\n".join(json.dumps(r) for r in [
            {"type": "model_call", "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            {"type": "action"}, {"type": "model_call", "usage": {"prompt_tokens": 20, "completion_tokens": 3}},
            {"type": "submit"}]))
        self.assertEqual(runner.transcript_stats("x.jsonl"), {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35,
                                                              "num_llm_calls": 2, "num_tool_calls": 1})
        self.assertEqual(runner.transcript_stats(None), {})
        self.assertEqual(runner.transcript_stats("missing.jsonl"), {})


class RejudgePlumbingTests(TmpRepoCase):
    def test_stream_rejudge_stores_verdicts_but_never_dry_run(self):
        sregym = self.tmp / "external" / "envs" / "sregym"
        sregym.mkdir(parents=True)
        script = self.tmp / "fake_rejudge.py"
        script.write_text(
            "import sys, json\n"
            "a = sys.argv\n"
            "inp, out = a[a.index('--in')+1], a[a.index('--out')+1]\n"
            "rows = [json.loads(l) for l in open(inp)]\n"
            "with open(out, 'w') as f:\n"
            "    for r in rows:\n"
            "        f.write(json.dumps({'run_id': r['run_id'], 'problem_id': r['problem_id'], 'judge_model_id': 'real', 'success': True,\n"
            "            'judgment': 'True', 'composite_score': 0.9, 'dimensions': {'D1': {'score': 1.0}}, 'reasoning': 'ok', 'checklist': []}) + '\\n')\n"
            "    f.write(json.dumps({'run_id': 'x', 'judge_model_id': 'dry-run', 'success': True}) + '\\n')\n")
        got = []
        # run the script with the current python instead of `uv run python`
        real_stream = runner._stream
        def fake_stream(cmd, **kw):
            return real_stream([sys.executable, *cmd[3:]], **kw)
        with mock.patch.object(runner, "_stream", side_effect=fake_stream), \
             mock.patch.object(runner, "merged_env_vars", return_value={}):
            out = "".join(runner.stream_rejudge("gemini/strong", [{"run_id": "r1", "problem_id": "p", "submission": "t"}],
                                                on_result=got.append, script=script))
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["run_id"], got[0]["judge_model_id"], got[0]["d1"]), ("r1", "gemini/strong", 1.0))
        self.assertIn("stored 1 re-judge verdict", out)


class ExportTests(unittest.TestCase):
    def test_svg_is_valid_xml_and_csv_has_header(self):
        groups = stats.summarize([run(i, f"p{i}", 1, i % 2 == 0) for i in range(6)])
        svg = export.summary_svg(groups, "e2e_pass")
        root = ET.fromstring(svg)
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("3/6", svg)
        self.assertTrue(export.summary_csv(groups).startswith("config_label,"))
        ET.fromstring(export.summary_svg([], "e2e_pass"))  # empty chart still valid


class ApiTests(TmpRepoCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.client = TestClient(app_module.app)
        (self.tmp / "results").mkdir()
        rows = []
        for i in range(1, 5):
            rows.append({"run_id": f"r{i}", "timestamp_utc": f"2026-10-29T10:00:{i:02d}Z", "problem_id": f"p{i}", "agent_id": "reliops_v1",
                         "model_id": "m", "judge_model_id": "j", "action_space": "typed", "config_label": "base", "run_class": "exploration",
                         "repeat_index": 1, "repeat_n": 1, "diagnosis_text": f"diag {i}",
                         "row": {"Diagnosis.success": str(i % 2 == 0), "Mitigation.success": "True"}})
        (self.tmp / "results" / "gui_runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def test_refuses_foreign_host_and_cross_site_post(self):
        r = self.client.get("/api/config", headers={"host": "evil.example.com"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/reviews", json={}, headers={"origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={"host": "localhost:8765"}).status_code, 200)

    def test_review_label_audit_flow(self):
        c = self.client
        self.assertEqual(c.post("/api/review", json={"run_id": "r1", "failure_mode_tag": "bogus"}).status_code, 400)
        self.assertTrue(c.post("/api/review", json={"run_id": "r1", "failure_mode_tag": "wrong-mechanism", "first_bad_step": 3, "note": "n", "reviewer": "o"}).json()["ok"])
        runs = {r["run_id"]: r for r in c.get("/api/results").json()["runs"]}
        self.assertEqual((runs["r1"]["failure_mode_tag"], runs["r1"]["first_bad_step"]), ("wrong-mechanism", 3))
        q = c.get("/api/label_queue").json()
        self.assertEqual(len(q["queue"]), 4)
        self.assertNotIn("diagnosis_pass", q["queue"][0])
        for i in range(1, 5):
            c.post("/api/label", json={"run_id": f"r{i}", "correct": i % 2 == 0, "labeler": "o"})
        a = c.get("/api/judge_audit").json()
        self.assertEqual(a["labeled"], 4)
        self.assertAlmostEqual(a["judges"][0]["kappa"], 1.0)
        self.assertEqual(c.get("/api/label_queue").json()["queue"], [])

    def test_frozen_flow_and_refusals(self):
        c = self.client
        with mock.patch.object(campaign, "agent_dirty", return_value=False), mock.patch.object(campaign, "git_commit", return_value="abc"):
            body = {"agent_id": "reliops_v1", "model_id": "m", "action_space": "typed", "judge_model_id": "j",
                    "config_label": "base", "problems": ["p1", "p2"], "repeat_n": 2}
            m = c.post("/api/manifest", json=body).json()["manifest"]
            g = c.get("/api/campaign", params={"manifest_id": m["manifest_id"], "k": 1, "n": 2}).json()
            self.assertEqual(g["counts"]["missing"], 4)
            self.assertEqual(len(g["slice"]["cells"]), 2)
            self.assertEqual(c.get("/api/campaign", params={"manifest_id": "nope"}).status_code, 404)
            # refused: wrong judge
            r = c.get("/api/run", params={"agent": "reliops_v1", "model": "m", "judge_model": "other", "problems": "p1",
                                          "action_space": "typed", "config_label": "base", "manifest_id": m["manifest_id"]})
            self.assertIn("REFUSED", r.text)
            self.assertIn("judge_model_id", r.text)
        # a frozen run without a manifest is refused
        r = c.get("/api/run", params={"agent": "reliops_v1", "model": "m", "judge_model": "j", "problems": "p1", "run_class": "frozen"})
        self.assertIn("needs a manifest", r.text)
        with mock.patch.object(campaign, "agent_dirty", return_value=True):
            self.assertEqual(c.post("/api/manifest", json=body).status_code, 400)

    def test_cards_exports_and_audit_endpoints(self):
        c = self.client
        self.assertEqual(c.get("/api/card/p1").status_code, 404)
        d = self.tmp / "experiments" / "problem-cards"; d.mkdir(parents=True); (d / "p1.md").write_text("# c\n")
        self.assertEqual(c.get("/api/card/p1").text, "# c\n")
        self.assertEqual(c.get("/api/cards").json()["cards"][0]["problem_id"], "p1")
        self.assertTrue(c.get("/api/export/runs.csv").text.startswith("run_id,"))
        self.assertTrue(c.get("/api/export/summary.csv").text.startswith("config_label,"))
        ET.fromstring(c.get("/api/export/summary.svg").text)
        self.assertEqual(c.get("/api/standing_audit").json()["rows"][0]["floor_status"], "no floor runs yet")
        self.assertEqual(c.get("/api/summary").json()["groups"][0]["failures_untagged"], 2)


if __name__ == "__main__":
    unittest.main()
