"""IncidentVoice: voice-first autonomous incident investigation (Streamlit UI).

    streamlit run app.py
"""
import time
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from engine import ENV_LOADED, llm, stt, tts
from engine.agents import fmt_t
from engine.orchestrator import run_investigation
from engine.report import build_report
from engine.simulator import SimulatedEnvironment, verify
from privacy_layer import describe, read_log, summarize

ROOT = Path(__file__).resolve().parent
SAMPLE = ("Checkout is down. Customers are getting HTTP 500 errors and the checkout page times out. It started "
          "around 2pm, right after we released checkout version 2.3. One customer, jane.doe@example.com, "
          "called support about a failed payment. Please investigate.")
AGENT_ICON = {"Privacy": "🛡️", "Triage": "🧭", "Log analyst": "📈", "Coordinator": "🧩", "Deploy analyst": "🚀",
              "Dependency analyst": "🕸️", "History (RAG)": "📚", "Root cause": "🎯", "Remediation": "🛠️"}

st.set_page_config(page_title="IncidentVoice", page_icon="🎙️", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 3.2rem; max-width: 1400px;}
.iv-steps {display:flex; gap:.4rem; flex-wrap:wrap; margin:.2rem 0 1rem;}
.iv-step {padding:.32rem .8rem; border-radius:999px; font-size:.85rem; border:1px solid #2d3142; color:#9aa0b4;}
.iv-step.done {background:#1d3b2a; color:#7ee2a8; border-color:#2f6b47;}
.iv-step.now {background:#7c5cff; color:white; border-color:#7c5cff; font-weight:600;}
.iv-live {border:1px solid #e5484d; border-radius:10px; padding:.8rem 1rem; background:#2a1215; font-size:1.05rem;}
.iv-live b {color:#ff6b6b;}
.iv-hero {font-size:2.1rem; font-weight:750; margin-bottom:0;}
.iv-sub {color:#9aa0b4; margin-top:.1rem;}
html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {overflow-x: hidden !important;}
.iv-trace {font-family: ui-monospace, Menlo, monospace; font-size:.86rem;}
</style>""", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("session_start", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
for k, v in {"audio_key": 0, "description": "", "inv": None, "sim": None, "approvals": [], "executions": [], "report": None, "tts": None}.items():
    ss.setdefault(k, v)
if "pending_transcript" in ss:  # set before the text_area widget is created
    ss.description = ss.pop("pending_transcript")


def reset():
    for k in ("inv", "sim", "report", "tts"):
        ss[k] = None
    ss.approvals, ss.executions = [], []


def load_sample():
    reset()
    ss.description = SAMPLE


# ------------------------------------------------------------------ sidebar
models = llm.list_models()
with st.sidebar:
    st.markdown("## 🎙️ IncidentVoice")
    st.caption("Voice-first autonomous incident response")
    if models:
        model = st.selectbox("Local LLM (Ollama)", models, index=models.index(llm.pick_default(models)))
        st.success(f"Ollama online · {len(models)} open models", icon="🟢")
    else:
        model = llm.DEFAULT_MODEL
        st.error("Ollama not reachable at " + llm.OLLAMA + ". Agents will use rule-based fallbacks.", icon="🔴")
    if stt.api_key():
        st.success("Gradium STT + TTS key loaded", icon="🎤")
        st.caption("Key source: " + (".env file" if "GRADIUM_API_KEY" in ENV_LOADED else
                                     "terminal environment (overrides .env)"))
    else:
        st.warning("GRADIUM_API_KEY not set. Add it to the .env file (see .env.example) and restart. "
                   "Until then, type or upload instead.", icon="🎤")

    st.markdown("### 🛡️ Privacy")
    egress = read_log()
    mine = summarize([r for r in egress if r["ts"] >= ss.session_start])
    st.markdown(f"- LLM runs **locally**: {'yes' if llm.is_local() else 'NO (remote, gated)'}\n"
                f"- Logs scrubbed before any agent sees them\n"
                f"- **Left this laptop (this session):**\n"
                f"  - 🎤 speech-to-text uploads: **{mine['stt']}**\n"
                f"  - 🔊 text-to-speech requests: **{mine['tts']}**\n"
                + (f"  - other: **{mine['other']}**\n" if mine["other"] else "")
                + f"  - ⛔ blocked by leak gate: **{mine['blocked']}**")
    st.caption(f"Everything else stayed local. Full audit log: {len(egress)} entries since install.")
    with st.expander("Egress audit log"):
        if egress:
            for r in egress[-6:][::-1]:
                st.caption(f"{r['ts'][11:19]} · {'⛔ BLOCKED' if r.get('blocked') else '✅ sent'} · "
                           f"{r['destination']} · {r['bytes']} B · {r.get('purpose') or ''}")
        else:
            st.caption("Nothing has left this machine.")
    st.divider()
    st.button("↺ New incident", on_click=lambda: (reset(), ss.update(
        description="", session_start=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))),
        use_container_width=True)

# ------------------------------------------------------------------ header + stepper
verified = any(e["verification"]["passed"] for e in ss.executions)
stage = 1 if not ss.inv else 3 if not verified else 4 if not ss.report else 5
steps = ["① Report", "② Investigate", "③ Approve", "④ Execute & verify", "⑤ Postmortem"]
chips = "".join(f'<span class="iv-step {"done" if i + 1 < stage or (i == 1 and ss.inv) else "now" if i + 1 == stage else ""}">{s}</span>'
                for i, s in enumerate(steps))
st.markdown('<p class="iv-hero">🎙️ IncidentVoice</p><p class="iv-sub">Speak the symptoms. Agents investigate logs, '
            'deploys, past incidents and dependencies, then fix it after your approval.</p>'
            f'<div class="iv-steps">{chips}</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ 1. report
st.subheader("1 · Report the incident")
left, right = st.columns([3, 2], gap="large")
with left:
    audio = st.audio_input("🎙️ Describe the incident by voice", sample_rate=24000, disabled=not stt.api_key(),
                           key=f"mic_{ss.audio_key}")
    with st.expander("Or upload a WAV recording instead"):
        wav_up = st.file_uploader("WAV file", type=["wav"], label_visibility="collapsed", key=f"wav_{ss.audio_key}")
    voice = audio or wav_up
    live = st.empty()
    if voice and st.button("📝 Transcribe live with Gradium", disabled=not stt.api_key()):
        text = ""
        try:
            for text in stt.transcribe_stream(voice.getvalue()):
                live.markdown(f'<div class="iv-live"><b>● LIVE</b>&nbsp; {text}</div>', unsafe_allow_html=True)
            ss.pending_transcript = text
            ss.audio_key += 1  # fresh recorder widget for the next take
            st.rerun()
        except Exception as e:
            st.error(f"Transcription failed: {e}")
            if stt.last_diagnostics:
                with st.expander("Gradium diagnostics"):
                    st.json(stt.last_diagnostics)
    st.text_area("Incident description (the live transcript lands here; edit it or just type)", key="description",
                 height=120, placeholder="e.g. Checkout is throwing 500s since the 2pm deploy…")
with right:
    up = st.file_uploader("📄 Attach a log file (optional)", type=["log", "txt", "jsonl", "json"])
    st.caption("Connected (simulated) production telemetry: logs for **checkout, payments, inventory**, "
               "**4** deploys, **15** past postmortems, service dependency map.")
    st.button("✨ Use sample incident", on_click=load_sample, use_container_width=True)

go = st.button("🚀 Investigate", type="primary", use_container_width=True, disabled=not ss.description.strip())
if go:
    reset()
    with st.status("🤖 Agents investigating…", expanded=True) as status:
        def on_event(ev):
            icon = AGENT_ICON.get(ev["agent"], "•")
            extra = f" · _LLM {ev['llm']['seconds']} s_" if ev.get("llm") else ""
            st.markdown(f'<div class="iv-trace">[{ev["t"]:>5.1f}s] {icon} <b>{ev["agent"]}</b>: {ev["msg"]}{extra}</div>',
                        unsafe_allow_html=True)
        upload = (up.name, up.getvalue().decode("utf-8", "replace")) if up else None
        inv = run_investigation(ss.description, model, upload, on_event)
        status.update(label=f"✅ Investigation complete in {inv['elapsed_s']:.0f} s. Root cause identified.", state="complete",
                      expanded=False)
    ss.inv = inv
    ss.sim = SimulatedEnvironment(inv["env"]["deploys"], inv["env"]["services"])
    st.rerun()

inv = ss.inv
if not inv:
    st.stop()

# ------------------------------------------------------------------ 2. findings
st.divider()
st.subheader("2 · Investigation findings")
tri, la, rca, deps = inv["triage"], inv["logs"], inv["rca"], inv["deps"]
m = st.columns(6)
m[0].metric("Severity", tri["severity"])
m[1].metric("Service", tri["affected_service"])
m[2].metric("Error onset", fmt_t(la.get("onset")))
m[3].metric("Peak error rate", f"{la.get('peak_error_rate', 0):.0%}")
m[4].metric("Blast radius", f"{len(deps['blast_radius'])} services")
m[5].metric("Time to diagnosis", f"{inv['elapsed_s']:.0f} s")
st.info(f"**What happened:** {rca['narrative']}", icon="🧠")

c1, c2 = st.columns(2, gap="large")
with c1:
    with st.container(border=True):
        st.markdown("#### 📈 Log analyst")
        st.caption(inv["summaries"]["logs"])
        if la.get("timeline"):
            df = pd.DataFrame([{"time": r["t"], "error rate": r["error_rate"], "requests": r["total"]} for r in la["timeline"]])
            base = alt.Chart(df).mark_area(line={"color": "#e5484d"}, color="#e5484d33").encode(
                x=alt.X("utchoursminutes(time):T", title="time (UTC)"),
                y=alt.Y("error rate:Q", axis=alt.Axis(format="%"), title="error rate"))
            marks = pd.DataFrame([{"time": pd.Timestamp(d["timestamp"]), "label": f"deploy {d['version']}"}
                                  for d in inv["deploys"][:1] if d.get("minutes_before_onset") is not None])
            chart = base
            if not marks.empty:
                chart = base + alt.Chart(marks).mark_rule(color="#7c5cff", strokeDash=[4, 3], size=2).encode(
                    x="utchoursminutes(time):T") + alt.Chart(marks).mark_text(color="#b8a6ff", dx=-40, align="center").encode(
                    x="utchoursminutes(time):T", y=alt.value(12), text="label")
            st.altair_chart(chart.properties(height=200), use_container_width=True)
        if la.get("signatures"):
            st.dataframe(pd.DataFrame([{"signature": s["signature"], "count": s["count"], "first seen": fmt_t(s["first_seen"])}
                                       for s in la["signatures"][:5]]), hide_index=True, use_container_width=True)
    with st.container(border=True):
        st.markdown("#### 📚 History agent (RAG over past postmortems)")
        st.caption(inv["summaries"].get("history", "") or "BM25 retrieval over 15 local postmortems; top hits fed to the root-cause agent.")
        top_score = inv["history"]["hits"][0]["score"] if inv["history"]["hits"] else 1
        for h in inv["history"]["hits"]:
            st.markdown(f"**{h['id']}** · {h['title']}")
            st.progress(min(1.0, h["score"] / top_score), text=f"relevance {h['score']:.1f}")
            with st.expander("Root cause and fix"):
                st.markdown(f"**Root cause:** {h['root_cause']}\n\n**Fix:** {h['fix']}")
with c2:
    with st.container(border=True):
        st.markdown("#### 🚀 Deploy analyst")
        st.caption(inv["summaries"]["deploys"])
        rows = [{"version": d["version"], "service": d["service"], "author": d["author"],
                 "min before onset": d["minutes_before_onset"],
                 "config change": ch["key"], "old → new": f"{ch['old']} → {ch['new']}",
                 "suspicious": "⚠️" if ch["suspicious"] else ""}
                for d in inv["deploys"] for ch in d["changes"]]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    with st.container(border=True):
        st.markdown("#### 🕸️ Dependency analyst · blast radius")
        st.caption(inv["summaries"]["deps"])
        st.graphviz_chart(deps["dot"], use_container_width=True)

with st.container(border=True):
    st.markdown("#### 🎯 Root-cause agent · ranked hypotheses")
    for i, h in enumerate(rca["hypotheses"][:4]):
        st.progress(h["confidence"], text=f"**#{i + 1} · {h['confidence']:.0%}** · {h['statement']}")
        with st.expander("Evidence and reasoning", expanded=(i == 0)):
            st.markdown(f"_{h['explanation']}_")
            st.markdown("\n".join(f"- {e}" for e in h["evidence"]))
            st.caption(f"evidence score {h['det_confidence']:.2f} · LLM confidence "
                       f"{h['llm_confidence'] if h['llm_confidence'] is not None else 'n/a'}")

with st.expander(f"🧾 Agent trace ({len(inv['trace'])} steps)"):
    st.dataframe(pd.DataFrame([{"t (s)": e["t"], "agent": e["agent"], "message": e["msg"],
                                "LLM": f"{e['llm']['model']} {e['llm']['seconds']}s {'ok' if e['llm']['ok'] else 'fallback'}"
                                if e.get("llm") else ""} for e in inv["trace"]]), hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ 3. approval
st.divider()
st.subheader("3 · Recommended remediation: human approval required")
actions = {a["id"]: a for a in inv["actions"]}
tried = {e["action"]["id"] for e in ss.executions}
if not verified:
    rec = rca["recommended_action_id"]
    order = [rec] + [a for a in actions if a != rec]
    with st.container(border=True):
        choice = st.radio("Action to execute", order, format_func=lambda a: (
            ("⭐ RECOMMENDED · " if a == rec else "") + actions[a]["title"] + f"  (risk {actions[a]['risk']}, ~"
            f"{actions[a]['est_minutes']} min)" + ("  · already tried" if a in tried else "")))
        a = actions[choice]
        st.code(a["command"], language="bash")
        if choice == rec:
            st.markdown(f"**Why:** {rca['action_rationale']}")
            if rca.get("guardrail"):
                st.caption(f"🛡️ {rca['guardrail']}")
        cc = st.columns([2, 3])
        approver = cc[0].text_input("Approver", value="on-call engineer")
        ok = cc[1].checkbox("I have reviewed the evidence and approve running this action in the simulated "
                            "production environment.", key=f"ok_{len(ss.executions)}")
        b1, b2 = st.columns(2)
        approve = b1.button("✅ Approve & execute", type="primary", disabled=not ok, use_container_width=True)
        if b2.button("✋ Reject", use_container_width=True):
            ss.approvals.append({"at": datetime.now(timezone.utc), "approver": approver, "decision": "rejected",
                                 "action_title": a["title"]})
            st.warning("Rejected. Pick another action or start a new investigation.")
    if approve:
        ss.approvals.append({"at": datetime.now(timezone.utc), "approver": approver, "decision": "approved",
                             "action_title": a["title"]})
        sim = ss.sim
        svc = tri["affected_service"]
        with st.status(f"⚙️ Executing in simulated environment: {a['title']}", expanded=True) as status:
            pre = sim.snapshot(svc)  # state before the action, for before/after deltas
            log = sim.execute(a)
            for line in log:
                st.markdown(f'<div class="iv-trace">{line}</div>', unsafe_allow_html=True)
                time.sleep(0.25)
            st.markdown("**Health checks** (every 30 s, simulated clock)")
            chart = st.empty()
            samples = [pre]
            for _ in range(8):
                samples.append(sim.health_check(svc))
                chart.line_chart(pd.DataFrame({"error rate %": [s["error_rate"] * 100 for s in samples]}), height=160)
                time.sleep(0.35)
            v = verify(samples)
            status.update(label=("✅ Recovery verified" if v["passed"] else "❌ Recovery NOT verified")
                          + f": {a['title']}", state="complete" if v["passed"] else "error", expanded=False)
        ss.executions.append({"at": datetime.now(timezone.utc), "action": a, "approver": approver, "log": log,
                              "samples": samples, "verification": v})
        st.rerun()
else:
    st.success("Remediation approved, executed and verified.", icon="✅")

# ------------------------------------------------------------------ 4. execution results
def render_attempt(n, e):
    v = e["verification"]
    head = st.columns([4, 1])
    head[0].markdown(f"**Attempt {n}: {e['action']['title']}** · approved by {e['approver']} at {fmt_t(e['at'])}")
    head[1].markdown("#### ✅ Verified" if v["passed"] else "#### ❌ Not verified")
    k = st.columns(4)
    f0, f1 = e["samples"][0], e["samples"][-1]
    k[0].metric("Error rate", f"{f1['error_rate']:.1%}", f"{(f1['error_rate'] - f0['error_rate']) * 100:+.1f} pts",
                delta_color="inverse")
    k[1].metric("p99 latency", f"{f1['p99_ms']} ms", f"{f1['p99_ms'] - f0['p99_ms']:+} ms", delta_color="inverse")
    k[2].metric("Pool waiting", f1["pool_waiting"], f1["pool_waiting"] - f0["pool_waiting"], delta_color="inverse")
    k[3].metric("Status", f1["status"])
    df = pd.DataFrame([{"check": fmt_t(s["t"]), "error rate": s["error_rate"], "p99 (s)": s["p99_ms"] / 1000}
                       for s in e["samples"]])
    st.altair_chart(alt.Chart(df).mark_line(point=True, color="#30a46c" if v["passed"] else "#e5484d").encode(
        x=alt.X("check:N", sort=None, title="health check (simulated clock)"),
        y=alt.Y("error rate:Q", axis=alt.Axis(format="%"))).properties(height=170), use_container_width=True)
    st.caption(f"Criteria: {v['criteria']}")


if ss.executions:
    st.divider()
    st.subheader("4 · Execution and recovery verification")
    win = next((i for i, e in enumerate(ss.executions) if e["verification"]["passed"]), None)
    for i, e in enumerate(ss.executions):
        n = i + 1
        if e["verification"]["passed"]:
            with st.container(border=True):
                render_attempt(n, e)
                if n > 1:
                    st.success(f"Attempt {n} restored service. Earlier attempt(s) above were caught by verification "
                               "and did not count as a fix.", icon="✅")
        elif win is not None:  # superseded failure: keep it, but collapsed and clearly labelled
            with st.expander(f"Attempt {n}: {e['action']['title']} · ❌ not verified (superseded by attempt {win + 1})"):
                render_attempt(n, e)
        else:
            with st.container(border=True):
                render_attempt(n, e)
                st.error(f"Attempt {n} did not restore service. The root cause is still active. "
                         "Choose another action above.")

# ------------------------------------------------------------------ 4b. talk back (Gradium TTS)
if verified:
    final = next(e for e in reversed(ss.executions) if e["verification"]["passed"])
    with st.container(border=True):
        st.markdown("#### 🔊 IncidentVoice reports back")
        if stt.api_key():
            if st.button("🔊 Speak verified summary (Gradium TTS)", use_container_width=True):
                summary = tts.spoken_summary(inv, final, attempts=len(ss.executions))
                try:
                    with st.spinner("Gradium is synthesizing the summary…"):
                        wav, sent = tts.speak(summary)
                    ss.tts = {"wav": wav, "text": sent, "autoplay": True}
                except Exception as e:  # EgressBlocked, TTSError, network
                    st.error(f"Text-to-speech failed: {e}")
                else:
                    st.rerun()  # redraw so the sidebar's egress counter includes this request
            if ss.tts:
                st.audio(ss.tts["wav"], format="audio/wav", autoplay=ss.tts.pop("autoplay", False))
                st.caption(f"🛡️ Sent to Gradium after scrubbing + leak gate (logged in the egress audit log): "
                           f"“{ss.tts['text']}”")
        else:
            st.caption("Set GRADIUM_API_KEY to hear the verified summary spoken by Gradium TTS.")

# ------------------------------------------------------------------ 5. report
if verified:
    st.divider()
    st.subheader("5 · Incident report and postmortem")
    if not ss.report and st.button("📝 Generate incident report & postmortem", type="primary", use_container_width=True):
        with st.spinner("Postmortem agent is writing the report with the local LLM…"):
            md, _ = build_report(inv, ss.executions, ss.approvals, model, egress_since=ss.session_start)
        out = ROOT / "reports"
        out.mkdir(exist_ok=True)
        path = out / f"incident_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.md"
        path.write_text(md)
        ss.report = {"md": md, "path": str(path)}
        st.rerun()
    if ss.report:
        st.download_button("⬇️ Download report (.md)", ss.report["md"], file_name=Path(ss.report["path"]).name,
                           mime="text/markdown")
        st.caption(f"Saved to {ss.report['path']}")
        with st.container(border=True):
            st.markdown(ss.report["md"])
