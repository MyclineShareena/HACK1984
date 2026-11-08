"""Load the environment's telemetry (logs, deploys, service map) and analyze logs.

Logs can be JSON lines (bundled data) or plain text (uploaded files), e.g.
  2026-11-05T14:02:11Z ERROR checkout connection pool exhausted ...
Every message is scrubbed on load, so agents and the LLM never see raw PII/secrets.
"""
import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from privacy_layer import TYPES, scrub

DATA = Path(__file__).resolve().parents[1] / "data"
ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL", "SEVERE", "EMERG", "ALERT"}
RESOURCE_WORDS = ["pool", "exhausted", "timeout", "timed out", "oom", "out of memory", "disk",
                  "connection refused", "too many connections", "deadlock", "429", "throttl",
                  "saturated", "queue", "limit"]

_TS = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|FATAL|CRITICAL|SEVERE)\b", re.I)
_STATUS = re.compile(r"\b(?:status[=: ]+|HTTP/\d\.\d\" |\s)([1-5]\d\d)\b")


@dataclass
class Event:
    ts: datetime
    level: str
    service: str
    msg: str
    fields: dict = field(default_factory=dict)

    @property
    def is_error(self):
        st = self.fields.get("status")
        return self.level in ERROR_LEVELS or (isinstance(st, int) and st >= 500)


def _parse_ts(s):
    try:
        t = datetime.fromisoformat(s.replace(" ", "T").replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_log(text, default_service="unknown"):
    """Parse JSON-lines or plain-text logs into scrubbed Events. Returns (events, scrub_counts)."""
    events, counts = [], dict.fromkeys(TYPES, 0)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        clean, c = scrub(line)
        for k, v in c.items():
            counts[k] += v
        ev = None
        if clean.startswith("{"):
            try:
                d = json.loads(clean)
                ts = _parse_ts(str(d.pop("ts", d.pop("timestamp", d.pop("time", "")))))
                if ts:
                    level = str(d.pop("level", d.pop("severity", "INFO"))).upper()
                    msg = str(d.pop("msg", d.pop("message", "")))
                    if d.get("error"):
                        msg = f"{msg} | {d['error']}"
                    ev = Event(ts, "WARN" if level == "WARNING" else level, d.pop("service", default_service), msg, d)
            except json.JSONDecodeError:
                pass
        if ev is None:
            m = _TS.search(clean)
            ts = _parse_ts(m.group(1)) if m else None
            if not ts:
                continue
            lv = _LEVEL.search(clean)
            level = lv.group(1).upper() if lv else "INFO"
            level = "WARN" if level == "WARNING" else level
            fields = {}
            sm = _STATUS.search(clean[m.end():])
            if sm:
                fields["status"] = int(sm.group(1))
            ev = Event(ts, level, default_service, clean[m.end():].strip(" -|:"), fields)
        events.append(ev)
    events.sort(key=lambda e: e.ts)
    return events, counts


def detect_service(events, fallback):
    names = Counter(e.service for e in events if e.service and e.service != "unknown")
    return names.most_common(1)[0][0] if names else fallback


def ensure_data():
    """Synthetic logs aren't committed (they contain fake secrets); build them on first run."""
    if not (DATA / "logs" / "checkout.log").exists():
        import subprocess
        import sys
        subprocess.run([sys.executable, str(DATA.parent / "generate_data.py")], check=True, capture_output=True)


def load_environment():
    ensure_data()
    logs, counts = {}, dict.fromkeys(TYPES, 0)
    for f in sorted((DATA / "logs").glob("*.log")):
        evs, c = parse_log(f.read_text(), f.stem)
        logs[f.stem] = evs
        for k, v in c.items():
            counts[k] += v
    deploys = json.loads((DATA / "deploys.json").read_text())
    services = json.loads((DATA / "services.json").read_text())
    return {"logs": logs, "deploys": deploys, "services": services, "scrubbed": counts}


# ------------------------------------------------------------------ analysis
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEX = re.compile(r"\b(?:req|ord|trace|span)_[0-9a-f]+\b|\b[0-9a-f]{8,}\b")


def signature(msg):
    s = _HEX.sub("<id>", msg)
    s = _NUM.sub("<n>", s)
    s = re.sub(r";\s.*$", "", s)  # drop appended context like "; receipt queued for [EMAIL]"
    return s[:140]


def _bucket(ts, minutes):
    return ts.replace(minute=ts.minute - ts.minute % minutes, second=0, microsecond=0)


def _flatten(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flatten(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def analyze_events(events, bucket_min=5):
    if not events:
        return {"total": 0, "errors": 0, "error_rate": 0.0, "timeline": [], "onset": None, "signatures": [],
                "resource_hints": [], "latency_before_ms": None, "latency_after_ms": None, "config_events": []}
    buckets = {}
    for e in events:
        b = buckets.setdefault(_bucket(e.ts, bucket_min), {"total": 0, "errors": 0, "lat": []})
        b["total"] += 1
        b["errors"] += e.is_error
        lat = e.fields.get("latency_ms")
        if isinstance(lat, (int, float)):
            b["lat"].append(lat)
    timeline = []
    for t in sorted(buckets):
        b = buckets[t]
        lat = sorted(b["lat"])
        timeline.append({"t": t, "total": b["total"], "errors": b["errors"],
                         "error_rate": round(b["errors"] / b["total"], 3),
                         "p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None})

    onset_bucket = next((r["t"] for r in timeline if r["error_rate"] >= 0.10 and r["errors"] >= 2), None)
    onset = None
    if onset_bucket:
        lo = onset_bucket - timedelta(minutes=bucket_min)
        onset = next((e.ts for e in events if e.is_error and e.ts >= lo), onset_bucket)

    errs = [e for e in events if e.is_error or e.level == "WARN"]
    sig = Counter(signature(e.msg) for e in errs)
    first_seen = {}
    for e in errs:
        first_seen.setdefault(signature(e.msg), e.ts)
    signatures = [{"signature": s, "count": n, "first_seen": first_seen[s]} for s, n in sig.most_common(6)]

    observed = {}
    for e in errs:
        for k, v in _flatten(e.fields):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                observed.setdefault(k, set()).add(v)
    observed = {k: sorted(v)[:12] for k, v in observed.items()}
    blob = " ".join(e.msg.lower() for e in errs)
    hints = [w for w in RESOURCE_WORDS if w in blob]

    def med(xs):
        return round(statistics.median(xs)) if xs else None

    lat_b = [e.fields["latency_ms"] for e in events if "latency_ms" in e.fields and (not onset or e.ts < onset)]
    lat_a = [e.fields["latency_ms"] for e in events if "latency_ms" in e.fields and onset and e.ts >= onset]
    rate_b = rate_a = None
    if onset:
        before = [e for e in events if e.ts < onset]
        after = [e for e in events if e.ts >= onset]
        if before and after:
            span_b = max((before[-1].ts - before[0].ts).total_seconds(), 60)
            span_a = max((after[-1].ts - after[0].ts).total_seconds(), 60)
            rate_b, rate_a = len(before) / span_b * 60, len(after) / span_a * 60
    errors_after = sum(e.is_error for e in events if onset and e.ts >= onset)
    after_n = sum(1 for e in events if onset and e.ts >= onset)
    config_events = [{"ts": e.ts, "msg": e.msg, "fields": {k: v for k, v in e.fields.items() if k not in ("request_id",)}}
                     for e in events if re.search(r"config|deploy|rollout", e.msg, re.I)]
    return {
        "total": len(events), "errors": sum(e.is_error for e in events),
        "error_rate": round(sum(e.is_error for e in events) / len(events), 3),
        "error_rate_after_onset": round(errors_after / after_n, 3) if after_n else 0.0,
        "peak_error_rate": max(r["error_rate"] for r in timeline),
        "timeline": timeline, "onset": onset, "signatures": signatures, "resource_hints": hints,
        "latency_before_ms": med(lat_b), "latency_after_ms": med(lat_a),
        "req_per_min_before": round(rate_b, 2) if rate_b else None,
        "req_per_min_after": round(rate_a, 2) if rate_a else None,
        "config_events": config_events[:6],
        "observed_values": observed, "error_text": blob[:50000],
        "window": (events[0].ts, events[-1].ts),
    }


def error_rate_since(events, since):
    after = [e for e in events if since is None or e.ts >= since]
    return round(sum(e.is_error for e in after) / len(after), 3) if after else 0.0
