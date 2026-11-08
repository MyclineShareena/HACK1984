"""Egress gate and incident search tests.   python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not (ROOT / "data" / "planted_secrets.json").exists():  # synthetic logs are generated, not committed
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "generate_data.py")], check=True, capture_output=True)
from privacy_layer import EgressBlocked, log_egress, read_log, search_incidents  # noqa: E402


class TestEgress(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "egress.jsonl"

    def test_scrubs_and_logs_exact_payload(self):
        safe = log_egress("gradium", "checkout down, user bob@example.com from 10.1.2.3", log_path=self.log)
        self.assertEqual(safe, "checkout down, user [EMAIL] from [IP]")
        (rec,) = read_log(self.log)
        self.assertFalse(rec["blocked"])
        self.assertEqual(rec["payload"], safe)
        self.assertEqual(rec["bytes"], len(safe.encode()))
        self.assertEqual(rec["scrubbed"], {"EMAIL": 1, "IP": 1})

    def test_dict_payload(self):
        safe = log_egress("llm", {"messages": [{"content": "key sk_live_abcdefgh12345678"}]}, log_path=self.log)
        self.assertEqual(safe["messages"][0]["content"], "key [API_KEY]")

    def test_blocks_residual_and_does_not_log_payload(self):
        for bad in ["db password=hunter2", "key sk_live_abc", "ssn 123-45-6789"]:
            with self.assertRaises(EgressBlocked):
                log_egress("llm", bad, log_path=self.log)
        recs = read_log(self.log)
        self.assertTrue(all(r["blocked"] and "payload" not in r for r in recs))
        self.assertNotIn("hunter2", self.log.read_text())

    def test_binary(self):
        audio = b"\xff\xfb\x90\x00" * 10
        with self.assertRaises(EgressBlocked):
            log_egress("gradium", audio, log_path=self.log)
        self.assertEqual(log_egress("gradium", audio, allow_binary=True, log_path=self.log), audio)
        self.assertIn("sha256", read_log(self.log)[-1])

    def test_whole_log_files_pass_gate(self):
        for f in ("checkout", "payments", "inventory"):
            text = (ROOT / "data" / "logs" / f"{f}.log").read_text()
            safe = log_egress("llm", text, log_path=self.log)
            for t in json.loads((ROOT / "data" / "planted_secrets.json").read_text()):
                self.assertNotIn(t["value"], safe)


class TestSearch(unittest.TestCase):
    def test_pool_incidents_rank_first(self):
        hits = search_incidents("checkout HTTP 500 after deploy v2.3, connection pool exhausted, timeouts", k=3)
        self.assertEqual({h["id"] for h in hits[:2]}, {"INC-2025-014", "INC-2026-003"})
        self.assertGreater(hits[0]["score"], hits[-1]["score"])
        self.assertTrue(hits[0]["root_cause"] and hits[0]["fix"])

    def test_other_topics(self):
        self.assertEqual(search_incidents("TLS certificate expired handshake", k=1)[0]["id"], "INC-2025-027")
        self.assertEqual(search_incidents("zzzz qqqq"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
