"""Specialized investigation agents.

Each agent does its evidence gathering deterministically (fast, reproducible, auditable)
and uses the local LLM where judgment or language is needed: triage, ranking root causes,
choosing a remediation, and writing explanations. Every LLM output is validated, and if
the model is down or returns junk, the agent falls back to rules so the pipeline never stalls.
"""
import re
from datetime import datetime, timedelta

from privacy_layer import search_incidents

from . import llm
from .data import analyze_events, error_rate_since

SEVERITIES = ["SEV1", "SEV2", "SEV3", "SEV4"]
CATEGORIES = ["availability", "latency", "error_rate", "capacity", "data_integrity", "security", "other"]


def fmt_t(t):
    return t.strftime("%H:%M:%S") if isinstance(t, datetime) else "n/a"


# ===================================================================== 1. Triage
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "severity": {"type": "string", "enum": SEVERITIES},
        "affected_service": {"type": "string"},
        "symptoms": {"type": "array", "items": {"type": "string"}},
        "suspected_trigger": {"type": "string"},
    },
    "required": ["summary", "category", "severity", "affected_service", "symptoms", "suspected_trigger"],
}


def triage_rules(text, services, fallback_service):
    t = text.lower()
    svc = next((s for s in services if s in t), fallback_service)
    sev = "SEV1" if re.search(r"down|outage|all (users|customers)|can't|cannot|failing|500|revenue", t) else "SEV2"
    cat = ("latency" if re.search(r"slow|latenc", t) and "500" not in t else
           "error_rate" if re.search(r"error|500|fail|exception", t) else "availability")
    symptoms = [s for s in ["HTTP 500 errors", "timeouts", "high latency", "failed checkouts", "connection errors"]
                if any(k in t for k in s.lower().split()[:2])]
    trig = "recent deploy" if re.search(r"deploy|release|rollout|v\d", t) else "unknown"
    return {"summary": text.strip()[:240], "category": cat, "severity": sev, "affected_service": svc,
            "symptoms": symptoms or ["errors reported by engineer"], "suspected_trigger": trig}


def triage_agent(text, services, fallback_service, model):
    sys = ("You are the Triage agent of an incident-response team. Classify the incident the on-call engineer "
           "describes. affected_service MUST be one of: " + ", ".join(services) + ". Severity: SEV1 = customer-facing "
           "outage or revenue loss, SEV2 = major degradation, SEV3 = minor, SEV4 = no user impact. Category 'security' only for breaches, "
           "attacks or leaked credentials (a customer merely being mentioned is NOT security). Keep symptoms short.")
    out, meta = llm.chat_json(sys, f"Engineer report:\n\"\"\"{text}\"\"\"", TRIAGE_SCHEMA, model)
    rules = triage_rules(text, services, fallback_service)
    if out:
        out = {**rules, **{k: v for k, v in out.items() if v}}
        if out.get("affected_service") not in services:
            out["affected_service"] = rules["affected_service"]
        if out.get("severity") not in SEVERITIES:
            out["severity"] = rules["severity"]
    return (out or rules), meta


# ===================================================================== 2. Log analyst
def log_agent(service, logs):
    a = analyze_events(logs.get(service, []))
    if not a["total"]:
        return a, f"No logs found for {service}."
    s = (f"{a['total']} log lines for {service}; error rate {a['error_rate_after_onset']:.0%} since onset "
         f"(peak {a['peak_error_rate']:.0%} per 5 min).")
    if a["onset"]:
        s += f" Errors began {fmt_t(a['onset'])}."
    if a["signatures"]:
        s += f" Top signature: “{a['signatures'][0]['signature']}” ×{a['signatures'][0]['count']}."
    if a["latency_before_ms"] and a["latency_after_ms"]:
        s += f" Median latency {a['latency_before_ms']} ms → {a['latency_after_ms']} ms."
    return a, s


# ===================================================================== 3. Deploy analyst
def _tokens(s):
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 2}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _value_match(key, new, log_analysis):
    """Evidence that the NEW config value is what the failing code sees, e.g. pool.max=5 or 'size 5'."""
    if _num(new) is None:
        return None
    kt = _tokens(key)
    for fk, vals in log_analysis.get("observed_values", {}).items():
        if new in vals and kt & _tokens(fk):
            return f"error events report {fk}={new}"
    for t in sorted(kt, key=len, reverse=True):
        m = re.search(rf"\b{re.escape(t)}\D{{0,12}}\b{new}\b", log_analysis.get("error_text", ""))
        if m:
            return f"error text: “{m.group(0)}”"
    return None


def deploy_agent(service, deploys, services, log_analysis):
    onset = log_analysis.get("onset")
    sig_text = " ".join(s["signature"] for s in log_analysis.get("signatures", [])) + " " + " ".join(
        log_analysis.get("resource_hints", []))
    sig_tokens = _tokens(sig_text)
    related = {service, *services.get(service, {}).get("depends_on", [])}
    cands = []
    for d in deploys:
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
        if d.get("service", service) not in related:
            continue
        if onset and not (onset - timedelta(hours=72) <= ts <= onset + timedelta(minutes=5)):
            continue
        gap_min = (onset - ts).total_seconds() / 60 if onset else None
        changes = []
        for key, diff in d.get("config_diff", {}).items():
            old, new = diff.get("old"), diff.get("new")
            overlap = sorted(_tokens(key) & sig_tokens)
            mag = None
            if _num(old) and _num(new) and old:
                mag = round(new / old, 3)
            vm = _value_match(key, new, log_analysis)
            suspicious = bool(overlap) or bool(vm) or (mag is not None and (mag <= 0.5 or mag >= 3))
            changes.append({"key": key, "old": old, "new": new, "ratio": mag, "keyword_overlap": overlap,
                            "value_match": vm, "suspicious": suspicious})
        cands.append({**d, "minutes_before_onset": round(gap_min, 1) if gap_min is not None else None,
                      "changes": changes})
    cands.sort(key=lambda c: abs(c["minutes_before_onset"]) if c["minutes_before_onset"] is not None else 1e9)
    if cands and cands[0]["minutes_before_onset"] is not None:
        c = cands[0]
        sus = [ch for ch in c["changes"] if ch["suspicious"]]
        s = f"Closest deploy: {c['service']} {c['version']} by {c['author']}, {c['minutes_before_onset']:.0f} min before error onset."
        if sus:
            s += " Suspicious change: " + "; ".join(f"{ch['key']} {ch['old']} → {ch['new']}" for ch in sus) + "."
    else:
        s = "No deploys correlate with the error onset."
    return cands, s


# ===================================================================== 4. History (RAG)
def history_agent(triage, log_analysis, deploy_cands, k=3):
    parts = [triage.get("summary", ""), " ".join(triage.get("symptoms", []))]
    parts += [s["signature"] for s in log_analysis.get("signatures", [])[:3]]
    parts += log_analysis.get("resource_hints", [])
    if deploy_cands:
        parts += [ch["key"].replace(".", " ") for ch in deploy_cands[0]["changes"] if ch["suspicious"]]
        parts.append("deploy config change")
    query = " ".join(parts)
    hits = search_incidents(query, k=k)
    s = ("Most similar past incident: " + f"{hits[0]['id']} “{hits[0]['title']}” (BM25 {hits[0]['score']:.1f})."
         if hits else "No similar past incidents.")
    return {"query": query, "hits": hits}, s


# ===================================================================== 5. Dependencies / blast radius
def dependency_agent(service, services, logs, onset):
    callers = {s for s, d in services.items() if service in d.get("depends_on", [])}
    frontier, upstream = set(callers), set()
    while frontier:  # transitive callers are impacted too
        s = frontier.pop()
        upstream.add(s)
        frontier |= {x for x, d in services.items() if s in d.get("depends_on", [])} - upstream
    deps = services.get(service, {}).get("depends_on", [])
    health = {}
    for s in services:
        evs = logs.get(s)
        if not evs:
            health[s] = {"status": "no telemetry", "error_rate": None, "warn_rate": None}
            continue
        after = [e for e in evs if onset is None or e.ts >= onset]
        er = error_rate_since(evs, onset)
        wr = round(sum(e.level == "WARN" for e in after) / len(after), 3) if after else 0.0
        status = "failing" if er >= 0.2 else "degraded" if (er >= 0.05 or wr >= 0.05) else "healthy"
        health[s] = {"status": status, "error_rate": er, "warn_rate": wr}
    health[service]["status"] = "failing" if (health[service].get("error_rate") or 0) >= 0.2 else health[service]["status"]
    unhealthy_deps = [d for d in deps if health.get(d, {}).get("status") == "failing"]
    blast = sorted({service} | upstream | {d for d in deps if health.get(d, {}).get("status") in ("degraded", "failing")})
    s = (f"{service} depends on {', '.join(deps) or 'nothing'}; "
         + (f"failing dependencies: {', '.join(unhealthy_deps)}. " if unhealthy_deps else "no dependency is failing on its own, ")
         + f"blast radius: {', '.join(blast)}.")
    if not unhealthy_deps:
        s += " Downstream warnings look like symptoms (aborted calls from the failing service), not causes."
    return {"callers": sorted(upstream), "depends_on": deps, "health": health, "unhealthy_deps": unhealthy_deps,
            "blast_radius": blast, "dot": _dot(service, services, health)}, s


def _dot(service, services, health):
    color = {"failing": "#e5484d", "degraded": "#f5a524", "healthy": "#30a46c", "no telemetry": "#8b8d98"}
    lines = ['digraph G {', 'rankdir=LR; bgcolor="transparent";',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontcolor="white", penwidth=0];',
             'edge [color="#8b8d98"];']
    for s in services:
        st = health.get(s, {}).get("status", "no telemetry")
        er = health.get(s, {}).get("error_rate")
        label = f"{s}\\n{st}" + (f" · {er:.0%} err" if er else "")
        extra = ', penwidth=3, color="#ffffff"' if s == service else ""
        lines.append(f'"{s}" [label="{label}", fillcolor="{color[st]}"{extra}];')
    for s, d in services.items():
        for t in d.get("depends_on", []):
            lines.append(f'"{s}" -> "{t}";')
    lines.append("}")
    return "\n".join(lines)


# ===================================================================== 6. Root cause + remediation
def build_hypotheses(service, log_a, deploy_cands, deps, history, services):
    hyps = []
    top_hit = history["hits"][0] if history["hits"] else None
    hist_text = (top_hit["title"] + " " + top_hit["root_cause"]).lower() if top_hit else ""
    for d in deploy_cands[:2]:
        gap = d["minutes_before_onset"]
        if gap is None:
            continue
        prox = max(0.0, 1 - abs(gap) / 240)
        for ch in d["changes"]:
            if not ch["suspicious"]:
                continue
            kw = min(1.0, len(ch["keyword_overlap"]) / 2)
            mag = 1.0 if ch["ratio"] is not None and (ch["ratio"] <= 0.2 or ch["ratio"] >= 5) else 0.5 if ch["ratio"] else 0.2
            hist = 1.0 if _tokens(ch["key"]) & _tokens(hist_text) else 0.0
            val = 1.0 if ch.get("value_match") else 0.0
            score = 0.3 * prox + 0.2 * kw + 0.15 * mag + 0.2 * val + 0.15 * hist
            hyps.append({
                "id": f"H-config-{ch['key']}",
                "kind": "config_regression",
                "statement": f"Deploy {d['version']} of {d['service']} changed {ch['key']} from {ch['old']} to {ch['new']}"
                             + (f", starving {d['service']} of capacity under normal load." if _tokens(ch["key"]) &
                                {"size", "max", "limit", "connections", "threads", "workers"} else
                                ", shortly before errors began."),
                "service": d["service"], "deploy": d["version"], "key": ch["key"], "old": ch["old"], "new": ch["new"],
                "evidence": [f"{d['version']} deployed {gap:.0f} min before error onset ({d['timestamp']})",
                             f"config diff: {ch['key']} {ch['old']} → {ch['new']}"
                             + (f" ({ch['ratio']:.0%} of previous)" if ch["ratio"] else ""),
                             *(f"error signatures mention “{w}”" for w in ch["keyword_overlap"][:3]),
                             *([ch["value_match"]] if ch.get("value_match") else []),
                             *([f"matches past incident {top_hit['id']}: {top_hit['title']}"] if hist else [])],
                "score": round(score, 3)})
    for dep in deps["unhealthy_deps"]:
        hyps.append({"id": f"H-dep-{dep}", "kind": "dependency_failure", "service": dep,
                     "statement": f"Dependency {dep} is failing and {service} errors are a downstream effect.",
                     "evidence": [f"{dep} error rate {deps['health'][dep]['error_rate']:.0%} since onset"],
                     "score": 0.55})
    if log_a.get("resource_hints"):
        hyps.append({"id": f"H-capacity-{service}", "kind": "capacity_exhaustion", "service": service,
                     "statement": f"{service} is exhausting an internal resource ({', '.join(log_a['resource_hints'][:3])}) "
                                  "due to load or a leak, independent of any deploy.",
                     "evidence": [f"error signatures contain: {', '.join(log_a['resource_hints'][:4])}"],
                     "score": 0.35})
    rb, ra = log_a.get("req_per_min_before"), log_a.get("req_per_min_after")
    traffic_ratio = (ra / rb) if rb and ra else 1.0
    hyps.append({"id": "H-traffic", "kind": "traffic_spike", "service": service,
                 "statement": f"A traffic spike overloaded {service}.",
                 "evidence": [f"log volume per minute {rb} → {ra} ({traffic_ratio:.1f}x)" if rb else "no volume data"],
                 "score": round(min(0.6, 0.1 + max(0.0, traffic_ratio - 1.3) * 0.4), 3)})
    db = [d for d in services.get(service, {}).get("depends_on", []) if deps["health"].get(d, {}).get("status") == "no telemetry"]
    for d in db:
        hyps.append({"id": f"H-db-{d}", "kind": "dependency_failure", "service": d,
                     "statement": f"{d} itself is overloaded or unavailable.",
                     "evidence": [f"other {d} clients (e.g. payments) " + ("look healthy" if any(
                         deps["health"].get(c, {}).get("status") == "healthy" and d in services[c].get("depends_on", [])
                         for c in services if c != service) else "unknown")],
                     "score": 0.15 if any(deps["health"].get(c, {}).get("status") == "healthy" for c in services
                                          if c != service and d in services[c].get("depends_on", [])) else 0.35})
    total = sum(h["score"] for h in hyps) or 1
    for h in hyps:
        h["det_confidence"] = round(h["score"] / total, 3)
    hyps.sort(key=lambda h: -h["score"])
    return hyps


def build_actions(hyps, deploys, service):
    actions, seen = [], set()

    def add(a):
        if a["id"] not in seen:
            seen.add(a["id"])
            actions.append(a)

    for h in hyps:
        if h["kind"] == "config_regression":
            add({"id": f"A-revert-{h['key']}", "type": "set_config", "service": h["service"],
                 "params": {"key": h["key"], "value": h["old"]}, "fixes": h["id"],
                 "title": f"Revert {h['key']} on {h['service']} to {h['old']}",
                 "command": f"kubectl -n prod set env deploy/{h['service']} {h['key'].upper().replace('.', '_')}={h['old']}",
                 "risk": "low", "est_minutes": 2, "reversible": True})
            versions = [d["version"] for d in deploys if d.get("service") == h["service"]]
            if h["deploy"] in versions and versions.index(h["deploy"]) > 0:
                prev = versions[versions.index(h["deploy"]) - 1]
                add({"id": f"A-rollback-{h['service']}-{prev}", "type": "rollback", "service": h["service"],
                     "params": {"to_version": prev}, "fixes": h["id"],
                     "title": f"Roll back {h['service']} {h['deploy']} → {prev}",
                     "command": f"kubectl -n prod rollout undo deploy/{h['service']} --to-revision={prev}",
                     "risk": "low", "est_minutes": 4, "reversible": True})
        elif h["kind"] == "dependency_failure":
            add({"id": f"A-restart-{h['service']}", "type": "restart", "service": h["service"], "params": {},
                 "fixes": h["id"], "title": f"Restart {h['service']}",
                 "command": f"kubectl -n prod rollout restart deploy/{h['service']}",
                 "risk": "medium", "est_minutes": 3, "reversible": True})
    add({"id": f"A-restart-{service}", "type": "restart", "service": service, "params": {}, "fixes": None,
         "title": f"Restart {service} pods", "command": f"kubectl -n prod rollout restart deploy/{service}",
         "risk": "low", "est_minutes": 2, "reversible": True})
    add({"id": f"A-scale-{service}", "type": "scale", "service": service, "params": {"replicas_delta": 3},
         "fixes": "H-traffic", "title": f"Scale out {service} by 3 replicas",
         "command": f"kubectl -n prod scale deploy/{service} --replicas=+3", "risk": "low", "est_minutes": 3,
         "reversible": True})
    return actions


RCA_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "confidence": {"type": "number"}, "explanation": {"type": "string"}},
            "required": ["id", "confidence", "explanation"]}},
        "recommended_action_id": {"type": "string"},
        "action_rationale": {"type": "string"},
        "narrative": {"type": "string"},
    },
    "required": ["ranked", "recommended_action_id", "action_rationale", "narrative"],
}


def root_cause_agent(ctx, model):
    hyps, actions = ctx["hypotheses"], ctx["actions"]
    la, deps, hist = ctx["logs"], ctx["deps"], ctx["history"]
    evidence = [
        f"Incident report (scrubbed): {ctx['triage']['summary']}",
        f"Logs: {ctx['summaries']['logs']}",
        "Top error signatures: " + "; ".join(f"{s['signature']} (x{s['count']})" for s in la.get("signatures", [])[:4]),
        f"Deploys: {ctx['summaries']['deploys']}",
        f"Dependencies: {ctx['summaries']['deps']}",
    ] + [f"Past incident {h['id']} '{h['title']}': root cause: {h['root_cause']} Fix: {h['fix']}" for h in hist["hits"][:2]]
    hyp_txt = "\n".join(f"- {h['id']}: {h['statement']} (prior score {h['det_confidence']:.2f}; evidence: "
                        f"{'; '.join(h['evidence'])})" for h in hyps)
    act_txt = "\n".join(f"- {a['id']}: {a['title']} (risk {a['risk']}, ~{a['est_minutes']} min)" for a in actions)
    sys = ("You are the Root-Cause agent. Using ONLY the evidence given, rank the candidate hypotheses (use their exact "
           "ids), give each a confidence between 0 and 1, and a one-sentence explanation citing evidence. Then choose the "
           "single safest action id that fixes the top cause (prefer the most targeted, reversible fix) and justify it. "
           "narrative: exactly 3 factual sentences: (1) what is broken and since when, (2) the most likely cause with "
           "the key evidence, (3) the recommended fix. Do not give instructions to the engineer in sentence 1 or 2. "
           "Never mention hypothesis or action ids (like H-... or A-...) in explanations or the narrative.")
    user = "EVIDENCE:\n" + "\n".join(evidence) + "\n\nHYPOTHESES:\n" + hyp_txt + "\n\nALLOWED ACTIONS:\n" + act_txt
    out, meta = llm.chat_json(sys, user, RCA_SCHEMA, model)

    by_id = {h["id"]: h for h in hyps}
    llm_conf, expl = {}, {}
    if out:
        for r in out.get("ranked", []):
            if r.get("id") in by_id:
                try:
                    llm_conf[r["id"]] = max(0.0, min(1.0, float(r["confidence"])))
                except (TypeError, ValueError):
                    pass
                expl[r["id"]] = r.get("explanation", "")
    for h in hyps:  # blend: deterministic evidence score + LLM judgment
        h["llm_confidence"] = llm_conf.get(h["id"])
        h["confidence"] = round(0.5 * h["det_confidence"] + 0.5 * llm_conf[h["id"]], 3) if h["id"] in llm_conf \
            else h["det_confidence"]
        h["explanation"] = expl.get(h["id"]) or h["statement"]
    total = sum(h["confidence"] ** 2 for h in hyps) or 1  # sharpen: squared-score normalization
    for h in hyps:
        h["confidence"] = round(h["confidence"] ** 2 / total, 3)
    hyps.sort(key=lambda h: -h["confidence"])

    act_ids = {a["id"] for a in actions}
    top = hyps[0]
    targeted = [a for a in actions if a["fixes"] == top["id"]]
    llm_rec = out.get("recommended_action_id") if out else None
    guardrail = None
    if llm_rec in act_ids and (not targeted or llm_rec in {a["id"] for a in targeted}):
        rec, rationale = llm_rec, out.get("action_rationale", "")
    else:  # guardrail: action must exist and must address the top-ranked cause
        rec = targeted[0]["id"] if targeted else actions[0]["id"]
        rationale = f"Most targeted, low-risk, reversible fix for the top-ranked cause ({top['id']})."
        if llm_rec:
            guardrail = f"LLM suggested {llm_rec}; overridden because it does not address {top['id']}."
    narrative = (out or {}).get("narrative") or (
        f"{top['statement']} Evidence: {'; '.join(top['evidence'][:3])}. Recommended: {next(a['title'] for a in actions if a['id'] == rec)}.")
    return {"hypotheses": hyps, "recommended_action_id": rec, "action_rationale": rationale,
            "narrative": narrative, "guardrail": guardrail}, meta
