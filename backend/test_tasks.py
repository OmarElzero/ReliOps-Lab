"""Run: python3 -I backend/test_tasks.py  (from the repo root; the API tests need fastapi + httpx)"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import jobs  # noqa: E402
import runner  # noqa: E402
import tasks  # noqa: E402
from test_v2 import TmpRepoCase  # noqa: E402


class TaskLogicTests(unittest.TestCase):
    def test_clean_task_and_audience(self):
        t = tasks.clean_task({"title": "  Run STRATUS   on dev-12 ", "assignees": ["Noura", "noura", " Ahmed "], "phase": "Round 2"})
        self.assertEqual(t["title"], "Run STRATUS on dev-12")
        self.assertEqual(t["assignees"], ["Noura", "Ahmed"])
        with self.assertRaises(ValueError):
            tasks.clean_task({"title": "  "})
        self.assertTrue(tasks.is_for(t, "ahmed"))
        self.assertFalse(tasks.is_for(t, "Omar"))
        self.assertTrue(tasks.is_for({"assignees": []}, "anyone"))          # no names = everyone

    def test_progress_only_for_assignees_and_open_count(self):
        a = tasks.clean_task({"title": "A", "assignees": ["Noura"]}, task_id="a")
        b = tasks.clean_task({"title": "B"}, task_id="b")                    # everyone
        with self.assertRaises(ValueError):
            tasks.clean_progress(a, "Omar", "done")
        with self.assertRaises(ValueError):
            tasks.clean_progress(a, "", "done")
        with self.assertRaises(ValueError):
            tasks.clean_progress(a, "Noura", "finished")
        prog = [tasks.clean_progress(a, "Noura", "done"), tasks.clean_progress(b, "Noura", "doing")]
        self.assertEqual(tasks.open_count([a, b], prog, "Noura"), 1)          # A done, B still open
        self.assertEqual(tasks.open_count([a, b], prog, "Omar"), 1)           # only B is for Omar, and he has not started it


class TaskFlagsLogicTests(unittest.TestCase):
    def test_urgent_and_state_defaults_and_validation(self):
        t = tasks.clean_task({"title": "A"})
        self.assertEqual((t["urgent"], t["state"]), (False, "active"))                 # tasks written before the fields existed
        t = tasks.clean_task({"title": "A", "urgent": True, "state": "on_hold"})
        self.assertEqual((t["urgent"], t["state"]), (True, "on_hold"))
        with self.assertRaises(ValueError):
            tasks.clean_task({"title": "A", "state": "someday"})

    def test_tasks_not_needed_or_on_hold_are_not_open(self):
        live = tasks.clean_task({"title": "L"}, task_id="l")
        hold = tasks.clean_task({"title": "H", "state": "on_hold"}, task_id="h")
        gone = tasks.clean_task({"title": "G", "state": "not_needed"}, task_id="g")
        old = {"task_id": "o", "title": "no fields at all", "assignees": []}         # a record from before this change
        self.assertEqual(tasks.open_count([live, hold, gone, old], [], "Omar"), 2)    # live + old
        self.assertTrue(tasks.is_live(old))
        self.assertFalse(tasks.is_live(gone))


class TaskApiTests(TmpRepoCase):
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
        jobs._current = None
        p = mock.patch("time.sleep")     # the wrong-password delay
        p.start()
        self.addCleanup(p.stop)

    def env(self, pw="boss"):
        env = {k: v for k, v in os.environ.items() if k != "LAB_OWNER_PASSWORD"}
        if pw:
            env["LAB_OWNER_PASSWORD"] = pw
        return mock.patch.dict(os.environ, env, clear=True)

    def test_only_the_owner_can_add_edit_delete(self):
        c = self.client
        with self.env(pw=""):
            self.assertTrue(c.get("/api/tasks/lock").json()["locked_off"])
            r = c.post("/api/tasks", json={"title": "x"})
            self.assertEqual(r.status_code, 403)
            self.assertIn("LAB_OWNER_PASSWORD", r.json()["error"])
        with self.env():
            self.assertFalse(c.get("/api/tasks/lock").json()["locked_off"])
            self.assertEqual(c.post("/api/tasks", json={"title": "x"}).status_code, 403)                                   # no password
            self.assertEqual(c.post("/api/tasks", json={"title": "x"}, headers={"x-owner-password": "nope"}).status_code, 403)
            self.assertEqual(c.post("/api/tasks/unlock", headers={"x-owner-password": "nope"}).status_code, 403)
            self.assertEqual(c.post("/api/tasks/unlock", headers={"x-owner-password": "boss"}).status_code, 200)
            ok = {"x-owner-password": "boss"}
            self.assertEqual(c.post("/api/tasks", json={"title": " "}, headers=ok).status_code, 400)
            r = c.post("/api/tasks", json={"title": "Run STRATUS on dev-12", "phase": "Round 2", "assignees": ["Noura"],
                                           "queue_id": "q1", "experiment": "Dev - STRATUS", "run_by": "Omar"}, headers=ok)
            self.assertEqual(r.status_code, 200, r.text)
            tid = r.json()["task"]["task_id"]
            got = c.get("/api/tasks").json()
            self.assertEqual([t["title"] for t in got["tasks"]], ["Run STRATUS on dev-12"])
            self.assertEqual(got["tasks"][0]["created_by"], "Omar")
            # editing keeps the id and who created it; a teammate without the password cannot
            self.assertEqual(c.put(f"/api/tasks/{tid}", json={"title": "Renamed"}).status_code, 403)
            r = c.put(f"/api/tasks/{tid}", json={"title": "Renamed", "assignees": ["Noura", "Ahmed"]}, headers=ok)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(c.get("/api/tasks").json()["tasks"][0]["assignees"], ["Noura", "Ahmed"])
            self.assertEqual(c.delete(f"/api/tasks/{tid}").status_code, 403)
            self.assertEqual(c.delete(f"/api/tasks/{tid}", headers=ok).status_code, 200)
            self.assertEqual(c.get("/api/tasks").json()["tasks"], [])
            self.assertEqual(c.put("/api/tasks/zzz", json={"title": "x"}, headers=ok).status_code, 404)

    def test_only_the_owner_sets_urgent_and_state_and_edits_keep_them(self):
        c = self.client
        ok = {"x-owner-password": "boss"}
        with self.env():
            tid = c.post("/api/tasks", json={"title": "T", "phase": "P"}, headers=ok).json()["task"]["task_id"]
            other = c.post("/api/tasks", json={"title": "U", "phase": "P"}, headers=ok).json()["task"]["task_id"]
            url = f"/api/tasks/{tid}/flags"
            self.assertEqual(c.post(url, json={"urgent": True}).status_code, 403)                                  # no password
            self.assertEqual(c.post("/api/tasks/zzz/flags", json={"urgent": True}, headers=ok).status_code, 404)
            self.assertEqual(c.post(url, json={"state": "someday"}, headers=ok).status_code, 400)
            self.assertEqual(c.post(url, json={"urgent": True}, headers=ok).status_code, 200)
            self.assertEqual(c.post(url, json={"state": "on_hold"}, headers=ok).status_code, 200)                  # urgent stays
            t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["task_id"] == tid)
            self.assertEqual((t["urgent"], t["state"], t["title"], t["phase"]), (True, "on_hold", "T", "P"))
            # an edit that does not mention the fields keeps them; one that does changes them
            self.assertEqual(c.put(f"/api/tasks/{tid}", json={"title": "T2", "phase": "P"}, headers=ok).status_code, 200)
            t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["task_id"] == tid)
            self.assertEqual((t["urgent"], t["state"], t["title"]), (True, "on_hold", "T2"))
            self.assertEqual(c.put(f"/api/tasks/{tid}", json={"title": "T2", "urgent": False, "state": "not_needed"}, headers=ok).status_code, 200)
            t = next(x for x in c.get("/api/tasks").json()["tasks"] if x["task_id"] == tid)
            self.assertEqual((t["urgent"], t["state"]), (False, "not_needed"))
            # an urgent task is listed before the others in its phase
            self.assertEqual(c.post(f"/api/tasks/{other}/flags", json={"urgent": True}, headers=ok).status_code, 200)
            order = [x["task_id"] for x in c.get("/api/tasks").json()["tasks"]]
            self.assertEqual(order[0], other)

    def test_the_page_serves_the_tab_and_its_script(self):
        c = self.client
        self.assertEqual(c.get("/tasks.js").status_code, 200)
        html = c.get("/").text
        for needle in ('id="tab-tasks"', 'data-tab="tasks"', 'id="tasks-badge"', 'src="/tasks.js"', 'id="tk-root"'):
            self.assertIn(needle, html)

    def test_each_person_marks_their_own_progress(self):
        c = self.client
        ok = {"x-owner-password": "boss"}
        with self.env():
            tid = c.post("/api/tasks", json={"title": "T", "assignees": ["Noura", "Ahmed"]}, headers=ok).json()["task"]["task_id"]
        url = f"/api/tasks/{tid}/progress"
        self.assertEqual(c.post(url, json={"who": "Omar", "status": "done"}).status_code, 400)       # not his task
        self.assertEqual(c.post(url, json={"who": "", "status": "done"}).status_code, 400)
        self.assertEqual(c.post(url, json={"who": "Noura", "status": "nonsense"}).status_code, 400)
        self.assertEqual(c.post("/api/tasks/zzz/progress", json={"who": "Noura", "status": "done"}).status_code, 404)
        self.assertEqual(c.post(url, json={"who": "Noura", "status": "doing", "note": "started"}).status_code, 200)
        self.assertEqual(c.post(url, json={"who": "noura", "status": "done", "note": "all 36 runs"}).status_code, 200)   # same person, later
        self.assertEqual(c.post(url, json={"who": "Ahmed", "status": "blocked", "note": "Docker"}).status_code, 200)
        prog = {p["who"]: p for p in c.get("/api/tasks").json()["progress"]}
        self.assertEqual(len(prog), 2)                                                              # Noura counted once
        self.assertEqual((prog["noura"]["status"], prog["noura"]["note"]), ("done", "all 36 runs"))
        self.assertEqual(prog["Ahmed"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
