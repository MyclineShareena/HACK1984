"""Scrubber recall on the synthetic logs (every planted secret must be gone) + edge cases.

    python tests/test_scrubber.py        # prints recall per type
    python -m unittest discover tests
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not (ROOT / "data" / "planted_secrets.json").exists():  # synthetic logs are generated, not committed
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "generate_data.py")], check=True, capture_output=True)
from privacy_layer.scrubber import TYPES, scrub  # noqa: E402

DATA = ROOT / "data"


def recall_report():
    truth = json.loads((DATA / "planted_secrets.json").read_text())
    lines = {}
    for f in ("checkout", "payments", "inventory"):
        lines[f"logs/{f}.log"] = (DATA / "logs" / f"{f}.log").read_text().splitlines()

    scrubbed, removed = {}, dict.fromkeys(TYPES, 0)
    for f, ls in lines.items():
        for n, line in enumerate(ls, 1):
            clean, c = scrub(line)
            scrubbed[(f, n)] = clean
            for k, v in c.items():
                removed[k] += v

    planted = dict.fromkeys(TYPES, 0)
    caught = dict.fromkeys(TYPES, 0)
    misses = []
    for t in truth:
        planted[t["type"]] += 1
        if t["value"] in scrubbed[(t["file"], t["line"])]:
            misses.append(t)
        else:
            caught[t["type"]] += 1
    return planted, caught, removed, misses, scrubbed


class TestSyntheticLogs(unittest.TestCase):
    def test_every_planted_value_removed(self):
        planted, caught, removed, misses, _ = recall_report()
        self.assertEqual(misses, [], f"{len(misses)} planted secrets survived: {misses[:5]}")

    def test_no_extra_replacements(self):
        """Every replacement corresponds to a planted value (no false positives on log noise)."""
        planted, _, removed, _, _ = recall_report()
        self.assertEqual(removed, planted)

    def test_log_lines_still_valid_json(self):
        *_, scrubbed = recall_report()
        for clean in scrubbed.values():
            json.loads(clean)


class TestEdgeCases(unittest.TestCase):
    def check(self, raw, expected):
        self.assertEqual(scrub(raw)[0], expected)

    def test_keeps_operational_data(self):
        for s in ["2026-11-05T14:00:02.104Z deploy v2.3 config db.pool.max_size 50 -> 5",
                  "latency_ms=30012 status=500 order_id=ord_879643 waiting=137",
                  "QueuePool limit of size 5 overflow 0 reached, timeout 30.00",
                  "request_id req_693aa9d207c8 version 10.2.1 port 5432 epoch 1762351200000"]:
            self.check(s, s)

    def test_types(self):
        self.check("mail jane.doe@example.com now", "mail [EMAIL] now")
        self.check("from 192.168.1.20.", "from [IP].")
        self.check("card 4111 **** **** 1234 ok", "card [CARD] ok")
        self.check("pan 4111111111111111", "pan [CARD]")
        self.check("key sk_live_abcdEFGH12345678", "key [API_KEY]")
        self.check("Authorization: Bearer abc123def456ghi", "Authorization: Bearer [TOKEN]")
        self.check("call (415) 555-0132 or +44 20 7946 0958", "call [PHONE] or [PHONE]")

    def test_spoken_email(self):
        self.check("customer is jane dot doe at example dot com", "customer is [EMAIL]")

    def test_idempotent(self):
        once = scrub("Bearer abcdefghijkl jane@x.io 10.0.0.1")[0]
        self.assertEqual(scrub(once), (once, dict.fromkeys(TYPES, 0)))


if __name__ == "__main__":
    planted, caught, removed, misses, _ = recall_report()
    print(f"{'type':<8} {'planted':>7} {'removed':>7} {'recall':>7}")
    for t in TYPES:
        r = caught[t] / planted[t] if planted[t] else 1.0
        print(f"{t:<8} {planted[t]:>7} {caught[t]:>7} {r:>7.1%}")
    tot_p, tot_c = sum(planted.values()), sum(caught.values())
    print(f"{'TOTAL':<8} {tot_p:>7} {tot_c:>7} {tot_c / tot_p:>7.1%}")
    extra = {t: removed[t] - planted[t] for t in TYPES if removed[t] != planted[t]}
    print("extra replacements (possible false positives):", extra or "none")
    for m in misses:
        print("MISS", m)
    unittest.main(argv=[sys.argv[0], "-v"], exit=True)
