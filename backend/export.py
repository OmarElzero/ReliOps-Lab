"""Downloads for papers and slides: tidy CSVs and a dependency-free SVG bar chart with Wilson error bars."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape

RUN_COLUMNS = [
    "run_id", "timestamp_utc", "run_by", "problem_id", "repeat_index", "repeat_n", "agent_id", "model_id",
    "action_space", "config_label", "agent_switches", "run_class", "manifest_id", "judge_model_id", "diagnosis_pass",
    "mitigation_pass", "e2e_pass", "ttd_s", "ttm_s", "total_tokens", "num_llm_calls", "failure_mode_tag",
    "first_bad_step", "review_note", "diagnosis_text", "judge_reasoning",
]
SUMMARY_COLUMNS = [
    "config_label", "agent_id", "model_id", "action_space", "judge_model_id", "run_class", "metric", "runs", "n",
    "passes", "incomplete", "rate", "wilson_lo", "wilson_hi", "problems", "boot_lo", "boot_hi",
    "mean_tokens", "mean_llm_calls", "mean_time_s", "tokens_per_resolved",
]


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})
    return buf.getvalue()


def runs_csv(runs: list[dict]) -> str:
    return _csv(runs, RUN_COLUMNS)


def summary_csv(groups: list[dict]) -> str:
    return _csv(groups, SUMMARY_COLUMNS)


def summary_svg(groups: list[dict], metric: str) -> str:
    """Horizontal bars: pass rate per configuration with the 95% Wilson interval as a whisker, and
    'pass/n' printed at the end so the sample size is never hidden. Plain colours, white background, so
    it can be dropped into a paper or a slide as is."""
    rows = [g for g in groups if g.get("n")]
    left, right, top, row_h, bar_h = 330, 110, 46, 34, 16
    width = 900
    plot_w = width - left - right
    height = top + row_h * max(1, len(rows)) + 36
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
           f'font-family="Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<text x="{left}" y="20" font-size="14" font-weight="bold" fill="#111">'
           f'{escape(metric.replace("_pass", ""))} pass rate, 95% Wilson interval</text>']
    for t in range(0, 101, 20):
        x = left + plot_w * t / 100
        out.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{height - 28}" stroke="#e3e3e3"/>')
        out.append(f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" fill="#555">{t}%</text>')
    if not rows:
        out.append(f'<text x="{left}" y="{top + 20}" fill="#555">no runs with a known outcome yet</text>')
    for i, g in enumerate(rows):
        y = top + i * row_h
        label = f'{g.get("config_label") if g.get("config_label") not in (None, "-") else "(no label)"} · {g.get("agent_id")} · {g.get("model_id")}'
        if g.get("run_class") == "frozen":
            label += " · frozen"
        out.append(f'<text x="{left - 10}" y="{y + bar_h - 3}" text-anchor="end" fill="#111">{escape(label[:52])}</text>')
        rate = g["rate"] or 0.0
        color = "#2b6cb0" if g.get("run_class") == "frozen" else "#7a8aa0"
        out.append(f'<rect x="{left}" y="{y}" width="{plot_w * rate:.1f}" height="{bar_h}" fill="{color}"/>')
        if g.get("wilson_lo") is not None:
            x1, x2, yc = left + plot_w * g["wilson_lo"], left + plot_w * g["wilson_hi"], y + bar_h / 2
            out.append(f'<line x1="{x1:.1f}" y1="{yc}" x2="{x2:.1f}" y2="{yc}" stroke="#111" stroke-width="1.5"/>')
            for x in (x1, x2):
                out.append(f'<line x1="{x:.1f}" y1="{yc - 5}" x2="{x:.1f}" y2="{yc + 5}" stroke="#111" stroke-width="1.5"/>')
        extra = f" (+{g['incomplete']} incomplete)" if g.get("incomplete") else ""
        out.append(f'<text x="{left + plot_w + 8}" y="{y + bar_h - 3}" fill="#111">{g["passes"]}/{g["n"]}{extra}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---- One experiment as a zip: logs, stories and diagnosis text for every run under that name --------------
MAX_LOG_BYTES = 3_000_000  # a single runaway log must not make a 100-run zip unmanageable


def _slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text or "")).strip("-")[:n] or "x"


def _dup_signature(r: dict):
    """Two rows with the same problem/config/repeat AND the same diagnosis, reasoning and timing are almost
    certainly one result recorded twice (e.g. copied rows), not two independent runs."""
    return (r.get("problem_id"), r.get("config_label"), r.get("action_space"), r.get("repeat_index"),
            r.get("diagnosis_text") or "", r.get("judge_reasoning") or "", r.get("ttd_s"), r.get("total_tokens"))


def experiment_names(runs: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for r in runs:
        n = (r.get("experiment") or "").strip()
        if n:
            counts[n] = counts.get(n, 0) + 1
    return [{"name": n, "runs": c} for n, c in sorted(counts.items())]


def experiment_zip(name: str, runs: list[dict], read_log, read_story) -> tuple[bytes, dict]:
    """`runs` are the flat dashboard rows of ONE experiment. `read_log(run) -> str|None` and
    `read_story(run) -> (str|None, why_not)` are supplied by the caller so this stays testable.
    Never includes keys or .env: only fields already shown in the Results tab are written."""
    runs = sorted(runs, key=lambda r: (r.get("timestamp_utc") or "", r.get("run_id") or ""))
    seen: dict = {}
    rows, report = [], {"runs": len(runs), "with_log": 0, "with_story": 0, "incomplete": 0, "duplicates": 0}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, r in enumerate(runs, 1):
            rid = r.get("run_id") or f"norun{i}"
            folder = f"runs/{i:03d}_{_slug(r.get('problem_id'))}_{_slug(rid, 8)}"
            notes = []
            sig = _dup_signature(r)
            if sig in seen:
                notes.append(f"POSSIBLE DUPLICATE of run {seen[sig]} (identical problem, repeat, diagnosis, judge text and timing)")
                report["duplicates"] += 1
            else:
                seen[sig] = rid
            if r.get("incomplete_reason"):
                notes.append(f"incomplete: {r['incomplete_reason']}")
                report["incomplete"] += 1
            log = read_log(r)
            if log is None:
                notes.append("no log file on this machine")
            else:
                data = log.encode("utf-8", "replace")
                if len(data) > MAX_LOG_BYTES:
                    data = data[:MAX_LOG_BYTES // 2] + b"\n\n----- [log truncated in the middle by the export] -----\n\n" + data[-MAX_LOG_BYTES // 2:]
                z.writestr(f"{folder}/log.txt", data)
                report["with_log"] += 1
            story, why = read_story(r)
            if story:
                z.writestr(f"{folder}/story.md", story)
                report["with_story"] += 1
            else:
                notes.append(f"no story: {why}")
            text = [f"run_id: {rid}", f"problem: {r.get('problem_id')}", f"config: {r.get('config_label')} / {r.get('action_space')}" + (f" / switches: {r.get('agent_switches')}" if r.get('agent_switches') else ""),
                    f"repeat: {r.get('repeat_index')}", f"outcome: diagnosis_pass={r.get('diagnosis_pass')} "
                    f"mitigation_pass={r.get('mitigation_pass')} e2e_pass={r.get('e2e_pass')}",
                    f"judge D1/D2/D3/composite: {r.get('diagnosis_d1')}/{r.get('diagnosis_d2')}/{r.get('diagnosis_d3')}/{r.get('diagnosis_composite')}",
                    f"failure tag: {r.get('failure_mode_tag') or '-'}  review note: {r.get('review_note') or '-'}",
                    f"notes: {'; '.join(notes) or '-'}", "", "=== DIAGNOSIS (agent's answer) ===", r.get("diagnosis_text") or "(empty)",
                    "", "=== JUDGE REASONING ===", r.get("judge_reasoning") or "(none)", "", "=== JUDGE CHECKLIST ===",
                    r.get("judge_checklist") if isinstance(r.get("judge_checklist"), str) else json.dumps(r.get("judge_checklist"), indent=1, default=str)]
            z.writestr(f"{folder}/diagnosis.txt", "\n".join(str(t) for t in text))
            z.writestr(f"{folder}/run.json", json.dumps(r, indent=1, default=str))
            rows.append({**r, "export_folder": folder, "export_notes": "; ".join(notes)})
        z.writestr("runs.csv", _csv(rows, RUN_COLUMNS + ["export_folder", "export_notes"]))
        z.writestr("runs.jsonl", "\n".join(json.dumps(r, default=str) for r in rows) + "\n")
        lines = [f"# Experiment export: {name}", "",
                 f"- runs: {report['runs']}  (logs: {report['with_log']}, stories: {report['with_story']}, "
                 f"incomplete: {report['incomplete']}, possible duplicates: {report['duplicates']})", "",
                 "Layout: `runs.csv` / `runs.jsonl` = one row per run; `runs/<n>_<problem>_<id>/` = `log.txt`, `story.md`, "
                 "`diagnosis.txt`, `run.json`. A missing file is explained in that run's `export_notes` below.", "",
                 "| # | run | problem | repeat | config | notes |", "|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            lines.append(f"| {i} | {str(r.get('run_id'))[:8]} | {r.get('problem_id')} | {r.get('repeat_index')} | "
                         f"{r.get('config_label') or '-'}{(' [' + r['agent_switches'] + ']') if r.get('agent_switches') else ''} | {r['export_notes'] or '-'} |")
        z.writestr("README.md", "\n".join(lines) + "\n")
    return buf.getvalue(), report
