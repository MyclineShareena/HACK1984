"""Judge demo: raw log lines next to their scrubbed versions, recall summary, and the leak gate.

    python demo_scrub.py            # 6 example lines
    python demo_scrub.py -n 10      # more lines
"""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from privacy_layer import EgressBlocked, TYPES, log_egress, read_log, scrub  # noqa: E402

TTY = sys.stdout.isatty()
RED, GREEN, DIM, BOLD, RESET = ("\033[31m", "\033[32m", "\033[2m", "\033[1m", "\033[0m") if TTY else ("",) * 5


def hl_raw(line, values):
    for v in sorted(values, key=len, reverse=True):
        line = line.replace(v, f"{RED}{v}{RESET}")
    return line


def hl_clean(line):
    return re.sub(r"\[(?:%s)\]" % "|".join(TYPES), lambda m: f"{GREEN}{m.group(0)}{RESET}", line)


def compact(line):
    """Drop noisy fields so the demo lines fit on screen."""
    d = json.loads(line)
    for k in ("request_id", "version", "service"):
        d.pop(k, None)
    return json.dumps(d)


def main():
    if not (ROOT / "data" / "planted_secrets.json").exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "generate_data.py")], check=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=6)
    args = ap.parse_args()

    truth = json.loads((ROOT / "data" / "planted_secrets.json").read_text())
    by_line = {}
    for t in truth:
        by_line.setdefault((t["file"], t["line"]), []).append(t)
    lines = (ROOT / "data" / "logs" / "checkout.log").read_text().splitlines()

    # pick examples covering every type, preferring post-deploy error lines
    picked, seen = [], set()
    for (f, n), ts in sorted(by_line.items(), key=lambda kv: ("ERROR" not in lines[kv[0][1] - 1], kv[0][1])):
        if f != "logs/checkout.log":
            continue
        kinds = {t["type"] for t in ts}
        if not kinds <= seen or len(picked) < 2:
            picked.append(n)
            seen |= kinds
        if len(picked) >= args.n:
            break

    print(f"{BOLD}== Scrubber: raw vs scrubbed (data/logs/checkout.log) =={RESET}\n")
    for n in sorted(picked):
        raw = compact(lines[n - 1])
        clean, counts = scrub(raw)
        removed = ", ".join(f"{k}x{v}" for k, v in counts.items() if v)
        print(f"{DIM}line {n}  removed: {removed}{RESET}")
        print(f"  RAW      {hl_raw(raw, [t['value'] for t in by_line[('logs/checkout.log', n)]])}")
        print(f"  SCRUBBED {hl_clean(clean)}\n")

    # recall over all three logs
    total = dict.fromkeys(TYPES, 0)
    caught = dict.fromkeys(TYPES, 0)
    for t in truth:
        total[t["type"]] += 1
        raw = (ROOT / "data" / t["file"]).read_text().splitlines()[t["line"] - 1]
        caught[t["type"]] += t["value"] not in scrub(raw)[0]
    print(f"{BOLD}== Recall over all logs ({len(truth)} planted fake secrets) =={RESET}")
    print("  " + "  ".join(f"{k} {caught[k]}/{total[k]}" for k in TYPES))
    print(f"  overall {sum(caught.values()) / len(truth):.1%}, fully offline (regex, stdlib only)\n")

    # leak gate
    print(f"{BOLD}== Egress leak gate =={RESET}")
    log = Path(tempfile.mkdtemp()) / "egress_demo.jsonl"
    msg = "Checkout is throwing 500s since the deploy. Customer jane.doe@example.com from 203.0.113.9 complained."
    safe = log_egress("llm-api", msg, purpose="agent prompt", log_path=log)
    print(f"  ALLOWED  {safe}")
    try:
        log_egress("llm-api", "Try the DB with password=Sup3rS3cret!", purpose="agent prompt", log_path=log)
    except EgressBlocked as e:
        print(f"  {RED}BLOCKED{RESET}  {e}")
    print(f"\n  audit log ({log}):")
    for r in read_log(log):
        r = {k: v for k, v in r.items() if k != "sha256"}
        print(f"  {DIM}{json.dumps(r)}{RESET}")


if __name__ == "__main__":
    main()
