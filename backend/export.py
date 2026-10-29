"""Downloads for papers and slides: tidy CSVs and a dependency-free SVG bar chart with Wilson error bars."""

from __future__ import annotations

import csv
import io
from xml.sax.saxutils import escape

RUN_COLUMNS = [
    "run_id", "timestamp_utc", "run_by", "problem_id", "repeat_index", "repeat_n", "agent_id", "model_id",
    "action_space", "config_label", "run_class", "manifest_id", "judge_model_id", "diagnosis_pass",
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
        out.append(f'<text x="{left + plot_w + 8}" y="{y + bar_h - 3}" fill="#111">{g["passes"]}/{g["n"]}'
                   f'{f" (+{g["incomplete"]} incomplete)" if g.get("incomplete") else ""}</text>')
    out.append("</svg>")
    return "\n".join(out)
