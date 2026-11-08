# 🎙️ IncidentVoice

**Speak the symptoms. Agents find the root cause and fix it after you approve.**

IncidentVoice is a voice-first, privacy-first incident response prototype. An on-call engineer
describes a production incident out loud, with an optional log file. A team of AI agents then
investigates logs, deploy history, past postmortems and service dependencies, ranks probable
root causes with evidence, and recommends a fix. After explicit human approval, it executes
the fix in a simulated production environment, verifies recovery with health checks, and
writes the postmortem.

Everything except speech-to-text runs on the laptop: open-source LLMs via Ollama, local
BM25 retrieval, and regex PII scrubbing. The one outbound call (audio to Gradium) goes through
a leak gate and an audit log.

![demo](docs/incidentvoice_demo.gif)

## Quick start

```bash
git clone https://github.com/MyclineShareena/HACK1984.git && cd HACK1984
ollama pull qwen2.5:7b             # once; any Ollama model works (llama3.2:3b is faster)
export GRADIUM_API_KEY=...         # optional: enables voice input
./run.sh                           # creates .venv, installs deps, opens http://localhost:8501
```

Manual equivalent:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python generate_data.py  # builds data/logs (also done automatically on first run)
.venv/bin/streamlit run app.py
```

Requirements: Python 3.10+, [Ollama](https://ollama.com) running locally. If Ollama is down,
every agent falls back to rules and the demo still works end to end.

### Demo in 60 seconds
1. Click **✨ Use sample incident**, or record your voice and press **📝 Transcribe live with
   Gradium**. You can also type a description or attach a `.log` file.
2. Click **🚀 Investigate**. Watch the agent trace stream in (~25 s with `qwen2.5:7b`).
3. Review the findings: error timeline with the deploy marker, the suspicious config diff,
   similar past incidents, the blast-radius graph, and ranked hypotheses with evidence.
4. Optional, to show safety: pick **Restart checkout pods**, tick approval, execute. Health
   checks show the errors return, so the result is **❌ not verified**.
5. Pick the **⭐ RECOMMENDED** fix (revert `db.pool.max_size` to 50), approve, execute. You
   get **✅ Verified**.
6. Click **📝 Generate incident report & postmortem**, then download the Markdown.

## How it works

```
 voice ──► Gradium STT (wss, gated + audited) ──► transcript ─┐
 typed text / uploaded log ───────────────────────────────────┤
                                                              ▼
                                    🛡️ Privacy layer: scrub PII/secrets
                                                              ▼
 🧭 Triage agent (LLM): severity, category, affected service
 📈 Log analyst: error timeline, onset, signatures, latency, observed config values
      ├─ 🚀 Deploy analyst: deploys before onset, config diffs, value/keyword evidence ┐
      ├─ 🕸️ Dependency analyst: health of deps, blast radius, graph                     ├ parallel
      └─ 📚 History agent (RAG): BM25 over postmortems                                 ┘
 🎯 Root-cause agent (LLM + evidence scores): ranked hypotheses, explanations
 🛠️ Remediation: action catalog + guardrail (must address top cause) ──► 👤 HUMAN APPROVAL
 ⚙️ Simulated prod executes action ──► 🩺 8 health checks ──► verify (or try another action)
 📝 Postmortem agent (LLM): Markdown report with timeline, evidence, verification, follow-ups
```

**Design choices that matter for judging**
- **Evidence first, LLM second.** Agents compute facts deterministically: onset time,
  config diffs, value matches such as `pool.max=5` in the error events, and BM25 hits. The
  LLM ranks the hypotheses and explains them. The final confidence blends both, and every
  LLM output is schema-constrained and validated.
- **Guardrails.** The LLM can only pick actions from a catalog, and the action must address
  the top-ranked cause. Otherwise it is overridden, and the override is shown in the UI.
- **Verification is real.** The simulator's physics don't depend on the diagnosis. Restart
  gives only brief relief and scale-out only partial relief; reverting the wrong key does
  nothing. Only the real fix passes the health checks.
- **Human approval.** Nothing executes without a named approver ticking the checkbox.
  Approvals and rejections both go into the report timeline.
- **Privacy.** Logs are scrubbed on load. The LLM runs on localhost, Streamlit telemetry is
  off, and the server binds to localhost only. Audio is the only data that leaves the laptop,
  and it is logged in `egress_log.jsonl` with its size and sha256.

## Project layout

```
HACK1984/
  app.py                  Streamlit UI (5-step flow)
  run.sh                  one-command start
  engine/
    stt.py                Gradium streaming STT (websocket, 24 kHz PCM, live partials)
    llm.py                Ollama client, JSON-schema output, egress-gated if remote
    data.py               log parsing (JSON lines or plain text), scrubbing, analysis
    agents.py             triage, logs, deploys, dependencies, history, root cause, actions
    orchestrator.py       runs the agents (parallel stage) and records the trace
    simulator.py          simulated prod: execute actions, health checks, verify
    report.py             postmortem generator
  privacy_layer/          stdlib-only: scrubber, egress gate, BM25 incident search
  data/                   synthetic scenario (seed 1984): deploys, services, 15 postmortems;
                          logs/ + planted_secrets.json are generated on first run (they hold FAKE
                          secrets for the scrubber tests, so they're kept out of git)
  tests/                  19 tests (scrubber recall, egress gate, search, offline engine)
  scripts/capture_demo.py headless-Chrome walkthrough → docs/screenshots + docs/*.gif
  docs/                   screenshots, demo GIF, SUBMISSION.md
  demo_scrub.py           privacy demo for judges (raw vs scrubbed lines + leak gate)
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `GRADIUM_API_KEY` | (unset) | Enables voice input |
| `GRADIUM_STT_URL` | `wss://api.gradium.ai/api/speech/asr` | e.g. the EU endpoint `wss://eu.api.gradium.ai/api/speech/asr` |
| `GRADIUM_PACE` | `0.25` | Audio send pacing (0.25 = 4x real time; lower = faster) |
| `OLLAMA_HOST` | `http://localhost:11434` | A remote host is allowed but goes through the leak gate |
| `INCIDENTVOICE_MODEL` | `qwen2.5:7b` | Default model (selectable in the sidebar) |
| `INCIDENTVOICE_EGRESS_LOG` | `./egress_log.jsonl` | Audit log path |

## Privacy layer API (for the agent flow)

```python
from privacy_layer import scrub, log_egress, search_incidents, EgressBlocked
clean, counts = scrub(text)                 # [EMAIL] [IP] [CARD] [API_KEY] [TOKEN] [PHONE]
safe = log_egress("gradium", payload)       # scrub + leak gate + audit log; send `safe`
hits = search_incidents("pool exhausted after deploy", k=3)
```

- 158/158 planted secrets removed (100% recall per type, 0 false positives on these logs).
- `python demo_scrub.py` shows raw vs scrubbed lines. `python -m privacy_layer.egress` prints
  the audit log.

## Tests and demo assets

```bash
.venv/bin/python -m unittest discover tests     # 19 tests, < 1 s, no LLM needed
.venv/bin/python scripts/capture_demo.py        # with the app running: screenshots + GIF
```

## Limitations (honest)
- **Voice input isn't truly live.** `st.audio_input` records a clip, then streams it to
  Gradium, so the transcript appears word by word *after* you stop recording. True
  while-you-speak streaming needs a browser component with a short-lived Gradium token.
- **The environment is simulated.** The `kubectl` commands are displayed, not run. The
  physics cover capacity-type config regressions well, and other fault types only coarsely.
- **Uploaded logs are analyzed with the bundled deploys and service map.** Real use would
  connect to CI/CD and a service catalog.
- **Spoken numbers aren't scrubbed.** The scrubber is regex-based, so a phone number read
  out as words ("four one five…") is not caught.
