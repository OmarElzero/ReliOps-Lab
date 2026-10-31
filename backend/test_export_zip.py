import io, json, zipfile
import export


def _run(i, **kw):
    r = {"run_id": f"run{i:04d}abcdef", "problem_id": "p1", "experiment": "E", "repeat_index": 0, "config_label": "c",
         "timestamp_utc": f"2026-10-0{i}", "diagnosis_text": f"diag{i}", "judge_reasoning": "j", "ttd_s": i}
    r.update(kw)
    return r


def test_zip_contents_missing_and_duplicates():
    runs = [_run(1), _run(2, diagnosis_text="diag1", ttd_s=1), _run(3, incomplete_reason="timeout")]
    runs[1]["judge_reasoning"] = "j"
    logs = {"run0001abcdef": "LOG1", "run0002abcdef": "LOG2"}
    data, rep = export.experiment_zip("E", runs, lambda r: logs.get(r["run_id"]),
                                      lambda r: ("# story", "") if r["run_id"] == "run0001abcdef" else (None, "no transcript"))
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert "README.md" in names and "runs.csv" in names and "runs.jsonl" in names
    assert any(n.endswith("log.txt") for n in names) and sum(n.endswith("story.md") for n in names) == 1
    assert rep == {"runs": 3, "with_log": 2, "with_story": 1, "incomplete": 1, "duplicates": 1}
    readme = z.read("README.md").decode()
    assert "POSSIBLE DUPLICATE" in readme and "no transcript" in readme and "timeout" in readme
    assert len(z.read("runs.jsonl").decode().strip().splitlines()) == 3
