"""End-to-end test against a mock Grok page in real Chromium (Playwright's build).

The mock reproduces what x.com/i/grok really does (captured live on 09.11.2026): the page
talks to wss://grok.com/ws/mgw/ with Realtime-API-like JSON events, answer tokens come as
"response.chunk" on CHANNEL_ASSISTANT_RESPONSE, and "conversation.live.updated
streaming:false" arrives before the last tokens. Like the live page, the mock ignores Enter
for the first second after load.

  1. Extension tier: a copy of extension/ patched to daemon port 29494 (so the real daemon
     and a real browser with the installed extension do not interfere), x.com is served by
     Playwright routes, the grok.com WebSocket by route_web_socket.
  2. CDP tier: Chromium with --remote-debugging-port, a local HTTP page + local WebSocket
     server, driven by GrokOrchestrator(mode="cdp").

Run: .venv\\Scripts\\python.exe tests\\test_e2e.py
"""
import asyncio
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOCK_PORT = 29393
MOCK_WS_PORT = 29395
CDP_PORT = 9334
EXT_PORT = 29494
os.environ["GROKX_GROK_URL"] = f"http://127.0.0.1:{MOCK_PORT}/i/grok"

import websockets  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from grokx_cli.client import GrokClient  # noqa: E402
from grokx_cli.core import GrokOrchestrator  # noqa: E402
from grokx_cli.server import GrokBridgeServer  # noqa: E402

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Grok / X</title></head><body>
<div id="msgs"></div>
<textarea placeholder="Ask Grok (AI agent)" id="in" style="width:500px;height:80px"></textarea>
<script>
const WSURL = %s;
const sock = new WebSocket(WSURL);
let ready = false;
setTimeout(() => { ready = true; }, 1000);      // like x.com: box visible before it can send
let cur = null;
sock.onmessage = (m) => {
  const ev = JSON.parse(m.data).event || {};
  if (ev.type === 'response.created') { cur = document.createElement('div'); msgs.append(cur); }
  const t = ev.chunk && ev.chunk.text;
  if (cur && t && t.channel === 'CHANNEL_ASSISTANT_RESPONSE') cur.textContent += t.text;
};
const ta = document.getElementById('in');
ta.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.shiftKey) return;
  e.preventDefault();
  if (!ready || sock.readyState !== 1) return;
  const text = ta.value.trim();
  if (!text) return;
  ta.value = '';
  sock.send(JSON.stringify({session_id: 's1', event: {type: 'response.create', event_id: 'evt_resp_' + Date.now(),
    item: {type: 'message', role: 'user', content: [{type: 'input_text', text: text}]}}}));
});
</script></body></html>"""

TOKENS = ["Ответ", " на: ", "{prompt}", " ✅"]


def expected(prompt: str) -> str:
    return "".join(TOKENS).replace("{prompt}", prompt)


def grok_frames(message: str):
    """Server frames for one response.create, in the order seen live."""
    msg = json.loads(message)
    ev = msg.get("event", {})
    if ev.get("type") != "response.create":
        return []
    prompt = ev["item"]["content"][0]["text"]
    sid, rid = "s1", f"r{abs(hash(prompt)) % 10**8}"

    def e(event):
        return json.dumps({"session_id": sid, "event": event}, ensure_ascii=False)

    frames = [
        e({"type": "conversation.queue.updated", "items": []}),
        json.dumps({"event": {"type": "conversation.live.updated", "streaming": True}}),
        e({"type": "conversation.item.added", "item": {"type": "message", "role": "user",
                                                       "content": [{"type": "input_text", "text": prompt}]}}),
        e({"type": "response.created", "client_event_id": ev["event_id"],
           "response": {"id": rid, "status": "in_progress"}}),
        e({"type": "response.output_item.added", "response_id": rid,
           "item": {"id": rid, "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]}}),
        e({"type": "response.content_part.added", "response_id": rid, "part": {"type": "output_text", "text": ""}}),
        e({"type": "response.chunk", "response_id": rid, "chunk": {"metadata": {"phase_index": 0}}}),
        e({"type": "response.chunk", "response_id": rid, "chunk": {
            "text": {"text": "Thinking about your request", "channel": "CHANNEL_ASSISTANT_NOTETAKER_HEADER"}}}),
        e({"type": "response.chunk", "response_id": rid, "chunk": {"phase_marker": {"kind": "KIND_RESPONSE_START"}}}),
    ]
    toks = [t.replace("{prompt}", prompt) for t in TOKENS]
    for i, tok in enumerate(toks):
        if i == len(toks) - 2:   # live: streaming:false arrives before the last tokens
            frames.append(json.dumps({"event": {"type": "conversation.live.updated", "streaming": False}}))
        frames.append(e({"type": "response.chunk", "response_id": rid, "chunk": {
            "text": {"text": tok, "channel": "CHANNEL_ASSISTANT_RESPONSE"}}}))
    frames += [
        e({"type": "response.output_text.done", "response_id": rid, "text": ""}),
        e({"type": "response.content_part.done", "response_id": rid, "part": {"type": "output_text", "text": ""}}),
    ]
    return frames


async def collect(gen):
    chunks, done, err = [], None, None
    async for c in gen:
        if c["type"] == "chunk":
            chunks.append(c["delta"])
        elif c["type"] == "done":
            done = c["full"]
        elif c["type"] == "error":
            err = c["error"]
    return "".join(chunks), done, err, len(chunks)


# ---------------------------------------------------------------- 1. extension tier
async def test_extension(pw):
    server = GrokBridgeServer(port=EXT_PORT, browser_wait=10, query_timeout=70)
    server_task = asyncio.create_task(server.start())
    await server.ready.wait()

    ext_dir = Path(tempfile.mkdtemp(prefix="grokx-extcopy-"))
    shutil.copytree(ROOT / "extension", ext_dir / "ext")
    bg = ext_dir / "ext" / "background.js"
    bg.write_text(bg.read_text(encoding="utf-8").replace("127.0.0.1:29191", f"127.0.0.1:{EXT_PORT}"), encoding="utf-8")
    profile = tempfile.mkdtemp(prefix="grokx-ext-")
    ctx = await pw.chromium.launch_persistent_context(
        profile, channel="chromium", headless=True,
        args=[f"--disable-extensions-except={ext_dir / 'ext'}", f"--load-extension={ext_dir / 'ext'}"])
    try:
        async def grok_page(route):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=PAGE % json.dumps("wss://grok.com/ws/mgw/?uid=test"))

        def grok_ws(ws):
            async def play(message):
                for f in grok_frames(message):
                    ws.send(f)
                    await asyncio.sleep(0.03)
            ws.on_message(lambda m: asyncio.ensure_future(play(m)))

        await ctx.route("https://x.com/**", grok_page)
        await ctx.route_web_socket("wss://grok.com/**", grok_ws)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # The extension reloads Grok tabs on install; let that settle before navigating.
        if not ctx.service_workers:
            await ctx.wait_for_event("serviceworker", timeout=10000)
        await asyncio.sleep(1)
        try:
            await page.goto("https://x.com/i/grok")
        except Exception:
            await page.goto("https://x.com/i/grok")
        await page.wait_for_selector("#grokx-bridge-badge", timeout=10000)

        client = GrokClient(f"ws://127.0.0.1:{EXT_PORT}")
        assert await client.connect()
        assert await client.wait_browser(15), "extension did not connect to the daemon"
        print("ok  extension connected:", (await client.status())["browser_info"])

        text, done, err, n = await collect(client.stream_query("Привет, Grok"))
        assert err is None, err
        assert done == expected("Привет, Grok") and text == done, (text, done)
        assert n >= 4, f"expected streamed chunks, got {n}"
        sent = await page.inner_text("#msgs")
        assert "Thinking" not in done
        print(f"ok  extension answer ({n} chunks): {done}")

        badge = await page.inner_text("#grokx-bridge-badge")
        assert "ready" in badge.lower(), badge

        ok, msg = await client.send_new_chat()
        assert ok, msg
        multi = "строка 1\nстрока 2"
        text, done, err, n = await collect(client.stream_query(multi))
        assert err is None and done == expected(multi), (done, err)
        print("ok  extension /new, then answer on the freshly loaded page (multiline prompt)")

        # the prompt must have been typed exactly once
        assert (await page.input_value("#in")) == ""
        text, done, err, n = await collect(client.stream_query("один раз"))
        assert done == expected("один раз"), (done, err)
        print("ok  prompt not duplicated, input left empty")

        await page.evaluate("history.pushState({}, '', '/home')")
        await asyncio.sleep(1.5)
        assert await page.query_selector("#grokx-bridge-badge") is None
        await page.evaluate("history.pushState({}, '', '/i/grok')")
        assert await client.wait_browser(5)
        text, done, err, n = await collect(client.stream_query("spa"))
        assert err is None and done == expected("spa"), (done, err)
        print("ok  SPA navigation away/back")

        # No Grok tab: the extension stays connected and opens one by itself. Playwright routes
        # do not apply to tabs an extension opens, so here we only check that the tab is opened.
        await ctx.new_page()
        await page.close()
        await asyncio.sleep(1)
        assert await client.is_browser_ready(), "extension must stay connected without a Grok tab"
        before = len(ctx.pages)
        q = asyncio.create_task(collect(client.stream_query("без вкладки")))
        await asyncio.sleep(4)
        assert len(ctx.pages) == before + 1, [p.url for p in ctx.pages]
        q.cancel()
        print("ok  no Grok tab -> extension opened one")

        await client.close()
    finally:
        await ctx.close()
        server.stop()
        await server_task
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(ext_dir, ignore_errors=True)


# ---------------------------------------------------------------- 2. CDP tier
class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (PAGE % json.dumps(f"ws://127.0.0.1:{MOCK_WS_PORT}/ws/mgw/")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_mock_http():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    srv.handle_error = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def mock_ws_handler(conn):
    try:
        async for message in conn:
            for f in grok_frames(message):
                await conn.send(f)
                await asyncio.sleep(0.03)
    except websockets.exceptions.ConnectionClosed:
        pass


async def test_cdp(pw):
    srv = start_mock_http()
    wss = await websockets.serve(mock_ws_handler, "127.0.0.1", MOCK_WS_PORT)
    profile = tempfile.mkdtemp(prefix="grokx-cdp-")
    # Real Chrome, as `grokx --launch` uses. Playwright's own Chromium build hangs in
    # connect_over_cdp when an http(s) page is already open (checked: example.com too).
    from grokx_cli.cdp import find_browser_executable
    exe = find_browser_executable() or pw.chromium.executable_path
    chrome = subprocess.Popen([
        exe, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile}",
        "--headless=new", "--no-first-run", os.environ["GROKX_GROK_URL"]],   # page already open, unhooked
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from grokx_cli.cdp import is_cdp_listening
        for _ in range(50):
            if is_cdp_listening(CDP_PORT):
                break
            await asyncio.sleep(0.2)
        assert is_cdp_listening(CDP_PORT)
        await asyncio.sleep(1.5)

        orch = GrokOrchestrator(cdp_port=CDP_PORT, mode="cdp")
        assert await orch.detect_best_tier() == "cdp"
        text, done, err, n = await collect(orch.query_stream("Привет по CDP"))
        assert err is None and done == expected("Привет по CDP") and text == done, (text, done, err)
        assert n >= 4, n
        print(f"ok  CDP answer on a tab opened before grokx ({n} chunks): {done}")

        ok, msg = await orch.new_chat()
        assert ok, msg
        ok, full = await orch.query_full("второй")
        assert ok and full == expected("второй"), full
        print("ok  CDP /new and second answer")

        await orch.close()
        assert is_cdp_listening(CDP_PORT), "closing grokx must not close Chrome"
        orch2 = GrokOrchestrator(cdp_port=CDP_PORT, mode="cdp")
        ok, full = await orch2.query_full("снова")
        assert ok and full == expected("снова"), full
        await orch2.close()
        print("ok  CDP reconnect, Chrome kept running")
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(chrome.pid)], capture_output=True)
        wss.close()
        srv.shutdown()
        await asyncio.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


async def main():
    async with async_playwright() as pw:
        await test_extension(pw)
        await test_cdp(pw)
    print("ALL E2E TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
