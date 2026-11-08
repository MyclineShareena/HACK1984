"""Gradium.ai text-to-speech (wss://api.gradium.ai/api/speech/tts): IncidentVoice talks back.

The spoken summary is plain text, so it goes through the egress leak gate like any other
payload: scrubbed, re-checked, and logged in egress_log.jsonl with the exact text sent.

    wav_bytes, spoken_text = speak(text)
"""
import base64
import io
import json
import os
import re
import wave

from privacy_layer import log_egress

from .stt import STTError, api_key

TTS_URL = os.environ.get("GRADIUM_TTS_URL", "wss://api.gradium.ai/api/speech/tts")
VOICE_ID = os.environ.get("GRADIUM_VOICE_ID", "YTpq7expH9539ERJ")  # "Emma" from Gradium's docs
OUT_RATE = 48000  # Gradium pcm output: 48 kHz, 16-bit, mono


class TTSError(STTError):
    pass


def _say_identifier(m):
    """db.pool.max_size -> 'database pool max size' so the voice doesn't read punctuation."""
    s = m.group(0).replace("db.", "database ").replace("_s", " seconds") if m.group(0).endswith("_s") else \
        m.group(0).replace("db.", "database ")
    return re.sub(r"[._]+", " ", s)


def speakable(text):
    text = re.sub(r"\b[a-z]+(?:[._][a-z0-9]+)+\b", _say_identifier, text)  # config keys
    text = re.sub(r"\bv(\d+)\.(\d+)\b", r"version \1.\2", text)
    text = text.replace("→", " to ").replace("p99", "p 99").replace("%", " percent")
    return re.sub(r"\s+", " ", text).strip()


def spoken_summary(inv, execution, attempts=1):
    """Short, ear-friendly recap of a verified incident (no ids, no markdown)."""
    tri, top = inv["triage"], inv["rca"]["hypotheses"][0]
    before, after = execution["samples"][0], execution["samples"][-1]
    lines = [
        "Incident resolved and verified.",
        f"Root cause: {top['statement']}",
        f"Fix: {execution['action']['title']}, approved by {execution['approver']}.",
        f"Error rate is down from {before['error_rate']:.0%} to {after['error_rate']:.0%}.",
    ]
    if attempts > 1:
        lines.append("The first attempt did not restore service; verification caught it.")
    return speakable(" ".join(lines))


def _pcm_to_wav(pcm, rate=OUT_RATE):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def speak(text, voice_id=None):
    """Synthesize text with Gradium. Returns (wav_bytes, text_actually_sent).

    Raises EgressBlocked if the text still looks sensitive after scrubbing, TTSError on API errors."""
    key = api_key()
    if not key:
        raise TTSError("GRADIUM_API_KEY is not set")
    safe = log_egress("gradium:speech/tts", text, purpose="text-to-speech of verified incident summary")
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    pcm, rate = bytearray(), OUT_RATE
    with connect(TTS_URL, additional_headers={"x-api-key": key}, open_timeout=15, max_size=2 ** 24) as ws:
        ws.send(json.dumps({"type": "setup", "voice_id": voice_id or VOICE_ID, "model_name": "default",
                            "output_format": "pcm"}))
        ready = json.loads(ws.recv(timeout=15))
        if ready.get("type") == "error":
            raise TTSError(f"Gradium TTS: {ready.get('message')} ({ready.get('code')})")
        rate = int(ready.get("sample_rate") or OUT_RATE)
        for sentence in re.split(r"(?<=[.!?])\s+", safe):  # split only at whitespace
            if sentence.strip():
                ws.send(json.dumps({"type": "text", "text": sentence.strip()}))
        ws.send(json.dumps({"type": "end_of_stream"}))
        while True:
            try:
                msg = json.loads(ws.recv(timeout=30))
            except (TimeoutError, ConnectionClosed):
                break
            kind = msg.get("type")
            if kind == "audio" and msg.get("audio"):
                pcm += base64.b64decode(msg["audio"])
            elif kind == "error":
                raise TTSError(f"Gradium TTS: {msg.get('message')} ({msg.get('code')})")
            elif kind == "end_of_stream":
                break
    if not pcm:
        raise TTSError("Gradium TTS returned no audio")
    return _pcm_to_wav(bytes(pcm), rate), safe


if __name__ == "__main__":  # python -m engine.tts "text to say" out.wav
    import sys
    wav, sent = speak(sys.argv[1] if len(sys.argv) > 1 else "IncidentVoice is online.")
    out = sys.argv[2] if len(sys.argv) > 2 else "tts_test.wav"
    open(out, "wb").write(wav)
    print(f"wrote {out} ({len(wav) / 2 / OUT_RATE:.1f}s) for text: {sent}")
