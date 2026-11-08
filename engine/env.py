"""Minimal .env loader (stdlib only), so keys and models live in a file, not the terminal.

Reads KEY=VALUE lines from <project>/.env. Blank lines, # comments, an optional `export `
prefix and surrounding quotes are handled. Variables already set in the environment win,
so `GRADIUM_API_KEY=... ./run.sh` still overrides the file for a single run.
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env(path=ENV_FILE, override=False):
    """Load path into os.environ. Returns the names that were set from the file."""
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # inline comment after an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if not key or not value:  # empty values don't clobber defaults
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
