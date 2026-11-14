"""The Tasks tab: what each person has to do now, written by the owner of the project and read by everyone.

A task says what to do (title, detail), under which phase, for whom (a list of names; empty means everyone), and optionally
which saved queue and experiment name it belongs to, so the team opens the right run with one click. Each person marks
their own progress on a task (todo, doing, done, blocked, with a note). Tasks and progress are two small stores that are
mirrored to the other machines like every other store in the Lab (store.py)."""

from __future__ import annotations

import uuid

STATUSES = ("todo", "doing", "done", "blocked")
# Set by the owner on the task itself (not per person): active, on hold for now, or not needed any more.
STATES = ("active", "on_hold", "not_needed")
LIMITS = {"title": 200, "detail": 2000, "phase": 80, "experiment": 200, "due": 40, "name": 60, "note": 500}
MAX_ASSIGNEES = 20


def _text(value, key: str) -> str:
    return " ".join(str(value or "").split())[: LIMITS[key]] if key != "detail" and key != "note" else str(value or "").strip()[: LIMITS[key]]


def clean_task(d: dict, task_id: str | None = None, created_by: str = "") -> dict:
    """A task as stored. Raises ValueError with a sentence the page can show. `urgent` and `state` default to the
    ordinary values when the caller does not send them, so tasks written before they existed keep working."""
    title = _text(d.get("title"), "title")
    if not title:
        raise ValueError("a task needs a title")
    names: list[str] = []
    for n in d.get("assignees") or []:
        n = _text(n, "name")
        if n and n.lower() not in {x.lower() for x in names}:
            names.append(n)
    if len(names) > MAX_ASSIGNEES:
        raise ValueError(f"at most {MAX_ASSIGNEES} names per task")
    state = str(d.get("state") or "active")
    if state not in STATES:
        raise ValueError(f"state must be one of: {', '.join(STATES)}")
    return {"task_id": task_id or uuid.uuid4().hex[:8], "title": title, "urgent": bool(d.get("urgent")), "state": state, "detail": _text(d.get("detail"), "detail"),
            "phase": _text(d.get("phase"), "phase"), "assignees": names,
            "queue_id": _text(d.get("queue_id"), "name"), "experiment": _text(d.get("experiment"), "experiment"),
            "due": _text(d.get("due"), "due"), "created_by": created_by, "deleted": False}


def is_live(task: dict) -> bool:
    """A task someone should be working on now: not deleted, not on hold, not marked as not needed."""
    return not task.get("deleted") and (task.get("state") or "active") == "active"


def is_for(task: dict, who: str) -> bool:
    """A task with no names is for everyone."""
    names = [n.lower() for n in task.get("assignees") or []]
    return not names or (who or "").strip().lower() in names


def clean_progress(task: dict, who: str, status: str, note: str = "") -> dict:
    who = _text(who, "name")
    if not who:
        raise ValueError("type your name in the box at the top first")
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    if not is_for(task, who):
        raise ValueError(f"this task is for {', '.join(task['assignees'])}; ask the owner to add you")
    return {"task_id": task["task_id"], "who_key": who.lower(), "who": who, "status": status, "note": _text(note, "note")}


def open_count(tasks: list[dict], progress: list[dict], who: str) -> int:
    """Tasks for `who` that are not done yet."""
    mine = {p["task_id"]: p for p in progress if (p.get("who_key") or "") == (who or "").strip().lower()}
    return sum(1 for t in tasks if is_live(t) and is_for(t, who) and (mine.get(t["task_id"]) or {}).get("status") != "done")
