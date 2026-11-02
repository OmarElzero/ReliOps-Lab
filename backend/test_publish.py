"""Shared results page: what leaves the machine, the live file, and the publisher."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish  # noqa: E402


def run(i, **kw):
    base = {"run_id": f"r{i}", "timestamp_utc": f"2026-11-01T10:{i:02d}:00Z", "run_by": "omar", "problem_id": "p1", "category": "c",
            "agent_id": "reliops_v2", "config_label": "B", "experiment": "E", "purpose": "dev", "e2e_pass": True,
            "diagnosis_pass": True, "mitigation_pass": True, "num_llm_calls": 9, "ttd_s": 10.0, "ttm_s": 20.0,
            # fields that must never be published
            "diagnosis_text": "SECRET DIAGNOSIS", "judge_reasoning": "SECRET JUDGE", "log_file": "/home/omar/x.log",
            "key_slot": "GEMINI_API_KEY_2", "git_commit": "abc"}
    base.update(kw)
    return base


class SnapshotTests(unittest.TestCase):
    def test_only_listed_fields_leave_and_text_fields_are_cleaned(self):
        snap = publish.build_snapshot([run(1, failure_mode_tag="see /home/omar/key.txt or omar@x.com")], "local", False, True)
        text = json.dumps(snap)
        for secret in ("SECRET DIAGNOSIS", "SECRET JUDGE", "x.log", "GEMINI_API_KEY_2", "git_commit", "omar@x.com", "/home/omar"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(snap["runs"][0]), set(publish.RUN_FIELDS))

    def test_frozen_set_runs_are_hidden_unless_switched_on(self):
        runs = [run(1), run(2, purpose="final")]
        hidden = publish.build_snapshot(runs, "local", include_final=False, names=True)
        self.assertEqual([r["run_id"] for r in hidden["runs"]], ["r1"])
        self.assertEqual(hidden["hidden_final_runs"], 1)
        shown = publish.build_snapshot(runs, "local", include_final=True, names=True)
        self.assertEqual(len(shown["runs"]), 2)

    def test_names_can_be_left_out(self):
        self.assertEqual(publish.build_snapshot([run(1)], "local", False, False)["runs"][0]["run_by"], "")


class LiveTests(unittest.TestCase):
    def job(self, **p):
        return {"state": "running", "started_at": 1000.0, "progress": {"step": 2, "steps": 12, "step_name": "a", "problem": "p1",
                "repeat": 1, "repeat_n": 1, "run_k": 3, "run_total": 12, "recorded": 4, "run_started_at": 1900.0, **p}}

    def test_running_has_progress_and_an_estimate(self):
        live = publish.build_live(self.job(), {"status": "running", "queue_name": "Q", "experiment": "E"}, left=8, now=2000.0)
        self.assertEqual((live["status"], live["problem"], live["runs_left"], live["eta_s"]), ("running", "p1", 8, 2000))
        self.assertEqual((live["queue"], live["experiment"]), ("Q", "E"))

    def test_paused_shows_why_and_no_current_problem(self):
        live = publish.build_live(None, {"status": "paused", "queue_name": "Q", "experiment": "E", "reason": "paused after the current run"}, left=None)
        self.assertEqual((live["status"], live["problem"], live["run_started_at"]), ("paused", None, None))
        self.assertIn("paused", live["reason"])

    def test_nothing_known_is_idle(self):
        self.assertEqual(publish.build_live(None, None)["status"], "idle")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = [run(1)]
        self.live = {"status": "idle", "step": None, "problem": None, "repeat": None, "runs_left": None, "generated_at": 1.0}

    def make(self, remote=""):
        return publish.Publisher(lambda: ("local", self.runs), lambda: dict(self.live), self.tmp / "site", remote=remote, every_s=300)

    def test_writes_site_and_data_and_only_again_when_something_changed(self):
        pub = self.make()
        self.assertTrue(pub.tick(now=0))
        self.assertTrue((self.tmp / "site" / "index.html").is_file())
        data = json.loads((self.tmp / "site" / "data" / "snapshot.json").read_text())
        self.assertEqual(data["n_runs"], 1)
        self.assertFalse(pub.tick(now=20))               # same runs, same state
        self.runs.append(run(2))
        self.assertTrue(pub.tick(now=40))
        self.assertEqual(json.loads((self.tmp / "site" / "data" / "snapshot.json").read_text())["n_runs"], 2)

    def test_push_is_rate_limited_and_replaces_one_commit(self):
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        pub = self.make(str(remote))
        pub.tick(now=1000)
        self.assertEqual(pub.last_error, "")
        first = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "gh-pages"], capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.runs.append(run(2))
        pub.tick(now=1100)                                # changed, but pushed less than 300 s ago
        self.assertTrue(pub._dirty)
        pub.tick(now=1400)                                # nothing changed since, interval has passed
        self.assertFalse(pub._dirty)
        count = subprocess.run(["git", "--git-dir", str(remote), "rev-list", "--count", "gh-pages"], capture_output=True, text=True)
        self.assertEqual(count.stdout.strip(), "1")
        shown = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "gh-pages"], capture_output=True, text=True).stdout.split()
        site_files = [f.name for f in publish.SITE_SRC.iterdir() if f.is_file()]
        self.assertEqual(sorted(shown), sorted(["data/live.json", "data/snapshot.json", *site_files]))
        self.assertNotIn("diagnosis", " ".join(shown))


if __name__ == "__main__":
    unittest.main()


class SyncNoticeTests(unittest.TestCase):
    def test_the_snapshot_says_whether_the_team_data_is_live_and_hides_paths_in_the_reason(self):
        old = publish.SYNC_PROVIDER
        publish.SYNC_PROVIDER = lambda: {"connected": True, "live": False, "error": "quota at /home/omar/key.json", "synced_at": "2026-11-02T00:00:00Z",
                                         "reads_today": 100, "read_budget": 8000, "secret": "x"}
        try:
            snap = publish.build_snapshot([run(1)], "firestore-saved-copy", False, True)
        finally:
            publish.SYNC_PROVIDER = old
        self.assertFalse(snap["sync"]["live"])
        self.assertNotIn("/home/omar", json.dumps(snap))
        self.assertNotIn("secret", snap["sync"])


class RepairTests(unittest.TestCase):
    """A hard stop can leave an empty object file in the local publishing repository; every later push then fails."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.runs = [run(1)]
        self.live = {"status": "idle", "step": None, "problem": None, "repeat": None, "runs_left": None, "generated_at": 1.0}
        self.pub = publish.Publisher(lambda: ("local", self.runs), lambda: dict(self.live), self.tmp / "site",
                                     remote=str(self.remote), branch="results-data", every_s=60)
        self.pub.tick(now=1000)
        self.assertEqual(self.pub.last_error, "")

    def damage_head_object(self):
        site = self.tmp / "site"
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=site, capture_output=True, text=True).stdout.strip()
        obj = site / ".git" / "objects" / sha[:2] / sha[2:]
        obj.chmod(0o644)
        obj.write_bytes(b"")                       # what a crash mid-write leaves behind

    def test_a_damaged_local_repository_is_recreated_and_the_push_goes_through(self):
        self.damage_head_object()
        self.runs.append(run(2))
        self.pub.tick(now=1100)
        self.assertEqual(self.pub.last_error, "")
        self.assertIn("repaired", self.pub.last_note)
        self.assertFalse(self.pub._dirty)
        count = subprocess.run(["git", "--git-dir", str(self.remote), "rev-list", "--count", "results-data"], capture_output=True, text=True)
        self.assertEqual(count.stdout.strip(), "1")
        shot = subprocess.run(["git", "--git-dir", str(self.remote), "show", "results-data:data/snapshot.json"], capture_output=True, text=True).stdout
        self.assertEqual(json.loads(shot)["n_runs"], 2)

    def test_a_folder_holding_other_files_is_never_wiped(self):
        (self.tmp / "site" / "my-notes.txt").write_text("not ours")
        self.damage_head_object()
        self.runs.append(run(2))
        self.pub.tick(now=1100)
        self.assertNotEqual(self.pub.last_error, "")
        self.assertTrue((self.tmp / "site" / ".git").exists())
        self.assertEqual((self.tmp / "site" / "my-notes.txt").read_text(), "not ours")
