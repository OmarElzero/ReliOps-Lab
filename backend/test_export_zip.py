"""Run: python3 -I backend/test_export_zip.py  (plain functions, so no pytest is needed)"""
import io, json, os, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


def test_failed_attempts_are_not_duplicates():
    """Three separate failed attempts (no diagnosis, no tokens, different durations) are three runs, not one recorded thrice."""
    import export
    base = {"problem_id": "p", "config_label": "c", "action_space": "typed", "repeat_index": 1}
    a, b = {**base, "duration_s": 1142.8}, {**base, "duration_s": 871.3}
    assert export._dup_signature(a) != export._dup_signature(b)
    assert export._dup_signature(a) == export._dup_signature(dict(a))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        f()
        print("ok", n)
    print(f"{len(tests)} tests passed")
