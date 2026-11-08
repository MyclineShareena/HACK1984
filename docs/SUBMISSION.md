# IncidentVoice: from spoken symptoms to verified recovery

**Team Tensor Titans · HACK1984** · Mycline Shareena, Sanika Girase

*Speak the symptoms. Agents find the root cause and fix it after you approve.*

## The problem

At 2 a.m. a pager goes off: "checkout error rate high". Recovery takes as long as it does
mostly because the on-call engineer has to piece together context by hand:

- grep through logs from several services to find when errors started and what they say;
- scroll deploy history to see what changed, and read config diffs;
- search old tickets and postmortems for "have we seen this before?";
- work out which services depend on which, and how far the damage spreads;
- decide on a fix, run it, watch dashboards to confirm recovery, then write the postmortem.

Each of these steps uses a different tool, and the context switching is what makes mean time
to recovery long. AI assistants could help, but pasting production logs into a cloud chatbot
leaks customer emails, card numbers, IPs and API keys. Letting an AI execute fixes on its own,
with no human in the loop, is a risk no SRE team will accept.

## Our solution

IncidentVoice lets the engineer just **talk**. They describe the incident by voice (Gradium
speech-to-text, live transcript), type it, and/or attach a log file. Then a team of AI agents
investigates on its own, and once the fix is verified, IncidentVoice **talks back** with a spoken
summary (Gradium text-to-speech):

| Agent | What it does |
|---|---|
| 🧭 Triage (LLM) | Severity, category, affected service, suspected trigger |
| 📈 Log analyst | Error-rate timeline, onset time, error signatures, latency shift, observed config values |
| 🚀 Deploy analyst | Deploys before the onset, config diffs, and whether the *new value* shows up in the errors |
| 🕸️ Dependency analyst | Dependency health, blast radius, and whether downstream warnings are symptoms or causes |
| 📚 History agent (RAG) | BM25 retrieval over past postmortems; top hits are fed to the root-cause agent |
| 🎯 Root-cause agent (LLM) | Ranks hypotheses by blending evidence scores with LLM judgment, citing evidence |
| 🛠️ Remediation | Picks from a safe action catalog, with a guardrail that the fix must address the top cause |
| 👤 **Human approval** | A named approver must tick the box; nothing runs without it |
| ⚙️ Executor + 🩺 Verifier | Runs the action in a simulated prod environment, then requires 4 healthy health checks in a row |
| 🔊 Talk-back (Gradium TTS) | Speaks the verified summary aloud; the text is scrubbed and leak-checked first |
| 📝 Postmortem (LLM) | Blameless report: timeline, impact, evidence, verification, follow-ups |

**What sets it apart:** IncidentVoice goes from the first spoken symptom all the way to
**verified** recovery, and a human approves before anything changes. In the demo, we approve
the wrong fix first (restart pods). The health checks show errors coming back, the system marks
it **not verified** and asks for another action. The recommended fix (revert
`db.pool.max_size` 5 → 50) then passes verification.

**Privacy by design (HACK1984):**
- All reasoning runs on **open-source models on the laptop** (Qwen2.5-7B / Llama 3.2-3B via Ollama).
- Logs are **scrubbed on load**: emails, IPs, cards, API keys, tokens and phones are replaced
  with typed placeholders. Recall is 100% on 158 planted secrets.
- **Egress leak gate + audit log:** every outbound request goes through one gate and is logged in
  `egress_log.jsonl`, so judges can see exactly what left the laptop. Text (the spoken summary)
  is scrubbed, then re-checked against a second, broader detector set and blocked if anything
  is left; it's logged verbatim. Audio can't be scanned, so it must be explicitly allowed, and
  it's logged with its size and sha256 hash.
- Streamlit telemetry is off, and the server binds to localhost only.

## Results on the demo scenario
The scenario: checkout deploy v2.3 shrank the Postgres pool from 50 to 5.
- Correct root cause ranked #1 at about 63–66% confidence. The runner-up is a decoy change in
  the same deploy (`idle_timeout_s`) at 18%.
- Time to diagnosis: **about 25–35 s** on a MacBook with local `qwen2.5:7b` (2 LLM calls; a third
  writes the postmortem when you ask for it).
- The most similar past incident (INC-2025-014, pool exhausted after a config refactor) is
  retrieved as the top hit.
- The wrong fix is caught by verification, and the right fix brings the error rate from 82.5%
  to 0% and p99 latency from 29.9 s to 192 ms in simulation.

## Slide: by the numbers
- **158 / 158** planted secrets removed: 100% recall for every type, 0 false positives
- **#1** correct root cause at ~66% confidence; the decoy change in the same deploy gets 18%
- **25–35 s** from spoken report to diagnosis, on a laptop, with a local open-source model
- **1 wrong fix caught**, 1 right fix verified: error rate 82% → 0%, p99 30 s → 0.2 s
- **2** outbound calls (voice in, voice out), both gated and audited · **0** log bytes leave the laptop

## Questions to be ready for
- **"The audio goes out unscrubbed?"** The engineer describes symptoms, not customer data; the
  logs (where customer data lives) never leave. Every upload is hashed and audited, and a local
  Whisper option is next on the roadmap. The spoken summary going back out is already scrubbed
  and leak-checked.
- **"What if the model is wrong?"** Catalog-only actions, a guardrail, human approval and
  health-check verification; the demo shows a wrong fix being caught.
- **"What if Ollama is down?"** Rule-based fallbacks; the demo still completes.

## What's next
*(Paste over the Devpost "What's next" section. Spoken summaries are done, so they're no longer listed.)*
- **Local speech-to-text (Whisper)** so the incident audio never leaves the laptop either.
- **Truly live transcription** while you speak, with a browser component and short-lived Gradium tokens.
- **Real integrations:** read-only connectors for CI/CD, Kubernetes, and log and metrics stores,
  plus a dry-run mode that executes approved actions against staging.
- **More fault types** in the simulator and analysis: memory leaks, bad feature flags, dependency outages.
- **Learning from resolved incidents:** each verified postmortem is added to the retrieval index automatically.

## Tech stack
Python · Streamlit · Ollama (Qwen2.5-7B, Llama 3.2-3B) · Gradium STT + TTS (websockets) · BM25
(stdlib) · regex PII scrubber · Altair · Graphviz · headless Chrome (demo capture)

## Demo video script (≈ 2 min)
| Time | Show | Say |
|---|---|---|
| 0:00 | Title screen, sidebar privacy panel | "It's 2 a.m., checkout is down. Instead of opening five dashboards, I just talk to IncidentVoice. Everything runs on this laptop with open-source models." |
| 0:12 | Press the mic, speak the incident, then **Transcribe live with Gradium** | "Checkout is down, customers get 500s since the 2 p.m. release of version 2.3…" (the transcript streams in word by word) |
| 0:30 | **Investigate**: agent trace streams | "The agents get to work: triage and logs first, then deploys, dependencies and past incidents (RAG) in parallel." |
| 0:55 | Findings: timeline with deploy marker, config diff, graph, hypotheses | "Errors start 4 minutes after deploy v2.3, which cut the DB pool from 50 to 5. The errors literally say 'limit of size 5'. We saw this before in INC-2025-014." |
| 1:15 | Pick **Restart checkout pods**, approve | "Let's try the lazy fix…" Health checks: errors come back, **not verified**. "It won't let me fool myself." |
| 1:35 | Approve the **recommended** revert | Error rate falls to 0%, **✅ Verified**. |
| 1:42 | **Speak verified summary** | IncidentVoice says: "Incident resolved and verified…" Say: "And it talks back, through the same leak gate." |
| 1:52 | Generate postmortem; sidebar egress log | "The postmortem writes itself. And here is every byte that left the laptop: the voice in and the voice out, nothing else." |

Screenshots: `docs/screenshots/01…08_*.png` (`02a_live_transcript.png` shows Gradium streaming the spoken report; `06b_talk_back.png` shows Gradium speaking the verified summary). Automated walkthrough GIF: `docs/incidentvoice_demo.gif`.
