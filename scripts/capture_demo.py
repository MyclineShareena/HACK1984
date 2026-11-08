"""Drive the running app in headless Chrome (DevTools protocol), click through the full
incident flow, save stage screenshots to docs/screenshots/ and a walkthrough GIF to docs/.

    streamlit run app.py            # in another terminal
    python scripts/capture_demo.py  # needs Google Chrome; uses websockets + Pillow
    python scripts/capture_demo.py --wav docs/demo_incident.wav   # voice path via Gradium
                                    # (the app must have GRADIUM_API_KEY set)

The flow deliberately approves a WRONG fix first (restart) to show verification catching it,
then approves the recommended fix.
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"
_args = sys.argv[1:]
WAV = None
if "--wav" in _args:
    i = _args.index("--wav")
    WAV = str(Path(_args[i + 1]).resolve())
    del _args[i:i + 2]
APP = _args[0] if _args else "http://localhost:8501"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W, H = 1440, 900


class CDP:
    def __init__(self, ws_url):
        self.ws = connect(ws_url, max_size=2 ** 28, open_timeout=20).__enter__()
        self.i = 0

    def __call__(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=120))
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


SCROLLER = """(() => { let best = document.scrollingElement, h = 0;
  for (const el of document.querySelectorAll('*')) { const s = getComputedStyle(el);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight && el.scrollHeight > h
        && !el.closest('[data-testid="stSidebar"]')) { best = el; h = el.scrollHeight; } }
  return best; })()"""


class Demo:
    def __init__(self, cdp):
        self.c, self.frames = cdp, []

    def wait_text(self, text, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.c.js(f"document.body.innerText.includes({json.dumps(text)})"):
                return True
            self.frame()
            time.sleep(1.0)
        raise TimeoutError(text)

    def wait_gone(self, text, timeout=240):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.c.js(f"document.body.innerText.includes({json.dumps(text)})"):
                return True
            self.frame(scroll="bottom")
            time.sleep(1.0)
        raise TimeoutError(text)

    def click(self, text, tag="button", timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            ok = self.c.js(f"""(() => {{ const el = [...document.querySelectorAll('{tag}')]
                .find(x => x.innerText.includes({json.dumps(text)}) && !x.disabled);
                if (!el) return false; el.click(); return true; }})()""")
            if ok:
                time.sleep(1.2)
                return
            time.sleep(0.5)
        raise RuntimeError(f"no enabled {tag} with text {text!r}")

    def mouse_click(self, text, tag="label", dx=None):
        """Real mouse click (CDP Input events) on the element containing text."""
        pos = self.c.js(f"""(() => {{ const s = {SCROLLER}; const el = [...document.querySelectorAll('{tag}')]
            .find(x => x.innerText.includes({json.dumps(text)})); if (!el) return null;
            let r = el.getBoundingClientRect();
            s.scrollTo(0, r.top - s.getBoundingClientRect().top + s.scrollTop - 300);
            r = el.getBoundingClientRect();
            return [r.left, r.top, r.width, r.height]; }})()""")
        if not pos:
            raise RuntimeError(f"no {tag} with text {text!r}")
        time.sleep(0.4)
        x = pos[0] + (dx if dx is not None else pos[2] / 2)
        y = pos[1] + pos[3] / 2
        self.c("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.c("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        self.c("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)
        time.sleep(1.2)

    def check(self, text, timeout=20):
        """Tick the approval checkbox, then wait until the Approve button is enabled."""
        self.mouse_click(text, dx=8)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.c.js("""[...document.querySelectorAll('button')].some(b => b.innerText.includes('Approve') && !b.disabled)"""):
                return
            time.sleep(0.5)
        raise RuntimeError(f"checkbox {text!r} did not enable Approve")

    def scroll(self, where):
        if where == "top":
            self.c.js(f"{SCROLLER}.scrollTo(0, 0)")
        elif where == "bottom":
            self.c.js(f"(() => {{ const s = {SCROLLER}; s.scrollTo(0, s.scrollHeight); }})()")
        else:  # scroll element containing text into view
            self.c.js(f"""(() => {{ const s = {SCROLLER}; const el = [...document.querySelectorAll('h1,h2,h3,h4,p,span')]
                .find(x => x.innerText.trim().startsWith({json.dumps(where)})); if (!el) return;
                s.scrollTo(0, el.getBoundingClientRect().top - s.getBoundingClientRect().top + s.scrollTop - 70); }})()""")
        time.sleep(0.5)

    def unshift(self):  # undo any horizontal scroll focus() may have caused
        self.c.js("for (const el of document.querySelectorAll('*')) if (el.scrollLeft) el.scrollLeft = 0")

    def frame(self, scroll=None, hold=1):
        if scroll:
            self.scroll(scroll)
        self.unshift()
        png = self.c("Page.captureScreenshot", format="png")["data"]
        import base64
        img = Image.open(io.BytesIO(base64.b64decode(png))).convert("RGB")
        img.thumbnail((1100, 1100))
        for _ in range(hold):
            self.frames.append(img)

    def shot(self, name, full=True):
        import base64
        self.unshift()
        if full:
            h = self.c.js(f"{SCROLLER}.scrollHeight") or H
            self.c("Emulation.setDeviceMetricsOverride", width=W, height=int(h) + 80, deviceScaleFactor=1, mobile=False)
            time.sleep(1.8)
        png = base64.b64decode(self.c("Page.captureScreenshot", format="png", captureBeyondViewport=True)["data"])
        (SHOTS / f"{name}.png").write_bytes(png)
        print("saved", SHOTS / f"{name}.png")
        if full:
            self.c("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
            time.sleep(1.0)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    prof = tempfile.mkdtemp()
    chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=9333", f"--user-data-dir={prof}",
                               f"--window-size={W},{H}", "--hide-scrollbars",
                               "--autoplay-policy=no-user-gesture-required", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json"))
                break
            except Exception:
                time.sleep(0.2)
        page = next(t for t in tabs if t["type"] == "page")
        c = CDP(page["webSocketDebuggerUrl"])
        c("Page.enable")
        c("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        c("Page.navigate", url=APP)
        d = Demo(c)
        d.wait_text("Report the incident", 60)
        time.sleep(2)
        d.frame(hold=3)
        d.shot("01_report_incident", full=False)

        if WAV:  # voice: upload a recording, stream the live Gradium transcript
            c.js("[...document.querySelectorAll('summary')].find(s => s.innerText.includes('upload a WAV recording'))?.click()")
            time.sleep(1.5)
            c("DOM.enable")
            root = c("DOM.getDocument", depth=-1, pierce=True)["root"]["nodeId"]
            first = c("DOM.querySelectorAll", nodeId=root, selector="input[type=file]")["nodeIds"][0]
            c("DOM.setFileInputFiles", nodeId=first, files=[WAV])
            d.click("Transcribe live with Gradium", timeout=30)
            t0, shot_taken = time.time(), False
            while time.time() - t0 < 60:
                if c.js("document.querySelector('textarea')?.value"):
                    break
                if c.js("document.querySelector('.iv-live')?.innerText.length > 60") and not shot_taken:
                    d.shot("02a_live_transcript", full=False)
                    shot_taken = True
                d.frame()
                time.sleep(0.3)
            time.sleep(1.5)
            d.frame(hold=3)
            d.shot("02_description_ready", full=False)
        else:
            d.click("Use sample incident")
            time.sleep(1.5)
            d.frame(hold=3)
            d.shot("02_description_ready", full=False)

        d.click("Investigate")
        time.sleep(4)
        d.frame(scroll="bottom", hold=2)
        d.wait_text("Investigation findings", 300)
        time.sleep(3)
        for sec in ["2 · Investigation findings", "#### 📈", "🎯 Root-cause agent"]:
            d.frame(scroll=sec, hold=3)
        d.shot("03_findings")

        # wrong fix first: restart -> verification fails
        d.scroll("3 · Recommended")
        d.mouse_click("Restart checkout pods", dx=8)
        d.check("I have reviewed")
        d.frame(scroll="3 · Recommended", hold=3)
        d.shot("04_approval_gate", full=False)
        d.click("Approve & execute")
        d.wait_gone("Executing in simulated environment", 120)
        time.sleep(2)
        d.frame(scroll="4 · Execution", hold=4)
        d.shot("05_wrong_fix_not_verified", full=False)

        # recommended fix
        d.scroll("3 · Recommended")
        d.mouse_click("RECOMMENDED", dx=8)
        d.check("I have reviewed")
        d.frame(scroll="3 · Recommended", hold=2)
        d.click("Approve & execute")
        d.wait_gone("Executing in simulated environment", 120)
        time.sleep(2)
        d.frame(scroll="4 · Execution", hold=5)
        d.shot("06_recovery_verified", full=False)

        if c.js("[...document.querySelectorAll('button')].some(b => b.innerText.includes('Speak verified summary'))"):
            d.click("Speak verified summary")  # Gradium TTS talk-back
            t0 = time.time()
            while time.time() - t0 < 60 and not c.js("!!document.querySelector('audio')"):
                d.frame(scroll="#### 🔊")
                time.sleep(0.5)
            time.sleep(1.5)
            d.scroll("🔊 IncidentVoice reports back")
            d.frame(hold=5)
            d.shot("06b_talk_back", full=False)
            sent = c.js("[...document.querySelectorAll('[data-testid=\"stCaptionContainer\"]')].map(x => x.innerText)"
                        ".find(t => t.includes('Sent to Gradium'))")
            dur = c.js("document.querySelector('audio')?.duration")
            print(f"talk-back audio: {dur:.1f}s; text sent: {sent}" if dur else "talk-back: no audio")

        d.click("Generate incident report")
        d.wait_text("Executive summary", 300)
        time.sleep(2)
        d.frame(scroll="5 · Incident report", hold=4)
        d.frame(scroll="bottom", hold=3)
        d.shot("07_postmortem")
        d.scroll("top")
        d.shot("08_full_page")

        gif = ROOT / "docs" / "incidentvoice_demo.gif"
        frames = d.frames
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=700, loop=0, optimize=True)
        print("saved", gif, f"({len(frames)} frames)")
    finally:
        chrome.terminate()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
