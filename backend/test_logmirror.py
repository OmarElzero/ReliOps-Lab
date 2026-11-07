"""Run: python3 -I backend/test_logmirror.py  (from the repo root; the API tests need fastapi + httpx)"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import firebase_sync  # noqa: E402
import logmirror  # noqa: E402
import runner  # noqa: E402
from test_v2 import TmpRepoCase  # noqa: E402


class FakeDoc:
    def __init__(self, data):
        self._d = data
        self.exists = data is not None

    def to_dict(self):
        return self._d


class FakeCollection:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def document(self, doc_id):
        return FakeRef(self.store, self.name, doc_id)


class FakeRef:
    def __init__(self, store, coll, doc_id):
        self.store, self.key = store, (coll, doc_id)

    def set(self, doc):
        self.store[self.key] = dict(doc)

    def get(self):
        return FakeDoc(self.store.get(self.key))


class FakeClient:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store, name)


class RedactTests(unittest.TestCase):
    def test_keys_tokens_and_secret_lines_are_masked(self):
        # The fake keys are put together here, not written out, so no key-shaped string sits in the repository
        # (GitHub secret scanning flags the shape even when the value is made up).
        google = "AI" + "za" + "SyA1234567890abcdefghijklmnopqrstuv"
        router = "sk-" + "or-v1-" + "abcdefghijklmnopqrstuvwxyz0123456789"
        groq = "gs" + "k_" + "abcdefghijklmnopqrstuvwxyz123456"
        github = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        text = "\n".join([
            f"calling with {google} now",
            f"key {router}",
            groq,
            github,
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "GEMINI_API_KEY=abc123secretvalue",
            "export OPENROUTER_API_KEY='quoted secret'",
            "RELIOPS_DELETE_TOKEN: hunter2",
            "LAB_OWNER_PASSWORD=correct horse",
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----",
        ])
        out = logmirror.redact(text)
        for leaked in ("AIzaSyA1234", "sk-or-v1", "gsk_abcdef", "ghp_abcdef", "abcdefghijklmnopqrstuvwxyz0123456789", "abc123secretvalue",
                       "quoted secret", "hunter2", "correct", "MIIEvQ"):
            self.assertNotIn(leaked, out, leaked)
        self.assertIn("GEMINI_API_KEY=[redacted]", out)
        self.assertIn("Bearer [redacted]", out)

    def test_ordinary_log_text_is_left_alone(self):
        text = "[machine] kind 0.30.0; helm v4.0.2\nrun 3 of 36: ingress_misroute\ntokens=1234 total_tokens: 5678\nkey_slot: 2\n"
        self.assertEqual(logmirror.redact(text), text)


class PackTests(unittest.TestCase):
    def test_round_trip_and_size(self):
        text = "line of the log\n" * 50000
        doc = logmirror.pack(text)
        self.assertFalse(doc["cut"])
        self.assertEqual(logmirror.unpack(doc), text)
        self.assertLess(len(doc["gz"]), logmirror.MAX_B64)

    def test_a_log_that_does_not_fit_keeps_both_ends_and_says_so(self):
        import random
        rnd = random.Random(1)
        text = "START\n" + "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789 \n") for _ in range(400_000)) + "\nEND-OF-LOG\n"
        doc = logmirror.pack(text, max_b64=60_000)
        self.assertTrue(doc["cut"])
        self.assertLessEqual(len(doc["gz"]), 60_000)
        back = logmirror.unpack(doc)
        self.assertTrue(back.startswith("START"))
        self.assertTrue(back.rstrip().endswith("END-OF-LOG"))
        self.assertIn("log cut in the middle", back)

    def test_a_damaged_document_is_no_log(self):
        self.assertIsNone(logmirror.unpack({"gz": "not base64!!"}))
        self.assertIsNone(logmirror.unpack({}))


class FirestoreLogTests(unittest.TestCase):
    def test_write_then_read_by_file_name_and_the_read_is_cached(self):
        fake = FakeClient()
        with mock.patch.object(firebase_sync, "_client", fake), mock.patch.object(firebase_sync, "status", lambda: None), \
             mock.patch.object(firebase_sync, "_touch"), mock.patch.object(firebase_sync, "_spend") as spend:
            firebase_sync._log_cache.clear()
            res = firebase_sync.write_log("1105_1806_psa.log", "hello GEMINI_API_KEY=abc and more", "run1")
            self.assertTrue(res.startswith("written"), res)
            stored = fake.store[("run_logs", "1105_1806_psa.log")]
            self.assertEqual((stored["log_file"], stored["run_id"]), ("1105_1806_psa.log", "run1"))
            self.assertNotIn("abc", logmirror.unpack(stored))                       # redacted before it left
            self.assertIn("hello", firebase_sync.read_log("1105_1806_psa.log"))
            firebase_sync.read_log("1105_1806_psa.log")
            self.assertEqual(spend.call_count, 1)                                   # second read came from the cache
            self.assertIsNone(firebase_sync.read_log("nothing.log"))

    def test_not_connected_is_not_an_error(self):
        with mock.patch.object(firebase_sync, "_client", None), mock.patch.object(firebase_sync, "status", lambda: None):
            firebase_sync._log_cache.clear()
            self.assertIn("not written", firebase_sync.write_log("a.log", "x"))
            self.assertIsNone(firebase_sync.read_log("a.log"))


class LogApiTests(TmpRepoCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            import app as app_module
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"fastapi/httpx not available: {e}")
        self.app_module = app_module
        self.client = TestClient(app_module.app)
        (self.tmp / "results" / "gui_logs").mkdir(parents=True)
        self.fake = FakeClient()
        # TmpRepoCase stubs write_doc to "not written"; this class wants the fake Firestore to receive the document.
        def write_doc(collection, doc_id, doc):
            self.fake.store[(collection, doc_id)] = dict(doc)
            return f"written to Firestore {collection}/{doc_id}"
        p = mock.patch.object(firebase_sync, "write_doc", side_effect=write_doc)
        p.start()
        self.addCleanup(p.stop)
        for target, val in (("_client", self.fake),):
            p = mock.patch.object(firebase_sync, target, val)
            p.start()
            self.addCleanup(p.stop)
        for name in ("_touch", "_spend"):
            p = mock.patch.object(firebase_sync, name)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(firebase_sync, "status", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        firebase_sync._log_cache.clear()

    def test_a_run_shares_its_log_and_a_teammate_can_read_it(self):
        log = self.tmp / "results" / "gui_logs" / "mine.log"
        log.write_text("step 1\nGEMINI_API_KEY=secretsecret\nstep 2\n", encoding="utf-8")
        self.assertTrue(self.app_module._share_log({"log_file": "mine.log", "run_id": "r1"}))
        log.unlink()                                                                # "the other machine" has no local file
        r = self.client.get("/api/log/mine.log")
        self.assertEqual(r.status_code, 200)
        self.assertIn("step 2", r.text)
        self.assertNotIn("secretsecret", r.text)
        self.assertEqual(self.client.get("/api/log/other.log").status_code, 404)
        self.assertEqual(self.client.get("/api/log/..%2Fx").status_code in (400, 404), True)

    def test_off_switch_and_bad_names(self):
        log = self.tmp / "results" / "gui_logs" / "mine.log"
        log.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"RELIOPS_LOG_MIRROR": "0"}):
            self.assertFalse(self.app_module._share_log({"log_file": "mine.log", "run_id": "r1"}))
            self.assertEqual(self.client.post("/api/logs/share-existing").status_code, 400)
        self.assertEqual(self.fake.store, {})
        for bad in ("../x.log", "a/b.log", ""):
            self.assertFalse(self.app_module._share_log({"log_file": bad}))
        self.assertFalse(self.app_module._share_log({"log_file": "missing.log"}))

    def test_share_existing_counts_what_it_sent(self):
        (self.tmp / "results" / "gui_logs" / "a.log").write_text("a", encoding="utf-8")
        recs = [{"run_id": "1", "log_file": "a.log"}, {"run_id": "2", "log_file": "gone.log"}, {"run_id": "3", "log_file": ""}]
        with mock.patch.object(runner, "list_local_runs", return_value=recs):
            r = self.client.post("/api/logs/share-existing")
        self.assertEqual((r.json()["shared"], r.json()["skipped"]), (1, 2))
        self.assertIn(("run_logs", "a.log"), self.fake.store)

    def test_the_export_falls_back_to_the_shared_log(self):
        firebase_sync.write_log("theirs.log", "teammate log text", "r9")
        run = {"run_id": "r9", "experiment": "E", "problem_id": "p", "log_file": "theirs.log", "run_by": "Noura", "timestamp_utc": "2026-11-05T00:00:00Z"}
        with mock.patch.object(self.app_module, "_collect_runs", return_value=("local", [run])), \
             mock.patch.object(runner, "list_local_runs", return_value=[]):
            r = self.client.get("/api/export/experiment.zip", params={"name": "E"})
        self.assertEqual(r.status_code, 200, r.text[:200])
        import io
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(r.content))
        logs = [n for n in z.namelist() if n.endswith("/log.txt")]
        self.assertEqual(len(logs), 1)
        self.assertIn("teammate log text", z.read(logs[0]).decode())


if __name__ == "__main__":
    unittest.main()
