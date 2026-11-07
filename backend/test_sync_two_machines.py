"""Two machines, one shared Firestore (a fake one): what Omar's Lab writes, a teammate's Lab reads, and the other way round.
Each machine has its own results/ folder; they only meet through the shared store. Real Firebase is checked by hand
with the checklist in docs/GUIDE.md ("Check the sync between two machines").

Run: python3 -I backend/test_sync_two_machines.py  (needs fastapi + httpx)"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import firebase_sync  # noqa: E402
import jobs  # noqa: E402
import runner  # noqa: E402

BOSS = {"X-Owner-Password": "boss"}


class TwoMachineSyncTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.client = TestClient(app_module.app)
        self.cloud: dict[tuple[str, str], dict] = {}
        self.here = {"dir": Path(tempfile.mkdtemp())}
        self.other = Path(tempfile.mkdtemp())
        for d in (self.here["dir"], self.other):
            (d / "results").mkdir()
        jobs._current = None
        env = {k: v for k, v in os.environ.items() if k != "LAB_OWNER_PASSWORD"}
        env["LAB_OWNER_PASSWORD"] = "boss"
        for p in (mock.patch.object(runner, "reliops_dir", side_effect=lambda: self.here["dir"]),
                  mock.patch.object(runner, "merged_env_vars", return_value={}),
                  mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(firebase_sync, "write_doc", side_effect=self._write),
                  mock.patch.object(firebase_sync, "list_docs", side_effect=self._list),
                  mock.patch.object(firebase_sync, "list_runs_ex", return_value={"runs": None, "live": False, "error": None, "synced_at": None}),
                  mock.patch.object(firebase_sync, "read_story", return_value=None),
                  mock.patch.object(firebase_sync, "write_story", return_value="not written: test")):
            p.start()
            self.addCleanup(p.stop)

    def _write(self, collection, doc_id, doc):
        self.cloud[(collection, doc_id)] = dict(doc)
        return "written"

    def _list(self, collection):
        return [dict(v) for (c, _), v in self.cloud.items() if c == collection]

    def on_machine(self, which):
        """Switch this process to the other machine's folder (its own local files), the cloud stays the same."""
        if which == "A":
            self.here["dir"] = self._a
        else:
            self.here["dir"] = self._b

    def test_queue_assignments_moves_tasks_and_progress_cross_machines(self):
        self._a, self._b = self.here["dir"], self.other
        c = self.client
        probs = [f"p{i}" for i in range(1, 7)]
        step = {"agent_id": "reliops_v1", "agent_label": "v1", "action_space": "typed", "model_id": "m", "judge_model_id": "j",
                "config_label": "A", "problems": probs, "repeat_n": 1}

        # --- machine A (Omar): queue, who runs what, a move, a task
        self.on_machine("A")
        q = c.post("/api/queue", json={"name": "Round 2", "machines": 3, "steps": [step]}).json()["queue"]
        qid = q["queue_id"]
        self.assertEqual(c.post(f"/api/queue/{qid}/assign", json={"share": 2, "names": ["Noura"], "run_by": "Omar"}).status_code, 200)
        moved = c.post(f"/api/queue/{qid}/reassign", json={"experiment": "R2 try", "moves": [{"unit": "p1", "to_share": 3}], "run_by": "Omar"})
        self.assertEqual(moved.status_code, 200, moved.text)
        task = c.post("/api/tasks", headers=BOSS, json={"title": "Run STRATUS on dev-12", "assignees": ["Noura"], "phase": "Round 2",
                                                        "queue_id": qid, "experiment": "R2 try", "run_by": "Omar"})
        self.assertEqual(task.status_code, 200, task.text)
        tid = task.json()["task"]["task_id"]

        # --- machine B (Noura): never touched A's files, still sees everything through the shared store
        self.on_machine("B")
        queues = c.get("/api/queues").json()["queues"]
        self.assertEqual([x["queue_id"] for x in queues], [qid])
        pv = c.get("/api/queue/preview", params={"queue_id": qid, "experiment": "R2 try", "share": 2}).json()
        lists = {s["share"]: s["problems"] for s in pv["shares"]}
        self.assertIn("p1", lists[3])                      # Omar's move reached machine B
        self.assertNotIn("p1", lists[1])
        self.assertEqual(next(s for s in pv["shares"] if s["share"] == 2)["assigned"], ["Noura"])   # who the share is planned for
        tasks = c.get("/api/tasks").json()
        self.assertEqual([t["task_id"] for t in tasks["tasks"]], [tid])
        # Noura marks her task; she cannot add tasks (no password)
        self.assertEqual(c.post(f"/api/tasks/{tid}/progress", json={"who": "Noura", "status": "doing", "note": "started"}).status_code, 200)
        self.assertIn(c.post("/api/tasks", json={"title": "x"}).status_code, (403,))

        # --- back on machine A: her progress is there
        self.on_machine("A")
        prog = c.get("/api/tasks").json()["progress"]
        self.assertEqual([(p["who"], p["status"], p["note"]) for p in prog], [("Noura", "doing", "started")])

        # --- the cloud holds one document per key: a second edit replaces, never duplicates
        c.put(f"/api/tasks/{tid}", headers=BOSS, json={"title": "Run STRATUS on dev-12 (all 3 repeats)", "assignees": ["Noura"], "run_by": "Omar"})
        self.assertEqual(len([k for k in self.cloud if k[0] == "tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
