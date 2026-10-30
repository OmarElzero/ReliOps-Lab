"""Run: python3 -I backend/test_cluster.py  (the API tests need fastapi + httpx)"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import cluster  # noqa: E402
import runner  # noqa: E402

NOW = 1_793_000_000.0   # any fixed moment


def node(name, ready, roles=("worker",), cordoned=False):
    return {"metadata": {"name": name, "labels": {f"node-role.kubernetes.io/{r}": "" for r in roles if r != "worker"}},
            "spec": {"unschedulable": True} if cordoned else {},
            "status": {"conditions": [{"type": "Ready", "status": ready, "reason": "KubeletNotReady" if ready != "True" else "KubeletReady"}]}}


def pod(name, node_name, ready="True", phase="Running", deleted_ago=None):
    meta = {"name": name}
    if deleted_ago is not None:
        meta["deletionTimestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - deleted_ago))
    return {"metadata": meta, "spec": {"nodeName": node_name},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": ready}]}}


def fake_run(nodes_items, pods_items, containers=None, kubectl=True):
    """Stands in for cluster._run: answers `kubectl get nodes`, `kubectl get pods` and `docker ps` from fixtures."""
    def _run(cmd, timeout=15):
        if cmd[0] == "kubectl":
            if not kubectl:
                return None
            body = {"items": pods_items if "pods" in cmd else nodes_items}
            return 0, json.dumps(body)
        if cmd[0] == "docker":
            if containers is None:
                return None
            if cmd[1] == "exec":
                return 0, f"{cmd[2]} kubelet[1]: failed to run Kubelet: disk pressure\n"
            return 0, "".join(f"{n}\t{s}\tUp 2 hours\n" for n, s in containers)
        return None
    return _run


def fake_stream(calls, code=0):
    def _s(cmd, cwd=None, env=None, result=None, capture=None):
        calls.append(list(cmd))
        if result is not None:
            result.append(code)
        yield "$ " + " ".join(cmd) + "\n"
    return _s


class StatusTests(unittest.TestCase):
    def _status(self, run, busy=False, kind=True):
        with mock.patch.object(cluster, "_run", side_effect=run), \
             mock.patch.object(cluster.shutil, "which", return_value="/usr/bin/kind" if kind else None), \
             mock.patch.object(cluster, "host_resources", return_value={"mem_available_mb": 9000, "mem_total_mb": 16000, "disk_free_gb": 100.0}):
            return cluster.status(busy=busy, now=NOW)

    HEALTHY_NODES = [node("kind-control-plane", "True", ("control-plane",)), node("kind-worker", "True"), node("kind-worker2", "True")]
    ALL_UP = [("kind-control-plane", "running"), ("kind-worker", "running"), ("kind-worker2", "running")]

    def test_a_healthy_cluster(self):
        s = self._status(fake_run(self.HEALTHY_NODES, [pod("coredns-1", "kind-worker")], self.ALL_UP))
        self.assertEqual(s["verdict"], "ok")
        self.assertEqual([f["level"] for f in s["findings"]], ["ok"])
        self.assertFalse(next(a for a in s["actions"] if a["id"] == "clear_stuck_pods")["enabled"])

    def test_the_broken_cluster_from_the_real_log(self):
        # kind-worker3 stopped answering; coredns and metrics-server on it were stuck in Terminating for 7 minutes.
        nodes_ = self.HEALTHY_NODES + [node("kind-worker3", "Unknown")]
        pods = [pod("coredns-d5ff7974-lzh2x", "kind-worker3", ready="False", deleted_ago=420),
                pod("metrics-server-676dc447b9-cmqg4", "kind-worker3", ready="False", deleted_ago=420),
                pod("etcd-kind-control-plane", "kind-control-plane")]
        s = self._status(fake_run(nodes_, pods, self.ALL_UP + [("kind-worker3", "running")]))
        self.assertEqual(s["verdict"], "problem")
        text = " ".join(f["text"] for f in s["findings"])
        self.assertIn("kind-worker3 (Unknown)", text)
        self.assertIn("coredns-d5ff7974-lzh2x on kind-worker3", text)
        by_id = {a["id"]: a for a in s["actions"]}
        self.assertTrue(by_id["restart_nodes"]["enabled"])
        self.assertTrue(by_id["clear_stuck_pods"]["enabled"])
        self.assertEqual(len(by_id["clear_stuck_pods"]["commands"]), 2)
        self.assertIn("docker restart kind-control-plane kind-worker kind-worker2 kind-worker3", by_id["restart_nodes"]["commands"][0])
        self.assertIn("Restart the kind nodes", s["advice"])

    def test_a_node_that_is_not_ready_comes_with_its_kubelet_log_and_a_bad_name_never_reaches_docker(self):
        nodes_ = self.HEALTHY_NODES + [node("kind-worker3", "Unknown")]
        s = self._status(fake_run(nodes_, [], self.ALL_UP + [("kind-worker3", "running")]))
        self.assertEqual(list(s["kubelet_logs"]), ["kind-worker3"])
        self.assertIn("disk pressure", s["kubelet_logs"]["kind-worker3"])
        with mock.patch.object(cluster, "_run") as run:
            self.assertIsNone(cluster.kubelet_log_tail("kind-worker3; rm -rf /"))
        run.assert_not_called()

    def test_a_pod_deleted_a_few_seconds_ago_is_not_stuck(self):
        s = self._status(fake_run(self.HEALTHY_NODES, [pod("coredns-1", "kind-worker", ready="False", deleted_ago=5)], self.ALL_UP))
        self.assertFalse(next(a for a in s["actions"] if a["id"] == "clear_stuck_pods")["enabled"])

    def test_a_stopped_node_container_is_reported(self):
        s = self._status(fake_run(self.HEALTHY_NODES, [], [("kind-control-plane", "running"), ("kind-worker", "exited")]))
        self.assertEqual(s["verdict"], "problem")
        self.assertIn("kind-worker", s["findings"][0]["text"])

    def test_no_kubectl_or_no_cluster_is_unknown_not_a_crash(self):
        s = self._status(fake_run([], [], None, kubectl=False))
        self.assertEqual(s["verdict"], "unknown")
        self.assertIn("kubectl program is not installed", s["findings"][0]["text"])

    def test_repairs_are_disabled_while_a_job_runs(self):
        nodes_ = self.HEALTHY_NODES + [node("kind-worker3", "False")]
        s = self._status(fake_run(nodes_, [], self.ALL_UP + [("kind-worker3", "running")]), busy=True)
        self.assertTrue(all(not a["enabled"] for a in s["actions"]))
        self.assertIn("running", s["actions"][0]["disabled_reason"])

    def test_rebuild_needs_the_kind_program(self):
        s = self._status(fake_run(self.HEALTHY_NODES, [], self.ALL_UP), kind=False)
        self.assertFalse(next(a for a in s["actions"] if a["id"] == "rebuild")["enabled"])

    def test_low_memory_and_disk_are_warnings(self):
        with mock.patch.object(cluster, "_run", side_effect=fake_run(self.HEALTHY_NODES, [], self.ALL_UP)), \
             mock.patch.object(cluster, "host_resources", return_value={"mem_available_mb": 600, "mem_total_mb": 16000, "disk_free_gb": 4.0}):
            s = cluster.status(now=NOW)
        levels = [f["level"] for f in s["findings"]]
        self.assertEqual(s["verdict"], "ok")      # warnings alone are not a broken cluster
        self.assertEqual(levels.count("warn"), 2)


class RepairTests(unittest.TestCase):
    def test_restart_names_come_from_docker_and_it_waits_for_ready(self):
        calls = []
        containers = [{"name": "kind-control-plane", "state": "running", "status": ""}, {"name": "kind-worker3", "state": "exited", "status": ""}]
        with mock.patch.object(cluster, "kind_containers", return_value=containers), \
             mock.patch.object(runner, "_stream", side_effect=fake_stream(calls)), \
             mock.patch.object(runner, "cluster_node_problems", side_effect=[None, ["kind-worker3 (NotReady)"], []]), \
             mock.patch.object(cluster.time, "sleep"):
            out = "".join(cluster.repair_lines("restart_nodes"))
        self.assertEqual(calls, [["docker", "restart", "kind-control-plane", "kind-worker3"]])
        self.assertIn("still waiting: the cluster is not answering yet", out)
        self.assertIn("still waiting: kind-worker3 (NotReady)", out)
        self.assertIn("All nodes are Ready", out)

    def test_restart_with_no_containers_does_nothing(self):
        calls = []
        with mock.patch.object(cluster, "kind_containers", return_value=[]), mock.patch.object(runner, "_stream", side_effect=fake_stream(calls)):
            out = "".join(cluster.repair_lines("restart_nodes"))
        self.assertEqual(calls, [])
        self.assertIn("Nothing to restart", out)

    def test_clear_stuck_pods_touches_only_stuck_kube_system_pods_with_valid_names(self):
        calls = []
        pods = [{"name": "coredns-1", "node": "w", "phase": "Running", "ready": False, "terminating_for_s": 300},
                {"name": "fresh-1", "node": "w", "phase": "Running", "ready": False, "terminating_for_s": 10},
                {"name": "bad name; rm -rf /", "node": "w", "phase": "Running", "ready": False, "terminating_for_s": 900},
                {"name": "not-ready-1", "node": "w", "phase": "Running", "ready": False, "terminating_for_s": None}]
        with mock.patch.object(cluster, "system_pods", return_value=(pods, "")), mock.patch.object(runner, "_stream", side_effect=fake_stream(calls)):
            "".join(cluster.repair_lines("clear_stuck_pods"))
        self.assertEqual(calls, [["kubectl", "-n", "kube-system", "delete", "pod", "coredns-1", "--force", "--grace-period=0"]])

    def test_rebuild_deletes_then_runs_setup_and_stops_if_the_delete_fails(self):
        calls = []
        with mock.patch.object(cluster.shutil, "which", return_value="/usr/bin/kind"), \
             mock.patch.object(runner, "_stream", side_effect=fake_stream(calls, code=0)), \
             mock.patch.object(runner, "stream_setup", return_value=iter(["setup ran\n"])):
            out = "".join(cluster.repair_lines("rebuild"))
        self.assertEqual(calls, [["kind", "delete", "cluster"]])
        self.assertIn("setup ran", out)
        calls.clear()
        with mock.patch.object(cluster.shutil, "which", return_value="/usr/bin/kind"), \
             mock.patch.object(runner, "_stream", side_effect=fake_stream(calls, code=1)), \
             mock.patch.object(runner, "stream_setup", return_value=iter(["setup ran\n"])):
            out = "".join(cluster.repair_lines("rebuild"))
        self.assertNotIn("setup ran", out)
        self.assertIn("not rebuilding", out)

    def test_an_unknown_repair_runs_nothing(self):
        calls = []
        with mock.patch.object(runner, "_stream", side_effect=fake_stream(calls)):
            out = "".join(cluster.repair_lines("rm -rf /"))
        self.assertEqual(calls, [])
        self.assertIn("Unknown repair", out)


class RepairApiTests(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.app_module = app_module
        self.client = TestClient(app_module.app)

    def test_unknown_action_and_missing_confirmation_are_refused(self):
        r = self.client.post("/api/cluster/repair", json={"action": "format c:", "confirm": True})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/cluster/repair", json={"action": "rebuild"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("confirmation", r.json()["error"])

    def test_refused_while_another_job_is_running(self):
        with mock.patch.object(self.app_module.jobs, "start", return_value=None) as start:
            r = self.client.post("/api/cluster/repair", json={"action": "restart_nodes", "confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(start.call_args.args[0], "cluster")

    def test_a_confirmed_known_repair_starts_a_cluster_job(self):
        fake = mock.Mock(id="abc123")
        with mock.patch.object(self.app_module.jobs, "start", return_value=fake) as start:
            r = self.client.post("/api/cluster/repair", json={"action": "restart_nodes", "confirm": True})
        self.assertEqual(r.json(), {"ok": True, "job_id": "abc123"})
        self.assertEqual(start.call_args.args[1], "Restart the kind nodes")

    def test_status_endpoint_answers_even_without_a_cluster(self):
        with mock.patch.object(cluster, "_run", return_value=None):
            r = self.client.get("/api/cluster")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["verdict"], "unknown")


if __name__ == "__main__":
    unittest.main()
