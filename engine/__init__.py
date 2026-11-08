"""IncidentVoice engine. Importing it loads <project>/.env first, so every module sees the
configured keys and models (real environment variables still take precedence)."""
from .env import load_env

ENV_LOADED = load_env()
