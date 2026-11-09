# 🎙️ IncidentVoice -  AI-powered, voice-first IT incident management and autonomous root-cause analysis platform

**Speak the symptoms. Agents find the root cause and fix it after you approve.**

IncidentVoice is a voice-first, privacy-first incident response prototype. An on-call engineer
describes a production incident out loud, with an optional log file. A team of AI agents then
investigates logs, deploy history, past postmortems and service dependencies, ranks probable
root causes with evidence, and recommends a fix. After explicit human approval, it executes
the fix in a simulated production environment, verifies recovery with health checks, **speaks
the verified summary back** (Gradium text-to-speech), and writes the postmortem.

Everything except Gradium's voice in and voice out runs on the laptop: open-source LLMs via
Ollama, local BM25 retrieval, and regex PII scrubbing. The two outbound calls (incident audio to
Gradium STT, scrubbed summary text to Gradium TTS) both go through a leak gate and an audit log.

