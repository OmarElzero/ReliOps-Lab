"""Run: python3 -I backend/test_runner_storage.py  (from the repo root)

Covers what the Lab now stores per run: the diagnosis text and judge fields pulled out of SREGym's
own row, the migration of an older results CSV to the wider header, and the story helper."""

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import runner  # noqa: E402

# Shapes below follow SREGym's source: main.py flattens one level into "Diagnosis.<key>", and dict /
# list values reach a CSV as Python reprs.
SREGYM_ROW = {
    "Diagnosis.success": "False",
    "Diagnosis.submission": "The frontend fails because of anti-affinity.",
    "Diagnosis.reasoning": "Right component, wrong mechanism.",
    "Diagnosis.composite_score": "0.67",
    "Diagnosis.dimensions": str({"D1": {"name": "Localization", "score": 1.0},
                                 "D2": {"name": "Characterization", "score": 0.0},
                                 "D3": {"name": "Scope", "score": 1.0}}),
    "Diagnosis.checklist": str([{"id": "D2-Q1", "answer": "No", "evidence": "never mentions the PVC", "confidence": 0.9}]),
}


class ExtractDiagnosisTests(unittest.TestCase):
    def test_reads_text_scores_and_checklist(self):
        d = runner.extract_diagnosis(SREGYM_ROW)
        self.assertEqual(d["diagnosis_text"], "The frontend fails because of anti-affinity.")
        self.assertEqual(d["judge_reasoning"], "Right component, wrong mechanism.")
        self.assertEqual((d["diagnosis_d1"], d["diagnosis_d2"], d["diagnosis_d3"]), (1.0, 0.0, 1.0))
        self.assertAlmostEqual(d["diagnosis_composite"], 0.67)
        self.assertEqual(json.loads(d["judge_checklist"])[0]["id"], "D2-Q1")

    def test_dict_values_that_are_not_strings(self):
        row = {"Diagnosis.dimensions": {"D1": {"score": 0.5}}}
        self.assertEqual(runner.extract_diagnosis(row)["diagnosis_d1"], 0.5)

    def test_missing_fields_are_none_not_guessed(self):
        d = runner.extract_diagnosis({"Diagnosis.success": "True"})
        self.assertTrue(all(v is None for v in d.values()))
        self.assertTrue(all(v is None for v in runner.extract_diagnosis(None).values()))

    def test_garbage_dimensions_do_not_crash(self):
        d = runner.extract_diagnosis({"Diagnosis.dimensions": "not a dict", "Diagnosis.checklist": "[oops"})
        self.assertIsNone(d["diagnosis_d1"])
        self.assertIsNone(d["judge_checklist"])


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "experiments").mkdir()
        (self.tmp / "experiments" / "run_log_template.csv").write_text("run_id,problem_id,e2e_pass,diagnosis_d2\n", encoding="utf-8")
        p = mock.patch.object(runner, "reliops_dir", return_value=self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def test_header_has_template_plus_lab_columns(self):
        h = runner._run_log_header()
        self.assertEqual(h[:3], ["run_id", "problem_id", "e2e_pass"])
        for c in ("diagnosis_text", "judge_reasoning", "judge_checklist", "config_label", "run_class", "transcript_rel"):
            self.assertIn(c, h)

    def test_old_csv_is_migrated_without_losing_rows_or_columns(self):
        path = self.tmp / "results" / "gui_runs.csv"
        path.parent.mkdir()
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "problem_id", "e2e_pass", "legacy_col"])
            w.writerow(["r1", "p1", "True", "keep-me"])
        new_header = runner._run_log_header()
        merged = runner._ensure_csv_header(path, new_header)
        self.assertIn("legacy_col", merged)
        self.assertIn("diagnosis_text", merged)
        with path.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual((rows[0]["run_id"], rows[0]["legacy_col"], rows[0]["diagnosis_text"]), ("r1", "keep-me", ""))

    def test_append_writes_text_and_config(self):
        record = {"run_id": "r2", "timestamp_utc": "2026-10-29T10:00:00Z", "problem_id": "p1", "agent_id": "reliops_v1",
                  "model_id": "m", "judge_model_id": "j", "config_label": "F7-on", "run_class": "exploration",
                  "row": SREGYM_ROW, **runner.extract_diagnosis(SREGYM_ROW)}
        runner.append_csv_run(record)
        runner.append_csv_run({**record, "run_id": "r3"})  # second append must not duplicate the header
        with (self.tmp / "results" / "gui_runs.csv").open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["run_id"] for r in rows], ["r2", "r3"])
        self.assertEqual(rows[0]["diagnosis_text"], "The frontend fails because of anti-affinity.")
        self.assertEqual((rows[0]["config_label"], rows[0]["judge_model_id"], rows[0]["diagnosis_d2"]), ("F7-on", "j", "0.0"))


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "scripts").mkdir()
        # A stand-in renderer with the same contract as scripts/replay_trace.py: render_story(transcript, cluster|None).
        (self.tmp / "scripts" / "replay_trace.py").write_text(
            "def render_story(t, c=None):\n    return 'story:' + t.split('/')[-1] + ':' + str(c is not None)\n", encoding="utf-8")
        t = self.tmp / "external" / "envs" / "sregym" / "results" / "a" / "b" / "p1" / "run_1"
        t.mkdir(parents=True)
        (t / "react_v1_transcript.jsonl").write_text("{}\n", encoding="utf-8")
        for target in ("reliops_dir",):
            p = mock.patch.object(runner, target, return_value=self.tmp)
            p.start()
            self.addCleanup(p.stop)

    def test_find_transcript_returns_path_relative_to_sregym(self):
        rel = runner._find_transcript("p1", 0)
        self.assertTrue(rel.endswith("p1/run_1/react_v1_transcript.jsonl"), rel)
        self.assertIsNone(runner._find_transcript("other", 0))

    def test_render_story_uses_cluster_file_when_present(self):
        rel = runner._find_transcript("p1", 0)
        rec = {"run_id": "r9", "transcript_rel": rel}
        self.assertTrue(runner.render_story(rec).endswith(":False"))
        wd = runner.walkthrough_dir("r9")
        wd.mkdir(parents=True)
        (wd / "cluster.jsonl").write_text("", encoding="utf-8")
        self.assertTrue(runner.render_story(rec).endswith(":True"))

    def test_missing_pieces_give_plain_errors(self):
        with self.assertRaises(FileNotFoundError):
            runner.render_story({"run_id": "x"})  # no transcript recorded
        with self.assertRaises(FileNotFoundError):
            runner.render_story({"run_id": "x", "transcript_rel": "nope.jsonl"})


if __name__ == "__main__":
    unittest.main()
