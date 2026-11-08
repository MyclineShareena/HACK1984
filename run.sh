#!/usr/bin/env bash
# One-command start: venv + deps (first run only), sanity checks, then the app on http://localhost:8501
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
[ -f .env ] || { cp .env.example .env && chmod 600 .env && echo "📝 Created .env from .env.example: add your GRADIUM_API_KEY there."; }
curl -sf "${OLLAMA_HOST:-http://localhost:11434}/api/tags" >/dev/null \
  || echo "⚠️  Ollama not reachable: start it with 'ollama serve' and 'ollama pull qwen2.5:7b' (agents fall back to rules)."
[ -n "$GRADIUM_API_KEY" ] || grep -qE '^[[:space:]]*(export[[:space:]]+)?GRADIUM_API_KEY=[^[:space:]#]' .env || echo "ℹ️  GRADIUM_API_KEY not set: voice input and spoken summaries disabled; typing and log upload still work."
[ -f data/logs/checkout.log ] || .venv/bin/python generate_data.py
echo "▶  IncidentVoice: open http://localhost:8501"
exec .venv/bin/streamlit run app.py
