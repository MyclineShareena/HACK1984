"""Gradium.ai streaming speech-to-text (wss://api.gradium.ai/api/speech/asr).

The incident audio passes the egress gate first (explicitly allowed binary; size + sha256
logged). Transcript text streams back word by word for the live view. The only other outbound
call is the spoken summary in tts.py, which is scrubbed and leak-checked as text.

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


last_diagnostics = {}  # what the last session saw (shown in the UI / CLI when something goes wrong)


def _diag_text(d):
    types = ", ".join(f"{k}×{v}" for k, v in d.get("message_types", {}).items()) or "none"
    out = f"server messages: {types}"
    if d.get("close"):
        out += f"; connection closed: {d['close']}"
    if d.get("send_error"):
        out += f"; send error: {d['send_error']}"
    return out


def transcribe_stream(wav_bytes, language="en"):
    """Generator yielding the transcript-so-far as Gradium returns words.

    Raises STTError with diagnostics (message types seen, close code/reason) if Gradium
    errors or returns no text, instead of silently yielding an empty transcript."""
    key = api_key()
    if not key:
        raise STTError("GRADIUM_API_KEY is not set")
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    pcm, seconds = wav_to_pcm24k(wav_bytes)
    peak = max(array.array("h", pcm), default=0, key=abs) if pcm else 0
    diag = {"audio_seconds": round(seconds, 2), "audio_peak": abs(peak), "message_types": {}, "close": None,
            "send_error": None, "url": GRADIUM_URL}
    last_diagnostics.clear()
    last_diagnostics.update(diag)
    if seconds < 0.5:
        raise STTError(f"recording too short ({seconds:.1f}s)")
    if abs(peak) < 300:
        raise STTError(f"recording is silent (peak amplitude {abs(peak)}/32767). Check the microphone input.")
    log_egress("gradium:speech/asr", pcm, allow_binary=True,
               purpose=f"speech-to-text ({seconds:.1f}s of 24kHz PCM audio)")

    words = []

    def seen(kind):
        mt = last_diagnostics["message_types"]
        mt[kind] = mt.get(kind, 0) + 1

    with connect(GRADIUM_URL, additional_headers={"x-api-key": key}, open_timeout=15,
                 max_size=2 ** 22) as ws:
        ws.send(json.dumps({"type": "setup", "model_name": "default", "input_format": "pcm",
                            "json_config": {"language": language}}))
        try:
            first = json.loads(ws.recv(timeout=15))
        except ConnectionClosed as e:
            raise STTError(f"Gradium closed the connection during setup: code {e.code} {e.reason!r}") from e
        seen(first.get("type", "?"))
        if first.get("type") == "error":
            raise STTError(f"Gradium: {first.get('message')} ({first.get('code')})")
        if first.get("type") == "text" and first.get("text"):
            words.append(first["text"].strip())

        def sender():
            try:
                step = CHUNK * 2
                for i in range(0, len(pcm), step):
                    ws.send(json.dumps({"type": "audio", "audio": base64.b64encode(pcm[i:i + step]).decode()}))
                    time.sleep(0.08 * PACE)
                # 1.5 s of trailing silence + flush so the recognizer emits the last word(s)
                silence = base64.b64encode(bytes(step)).decode()
                for _ in range(int(1.5 / 0.08)):
                    ws.send(json.dumps({"type": "audio", "audio": silence}))
                ws.send(json.dumps({"type": "flush", "flush_id": 1}))
                ws.send(json.dumps({"type": "end_of_stream"}))
            except Exception as e:  # socket closed by server error
                last_diagnostics["send_error"] = f"{type(e).__name__}: {e}"

        t = threading.Thread(target=sender, daemon=True)
        t.start()
        while True:
            try:
                raw = ws.recv(timeout=30)
            except TimeoutError:
                last_diagnostics["close"] = "no message from Gradium for 30 s"
                break
            except ConnectionClosed as e:
                last_diagnostics["close"] = f"code {e.code} {e.reason!r}"
                break
            if isinstance(raw, bytes):
                seen("binary")
                continue
            msg = json.loads(raw)
            kind = msg.get("type", "?")
            seen(kind)
            if kind == "text" and msg.get("text"):
                words.append(msg["text"].strip())
                yield " ".join(words)
            elif kind == "error":
                raise STTError(f"Gradium: {msg.get('message')} ({msg.get('code')}). {_diag_text(last_diagnostics)}")
            elif kind == "end_of_stream":
                break
        t.join(timeout=2)
    if not words:
        raise STTError("Gradium returned no transcript. " + _diag_text(last_diagnostics))
    yield " ".join(words)


if __name__ == "__main__":  # python -m engine.stt recording.wav
    import sys
    try:
        for partial in transcribe_stream(open(sys.argv[1], "rb").read()):
            print("\r" + partial[-150:], end="", flush=True)
        print("\nOK.", _diag_text(last_diagnostics))
    except STTError as e:
        print("\nFAILED:", e)
        sys.exit(1)
