"""Gradium.ai streaming speech-to-text (wss://api.gradium.ai/api/speech/asr).

Audio is the only thing that leaves the laptop, and it passes the egress gate first
(size + sha256 logged). Transcript text streams back word by word for the live view.

    for partial in transcribe_stream(wav_bytes):   # yields the growing transcript
        print(partial)
"""
import array
import base64
import io
import json
import os
import threading
import time
import wave

from privacy_layer import log_egress

GRADIUM_URL = os.environ.get("GRADIUM_STT_URL", "wss://api.gradium.ai/api/speech/asr")
RATE = 24000          # Gradium PCM default: 24 kHz, 16-bit, mono
CHUNK = 1920          # samples per message = 80 ms
PACE = float(os.environ.get("GRADIUM_PACE", "0.25"))  # 0.25 = send at 4x real time


class STTError(RuntimeError):
    pass


def api_key():
    return os.environ.get("GRADIUM_API_KEY", "").strip()


def wav_to_pcm24k(wav_bytes):
    """Decode WAV -> mono 16-bit PCM at 24 kHz (pure stdlib, linear resampling)."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise STTError(f"need 16-bit WAV, got {8 * width}-bit")
    s = array.array("h", raw)
    if ch > 1:
        s = array.array("h", (sum(s[i:i + ch]) // ch for i in range(0, len(s), ch)))
    if rate != RATE and len(s) > 1:
        out_n = int(len(s) * RATE / rate)
        step = (len(s) - 1) / max(out_n - 1, 1)
        res = array.array("h", bytes(2 * out_n))
        for i in range(out_n):
            x = i * step
            j = int(x)
            f = x - j
            nxt = s[j + 1] if j + 1 < len(s) else s[j]
            res[i] = int(s[j] + (nxt - s[j]) * f)
        s = res
    return s.tobytes(), len(s) / RATE


def transcribe_stream(wav_bytes, language="en"):
    """Generator yielding the transcript-so-far as Gradium returns words."""
    key = api_key()
    if not key:
        raise STTError("GRADIUM_API_KEY is not set")
    from websockets.sync.client import connect

    pcm, seconds = wav_to_pcm24k(wav_bytes)
    log_egress("gradium:speech/asr", pcm, allow_binary=True,
               purpose=f"speech-to-text ({seconds:.1f}s of 24kHz PCM audio)")

    words, errors = [], []
    with connect(GRADIUM_URL, additional_headers={"x-api-key": key}, open_timeout=15,
                 max_size=2 ** 22) as ws:
        ws.send(json.dumps({"type": "setup", "model_name": "default", "input_format": "pcm",
                            "json_config": {"language": language}}))
        first = json.loads(ws.recv(timeout=15))
        if first.get("type") == "error":
            raise STTError(f"Gradium: {first.get('message')} ({first.get('code')})")

        def sender():
            try:
                step = CHUNK * 2
                for i in range(0, len(pcm), step):
                    ws.send(json.dumps({"type": "audio", "audio": base64.b64encode(pcm[i:i + step]).decode()}))
                    time.sleep(0.08 * PACE)
                ws.send(json.dumps({"type": "end_of_stream"}))
            except Exception as e:  # socket closed by server error
                errors.append(e)

        t = threading.Thread(target=sender, daemon=True)
        t.start()
        while True:
            try:
                msg = json.loads(ws.recv(timeout=30))
            except Exception:
                break
            kind = msg.get("type")
            if kind == "text" and msg.get("text"):
                words.append(msg["text"].strip())
                yield " ".join(words)
            elif kind == "error":
                raise STTError(f"Gradium: {msg.get('message')} ({msg.get('code')})")
            elif kind == "end_of_stream":
                break
        t.join(timeout=2)
    if not words and errors:
        raise STTError(f"Gradium stream failed: {errors[0]}")
    yield " ".join(words)
