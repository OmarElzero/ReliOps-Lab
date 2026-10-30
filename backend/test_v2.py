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


class KeyPoolTests(unittest.TestCase):
    def test_off_by_default_and_round_robin_when_named(self):
        import runner
        self.assertEqual(runner.key_pool({"GEMINI_API_KEY": "a"}), [])
        runner._key_counter = 0
        self.assertIsNone(runner.next_key(runner.key_pool({"RELIOPS_KEY_POOL": "K1", "K1": "a"})))   # one key: nothing to rotate
        env = {"RELIOPS_KEY_POOL": "K1,K2,K3", "RELIOPS_KEY_OWNERS": "omar,sara", "K1": "a", "K2": "b", "K3": "c"}
        pool = runner.key_pool(env)
        got = [runner.next_key(pool) for _ in range(4)]
        self.assertEqual([g[0] for g in got], [0, 1, 2, 0])
        self.assertEqual([g[1]["owner"] for g in got], ["omar", "sara", "K3", "omar"])
        self.assertEqual(len(runner.key_pool({**env, "K2": ""})), 2)         # a listed key with no value is skipped


class KeyHealthTests(unittest.TestCase):
    def setUp(self):
        import runner
        self.r = runner
        self.state = {}
        runner.KEY_STATE_PROVIDER = lambda: self.state
        runner._key_counter = 0
        self.addCleanup(lambda: setattr(runner, "KEY_STATE_PROVIDER", None))

    def test_classification(self):
        c = self.r.classify_key_problem
        self.assertEqual(c("429 RESOURCE_EXHAUSTED: quota exceeded for RequestsPerDay")["status"], "exhausted")
        daily = c("RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel", now=0)
        self.assertGreater(daily["until"], 3600)
        self.assertEqual(c("RateLimitError 429 requests per minute", now=0)["until"], 120)
        self.assertEqual(c("PERMISSION_DENIED: API key not valid")["status"], "dead")
        self.assertEqual(c("status ACCOUNT_STATE_INVALID")["until"], None)
        self.assertIsNone(c("everything fine, 4290 files"))

    def test_rotation_skips_dead_and_resting_then_raises(self):
        import time
        env = {"RELIOPS_KEY_POOL": "K1,K2,K3", "K1": "a", "K2": "b", "K3": "c"}
        pool = self.r.key_pool(env)
        self.state = {"K2": {"status": "dead"}, "K1": {"status": "exhausted", "until": time.time() + 600}}
        self.assertEqual(self.r.next_key(pool)[1]["var"], "K3")
        self.assertEqual(self.r.next_key(pool)[1]["var"], "K3")
        self.state["K3"] = {"status": "dead"}
        with self.assertRaises(self.r.KeyPoolEmpty):
            self.r.next_key(pool)
        self.assertAlmostEqual(self.r.pool_wait_until(pool), self.state["K1"]["until"])
        self.state["K1"] = {"status": "exhausted", "until": time.time() - 1}     # rested
        self.assertEqual(self.r.next_key(pool)[1]["var"], "K1")
        self.state = {"K1": {"status": "dead"}, "K2": {"status": "dead"}, "K3": {"status": "dead"}}
        self.assertIsNone(self.r.pool_wait_until(pool))


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


class SetupGuardTests(unittest.TestCase):
    """Protections against spending a night on a broken setup (found in the first live queue)."""

    def setUp(self):
        import runner
        self.r = runner

    def test_auth_error_marks_key_dead_only_when_going_straight_to_the_provider(self):
        text = "litellm.AuthenticationError: Vertex_ai_betaException - 401 Upstream request failed: Invalid credential"
        self.assertEqual(self.r.classify_key_problem(text)["status"], "dead")
        self.assertIsNone(self.r.classify_key_problem(text, via_custom_base=True))

    def test_preflight_warns_about_a_gateway_base_and_a_gemini_model(self):
        out = self.r.preflight_warnings({"AGENT_API_BASE": "https://opencode.ai/zen/v1"}, "gemini/gemini-3.5-flash-lite", "other")
        self.assertEqual(len(out), 1)
        self.assertIn("opencode.ai", out[0])
        self.assertIn("401", out[0])
        self.assertEqual(self.r.preflight_warnings({}, "gemini/x", "other"), [])

    def test_incomplete_helpers(self):
        self.assertTrue(self.r.run_incomplete(None))
        self.assertTrue(self.r.run_incomplete({"run_status": "incomplete"}))
        self.assertFalse(self.r.run_incomplete({"Diagnosis.success": True}))
        self.assertEqual(self.r.max_incomplete_streak({}), 3)
        self.assertEqual(self.r.max_incomplete_streak({"RELIOPS_MAX_INCOMPLETE": "0"}), 0)
        self.assertEqual(self.r.max_incomplete_streak({"RELIOPS_MAX_INCOMPLETE": "x"}), 3)

    def _run(self, rows, env=None, stream=("line\n",), nodes=None):
        import tempfile
        from pathlib import Path
        from unittest import mock
        r = self.r
        rows = list(rows)
        tmp = Path(tempfile.mkdtemp())
        records = []
        with mock.patch.object(r, "sregym_dir", return_value=tmp), \
             mock.patch.object(r, "merged_env_vars", return_value=env or {}), \
             mock.patch.object(r, "preflight_warnings", return_value=[]), \
             mock.patch.object(r, "cluster_node_problems", return_value=nodes), \
             mock.patch.object(r, "_stream", side_effect=lambda *a, **k: iter(list(stream))), \
             mock.patch.object(r, "_latest_result_row", side_effect=lambda *a, **k: rows.pop(0)), \
             mock.patch.object(r, "_find_transcript", return_value=None), \
             mock.patch.object(r, "_start_recorder", return_value=None), \
             mock.patch.object(r, "_stop_recorder"), \
             mock.patch.object(r, "_git_commit_safe", return_value="abc"), \
             mock.patch.object(r, "_problem_log_path", side_effect=lambda p, rep, n: tmp / f"{p}-{rep}.log"):
            log = "".join(r.stream_campaign(agent="reliops_v1", action_space="typed", model="gemini/x", judge_model="gemini/x",
                                            problems=["p1", "p2", "p3", "p4", "p5"], repeat_n=1, force_build=False,
                                            on_result=records.append))
        return log, records

    def test_queue_stops_after_three_incomplete_runs_in_a_row(self):
        log, records = self._run([{"run_status": "incomplete"}] * 5)
        self.assertEqual(len(records), 3)
        self.assertIn("3 runs in a row ended incomplete", log)

    def test_a_good_run_resets_the_streak(self):
        good = {"Diagnosis.success": True, "Mitigation.success": True}
        bad = {"run_status": "incomplete"}
        log, records = self._run([bad, bad, good, bad, bad])
        self.assertEqual(len(records), 5)
        self.assertNotIn("in a row ended incomplete", log)

    def test_the_stop_can_be_turned_off(self):
        log, records = self._run([{"run_status": "incomplete"}] * 5, env={"RELIOPS_MAX_INCOMPLETE": "0"})
        self.assertEqual(len(records), 5)

    def test_a_campaign_that_stops_itself_leaves_a_halt_reason_and_a_clean_one_clears_it(self):
        self._run([{"run_status": "incomplete"}] * 5)
        self.assertIn("3 runs in a row", self.r.halt_reason())
        good = {"Diagnosis.success": True, "Mitigation.success": True}
        self._run([good] * 5)
        self.assertIsNone(self.r.halt_reason())

    # Lines copied from a real log (a run on 30 Oct 2026 where two kind workers never became Ready).
    CLUSTER_LOG = [
        "[10/30/26 11:38:40] INFO     __main__ - Judge pre-flight check passed\n",
        "Timed out after 120s waiting for nodes to become Ready.\n",
        "Timed out after 120s waiting for kind-worker to become Ready.\n",
        "Timed out after 120s waiting for kind-worker3 to become Ready.\n",
        "start_problem failed (deploy attempt 1/3): Timeout: Not all pods in namespace\n",
        "'kube-system' reached the Ready state within 600 seconds.\n",
        "Not all pods in namespace 'kube-system' reached the Ready state within 600 seconds.\n",
        "Cleanup failed; refusing to retry deployment against uncertain state\n",
        "Benchmark campaign aborted: Benchmark cleanup did not terminate during deployment\n",
    ]

    def test_reason_for_a_cluster_that_never_became_ready(self):
        kind, why = self.r.classify_incomplete("".join(self.CLUSTER_LOG))
        self.assertEqual(kind, "cluster")
        self.assertIn("kind-worker, kind-worker3", why)
        self.assertIn("kube-system", why)
        self.assertIn("cleanup did not finish", why)

    # The 12:47 run on 30 Oct, trimmed to the lines that matter: three failed deploys, four pods stuck in Terminating on two nodes.
    STUCK_PODS_LOG = (
        "[12:47:57] Starting agent now: reliops_v1                            main.py:297\n"
        "Timed out after 120s waiting for nodes to become Ready.\n"
        "Timed out after 120s waiting for kind-worker2 to become Ready.\n"
        "Timed out after 120s waiting for kind-worker3 to become Ready.\n"
        "[13:04:36] start_problem failed for 'storage_user_unregistered-2' (deploy attempt 1/3): Timeout: Not all pods in namespace\n"
        "           'kube-system' reached the Ready state within 600 seconds.\n"
        "           Blocking pods: calico-kube-controllers-79949b87d-5w5lr\n"
        "           (phase=Running; node=kind-worker3;\n"
        "           condition=Ready/ContainersNotReady: containers with unready status: ;\n"
        "           container/calico-kube-controllers=terminated/Error;\n"
        "           deleting=2026-10-30 10:54:10+00:00) |\n"
        "           calico-node-7ntjb (phase=Running;\n"
        "           node=kind-worker3) | coredns-d5ff7974-5prnl (phase=Running; node=kind-worker2;\n"
        "           container/coredns=terminated/Unknown; deleting=2026-10-30\n"
        "           10:53:59+00:00) | coredns-d5ff7974-fgqmx (phase=Running;\n"
        "           node=kind-worker3;\n"
        "           container/coredns=terminated/Unknown; deleting=2026-10-30\n"
        "           10:54:10+00:00) | coredns-d5ff7974-fz7hj (phase=Running;\n"
        "           node=kind-control-plane) | metrics-server-676dc447b9-z76jc (phase=Running;\n"
        "           node=kind-worker2; container/metrics-server=terminated/Unknown;\n"
        "           deleting=2026-10-30 10:53:59+00:00)\n"
        "[13:51:21] start_problem failed for 'storage_user_unregistered-2' (deploy attempt 3/3): Timeout\n"
        "           All 3 deploy attempts failed for 'storage_user_unregistered-2', skipping this attempt\n"
        "Benchmark complete! Results for reliops_v1 written to results/1030_1247/reliops_v1_ALL_results.csv\n"
    )

    def test_reason_for_a_run_whose_app_never_deployed(self):
        kind, why = self.r.classify_incomplete(self.STUCK_PODS_LOG)
        self.assertEqual(kind, "cluster")
        self.assertIn("kind-worker2, kind-worker3 did not become Ready", why)
        self.assertIn("4 kube-system pods stuck in Terminating on kind-worker2, kind-worker3", why)
        self.assertIn("failed to deploy 3 times", why)

    def test_a_line_saying_the_agent_is_starting_does_not_mean_the_app_deployed(self):
        # "Starting agent now" is printed before the problem is even deployed, so it proves nothing about the cluster.
        self.assertEqual(self.r.classify_incomplete(self.STUCK_PODS_LOG)[0], "cluster")

    def test_node_timeouts_alone_do_not_blame_the_cluster_when_the_app_deployed(self):
        log = "Timed out after 120s waiting for kind-worker3 to become Ready.\n[STAGE] diagnosis\nsomething odd\n"
        self.assertEqual(self.r.classify_incomplete(log), ("other", "The run ended without a result (see the log)."))

    def test_a_deploy_failure_with_no_known_cause_is_still_named(self):
        kind, why = self.r.classify_incomplete("start_problem failed for 'p' (deploy attempt 1/1): boom\n")
        self.assertEqual((kind, why), ("other", "The app failed to deploy (see the log)."))

    def test_a_row_without_any_outcome_or_with_a_failed_deploy_counts_as_incomplete(self):
        self.assertTrue(self.r.run_incomplete({"deploy_failed": True}))
        self.assertTrue(self.r.run_incomplete({"deploy_failed": "True"}))
        self.assertTrue(self.r.run_incomplete({}))
        self.assertFalse(self.r.run_incomplete({"Diagnosis.success": False, "Mitigation.success": False}))
        self.assertFalse(self.r.run_incomplete({"Diagnosis.success": True}))   # diagnosis-only problems are complete

    def test_the_queue_stops_after_a_run_whose_row_shows_no_outcome(self):
        log, records = self._run([{"Some.metric": 1}] * 5, stream=self.STUCK_PODS_LOG.splitlines(True))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["incomplete_kind"], "cluster")

    def test_reason_for_a_rejected_key_and_for_an_unknown_ending(self):
        kind, why = self.r.classify_incomplete("litellm.BadRequestError: API key not valid. Please pass a valid API key.")
        self.assertEqual(kind, "key")
        self.assertIn("rejected", why)
        self.assertEqual(self.r.classify_incomplete("some output\n"), ("other", "The run ended without a result (see the log)."))
        self.assertEqual(self.r.classify_incomplete("")[0], "other")

    def test_key_problem_through_a_gateway_is_not_read_as_a_dead_key(self):
        log = "agent_api_base: https://opencode.ai/zen/v1, judge_api_base: unset\nAuthenticationError: Invalid credential\n"
        self.assertEqual(self.r.classify_incomplete(log)[0], "other")

    def test_queue_stops_at_the_first_run_that_ended_on_a_broken_cluster(self):
        log, records = self._run([{"run_status": "incomplete"}] * 5, stream=self.CLUSTER_LOG)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["incomplete_kind"], "cluster")
        self.assertIn("kind-worker3", records[0]["incomplete_reason"])
        self.assertIn("kubectl get nodes", log)
        self.assertNotIn("runs in a row ended incomplete", log)

    def test_a_complete_run_has_no_reason(self):
        good = {"Diagnosis.success": True, "Mitigation.success": True}
        log, records = self._run([good] * 5, stream=self.CLUSTER_LOG)
        self.assertEqual(len(records), 5)
        self.assertTrue(all(r["incomplete_reason"] == "" and r["incomplete_kind"] == "" for r in records))

    def test_nodes_not_ready_before_a_run_give_a_warning_and_the_run_still_goes_ahead(self):
        log, records = self._run([{"Diagnosis.success": True, "Mitigation.success": True}] * 5, nodes=["kind-worker3 (NotReady)"])
        self.assertIn("kind-worker3 (NotReady)", log)
        self.assertEqual(len(records), 5)

    def test_node_check_reads_kubectl_output_and_stays_quiet_when_it_cannot_run(self):
        out = "kind-control-plane   Ready      control-plane   5d   v1.35\nkind-worker3   NotReady   <none>   5d   v1.35\n" \
              "kind-worker2   Ready,SchedulingDisabled   <none>   5d   v1.35\n"
        done = mock.Mock(returncode=0, stdout=out)
        with mock.patch.object(self.r.shutil, "which", return_value="/usr/bin/kubectl"), \
             mock.patch.object(self.r.subprocess, "run", return_value=done):
            self.assertEqual(self.r.cluster_node_problems(), ["kind-worker3 (NotReady)"])
        with mock.patch.object(self.r.shutil, "which", return_value=None):
            self.assertIsNone(self.r.cluster_node_problems())
        with mock.patch.object(self.r.shutil, "which", return_value="/usr/bin/kubectl"), \
             mock.patch.object(self.r.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertIsNone(self.r.cluster_node_problems())


class EnvFileTests(unittest.TestCase):
    """The first live run failed because `KEY="value" # note` put the note inside the API key."""

    def _read(self, text):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp()) / ".env"
        d.write_text(text, encoding="utf-8")
        import runner
        return runner.read_env_file(d)

    def test_inline_comments_quotes_and_export(self):
        o = self._read('GEMINI_API_KEY="AIzaX" # ReliOps -> active\nK2=\'abc\'\nexport K3=plain   # note\nK4=\nURL=https://x/y#frag\n')
        self.assertEqual(o["GEMINI_API_KEY"], "AIzaX")
        self.assertEqual(o["K2"], "abc")
        self.assertEqual(o["K3"], "plain")
        self.assertEqual(o["K4"], "")
        self.assertEqual(o["URL"], "https://x/y#frag")

    def test_comments_after_a_json_value_are_not_part_of_it(self):
        import json
        blob = json.dumps({"type": "service_account", "client_email": "a@b"}, indent=2)
        o = self._read(f"FIREBASE_SERVICE_ACCOUNT_JSON={blob}\n\n# keys below\nGEMINI_API_KEY=AIzaX\n# trailing note\n")
        self.assertEqual(json.loads(o["FIREBASE_SERVICE_ACCOUNT_JSON"])["client_email"], "a@b")
        self.assertEqual(o["GEMINI_API_KEY"], "AIzaX")

    def test_blank_lines_inside_a_multiline_value_are_kept(self):
        o = self._read("PEM=line one\n\nline two\nNEXT=1\n")
        self.assertEqual(o["PEM"], "line one\n\nline two")
        self.assertEqual(o["NEXT"], "1")

    def test_both_google_key_formats_pass_and_a_garbled_value_warns(self):
        import runner
        # An 'AIza' key (39 characters) and a longer 'AQ.' key are both valid and must not warn.
        self.assertEqual(runner.preflight_warnings({"GEMINI_API_KEY": "AIza" + "x" * 35}, "gemini/x", "other"), [])
        self.assertEqual(runner.preflight_warnings({"GEMINI_API_KEY": "AQ.Ab8" + "x" * 47}, "gemini/x", "other"), [])
        # Extra text on the line (a space, a quote, a '#') is never part of a key.
        w = runner.preflight_warnings({"GEMINI_API_KEY": 'AQ.Ab8xxxxxxxx" # note'}, "gemini/x", "other")
        self.assertEqual(len(w), 1)
        self.assertIn("AQ.A", w[0])
        self.assertNotIn("xxxxxxxx", w[0])   # only the first 4 characters and the length are shown


class IncompleteReasonInTheTableTests(TmpRepoCase):
    """Runs recorded before the reason was stored get it read from their own log on this machine."""

    def test_old_incomplete_runs_get_a_reason_from_their_log_and_complete_runs_get_none(self):
        try:
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi not available: {e}")
        logs = self.tmp / "results" / "gui_logs"
        logs.mkdir(parents=True)
        (logs / "a.log").write_text("Timed out after 120s waiting for kind-worker3 to become Ready.\nstart_problem failed for 'p' (deploy attempt 1/3): Timeout\n", encoding="utf-8")
        reason = app_module._incomplete_reason({"row": None, "log_file": "a.log"})
        self.assertIn("kind-worker3", reason)
        self.assertEqual(app_module._incomplete_reason({"row": {"Diagnosis.success": True}, "log_file": "a.log"}), "")
        self.assertEqual(app_module._incomplete_reason({"row": None, "log_file": "../x"}), "")
        self.assertEqual(app_module._incomplete_reason({"row": None, "log_file": "missing.log"}), "")
        self.assertEqual(app_module._incomplete_reason({"row": None, "incomplete_reason": "stored", "log_file": "a.log"}), "stored")


class QueueStopsWhenAStepHaltsTests(TmpRepoCase):
    """A step that stops itself (credentials, cluster) used to end only that step: the queue went on to the
    next one, although the message said the queue stops. The later steps would hit the same fault."""

    def _drive(self, halting_steps):
        try:
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi not available: {e}")
        from unittest import mock
        import runner
        started = []
        step = lambda n: {"name": f"step {n}", "enabled": True, "agent_id": "a", "model_id": "m", "judge_model_id": "j",
                          "problems": ["p"], "repeat_n": 1, "run_class": "exploration"}
        queue = {"name": "q", "steps": [step(1), step(2), step(3)]}

        def fake_run_lines(**kw):
            n = len(started) + 1
            started.append(n)
            if n in halting_steps:
                runner._halt("3 runs in a row ended incomplete")
            yield f"run for step {n}\n"

        with mock.patch.object(app_module, "_run_lines", side_effect=fake_run_lines), \
             mock.patch.object(app_module.store, "read", return_value=[]), \
             mock.patch.object(app_module, "_collect_runs", return_value=("local", [])), \
             mock.patch.object(app_module.queueplan, "todo", return_value=[("p", 1)]):
            runner._halt(None)
            log = "".join(app_module._queue_lines(queue, "me", "exp"))
        return log, started

    def test_a_halting_step_stops_the_whole_queue(self):
        log, started = self._drive({1})
        self.assertEqual(started, [1])
        self.assertIn("stopped after step 1/3", log)
        self.assertIn("2 later step(s) were not started", log)
        self.assertNotIn("[QUEUE] finished", log)

    def test_a_queue_without_a_halt_runs_every_step(self):
        log, started = self._drive(set())
        self.assertEqual(started, [1, 2, 3])
        self.assertIn("[QUEUE] finished", log)


class JudgeScoresReachThePageTests(TmpRepoCase):
    """The detail panel read D1-D3 from the run list, which never carried them ("D1=? D2=? D3=?")."""

    DIMENSIONS = ("{'D1': {'name': 'Fault Localization', 'score': 1.0}, "
                  "'D2': {'name': 'Fault Characterization', 'score': 0.5}, "
                  "'D3': {'name': 'Scope Precision', 'score': 1.0}}")  # the shape in a real SREGym CSV cell

    def _runs(self, records):
        try:
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi not available: {e}")
        from unittest import mock
        with mock.patch.object(app_module.firebase_sync, "list_runs", return_value=records):
            return app_module._collect_runs(50)[1]

    def test_scores_are_sent_whether_stored_with_the_run_or_recovered_from_its_row(self):
        row = {"Diagnosis.dimensions": self.DIMENSIONS, "Diagnosis.composite_score": "0.83"}
        runs = self._runs([
            {"run_id": "a", "problem_id": "p", "row": row},  # older record: only the raw row
            {"run_id": "b", "problem_id": "p", "row": row, "diagnosis_d1": 0.0, "diagnosis_composite": 0.1},
        ])
        a, b = {r["run_id"]: r for r in runs}["a"], {r["run_id"]: r for r in runs}["b"]
        self.assertEqual((a["diagnosis_d1"], a["diagnosis_d2"], a["diagnosis_d3"], a["diagnosis_composite"]),
                         (1.0, 0.5, 1.0, 0.83))
        # A stored value wins over the row, and a stored 0.0 is a real score, not "missing".
        self.assertEqual((b["diagnosis_d1"], b["diagnosis_d2"], b["diagnosis_composite"]), (0.0, 0.5, 0.1))

    def test_a_run_without_a_row_has_no_scores_rather_than_made_up_ones(self):
        runs = self._runs([{"run_id": "c", "problem_id": "p", "row": None}])
        self.assertIsNone(runs[0]["diagnosis_d1"])


class KeyMarkFollowsTheValueTests(TmpRepoCase):
    def test_a_dead_mark_clears_when_the_key_value_changes(self):
        import json
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.app_module = app_module
        (self.tmp / "results").mkdir(exist_ok=True)
        env = self.tmp / "lab.env"
        import runner
        with mock.patch.object(runner, "lab_env_path", return_value=env):
            env.write_text("GEMINI_API_KEY=AIza" + "a" * 35 + "\n")
            c = TestClient(self.app_module.app)
            self.app_module._record_key_event("GEMINI_API_KEY", "you", "dead", None, "AuthenticationError")
            k = c.get("/api/keys").json()["keys"][0]
            self.assertEqual(k["status"], "dead")
            self.assertEqual(k["source"], "Lab .env")
            self.assertTrue(k["shape"].startswith("AIza"))
            self.assertNotIn("a" * 10, json.dumps(k))
            env.write_text("GEMINI_API_KEY=AIza" + "b" * 35 + "\n")     # the person replaced the key
            k = c.get("/api/keys").json()["keys"][0]
            self.assertEqual(k["status"], "ok")
            self.assertTrue(runner.key_available("GEMINI_API_KEY"))


class RotationFromTheScreenTests(TmpRepoCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        import runner
        self.runner = runner
        self.app_module = app_module
        (self.tmp / "results").mkdir(exist_ok=True)
        self.env = self.tmp / "lab.env"
        self.env.write_text('# my keys\nGEMINI_API_KEY="AIza' + "a" * 35 + '" # main\nGEMINI_API_KEY_2="AIza' + "b" * 35
                            + '"\nGEMINI_API_KEY_3=AIza' + "c" * 35 + "\nOTHER=1\n")
        p = mock.patch.object(runner, "lab_env_path", return_value=self.env)
        p.start()
        self.addCleanup(p.stop)
        self.c = TestClient(app_module.app)

    def test_write_pool_changes_only_its_two_lines_and_turns_off(self):
        r = self.runner
        r.write_pool(["GEMINI_API_KEY", "GEMINI_API_KEY_2"], {"GEMINI_API_KEY": "omar", "GEMINI_API_KEY_2": "sara, x"})
        text = self.env.read_text()
        self.assertIn("RELIOPS_KEY_POOL=GEMINI_API_KEY,GEMINI_API_KEY_2\n", text)
        self.assertIn("RELIOPS_KEY_OWNERS=omar,sara  x\n", text)       # a comma in a name cannot split the list
        self.assertIn('# my keys', text)
        self.assertIn("OTHER=1", text)
        pool = r.key_pool(r.read_env_file(self.env))
        self.assertEqual([p["owner"] for p in pool], ["omar", "sara  x"])
        self.assertEqual(r.pool_target(pool), "GEMINI_API_KEY")
        r.write_pool(["GEMINI_API_KEY_2"], {})                         # one key: rotation off
        self.assertNotIn("RELIOPS_KEY_POOL", self.env.read_text())
        self.assertIn("OTHER=1", self.env.read_text())

    def test_screen_flow_save_dead_alert_remove(self):
        c = self.c
        keys = {k["var"]: k for k in c.get("/api/keys").json()["keys"]}
        self.assertEqual(sorted(keys), ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"])
        self.assertTrue(all(not k["in_pool"] for k in keys.values()))
        self.assertEqual(c.post("/api/keys/pool", json={"vars": ["NOPE"]}).status_code, 400)
        r = c.post("/api/keys/pool", json={"vars": ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"],
                                           "owners": {"GEMINI_API_KEY_2": "sara"}}).json()
        self.assertTrue(r["rotation"])
        k = c.get("/api/keys").json()
        self.assertTrue(k["pool"])
        self.assertEqual(next(x for x in k["keys"] if x["var"] == "GEMINI_API_KEY_2")["owner"], "sara")
        self.assertEqual(c.get("/api/state").json()["key_alerts"], [])
        self.app_module._record_key_event("GEMINI_API_KEY_2", "sara", "dead", None, "API key not valid")
        alerts = c.get("/api/state").json()["key_alerts"]
        self.assertEqual([(a["var"], a["owner"], a["status"], a["in_pool"]) for a in alerts], [("GEMINI_API_KEY_2", "sara", "dead", True)])
        out = c.post("/api/keys/pool/remove", json={"var": "GEMINI_API_KEY_2"}).json()
        self.assertEqual(out["left"], 2)
        self.assertEqual(c.get("/api/state").json()["key_alerts"], [])     # no longer in use
        self.assertIn("GEMINI_API_KEY,GEMINI_API_KEY_3", self.env.read_text())

    def test_a_single_dead_key_raises_an_alert_without_a_remove_button_case(self):
        self.app_module._record_key_event("GEMINI_API_KEY", "you", "dead", None, "AuthenticationError")
        a = self.c.get("/api/state").json()["key_alerts"]
        self.assertEqual(len(a), 1)
        self.assertFalse(a[0]["in_pool"])
