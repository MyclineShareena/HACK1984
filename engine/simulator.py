"""Simulated production environment: executes approved actions and serves health checks.

The physics are independent of the agents' diagnosis, so a wrong fix really fails:
- Each service's config is rebuilt from deploy history (v2.0 -> v2.3 diffs).
- Capacity keys (…max_size, …limit, …connections, …workers) are compared to the last
  known-good release. Demand per pod is ~40% of the old capacity, so dropping the pool
  from 50 to 5 makes most requests wait 30 s and fail.
- restart only drains the queue briefly; scale-out spreads load but doesn't restore capacity;
  reverting the key or rolling back actually fixes it.
"""
import copy
import math
import re
from datetime import datetime, timedelta, timezone

CAPACITY_TOKENS = {"size", "max", "limit", "connections", "threads", "workers", "concurrency"}
DEMAND_FRACTION = 0.4
BASE_REPLICAS = 3


def _tok(key):
    return set(re.split(r"[^a-z0-9]+", key.lower()))


class SimulatedEnvironment:
    def __init__(self, deploys, services, start=None):
        self.services = {}
        for name, meta in services.items():
            mine = [d for d in deploys if d.get("service") == name]
            cfg, snaps, versions = {}, {}, []
            for d in mine:  # keys first introduced later still had an "old" value before
                for k, diff in d.get("config_diff", {}).items():
                    cfg.setdefault(k, diff.get("old"))
            for d in mine:
                for k, diff in d.get("config_diff", {}).items():
                    cfg[k] = diff.get("new")
                snaps[d["version"]] = copy.deepcopy(cfg)
                versions.append(d["version"])
            baseline = snaps[versions[-2]] if len(versions) > 1 else copy.deepcopy(cfg)
            self.services[name] = {"version": versions[-1] if versions else "n/a", "versions": versions,
                                   "snapshots": snaps, "config": cfg, "baseline": baseline,
                                   "replicas": BASE_REPLICAS, "relief_ticks": 0, "meta": meta}
        self.clock = start or datetime.now(timezone.utc)
        self.ticks = 0
        self._last = {n: self.error_rate(n) for n in self.services}

    # ------------------------------------------------------------ physics
    def error_rate(self, name):
        s = self.services[name]
        worst = 0.0
        for key, base in s["baseline"].items():
            cur = s["config"].get(key)
            if not (_tok(key) & CAPACITY_TOKENS) or not isinstance(cur, (int, float)) or not isinstance(base, (int, float)):
                continue
            if isinstance(cur, bool) or base <= 0:
                continue
            demand_per_pod = DEMAND_FRACTION * base * BASE_REPLICAS / s["replicas"]
            if cur < demand_per_pod:
                worst = max(worst, min(0.85, (demand_per_pod - cur) / demand_per_pod * 1.1))
        if s["relief_ticks"] > 0:
            worst *= 0.25
        return round(worst, 3)

    def snapshot(self, name, er=None):
        er = self.error_rate(name) if er is None else er
        cfg = self.services[name]["config"]
        pool = next((v for k, v in cfg.items() if "pool" in k and "size" in k), None)
        p99 = 180 + int(er * 36000) if er else 160 + (self.ticks * 7) % 40
        return {"t": self.clock, "service": name, "version": self.services[name]["version"],
                "replicas": self.services[name]["replicas"], "error_rate": er, "p99_ms": min(p99, 30100),
                "pool_max": pool, "pool_waiting": int(er * 160), "status": "healthy" if er < 0.01 else
                "degraded" if er < 0.2 else "failing"}

    # ------------------------------------------------------------ actions
    def execute(self, action):
        """Apply an approved action. Returns the simulated execution log (list of str)."""
        s = self.services[action["service"]]
        typ, p = action["type"], action.get("params", {})
        log = [f"$ {action['command']}"]
        if typ == "set_config":
            old = s["config"].get(p["key"])
            s["config"][p["key"]] = p["value"]
            log += [f"configmap/{action['service']} patched: {p['key']} {old} → {p['value']}",
                    f"deployment.apps/{action['service']} rolling restart to pick up config",
                    *[f"pod {action['service']}-{i} replaced (ready 1/1)" for i in range(s["replicas"])]]
        elif typ == "rollback":
            v = p["to_version"]
            old = s["version"]
            s["config"] = copy.deepcopy(s["snapshots"][v])
            s["version"] = v
            log += [f"deployment.apps/{action['service']} rolled back {old} → {v}",
                    *[f"pod {action['service']}-{i} running image {action['service']}:{v} (ready 1/1)"
                      for i in range(s["replicas"])]]
        elif typ == "restart":
            s["relief_ticks"] = 2
            log += [f"deployment.apps/{action['service']} restarted",
                    *[f"pod {action['service']}-{i} replaced (ready 1/1)" for i in range(s["replicas"])]]
        elif typ == "scale":
            s["replicas"] += p.get("replicas_delta", 1)
            log += [f"deployment.apps/{action['service']} scaled to {s['replicas']} replicas"]
        else:
            raise ValueError(f"unknown action type {typ}")
        log.append("rollout status: successfully rolled out")
        self.clock += timedelta(minutes=action.get("est_minutes", 2))
        return log

    def health_check(self, name):
        """Advance the simulated clock 30 s and return one health-check sample."""
        self.clock += timedelta(seconds=30)
        self.ticks += 1
        target = self.error_rate(name)
        prev = self._last.get(name, target)
        er = target if abs(prev - target) < 0.02 else round(0.35 * prev + 0.65 * target, 3)  # pods roll gradually
        self._last[name] = er
        snap = self.snapshot(name, er)
        s = self.services[name]
        if s["relief_ticks"] > 0:
            s["relief_ticks"] -= 1
        return snap


def verify(samples, window=4):
    """Recovery is verified when the last `window` checks are all healthy."""
    tail = samples[-window:]
    ok = len(tail) == window and all(x["error_rate"] < 0.01 and x["p99_ms"] < 1000 for x in tail)
    return {"passed": ok,
            "criteria": f"last {window} health checks: error rate < 1% and p99 < 1000 ms",
            "final": tail[-1] if tail else None}
