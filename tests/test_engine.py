"""End-to-end engine test with the LLM forced OFF (rule fallbacks), so it runs anywhere in <1 s.

    .venv/bin/python -m unittest tests.test_engine
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import llm  # noqa: E402
from engine import agents, report  # noqa: E402
from engine.orchestrator import run_investigation  # noqa: E402
from engine.simulator import SimulatedEnvironment, verify  # noqa: E402

DESC = "Checkout is down, customers get HTTP 500s since the 2pm deploy of checkout 2.3. Call me at (415) 555-0132."


def no_llm(*a, **k):
    return None, {"model": "none", "ok": False, "seconds": 0, "error": "disabled in test", "local": True}


class TestEngineOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig = llm.chat_json
        agents.llm.chat_json = report.llm.chat_json = no_llm
        cls.inv = run_investigation(DESC, model="none")

    @classmethod
    def tearDownClass(cls):
        agents.llm.chat_json = report.llm.chat_json = cls._orig

    def test_description_scrubbed(self):
        self.assertIn("[PHONE]", self.inv["description"])
        self.assertNotIn("555-0132", self.inv["description"])

    def test_triage_and_onset(self):
        self.assertEqual(self.inv["triage"]["affected_service"], "checkout")
        self.assertEqual(self.inv["logs"]["onset"].strftime("%H"), "14")

    def test_root_cause_and_recommendation(self):
        top = self.inv["rca"]["hypotheses"][0]
        self.assertEqual(top["id"], "H-config-db.pool.max_size")
        self.assertEqual(self.inv["rca"]["recommended_action_id"], "A-revert-db.pool.max_size")
        self.assertEqual({h["id"] for h in self.inv["history"]["hits"][:3]} & {"INC-2025-014"}, {"INC-2025-014"})

    def test_simulator_only_real_fix_verifies(self):
        results = {}
        for a in self.inv["actions"]:
            env = SimulatedEnvironment(self.inv["env"]["deploys"], self.inv["env"]["services"])
            env.execute(a)
            results[a["id"]] = verify([env.health_check("checkout") for _ in range(8)])["passed"]
        self.assertTrue(results["A-revert-db.pool.max_size"])
        self.assertTrue(results["A-rollback-checkout-v2.2"])
        self.assertFalse(results["A-restart-checkout"])
        self.assertFalse(results["A-scale-checkout"])
        self.assertFalse(results["A-revert-db.pool.idle_timeout_s"])

    def test_report_fallback(self):
        env = SimulatedEnvironment(self.inv["env"]["deploys"], self.inv["env"]["services"])
        a = next(x for x in self.inv["actions"] if x["id"] == self.inv["rca"]["recommended_action_id"])
        log = env.execute(a)
        samples = [env.health_check("checkout") for _ in range(8)]
        ex = [{"at": samples[-1]["t"], "action": a, "approver": "test", "log": log, "samples": samples,
               "verification": verify(samples)}]
        md, _ = report.build_report(self.inv, ex, [], "none")
        self.assertIn("RESOLVED (verified)", md)
        self.assertIn("db.pool.max_size", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTalkBack(unittest.TestCase):
    """Gradium TTS summary: speakable, passes the leak gate, fails cleanly without a key."""

    def test_summary_is_speakable_and_gate_clean(self):
        from engine import tts
        from privacy_layer import find_residual, scrub
        agents.llm.chat_json = no_llm
        try:
            inv = run_investigation(DESC, model="none")
        finally:
            agents.llm.chat_json = TestEngineOffline._orig if hasattr(TestEngineOffline, "_orig") else llm.chat_json
        env = SimulatedEnvironment(inv["env"]["deploys"], inv["env"]["services"])
        a = next(x for x in inv["actions"] if x["id"] == inv["rca"]["recommended_action_id"])
        pre = env.snapshot("checkout")
        env.execute(a)
        samples = [pre] + [env.health_check("checkout") for _ in range(8)]
        text = tts.spoken_summary(inv, {"samples": samples, "action": a, "approver": "on-call engineer"})
        self.assertIn("database pool max size", text)
        self.assertNotRegex(text, r"H-|A-|db\.|_|→|\*\*")
        self.assertEqual(find_residual(text), [])
        self.assertEqual(scrub(text)[0], text)

    def test_speak_without_key_raises(self):
        import os
        from engine import tts
        old = os.environ.pop("GRADIUM_API_KEY", None)
        try:
            with self.assertRaises(tts.TTSError):
                tts.speak("hello")
        finally:
            if old is not None:
                os.environ["GRADIUM_API_KEY"] = old
