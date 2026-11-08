"""Egress leak gate + audit log. Call before ANY network request; send what it returns.

    safe = log_egress("gradium", text)          # scrubbed str/dict/list, safe to send
    requests.post(url, json=safe)

1. Scrubs the payload (strings inside dicts/lists too).
2. Re-checks the scrubbed payload with a broader, paranoid detector set. Anything left
   (e.g. "password=hunter2", a short sk_live_ key, a 16-digit number) -> EgressBlocked.
3. Appends one JSON line per attempt to egress_log.jsonl: timestamp, destination, size,
   sha256 and the exact payload sent. Blocked attempts are logged WITHOUT the payload.

Binary payloads (e.g. audio for speech-to-text) can't be scanned; they're refused unless
allow_binary=True, and then only their size + sha256 are logged.

Log path: $INCIDENTVOICE_EGRESS_LOG, else <project>/egress_log.jsonl.
    python -m privacy_layer.egress      # print the audit log
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .scrubber import scrub_obj

DEFAULT_LOG = Path(__file__).resolve().parents[1] / "egress_log.jsonl"
_lock = threading.Lock()

# High-recall detectors run AFTER scrubbing. Hits here mean the scrubber missed something.
RESIDUAL = {
    "secret_assignment": re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|access[_-]?key|private[_-]?key)"
        r"[\"']?\s*[=:]\s*[\"']?(?!\[)[^\s\"',;}]{4,}"),
    "key_prefix": re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_\w+|\bAKIA[0-9A-Z]{8,}"),
    "jwt_fragment": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}"),
    "bearer": re.compile(r"(?i)\bbearer\s+(?!\[TOKEN\])[^\s\"']{6,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "email_like": re.compile(r"[^\s@\[\]\"'<>]+@[^\s@\[\]\"'<>]+\.[A-Za-z]{2,}"),
    "card_like_digits": re.compile(r"(?<!\d)[3-6](?:[ -]?\d){14,18}(?!\d)"),
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "ipv6": re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}(?![\w:])"),
}


class EgressBlocked(RuntimeError):
    """Raised when a payload still looks sensitive after scrubbing. Nothing was sent."""

    def __init__(self, destination, findings):
        self.destination, self.findings = destination, findings
        super().__init__(f"egress to {destination!r} blocked: residual {sorted(set(findings))}")


def find_residual(text):
    """Return names of residual detectors that fire on text (empty list = clean)."""
    return [name for name, rx in RESIDUAL.items() if rx.search(text)]


def _serialize(obj):
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=False)


def _write(record, log_path):
    path = Path(log_path or os.environ.get("INCIDENTVOICE_EGRESS_LOG") or DEFAULT_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_egress(destination, payload, *, purpose=None, allow_binary=False, log_path=None):
    """Scrub + gate + audit-log an outbound payload. Returns the safe payload to send.

    payload: str, dict/list (JSON-able), or bytes. Raises EgressBlocked if unsafe.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    base = {"ts": now, "destination": destination, "purpose": purpose}

    if isinstance(payload, (bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            digest = hashlib.sha256(payload).hexdigest()
            if not allow_binary:
                _write({**base, "blocked": True, "reason": "binary payload (pass allow_binary=True)",
                        "bytes": len(payload), "sha256": digest}, log_path)
                raise EgressBlocked(destination, ["unscannable_binary"])
            _write({**base, "blocked": False, "kind": "binary", "bytes": len(payload), "sha256": digest,
                    "payload": f"<binary {len(payload)} bytes, not scannable>"}, log_path)
            return payload
        safe_text = log_egress(destination, text, purpose=purpose, log_path=log_path)
        return safe_text.encode("utf-8")

    safe, counts = scrub_obj(payload)
    wire = _serialize(safe)
    findings = find_residual(wire)
    size = len(wire.encode("utf-8"))
    if findings:
        _write({**base, "blocked": True, "reason": "residual sensitive pattern after scrubbing",
                "findings": findings, "bytes": size, "scrubbed": {k: v for k, v in counts.items() if v}},
               log_path)
        raise EgressBlocked(destination, findings)

    _write({**base, "blocked": False, "kind": "json" if not isinstance(safe, str) else "text",
            "bytes": size, "sha256": hashlib.sha256(wire.encode("utf-8")).hexdigest(),
            "scrubbed": {k: v for k, v in counts.items() if v}, "payload": safe}, log_path)
    return safe


def read_log(log_path=None):
    path = Path(log_path or os.environ.get("INCIDENTVOICE_EGRESS_LOG") or DEFAULT_LOG)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def summarize(rows):
    """Count audit-log rows by kind: speech-to-text uploads, text-to-speech requests, other, blocked."""
    out = {"stt": 0, "tts": 0, "other": 0, "blocked": 0}
    for r in rows:
        if r.get("blocked"):
            out["blocked"] += 1
        elif r.get("destination", "").startswith("gradium:speech/asr"):
            out["stt"] += 1
        elif r.get("destination", "").startswith("gradium:speech/tts"):
            out["tts"] += 1
        else:
            out["other"] += 1
    return out


def describe(counts):
    """'1 speech-to-text upload + 1 text-to-speech request, 0 blocked'"""
    parts = []
    if counts["stt"]:
        parts.append(f"{counts['stt']} speech-to-text upload{'s' if counts['stt'] != 1 else ''}")
    if counts["tts"]:
        parts.append(f"{counts['tts']} text-to-speech request{'s' if counts['tts'] != 1 else ''}")
    if counts["other"]:
        parts.append(f"{counts['other']} other")
    return (" + ".join(parts) or "nothing") + f", {counts['blocked']} blocked"


if __name__ == "__main__":
    rows = read_log()
    print(f"{len(rows)} egress attempts, {sum(r['blocked'] for r in rows)} blocked\n")
    for r in rows:
        status = "BLOCKED" if r["blocked"] else "sent   "
        preview = _serialize(r.get("payload", r.get("findings", r.get("reason", ""))))
        print(f"{r['ts']}  {status}  {r['destination']:<12} {r['bytes']:>7}B  {preview[:90]}")
