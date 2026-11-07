"""End to end: a queue run through the real API and the real runner.stream_campaign, with only the SREGym subprocess,
the cluster and Firestore replaced. Checks what a teammate's machine would record and what the other machines would see.

Run: python3 -I backend/test_e2e_run.py  (needs fastapi + httpx)"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import firebase_sync  # noqa: E402
import jobs  # noqa: E402
import logmirror  # noqa: E402
import runner  # noqa: E402
from test_v2 import TmpRepoCase  # noqa: E402

VERSIONS = "kind 0.30.0; docker 27.1.1; kubectl 1.35.0"
LOG_TEXT = "agent started\nOPENAI_API_KEY=sk-testtesttesttesttesttest\nnode kind-worker Ready\n"


class EndToEndRunTests(TmpRepoCase):
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
        (self.tmp / "sregym").mkdir()
        jobs._current = None
        runner._stop_event.clear()
        self.shared_logs, self.shared_runs = {}, []
        patches = [
            mock.patch.object(runner, "new_log_file", side_effect=lambda kind: self.tmp / "results" / f"{kind}.log"),
            mock.patch.object(runner, "sregym_dir", return_value=self.tmp / "sregym"),
            mock.patch.object(runner, "merged_env_vars", return_value={}),
            mock.patch.object(runner, "preflight_warnings", return_value=[]),
            mock.patch.object(runner, "agent_setup_problem", return_value=None),
            mock.patch.object(runner, "agent_switch_problem", return_value=None),
            mock.patch.object(runner, "cluster_node_problems", return_value=[]),
            mock.patch.object(runner, "machine_versions", return_value=VERSIONS),
            mock.patch.object(runner, "_git_commit_safe", return_value="abc1234def"),
            mock.patch.object(runner, "_start_recorder", return_value=None),
            mock.patch.object(runner, "_find_transcript", return_value=None),
            mock.patch.object(runner, "_stream", side_effect=self._fake_harness),
            mock.patch.object(runner, "_latest_result_row", side_effect=self._fake_row),
            mock.patch.object(firebase_sync, "write_run", side_effect=self._share_run),
            mock.patch.object(firebase_sync, "write_doc", side_effect=self._share_doc),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # --- the replaced parts -------------------------------------------------------------------------------------
    def _fake_harness(self, cmd, **kw):
        yield LOG_TEXT
        yield f"ran {cmd[cmd.index('--problem') + 1]}\n"

    def _fake_row(self, problem, after_ts):
        ok = "False" if problem == "p3" else "True"
        return {"Diagnosis.success": ok, "Mitigation.success": ok, "Diagnosis.submission": f"diagnosis for {problem}"}

    def _share_run(self, record):
        self.shared_runs.append(record)
        return "written"

    def _share_doc(self, collection, doc_id, data):
        if collection == "run_logs":   # the real write_log ran: redacted, gzipped, base64
            self.shared_logs[doc_id] = logmirror.unpack(data)
        return "written"

    # --- the test -----------------------------------------------------------------------------------------------
    def _step(self, label):
        return {"agent_id": "reliops_v1", "agent_label": "ReliOps v1 (typed)", "action_space": "typed", "model_id": "m",
                "judge_model_id": "j", "config_label": label, "problems": ["p1", "p2", "p3"], "repeat_n": 1}

    def test_queue_run_records_everything_the_other_machines_need(self):
        c = self.client
        q = c.post("/api/queue", json={"name": "E2E", "machines": 2, "steps": [self._step("A"), self._step("B")]}).json()["queue"]
        qid = q["queue_id"]
        with mock.patch.object(runner.time, "sleep"):
            r = c.get("/api/queue/run", params={"queue_id": qid, "experiment": "E2E try", "share": 1, "run_by": "Omar"})
        self.assertIn("[[DONE]]", r.text)

        lines = (self.tmp / "results" / "gui_runs.jsonl").read_text().splitlines()
        recs = [json.loads(x) for x in lines]
        # share 1 of 2 = p1 and p3, two steps => four runs; p2 belongs to the other machine
        self.assertEqual(sorted((x["problem_id"], x["config_label"]) for x in recs),
                         [("p1", "A"), ("p1", "B"), ("p3", "A"), ("p3", "B")])
        for x in recs:
            self.assertEqual(x["origin"], "queue")
            self.assertEqual(x["machine_versions"], VERSIONS)
            self.assertEqual(x["experiment"], "E2E try")
            self.assertEqual(x["run_by"], "Omar")
            self.assertEqual(x["git_commit"], "abc1234def")
            self.assertTrue(x["log_shared"], "the run log was not shared")
            self.assertIn(firebase_sync._log_doc_id(x["log_file"]), self.shared_logs)
        self.assertEqual(len(self.shared_runs), 4)

        # the shared copy of a log has the secret masked and the rest intact
        shared = next(iter(self.shared_logs.values()))
        self.assertNotIn("sk-testtesttesttest", shared)
        self.assertIn("node kind-worker Ready", shared)

        # the dashboard row carries the fields (machine_versions was dropped by the row builder once)
        flat = c.get("/api/results").json()["runs"]
        self.assertEqual(len(flat), 4)
        self.assertTrue(all(f["machine_versions"] == VERSIONS and f["origin"] == "queue" for f in flat))
        failed = [f for f in flat if f["problem_id"] == "p3"]
        self.assertTrue(all(f["e2e_pass"] is False for f in failed))

        # the other machine's preview: share 1 is finished, share 2 has not started
        pv = c.get("/api/queue/preview", params={"queue_id": qid, "experiment": "E2E try", "share": 2}).json()
        shares = {s["share"]: s for s in pv["shares"]}
        self.assertEqual((shares[1]["done"], shares[1]["left"]), (4, 0))
        self.assertEqual((shares[2]["done"], shares[2]["left"]), (0, 2))
        self.assertEqual([x["name"] for x in shares[1]["people"]], ["Omar"])
        self.assertEqual(pv["machine_versions"][0]["versions"], VERSIONS)

        # a second press on Run does nothing for share 1: only missing cells run
        with mock.patch.object(runner.time, "sleep"):
            c.get("/api/queue/run", params={"queue_id": qid, "experiment": "E2E try", "share": 1, "run_by": "Omar"})
        self.assertEqual(len((self.tmp / "results" / "gui_runs.jsonl").read_text().splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
