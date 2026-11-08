"""Local open-source LLM client (Ollama). JSON-schema constrained output, no cloud calls.

If OLLAMA_HOST points somewhere other than this machine, every prompt goes through the
egress leak gate first, so remote use is scrubbed and audited too.
"""
import json
import os
import time
from urllib.parse import urlparse

import requests

from privacy_layer import log_egress

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
DEFAULT_MODEL = os.environ.get("INCIDENTVOICE_MODEL", "qwen2.5:7b")
PREFERRED = ["qwen2.5:7b", "llama3.2:3b", "qwen3.5:4b", "qwen2.5:3b-instruct"]


def is_local():
    return urlparse(OLLAMA).hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def list_models():
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=3)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def pick_default(models):
    if DEFAULT_MODEL in models:
        return DEFAULT_MODEL
    for m in PREFERRED:
        if m in models:
            return m
    return models[0] if models else DEFAULT_MODEL


def chat_json(system, user, schema, model=None, temperature=0.1, timeout=240):
    """Return (parsed_dict_or_None, meta). Never raises; callers fall back to rules on None."""
    model = model or DEFAULT_MODEL
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    body = {"model": model, "messages": messages, "format": schema, "stream": False,
            "options": {"temperature": temperature, "num_ctx": 8192}}
    meta = {"model": model, "ok": False, "seconds": 0.0, "error": None, "local": is_local()}
    t0 = time.time()
    try:
        if not is_local():
            body["messages"] = log_egress(f"ollama:{OLLAMA}", messages, purpose="llm prompt")
        r = requests.post(f"{OLLAMA}/api/chat", json=body, timeout=timeout)
        r.raise_for_status()
        content = r.json()["message"]["content"]
        meta["seconds"] = round(time.time() - t0, 1)
        out = json.loads(content)
        meta["ok"] = isinstance(out, dict)
        return (out if meta["ok"] else None), meta
    except Exception as e:  # network, timeout, bad JSON, EgressBlocked
        meta["seconds"] = round(time.time() - t0, 1)
        meta["error"] = f"{type(e).__name__}: {e}"[:300]
        return None, meta
