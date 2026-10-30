"""Cluster health for the Setup tab: a read-only status check and three fixed repairs.

Why this is not a terminal. The Lab is a web server on this machine; an endpoint that ran whatever text it was
given would be code execution for any page that managed to reach it. So the browser never sends a command. It sends
the name of one of the actions below, with a confirmation flag, and the server builds the command itself from names
it just read from `docker` / `kubectl` and checked against a pattern.

Everything here talks to the local kind cluster the benchmark uses (default cluster name "kind", node containers
"kind-control-plane", "kind-worker", "kind-worker2", ...).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import runner

NODE_CONTAINER_RE = re.compile(r"^kind-(?:control-plane\d*|worker\d*)$")
K8S_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
STUCK_AFTER_S = 60          # a pod this long in Terminating is stuck, not just slow
LOW_MEM_MB = 1500
LOW_DISK_GB = 10
READY_WAIT_S = 240          # how long "restart nodes" waits for every node to report Ready

ACTION_IDS = ("restart_nodes", "clear_stuck_pods", "rebuild")


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str] | None:
    """(exit code, stdout), or None when the program is not installed. A timeout counts as exit code 124."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return 124, ""
    return r.returncode, r.stdout


def _kubectl_json(args: list[str]) -> tuple[dict | None, str]:
    """(parsed JSON, "") on success; (None, why) otherwise."""
    out = _run(["kubectl", *args, "-o", "json", "--request-timeout=10s"])
    if out is None:
        return None, "The kubectl program is not installed on this machine"
    rc, text = out
    if rc != 0:
        return None, "No cluster answered kubectl"
    try:
        return json.loads(text), ""
    except ValueError:
        return None, "kubectl returned something unreadable"


def nodes() -> tuple[list[dict] | None, str]:
    data, why = _kubectl_json(["get", "nodes"])
    if data is None:
        return None, why
    rows = []
    for item in data.get("items", []):
        meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
        cond = next((c for c in status.get("conditions", []) if c.get("type") == "Ready"), {})
        ready = {"True": "Ready", "False": "NotReady"}.get(cond.get("status"), "Unknown")
        roles = sorted(k.split("/", 1)[1] for k in (meta.get("labels") or {}) if k.startswith("node-role.kubernetes.io/"))
        rows.append({"name": meta.get("name", "?"), "status": ready, "roles": roles or ["worker"],
                     "cordoned": bool(spec.get("unschedulable")), "reason": cond.get("reason") or ""})
    return rows, ""


def _age_s(stamp: str | None, now: float) -> int | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int(now - t.timestamp()))


def system_pods(now: float | None = None) -> tuple[list[dict] | None, str]:
    """Pods in kube-system that are not Ready or are stuck being deleted."""
    now = time.time() if now is None else now
    data, why = _kubectl_json(["-n", "kube-system", "get", "pods"])
    if data is None:
        return None, why
    out = []
    for item in data.get("items", []):
        meta, status = item.get("metadata", {}), item.get("status", {})
        if status.get("phase") == "Succeeded":
            continue
        ready = next((c.get("status") for c in status.get("conditions", []) if c.get("type") == "Ready"), None)
        terminating_for = _age_s(meta.get("deletionTimestamp"), now)
        if terminating_for is None and ready == "True":
            continue
        out.append({"name": meta.get("name", "?"), "node": item.get("spec", {}).get("nodeName") or "",
                    "phase": status.get("phase") or "", "ready": ready == "True",
                    "terminating_for_s": terminating_for})
    return out, ""


def kind_containers() -> list[dict] | None:
    """The kind node containers, started or not. None when docker is not installed or not answering."""
    out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if out is None or out[0] != 0:
        return None
    rows = []
    for line in out[1].splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and NODE_CONTAINER_RE.match(parts[0]):
            rows.append({"name": parts[0], "state": parts[1], "status": parts[2] if len(parts) > 2 else ""})
    return rows


def host_resources(path) -> dict:
    mem_mb = total_mb = None
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0])
        mem_mb, total_mb = info["MemAvailable"] // 1024, info["MemTotal"] // 1024
    except (OSError, KeyError, ValueError, IndexError):
        pass
    disk_free_gb = None
    try:
        disk_free_gb = round(shutil.disk_usage(path).free / 1e9, 1)
    except OSError:
        pass
    return {"mem_available_mb": mem_mb, "mem_total_mb": total_mb, "disk_free_gb": disk_free_gb}


def _plain(items: list[str]) -> str:
    return ", ".join(items)


def status(busy: bool = False, now: float | None = None) -> dict:
    """What the panel shows: facts, plain-language findings (bad / warn / ok), and which repairs make sense now."""
    now = time.time() if now is None else now
    node_rows, node_why = nodes()
    pod_rows, _ = system_pods(now) if node_rows is not None else (None, "")
    containers = kind_containers()
    host = host_resources(runner.reliops_dir())
    findings: list[dict] = []

    def add(level: str, text: str) -> None:
        findings.append({"level": level, "text": text})

    stopped = [c["name"] for c in (containers or []) if c["state"] != "running"]
    if stopped:
        add("bad", f"Node container{'s' if len(stopped) > 1 else ''} not running: {_plain(stopped)}.")
    if node_rows is None:
        add("bad" if containers else "warn", f"{node_why}." + (
            "" if "not installed" in node_why else " Is the kind cluster up? Run setup if it was never built."))
    else:
        bad_nodes = [f"{n['name']} ({n['status']})" for n in node_rows if n["status"] != "Ready"]
        if bad_nodes:
            add("bad", f"Nodes not Ready: {_plain(bad_nodes)}. A node whose kubelet stopped answering looks like this.")
        stuck = [p for p in (pod_rows or []) if (p["terminating_for_s"] or 0) >= STUCK_AFTER_S]
        if stuck:
            add("bad", "Pods stuck in Terminating in kube-system: " + _plain(f"{p['name']} on {p['node'] or '?'}" for p in stuck) + ".")
        unready = [p for p in (pod_rows or []) if p["terminating_for_s"] is None and not p["ready"]]
        if unready:
            add("warn", "kube-system pods not Ready: " + _plain(p["name"] for p in unready) + ".")
        if not bad_nodes and not stuck and not unready:
            add("ok", f"All {len(node_rows)} nodes are Ready and kube-system looks fine.")
    if host["mem_available_mb"] is not None and host["mem_available_mb"] < LOW_MEM_MB:
        add("warn", f"Only {host['mem_available_mb']} MB of memory is free. A 4-node kind cluster plus an app needs several GB; "
                    "if you use WSL or Docker Desktop, check the memory limit given to it.")
    if host["disk_free_gb"] is not None and host["disk_free_gb"] < LOW_DISK_GB:
        add("warn", f"Only {host['disk_free_gb']} GB of disk is free on the drive holding this repo; kubelets report disk pressure when it runs low.")

    verdict = ("problem" if any(f["level"] == "bad" for f in findings)
               else "unknown" if node_rows is None else "ok")
    names = [c["name"] for c in (containers or [])]
    stuck_names = [p["name"] for p in (pod_rows or []) if (p["terminating_for_s"] or 0) >= STUCK_AFTER_S and K8S_NAME_RE.match(p["name"])]
    can_kind = bool(shutil.which("kind"))
    busy_why = "A run, queue or other job is running; wait for it or stop it first."

    def action(id_: str, title: str, effect: str, commands: list[str], usable: bool, why_not: str) -> dict:
        return {"id": id_, "title": title, "effect": effect, "commands": commands,
                "enabled": usable and not busy, "disabled_reason": busy_why if busy else ("" if usable else why_not)}

    actions = [
        action("restart_nodes", "Restart the kind nodes",
               "Restarts the node containers, then waits up to 4 minutes for every node to report Ready. Nothing is deleted.",
               ["docker restart " + " ".join(names)] if names else [], bool(names), "No kind node containers were found."),
        action("clear_stuck_pods", "Clear stuck system pods",
               "Force-deletes pods that have been stuck in Terminating for over a minute in kube-system only. "
               "Their controllers create fresh ones.",
               [f"kubectl -n kube-system delete pod {n} --force --grace-period=0" for n in stuck_names], bool(stuck_names),
               "No pod in kube-system is stuck in Terminating."),
        action("rebuild", "Rebuild the cluster",
               "Deletes the kind cluster and everything in it, then runs setup again (about 10 minutes). Use it when restarting does not help.",
               ["kind delete cluster", "bash scripts/quickstart.sh --setup-only"], can_kind, "The kind program was not found."),
    ]
    advice = ""
    if verdict == "problem":
        advice = ("Try 'Restart the kind nodes' first. If a node is still NotReady after it, or the problem comes back, use 'Rebuild the cluster'."
                  if any("not Ready" in f["text"] or "not running" in f["text"] for f in findings if f["level"] == "bad")
                  else "Try 'Clear stuck system pods', then check again.")
    return {"verdict": verdict, "findings": findings, "nodes": node_rows, "system_pods": pod_rows, "containers": containers,
            "host": host, "actions": actions, "advice": advice, "checked_at": now}


# ---- the three repairs (each is a generator of log lines, run as a job so only one thing touches the cluster) ----

def repair_lines(action_id: str) -> Iterator[str]:
    if action_id == "restart_nodes":
        yield from _restart_nodes()
    elif action_id == "clear_stuck_pods":
        yield from _clear_stuck_pods()
    elif action_id == "rebuild":
        yield from _rebuild()
    else:
        yield f"Unknown repair: {action_id}\n"


def _restart_nodes() -> Iterator[str]:
    containers = kind_containers()
    names = [c["name"] for c in (containers or [])]
    if not names:
        yield "No kind node containers found (docker ps -a). Nothing to restart.\n"
        return
    result: list = []
    yield from runner._stream(["docker", "restart", *names], result=result)
    if result and result[0] != 0:
        yield "docker restart reported an error; see above. Not waiting for the nodes.\n"
        return
    deadline = time.time() + READY_WAIT_S
    yield f"Waiting up to {READY_WAIT_S // 60} minutes for every node to report Ready...\n"
    while time.time() < deadline:
        bad = runner.cluster_node_problems()
        if bad is not None and not bad:
            yield "All nodes are Ready. kube-system pods can take another minute to settle; run the check again to see.\n"
            return
        yield "  still waiting: " + ("the cluster is not answering yet" if bad is None else ", ".join(bad)) + "\n"
        time.sleep(15)
    yield ("Some nodes are still not Ready after the wait. If this repeats, use 'Rebuild the cluster'.\n")


def _clear_stuck_pods() -> Iterator[str]:
    pods, why = system_pods()
    if pods is None:
        yield f"{why}. Nothing done.\n"
        return
    stuck = [p["name"] for p in pods if (p["terminating_for_s"] or 0) >= STUCK_AFTER_S and K8S_NAME_RE.match(p["name"])]
    if not stuck:
        yield "No pod in kube-system is stuck in Terminating.\n"
        return
    for name in stuck:
        yield from runner._stream(["kubectl", "-n", "kube-system", "delete", "pod", name, "--force", "--grace-period=0"])
    yield "Done. Run the check again in a minute to see whether replacements came up.\n"


def _rebuild() -> Iterator[str]:
    if not shutil.which("kind"):
        yield "The kind program was not found, so the cluster cannot be deleted from here. Nothing done.\n"
        return
    result: list = []
    yield "Deleting the kind cluster, then running setup again. This takes about 10 minutes.\n"
    yield from runner._stream(["kind", "delete", "cluster"], result=result)
    if result and result[0] != 0:
        yield "kind delete cluster failed; not rebuilding. See the lines above.\n"
        return
    yield from runner.stream_setup()
