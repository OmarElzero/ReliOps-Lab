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


class ExperimentLabelTests(unittest.TestCase):
    def test_labels_rounds_and_progress_per_experiment(self):
        q = queueplan
        self.assertEqual(q.parse_label("Night one"), ("Night one", 1))
        self.assertEqual(q.parse_label("Night one (restart 3)"), ("Night one", 3))
        self.assertEqual(q.make_label("A", 1), "A")
        self.assertEqual(q.make_label("A", 2), "A (restart 2)")
        runs = [dict(run(1, "p1", 1, True), experiment="A"), dict(run(2, "p1", 2, True), experiment="A")]
        self.assertEqual(q.experiment_label("A", runs), "A")
        self.assertEqual(q.experiment_label("A", runs, restart=True), "A (restart 2)")
        self.assertEqual(q.experiment_label("B", runs, restart=True), "B")   # nothing to restart from
        runs.append(dict(run(3, "p1", 1, True), experiment="A (restart 2)"))
        self.assertEqual(q.experiment_label("A", runs), "A (restart 2)")
        self.assertEqual(q.experiment_label("A", runs, restart=True), "A (restart 3)")
        s = q.clean_step(step())
        by = lambda lab: {(c["problem"], c["repeat"]): c["status"] for c in q.cells(s, runs, None, lab)}
        self.assertEqual(by("A")[("p1", 1)], "pass")
        self.assertEqual(by("A (restart 2)")[("p1", 2)], "missing")
        self.assertEqual(by("B")[("p1", 1)], "missing")
        self.assertEqual(q.cells(s, runs)[0]["status"], "pass")      # no experiment given: old behaviour
        names = q.experiment_names(runs)
        self.assertEqual([n["name"] for n in names], ["A"])
        self.assertEqual(names[0]["runs"], 3)

    def test_preview_counts_the_typed_name(self):
        s = queueplan.clean_step(step())
        queue = {"queue_id": "x", "name": "q", "steps": [s]}
        runs = [dict(run(1, "p1", 1, True), experiment="A")]
        self.assertEqual(queueplan.preview(queue, runs, [], "A")["runs_left"], 3)
        self.assertEqual(queueplan.preview(queue, runs, [], "B")["runs_left"], 4)
        self.assertEqual(queueplan.preview(queue, runs, [], "B")["experiment_label"], "B")


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
        with mock.patch.dict(os.environ, {"LAB_DELETE_PASSWORD": "pw"}):
            self.assertEqual(c.delete(f"/api/queue/{q['queue_id']}", headers={"X-Delete-Password": "pw"}).status_code, 200)
        self.assertEqual(c.get("/api/queues").json()["queues"], [])
        self.assertEqual(c.get("/api/queue/preview", params={"queue_id": "nope"}).status_code, 404)

    def test_delete_needs_the_password(self):
        c = self.client
        q = self.save([step()])
        url = f"/api/queue/{q['queue_id']}"
        env = {k: v for k, v in os.environ.items() if k != "LAB_DELETE_PASSWORD"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("time.sleep"):
            self.assertTrue(c.get("/api/queue/delete-lock").json()["locked_off"])
            r = c.delete(url, headers={"X-Delete-Password": ""})          # no password set on this machine: locked for everyone
            self.assertEqual(r.status_code, 403)
            self.assertIn("LAB_DELETE_PASSWORD", r.json()["error"])
        with mock.patch.dict(os.environ, {"LAB_DELETE_PASSWORD": "s3cret"}), mock.patch("time.sleep") as slept:
            self.assertFalse(c.get("/api/queue/delete-lock").json()["locked_off"])
            self.assertNotIn("s3cret", c.get("/api/queue/delete-lock").text)
            self.assertEqual(c.delete(url).status_code, 403)                                         # no header
            self.assertEqual(c.delete(url, headers={"X-Delete-Password": "wrong"}).status_code, 403)
            self.assertTrue(slept.called)                                                            # guessing is slowed down
            self.assertEqual(len(c.get("/api/queues").json()["queues"]), 1)                          # still there
            self.assertEqual(c.delete(url, headers={"X-Delete-Password": "s3cret"}).status_code, 200)
        self.assertEqual(c.get("/api/queues").json()["queues"], [])

    def test_the_delete_dialog_sends_the_password_from_a_masked_field(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "frontend", "queue.js")).read()
        html = open(os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")).read()
        self.assertIn('id="dd-pass" type="password"', html)
        self.assertIn("X-Delete-Password", js)
        self.assertNotIn("confirm(`Delete the queue", js)     # the old one-click confirm is gone

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
                                 "run_class": kw["run_class"], "experiment": kw.get("experiment") or "",
                                 "row": {"Diagnosis.success": "True", "Mitigation.success": "True"}})
                yield f"\n===== [1/1] problem={p} repeat={rep}/2 agent=a action_space=x =====\n"
                yield f"----- recorded result for {p}: parsed row found -----\n"

        with mock.patch.object(runner, "stream_campaign", side_effect=fake_campaign), \
                mock.patch.object(self.app_module, "_on_result", side_effect=lambda rec: self._record(rec)):
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"]})
            self.assertIn("Give this experiment a name", r.text)   # every run is recorded under a name
            self.assertEqual(seen, [])
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"], "experiment": "Night one"})
            self.assertIn("[[DONE]]", r.text)
            self.assertIn("experiment: Night one", r.text)
            self.assertIn("step 1/3 start: typed | runs 4 to do", r.text)
            self.assertIn("step 3/3 skipped", r.text)
            self.assertIn("finished", r.text)
            self.assertLess(r.text.index("step 1/3 start"), r.text.index("step 2/3 start"))
            self.assertEqual([s[:2] for s in seen], [("typed", "typed"), ("shell", "typed+shell")])
            self.assertEqual(len(seen[0][2]), 4)
            # second go: everything is done, nothing runs again
            seen.clear()
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"], "experiment": "Night one"})
            self.assertEqual(seen, [])
            self.assertEqual(r.text.count("nothing to do"), 2)
            # a new name starts fresh; the old runs stay but do not count for it
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"], "experiment": "Night two"})
            self.assertEqual(len(seen), 2)
            self.assertEqual({s[1] for s in seen}, {"typed", "typed+shell"})
            # restart: same name, new round, everything runs again and is stamped with the round
            seen.clear()
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"], "experiment": "Night one", "restart": "true"})
            self.assertIn("experiment: Night one (restart 2)", r.text)
            self.assertEqual(len(seen), 2)
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
            job = jobs.start("queue", "q", lambda: self.app_module._queue_lines(self.app_module._find_queue(q["queue_id"]), None, "x"))
            time.sleep(0.2)
            r = c.get("/api/queue/run", params={"queue_id": q["queue_id"], "experiment": "x"})
            self.assertIn("already running", r.text)
            self.assertTrue(c.get("/api/state").json()["busy"])
            c.post("/api/stop")
            gate.set()
            end = time.time() + 5
            while job.running and time.time() < end:
                time.sleep(0.01)
        self.assertEqual(started, ["one"])
        self.assertEqual(job.state, "stopped")
        self.assertIn("paused after the current run, in step 1/2", "".join(job.read(0, wait=0.1)[0]))
        # re-attach: the last lines and then DONE
        r = c.get("/api/job/stream", params={"offset": -3})
        self.assertIn("[[DONE]]", r.text)


class MachineShareTests(unittest.TestCase):
    """One queue, shown whole to everyone; each machine picks share k of N and runs only that share's problems."""

    def _queue(self, **kw):
        probs = [f"p{i}" for i in range(1, 8)]
        raw = {"name": "R2", "steps": [step(problems=probs, repeat_n=1, config_label="A"),
                                       step(problems=probs, repeat_n=1, config_label="B")], **kw}
        return queueplan.clean_queue(raw)

    def test_round_robin_by_problem_covers_everything_once(self):
        q = self._queue(machines=3)
        lists = queueplan.share_lists(q)
        self.assertEqual(lists, [["p1", "p4", "p7"], ["p2", "p5"], ["p3", "p6"]])
        self.assertEqual(sorted(p for x in lists for p in x), sorted(queueplan.queue_problems(q)))
        self.assertIsNone(queueplan.share_set(q, None))
        self.assertEqual(queueplan.share_set(q, 2), {"p2", "p5"})
        with self.assertRaises(ValueError):
            queueplan.share_set(q, 4)

    def test_hand_made_shares_are_kept_and_unnamed_problems_are_placed(self):
        q = self._queue(machines=2, shares=[["p1", "p2", "p3"], ["p4"]])
        self.assertEqual(queueplan.share_lists(q), [["p1", "p2", "p3", "p7"], ["p4", "p5", "p6"]])  # each unnamed one goes to the smaller share
        with self.assertRaises(ValueError):
            self._queue(machines=2, shares=[["p1"], ["p1"]])
        with self.assertRaises(ValueError):
            self._queue(machines=2, shares=[["p1"]])
        with self.assertRaises(ValueError):
            self._queue(machines=25)

    def test_every_configuration_of_a_problem_stays_on_one_machine(self):
        q = self._queue(machines=3)
        runs = []
        for k in (1, 2, 3):
            only = queueplan.share_set(q, k)
            for s in q["steps"]:
                for p, _ in queueplan.todo(s, runs, None, None, only):
                    self.assertIn(p, only)
        mine = [{p for p, _ in queueplan.todo(s, runs, None, None, queueplan.share_set(q, 1))} for s in q["steps"]]
        self.assertEqual(mine[0], mine[1])

    def test_preview_counts_the_share_but_shows_every_cell_and_who_ran_each_share(self):
        q = self._queue(machines=3)
        runs = [dict(run(1, "p1", 1, True, label="A"), experiment="X", run_by="Omar"),
                dict(run(2, "p2", 1, False, label="A"), experiment="X", run_by="Mona")]
        pv = queueplan.preview(q, runs, [], "X", share=1)
        self.assertEqual(pv["total_runs"], 6)              # 3 problems x 2 steps
        self.assertEqual(pv["runs_left"], 5)               # p1 under A is done
        self.assertEqual(pv["all_total_runs"], 14)
        self.assertEqual(pv["all_runs_left"], 12)
        self.assertEqual(len(pv["steps"][0]["cells"]), 7)  # the whole grid is still returned
        self.assertEqual(sum(c["mine"] for c in pv["steps"][0]["cells"]), 3)
        sh = pv["shares"]
        self.assertEqual([x["done"] for x in sh], [1, 1, 0])
        self.assertEqual(sh[0]["people"][0]["name"], "Omar")
        self.assertEqual(sh[1]["people"][0]["name"], "Mona")
        whole = queueplan.preview(q, runs, [], "X")
        self.assertIsNone(whole["share"])
        self.assertEqual(whole["runs_left"], 12)

    def test_split_by_repeat_balances_and_keeps_configs_of_a_pair_together(self):
        probs = [f"p{i}" for i in range(1, 13)]
        q = queueplan.clean_queue({"name": "R2", "machines": 5, "split": "repeat",
                                   "steps": [step(problems=probs, repeat_n=3, config_label="A"),
                                             step(problems=probs, repeat_n=3, config_label="B")]})
        lists = queueplan.share_lists(q)
        self.assertEqual(sorted(len(x) for x in lists), [7, 7, 7, 7, 8])
        self.assertEqual(len({u for x in lists for u in x}), 36)
        cells_by_share = []
        for k in range(1, 6):
            only = queueplan.share_set(q, k)
            a, b = (set(queueplan.todo(s, [], None, None, only)) for s in q["steps"])
            self.assertEqual(a, b)                       # both configurations of a (problem, repeat) on one machine
            cells_by_share.append(a)
        self.assertEqual(sum(len(x) for x in cells_by_share), 36)
        self.assertEqual(len(set().union(*cells_by_share)), 36)
        with self.assertRaises(ValueError):
            queueplan.clean_queue({"name": "x", "split": "cell", "steps": [step()]})

    def test_a_campaign_slice_does_not_shift_as_other_machines_finish(self):
        import campaign
        manifest = {"problems": ["a", "b", "c", "d"], "repeat_n": 1}
        every = [("a", 1), ("b", 1), ("c", 1), ("d", 1)]
        before = campaign.slice_cells(every, 2, 2, manifest)
        after = campaign.slice_cells([("c", 1), ("d", 1)], 2, 2, manifest)   # machine 1 finished a and b
        self.assertEqual(before, [("b", 1), ("d", 1)])
        self.assertEqual(after, [("d", 1)])                                     # still never takes c


class QueueSummaryTests(unittest.TestCase):
    def _run(self, i, problem, rep, passed, **kw):
        r = {"run_id": f"r{i}", "timestamp_utc": f"2026-11-04T10:{i:02d}:00Z", "problem_id": problem, "repeat_index": rep,
             "agent_id": "reliops_v2", "model_id": "m", "judge_model_id": "j", "action_space": "typed", "config_label": "B2",
             "agent_switches": "", "experiment": "R2 main", "run_by": "omar", "e2e_pass": passed,
             "diagnosis_pass": passed, "total_tokens": 1000, "duration_s": 300}
        r.update(kw)
        return r

    def test_groups_by_experiment_config_and_problem_and_the_latest_run_counts(self):
        import queuesummary
        runs = [self._run(1, "p1", 1, False), self._run(2, "p1", 1, True),          # re-run of the same cell: the latest counts
                self._run(3, "p1", 2, True), self._run(4, "p2", 1, False),
                self._run(5, "p3", 1, None, duration_s=None, ttd_s=60, ttm_s=30),    # no outcome: incomplete, never a failure
                self._run(6, "p1", 1, True, config_label="ALL"),                    # another configuration, same experiment
                self._run(7, "p9", 1, True, experiment="Other"),
                self._run(8, "p9", 1, True, experiment="")]                         # no experiment name: not summarized
        out = queuesummary.summarize(runs)
        self.assertEqual(sorted(e["name"] for e in out), ["Other", "R2 main"])
        e = next(x for x in out if x["name"] == "R2 main")
        self.assertEqual((e["runs"], e["cells"], e["passed"], e["failed"], e["incomplete"]), (6, 5, 3, 1, 1))
        b2 = next(c for c in e["configs"] if c["config_label"] == "B2")
        self.assertEqual((b2["passed"], b2["failed"], b2["incomplete"], b2["attempts_extra"]), (2, 1, 1, 1))
        self.assertEqual([p["problem"] for p in b2["problem_rows"]], ["p2", "p3", "p1"])      # what needs a look comes first
        self.assertEqual([p["verdict"] for p in b2["problem_rows"]], ["not solved", "incomplete", "solved"])
        self.assertAlmostEqual(b2["pass_rate"], 2 / 3)                                          # the incomplete cell is not in the rate
        self.assertEqual((b2["problems_solved"], b2["problems"]), (1, 3))

    def test_time_is_the_whole_run_or_labelled_agent_time(self):
        import queuesummary
        whole = queuesummary.run_seconds({"duration_s": 412.5, "ttd_s": 10, "ttm_s": 5})
        self.assertEqual(whole, (412.5, "run"))
        self.assertEqual(queuesummary.run_seconds({"ttd_s": 60, "ttm_s": 30}), (90.0, "agent"))
        self.assertEqual(queuesummary.run_seconds({}), (None, ""))
        out = queuesummary.summarize([self._run(1, "p1", 1, True, duration_s=None, ttd_s=60, ttm_s=30)])
        self.assertEqual(out[0]["configs"][0]["time_kind"], "agent")
        self.assertEqual(out[0]["seconds"], 90.0)

    def test_restart_rounds_belong_to_the_same_experiment(self):
        import queuesummary
        out = queuesummary.summarize([self._run(1, "p1", 1, False), self._run(2, "p1", 1, True, experiment="R2 main (restart 2)")])
        self.assertEqual([e["name"] for e in out], ["R2 main"])
        self.assertEqual(out[0]["configs"][0]["passed"], 1)

    def test_endpoint_and_page(self):
        here = os.path.dirname(__file__)
        js = open(os.path.join(here, "..", "frontend", "v2.js")).read()
        self.assertIn("/api/experiments/summary", js)
        self.assertIn('id="exp-list"', open(os.path.join(here, "..", "frontend", "index.html")).read())


class QueueBoardTests(TmpRepoCase):
    """Who took which share, what each machine is doing, and how each share ended."""

    def setUp(self):
        super().setUp()
        try:
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi not available: {e}")
        import claims
        self.app, self.claims = app_module, claims
        self.q = queueplan.clean_queue({"name": "Calibration", "machines": 2, "steps": [step(problems=["p1", "p2", "p3", "p4"], repeat_n=1)]})
        self.q["queue_id"] = "q9"

    def test_share_states(self):
        c = self.claims
        now = 1_000_000_000.0
        stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - ago))
        busy = {"total": 2, "left": 2, "people": []}
        self.assertEqual(c.share_state(busy, None, now), "not taken")
        self.assertEqual(c.share_state({**busy, "people": [{"name": "a"}]}, None, now), "worked")
        self.assertEqual(c.share_state(busy, {"status": "running", "updated_at": stamp(60)}, now), "running")
        self.assertEqual(c.share_state(busy, {"status": "running", "updated_at": stamp(c.STALE_S + 60)}, now), "silent")
        self.assertEqual(c.share_state(busy, {"status": "paused", "updated_at": stamp(60)}, now), "paused")
        self.assertEqual(c.share_state(busy, {"status": "halted", "updated_at": stamp(60)}, now), "stopped")
        self.assertEqual(c.share_state(busy, {"status": "interrupted", "updated_at": stamp(60)}, now), "stopped")
        self.assertEqual(c.share_state(busy, {"status": "finished", "updated_at": stamp(60)}, now), "ended")
        # a share whose cells all have results is finished, whatever the claim says
        self.assertEqual(c.share_state({"total": 2, "left": 0, "people": []}, {"status": "running", "updated_at": stamp(5)}, now), "finished")

    def test_the_claim_follows_the_queue_from_running_to_finished(self):
        seen = []

        def fake_run_lines(**kw):
            yield "===== [1/1] problem=%s agent=a action_space=- =====\n" % kw["cells"][0][0]
            seen.append(self.claims.claim_for(self.claims.for_label("E"), 1))    # what teammates would see mid-run
            yield "----- recorded result for %s: parsed row found -----\n" % kw["cells"][0][0]

        with mock.patch.object(self.app, "_run_lines", side_effect=fake_run_lines), \
             mock.patch.object(self.app, "_collect_runs", return_value=("local", [])):
            "".join(self.app._queue_lines(self.q, "Omar", "E", False, 1))
        self.assertTrue(seen)
        self.assertEqual((seen[0]["status"], seen[0]["run_by"], seen[0]["share"]), ("running", "Omar", 1))
        self.assertEqual(seen[0]["machine"], self.claims.machine_name())
        self.assertEqual(seen[0]["now_problem"], "p1")
        last = self.claims.claim_for(self.claims.for_label("E"), 1)
        self.assertEqual((last["status"], last["now_problem"]), ("finished", ""))
        self.assertIsNone(self.claims.claim_for(self.claims.for_label("E"), 2))   # nobody took share 2
        self.assertEqual(self.claims.for_label("another experiment"), [])

    def test_a_paused_queue_says_paused(self):
        def fake_run_lines(**kw):
            yield "===== [1/1] problem=p1 agent=a action_space=- =====\n"
            runner._stop_event.set()
        self.addCleanup(runner._stop_event.clear)
        with mock.patch.object(self.app, "_run_lines", side_effect=fake_run_lines), \
             mock.patch.object(self.app, "_collect_runs", return_value=("local", [])):
            "".join(self.app._queue_lines(self.q, "Omar", "E", False, 2))
        self.assertEqual(self.claims.claim_for(self.claims.for_label("E"), 2)["status"], "paused")

    def test_starting_on_a_share_someone_else_is_running_says_so(self):
        import store
        store.append("claims", {"experiment": "E", "share": 1, "machines": 2, "run_by": "Fady", "machine": "other-laptop",
                                "status": "running", "started_at": "x", "now_problem": "p1"})
        mine = self.claims.Tracker(self.q, "Omar", 1)
        note = mine.start("E")
        self.assertIn("Fady (other-laptop) is already running share 1", note)
        self.assertEqual(self.claims.Tracker(self.q, "Omar", 2).start("E"), "")        # a different share: nothing to say
        store.append("claims", {"experiment": "E", "share": 2, "machines": 2, "run_by": "Omar", "machine": self.claims.machine_name(),
                                "status": "running", "started_at": "x"})
        self.assertEqual(self.claims.Tracker(self.q, "Omar", 2).start("E"), "")        # my own earlier start on this machine

    def test_preview_shows_who_took_what_even_before_a_first_run(self):
        now = 1_000_000_000.0
        stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - ago))
        taken = [{"experiment": "E", "share": 1, "run_by": "Fady", "machine": "lap", "status": "running", "updated_at": stamp(120),
                  "started_at": stamp(900), "now_problem": "p1"}]
        pv = queueplan.preview(self.q, [], [], "E", None, lambda label: taken if label == "E" else [], now)
        s1, s2 = pv["shares"]
        self.assertEqual((s1["state"], s1["claim"]["run_by"], s1["claim"]["now_problem"]), ("running", "Fady", "p1"))
        self.assertEqual((s2["state"], s2["claim"]), ("not taken", None))
        self.assertEqual((s1["passed"], s1["failed"], s1["left"]), (0, 0, 2))
        self.assertTrue(pv["this_machine"] is not None)
        # no claims function (an older caller): the board still works, from recorded runs alone
        self.assertEqual([s["state"] for s in queueplan.preview(self.q, [], [], "E")["shares"]], ["not taken", "not taken"])
        # every cell of share 1 has a result: finished, with its pass/fail counts
        runs = [dict(run(1, "p1", 1, True), experiment="E"), dict(run(2, "p3", 1, False), experiment="E")]     # share 1 is p1 and p3
        pv = queueplan.preview(self.q, runs, [], "E", None, lambda label: taken, now)
        self.assertEqual((pv["shares"][0]["state"], pv["shares"][0]["passed"], pv["shares"][0]["failed"]), ("finished", 1, 1))

    def test_the_page_shows_the_board_and_warns_before_a_double_start(self):
        js = open(os.path.join(os.path.dirname(__file__), "..", "frontend", "queue.js")).read()
        for needle in ("Queue board", "busyElsewhere", "Taken by", "no sign of life", "setInterval"):
            self.assertIn(needle, js)


if __name__ == "__main__":
    unittest.main()


class MachineVersionTests(unittest.TestCase):
    def test_preview_groups_runs_by_versions(self):
        q = queueplan.clean_queue({"name": "q", "machines": 2, "steps": [step()]})
        label = queueplan.experiment_label("E", [])
        runs = [{"experiment": label, "run_by": "Omar", "machine_versions": "kind 0.30.0; docker 27.1.1"},
                {"experiment": label, "run_by": "Sara", "machine_versions": "kind 0.25.0; docker 27.1.1"},
                {"experiment": label, "run_by": "Omar", "machine_versions": "kind 0.30.0; docker 27.1.1"},
                {"experiment": "Other", "run_by": "Zed", "machine_versions": "kind 0.1.0"}]
        got = queueplan.preview(q, runs, [], "E")["machine_versions"]
        self.assertEqual([g["n"] for g in got], [2, 1])
        self.assertEqual(got[0]["people"], ["Omar"])
        self.assertEqual(got[1]["people"], ["Sara"])


class CellOrderProgressTests(unittest.TestCase):
    def _job(self):
        import jobs
        return jobs.Job("queue", "q", {})

    def test_cell_line_names_the_problem_and_totals_survive_every_step_line(self):
        j = self._job()
        j.add("##### [QUEUE] cell order: 12 problem-repeat pair(s), every step of a pair back to back; 12 run(s) to do #####\n")
        j.add("\n##### [QUEUE] step 1/1 start: R2 NULL · 12 problems × 1 | runs 1 to do #####\n")
        self.assertEqual(j.progress["step_runs"], 12)
        self.assertIsNone(j.progress["problem"])
        j.add("[cell] gc_capacity_degradation, repeat 1 (pair 2 of 12)\n")
        self.assertEqual((j.progress["problem"], j.progress["repeat"], j.progress["pair"]), ("gc_capacity_degradation", 1, 2))
        j.add("===== [1/1] problem=gc_capacity_degradation agent=reliops_v2 action_space=typed+shell =====\n")
        j.add("----- recorded result for gc_capacity_degradation: parsed row found -----\n")
        self.assertEqual(j.progress["step_recorded"], 1)
        # the next cell's step line must not wipe the count or the total
        j.add("\n##### [QUEUE] step 1/1 start: R2 NULL · 12 problems × 1 | runs 1 to do #####\n")
        self.assertEqual((j.progress["step_recorded"], j.progress["step_runs"]), (1, 12))

    def test_step_by_step_order_still_resets_per_step(self):
        j = self._job()
        j.add("\n##### [QUEUE] step 1/2 start: A | runs 5 to do #####\n")
        j.add("----- recorded result for p: x -----\n")
        j.add("\n##### [QUEUE] step 2/2 start: B | runs 7 to do #####\n")
        self.assertEqual((j.progress["step_runs"], j.progress["step_recorded"]), (7, 0))
