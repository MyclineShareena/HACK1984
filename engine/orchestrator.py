"""Coordinates the investigation agents and records a trace of every step.

    inv = run_investigation("checkout is throwing 500s since the deploy", model="qwen2.5:7b",
                            on_event=print)
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from privacy_layer import TYPES, scrub

from . import agents
from .data import detect_service, load_environment, parse_log


def run_investigation(description, model, uploaded_log=None, on_event=None):
    """uploaded_log: optional (filename, text). Returns the full investigation dict."""
    t0 = time.time()
    trace = []

    def emit(agent, msg, **extra):
        ev = {"t": round(time.time() - t0, 1), "agent": agent, "msg": msg, **extra}
        trace.append(ev)
        if on_event:
            on_event(ev)

    env = load_environment()
    services = env["services"]
    scrubbed = dict(env["scrubbed"])
    clean_desc, c = scrub(description)
    for k, v in c.items():
        scrubbed[k] += v
    emit("Privacy", f"Scrubbed bundled logs + description: {sum(scrubbed.values())} sensitive values replaced "
                    f"({', '.join(f'{k} {v}' for k, v in scrubbed.items() if v)})")

    upload_service = None
    if uploaded_log:
        name, text = uploaded_log
        stem = name.rsplit("/", 1)[-1].split(".")[0].lower()
        events, c = parse_log(text, stem if stem in services else "unknown")
        upload_service = detect_service(events, stem if stem in services else None)
        for k, v in c.items():
            scrubbed[k] += v
        if events:
            key = upload_service or "uploaded"
            for e in events:
                if e.service == "unknown":
                    e.service = key
            env["logs"][key] = events
            if key not in services:
                services[key] = {"depends_on": [], "tier": 2, "owner": "unknown"}
            emit("Privacy", f"Parsed uploaded log '{name}': {len(events)} events for service '{key}', "
                            f"{sum(c.values())} sensitive values scrubbed")
        else:
            emit("Privacy", f"Uploaded log '{name}' had no parseable timestamped lines; ignored")

    # 1. triage (LLM)
    emit("Triage", "Classifying incident with local LLM…", status="running")
    triage, meta = agents.triage_agent(clean_desc, list(services), upload_service or "checkout", model)
    emit("Triage", f"{triage['severity']} · {triage['category']} · service={triage['affected_service']} · "
                   f"trigger: {triage['suspected_trigger']}", llm=meta)
    svc = triage["affected_service"]

    # 2. log analysis (needed by the others)
    emit("Log analyst", f"Analyzing {len(env['logs'].get(svc, []))} {svc} log events…", status="running")
    log_a, log_s = agents.log_agent(svc, env["logs"])
    emit("Log analyst", log_s)

    # 3-5. deploys, dependencies, history in parallel
    emit("Coordinator", "Dispatching deploy, dependency and history agents in parallel", status="running")
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_dep = pool.submit(agents.deploy_agent, svc, env["deploys"], services, log_a)
        f_graph = pool.submit(agents.dependency_agent, svc, services, env["logs"], log_a.get("onset"))
        deploy_c, deploy_s = f_dep.result()
        deps, deps_s = f_graph.result()
        history, hist_s = pool.submit(agents.history_agent, triage, log_a, deploy_c).result()
    emit("Deploy analyst", deploy_s)
    emit("Dependency analyst", deps_s)
    emit("History (RAG)", hist_s)

    # 6. hypotheses + actions + RCA (LLM)
    hyps = agents.build_hypotheses(svc, log_a, deploy_c, deps, history, services)
    actions = agents.build_actions(hyps, env["deploys"], svc)
    emit("Root cause", f"Ranking {len(hyps)} hypotheses with local LLM…", status="running")
    ctx = {"triage": triage, "logs": log_a, "deps": deps, "history": history, "hypotheses": hyps,
           "actions": actions, "summaries": {"logs": log_s, "deploys": deploy_s, "deps": deps_s, "history": hist_s}}
    rca, meta = agents.root_cause_agent(ctx, model)
    top = rca["hypotheses"][0]
    emit("Root cause", f"Top cause ({top['confidence']:.0%}): {top['statement']}", llm=meta)
    rec = next(a for a in actions if a["id"] == rca["recommended_action_id"])
    emit("Remediation", f"Recommended: {rec['title']} (risk {rec['risk']}). Awaiting human approval."
         + (f" Guardrail: {rca['guardrail']}" if rca.get("guardrail") else ""))

    return {"description": clean_desc, "started_at": datetime.now(timezone.utc), "model": model,
            "triage": triage, "logs": log_a, "deploys": deploy_c, "deps": deps, "history": history,
            "rca": rca, "actions": actions, "summaries": ctx["summaries"], "trace": trace,
            "scrubbed": {k: scrubbed.get(k, 0) for k in TYPES}, "elapsed_s": round(time.time() - t0, 1),
            "env": {"deploys": env["deploys"], "services": services}}
