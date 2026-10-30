"""Run: python3 -I backend/test_queue.py  (from the repo root; the API tests need fastapi + httpx)"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import jobs  # noqa: E402
import queueplan  # noqa: E402
import runner  # noqa: E402
from test_v2 import TmpRepoCase, run  # noqa: E402


def step(**kw):
    base = {"agent_id": "reliops_v1", "agent_label": "ReliOps v1 (typed)", "action_space": "typed", "model_id": "m",
            "judge_model_id": "j", "config_label": "base", "problems": ["p1", "p2"], "repeat_n": 2}
    base.update(kw)
    return base


class PlanTests(unittest.TestCase):
    def test_step_validation(self):
        self.assertEqual(queueplan.clean_step(step())["repeat_n"], 2)
        for bad, msg in ((dict(agent_id=""), "agent"), (dict(model_id=""), "model"), (dict(judge_model_id=""), "judge"),
                         (dict(problems=[]), "problem"), (dict(run_class="frozen"), "frozen"),
                         (dict(run_class="weird"), "run class")):
            with self.assertRaises(ValueError) as cm:
                queueplan.clean_step(step(**bad))
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(queueplan.clean_step(step(purpose="dev"))["purpose"], "dev")
        self.assertIsNone(queueplan.clean_step(step())["purpose"])
        for bad in ("final", "weird"):
            with self.assertRaises(ValueError):
                queueplan.clean_step(step(purpose=bad))
        s = queueplan.clean_step(step(problems=["a", " a ", "b"], repeat_n="0"))
        self.assertEqual(s["problems"], ["a", "b"])
        self.assertEqual(s["repeat_n"], 1)

    def test_queue_needs_a_name_and_names_the_bad_step(self):
        with self.assertRaises(ValueError):
            queueplan.clean_queue({"name": " ", "steps": []})
        with self.assertRaises(ValueError) as cm:
            queueplan.clean_queue({"name": "q", "steps": [step(), step(model_id="")]})
        self.assertIn("step 2", str(cm.exception))

    def test_cells_only_count_matching_runs_and_todo_resumes(self):
        s = queueplan.clean_step(step())
        runs = [run(1, "p1", 1, True), run(2, "p1", 2, False), run(3, "p2", 1, None),
                run(4, "p2", 2, True, label="other"),          # different label: not this step's
                run(5, "p2", 2, True, judge="another judge")]  # different judge: not this step's
        by = {(c["problem"], c["repeat"]): c["status"] for c in queueplan.cells(s, runs)}
        self.assertEqual(by, {("p1", 1): "pass", ("p1", 2): "fail", ("p2", 1): "incomplete", ("p2", 2): "missing"})
        self.assertEqual(sorted(queueplan.todo(s, runs)), [("p2", 1), ("p2", 2)])  # incomplete is re-run, never a failure
        again = queueplan.clean_step(step(only_missing=False))
        self.assertEqual(len(queueplan.todo(again, runs)), 4)

    def test_preview_totals_skip_switched_off_steps(self):
        q = queueplan.clean_queue({"name": "q", "steps": [step(), step(enabled=False, config_label="x")]})
        p = queueplan.preview(q, [run(1, "p1", 1, True)], [])
        self.assertEqual((p["total_runs"], p["runs_left"]), (4, 3))
        self.assertEqual(p["steps"][1]["todo"], 0)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp_patch = mock.patch.object(runner, "new_log_file")
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        self.tmp_patch.start().side_effect = lambda kind: d / f"{kind}.log"
        self.addCleanup(self.tmp_patch.stop)
        runner._stop_event.clear()
        jobs._current = None

    def wait(self, job, state="done", t=5):
        end = time.time() + t
        while job.running and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(job.state, state)

    def test_runs_in_background_and_tracks_progress(self):
        def gen():
            yield "##### [QUEUE] step 1/2 start: typed | runs 3 to do #####\n"
            yield "\n===== [1/3] problem=p1 repeat=1/3 agent=a action_space=typed =====\n"
            yield "----- recorded result for p1: parsed row found (repeat 1/3) -----\n"
        job = jobs.start("queue", "q", gen)
        self.wait(job)
        self.assertEqual(job.progress["step"], 1)
        self.assertEqual(job.progress["problem"], "p1")
        self.assertEqual(job.progress["recorded"], 1)
        lines, nxt, finished = job.read(0, wait=0.1)
        self.assertTrue(finished)
        self.assertIn("recorded result for p1", "".join(lines))
        tail, _, _ = job.read(-1, wait=0.1)
        self.assertEqual(len(tail), 1)

    def test_only_one_job_at_a_time_and_a_late_reader_gets_everything(self):
        gate = threading.Event()

        def slow():
            yield "first\n"
            gate.wait(5)
            yield "second\n"
        job = jobs.start("run", "a", slow)
        self.assertTrue(jobs.busy())
        self.assertIsNone(jobs.start("run", "b", lambda: iter(["x\n"])))
        gate.set()
        self.wait(job)
        self.assertFalse(jobs.busy())
        self.assertIn("second", "".join(job.read(0, wait=0.1)[0]))

    def test_crash_is_reported_and_stop_is_recorded(self):
        def boom():
            yield "ok\n"
            raise RuntimeError("boom")
        job = jobs.start("run", "a", boom)
        self.wait(job, "error")
        self.assertIn("boom", "".join(job.read(0, wait=0.1)[0]))

        def stopping():
            runner._stop_event.set()
            yield "x\n"
        job2 = jobs.start("run", "b", stopping)
        self.wait(job2, "stopped")


class QueueApiTests(TmpRepoCase):
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
        runner._stop_event.clear()
        p = mock.patch.object(runner, "new_log_file", side_effect=lambda kind: self.tmp / "results" / f"{kind}.log")
        p.start()
        self.addCleanup(p.stop)

    def save(self, steps, name="Phase B"):
        r = self.client.post("/api/queue", json={"name": name, "steps": steps})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["queue"]

    def test_save_list_validate_delete(self):
        c = self.client
        self.assertEqual(c.post("/api/queue", json={"name": "q", "steps": [step(model_id="")]}).status_code, 400)
        q = self.save([step()])
        self.assertEqual(len(c.get("/api/queues").json()["queues"]), 1)
        q["name"] = "renamed"
        self.assertEqual(c.post("/api/queue", json=q).status_code, 200)
        listed = c.get("/api/queues").json()["queues"]
        self.assertEqual([x["name"] for x in listed], ["renamed"])
        pv = c.get("/api/queue/preview", params={"queue_id": q["queue_id"]}).json()
        self.assertEqual((pv["total_runs"], pv["runs_left"]), (4, 4))
        self.assertEqual(c.delete(f"/api/queue/{q['queue_id']}").status_code, 200)
        self.assertEqual(c.get("/api/queues").json()["queues"], [])
        self.assertEqual(c.get("/api/queue/preview", params={"queue_id": "nope"}).status_code, 404)

    def test_saved_problem_set_shows_up_with_csv_details(self):
        c = self.client
        (self.tmp / "experiments").mkdir()
        (self.tmp / "experiments" / "first-30.csv").write_text(
            "n,problem_id,type,expected_difficulty,app\n1,p1,Misconfiguration,easy,hotel\n")
        self.assertEqual(c.post("/api/problem_set", json={"name": " ", "problems": ["p1"]}).status_code, 400)
        r = c.post("/api/problem_set", json={"name": "Dev 12", "problems": ["p1", "zz", "p1"]})
        self.assertEqual(r.status_code, 200)
        sets = c.get("/api/config").json()["problem_sets"]
        saved = next(s for s in sets if s.get("saved"))
        self.assertEqual([p["id"] for p in saved["problems"]], ["p1", "zz"])
        self.assertEqual(saved["problems"][0]["category"], "Misconfiguration")
        self.assertEqual(sets[-1]["id"], "custom")
        self.assertEqual(c.delete("/api/problem_set/dev-12").status_code, 200)
        self.assertFalse(any(s.get("saved") for s in c.get("/api/config").json()["problem_sets"]))

    def test_queue_runs_steps_in_order_then_resumes_only_missing_cells(self):
        c = self.client
        q = self.save([step(name="typed", config_label="typed"),
                       step(name="shell", config_label="shell", action_space="typed+shell"),
                       step(name="off", config_label="off", enabled=False)])
        seen = []

        def fake_campaign(**kw):
            seen.append((kw["config_label"], kw["action_space"], sorted(kw["cells"])))
            for p, rep in kw["cells"]:
                kw["on_result"]({"run_id": f"{kw['config_label']}-{p}-{rep}", "timestamp_utc": "2026-10-30T10:00:00Z",
                                 "problem_id": p, "repeat_index": rep, "agent_id": kw["agent"],
                                 "model_id": kw["model"], "judge_model_id": kw["judge_model"],
                                 "action_space": kw["action_space"], "config_label": kw["config_label"] or "",
                                 "run_class": kw["run_class"], "row": {"Diagnosis.success": "True", "Mitigation.success": "True"}})
                yield f"\n===== [1/1] problem={p} repeat={rep}/2 agent=a action_space=x =====\n"
                yield f"----- recorded result for {p}: parsed row found -----\n"

        with mock.patch.object(runner, "stream_campaign", side_effect=fake_campaign), \
                mock.patch.object(self.app_module, "_on_result", side_effect=lambda rec: self._record(rec)):
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"]})
            self.assertIn("[[DONE]]", r.text)
            self.assertIn("step 1/3 start: typed | runs 4 to do", r.text)
            self.assertIn("step 3/3 skipped", r.text)
            self.assertIn("finished", r.text)
            self.assertLess(r.text.index("step 1/3 start"), r.text.index("step 2/3 start"))
            self.assertEqual([s[:2] for s in seen], [("typed", "typed"), ("shell", "typed+shell")])
            self.assertEqual(len(seen[0][2]), 4)
            # second go: everything is done, nothing runs again
            seen.clear()
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"]})
            self.assertEqual(seen, [])
            self.assertEqual(r.text.count("nothing to do"), 2)
            pv = c.get("/api/queue/preview", params={"queue_id": q["queue_id"]}).json()
            self.assertEqual(pv["runs_left"], 0)
            self.assertEqual(pv["steps"][0]["counts"]["pass"], 4)
        st = c.get("/api/state").json()
        self.assertFalse(st["busy"])
        self.assertEqual(st["job"]["kind"], "queue")

    def _record(self, rec):
        import json
        with (self.tmp / "results" / "gui_runs.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def test_second_start_is_refused_while_busy_and_stop_ends_the_queue(self):
        c = self.client
        q = self.save([step(name="one", config_label="one"), step(name="two", config_label="two")])
        gate = threading.Event()
        started = []

        def slow_campaign(**kw):
            started.append(kw["config_label"])
            yield "working\n"
            gate.wait(5)

        with mock.patch.object(runner, "stream_campaign", side_effect=slow_campaign):
            job = jobs.start("queue", "q", lambda: self.app_module._queue_lines(self.app_module._find_queue(q["queue_id"]), None))
            time.sleep(0.2)
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"]})
            self.assertIn("already running", r.text)
            self.assertTrue(c.get("/api/state").json()["busy"])
            c.post("/api/stop")
            gate.set()
            end = time.time() + 5
            while job.running and time.time() < end:
                time.sleep(0.01)
        self.assertEqual(started, ["one"])
        self.assertEqual(job.state, "stopped")
        self.assertIn("stopped before step 2/2", "".join(job.read(0, wait=0.1)[0]))
        # re-attach: the last lines and then DONE
        r = c.get("/api/job/stream", params={"offset": -3})
        self.assertIn("[[DONE]]", r.text)


if __name__ == "__main__":
    unittest.main()
