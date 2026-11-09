"""Bridge daemon protocol test: daemon <-> mock browser <-> real GrokClient.

Run: .venv\\Scripts\\python.exe tests\\test_bridge.py
"""
import asyncio
import json

import websockets

from grokx_cli.client import GrokClient
from grokx_cli.server import GrokBridgeServer

PORT = 29292
URL = f"ws://127.0.0.1:{PORT}"


class MockBrowser:
    """Behaves like the extension's service worker."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.ws = None
        self.prompts = []
        self.task = None

    async def start(self):
        self.ws = await websockets.connect(URL)
        await self.ws.send(json.dumps({"role": "browser", "info": "mock"}))
        assert json.loads(await self.ws.recv())["type"] == "welcome"
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg.get("action") == "query":
                    asyncio.create_task(self.answer(msg["query_id"], msg["text"]))
                elif msg.get("action") == "new_chat":
                    await self.ws.send(json.dumps({"type": "new_chat_done", "req_id": msg["req_id"], "ok": True}))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def answer(self, qid, text):
        self.prompts.append(text)
        if text == "hang":
            return
        if text == "die":
            await self.ws.close()
            return
        full = ""
        for tok in ["Ответ", " на: ", text]:
            await asyncio.sleep(self.delay)
            full += tok
            await self.ws.send(json.dumps({"type": "chunk", "query_id": qid, "delta": tok, "full": full}))
        await self.ws.send(json.dumps({"type": "done", "query_id": qid, "full": full}))


async def collect(client, prompt):
    chunks, done, err = [], None, None
    async for c in client.stream_query(prompt):
        if c["type"] == "chunk":
            chunks.append(c["delta"])
        elif c["type"] == "done":
            done = c["full"]
        elif c["type"] == "error":
            err = c["error"]
    return "".join(chunks), done, err


async def main():
    server = GrokBridgeServer(port=PORT, browser_wait=1.0, query_timeout=1.5)
    server_task = asyncio.create_task(server.start())
    await server.ready.wait()

    # 1. no browser -> clear error after browser_wait
    c1 = GrokClient(URL)
    assert await c1.connect()
    assert not await c1.is_browser_ready()
    text, done, err = await collect(c1, "x")
    assert err and "not connected" in err, err
    print("ok  no browser -> error")

    # 2. browser connects, streaming answer with Cyrillic
    browser = MockBrowser()
    await browser.start()
    assert await c1.wait_browser(2)
    text, done, err = await collect(c1, "Привет")
    assert err is None and text == done == "Ответ на: Привет", (text, done, err)
    print("ok  streaming answer")

    # 3. two clients at once are served one after another, nothing mixed up
    c2 = GrokClient(URL)
    r = await asyncio.gather(collect(c1, "A"), collect(c2, "B"))
    assert r[0][1] == "Ответ на: A" and r[1][1] == "Ответ на: B", r
    print("ok  concurrent queries serialized")

    # 4. new chat round trip
    ok, msg = await c1.send_new_chat(timeout=3)
    assert ok, msg
    print("ok  new chat")

    # 5. browser never answers -> daemon timeout, and the queue is free again
    text, done, err = await collect(c1, "hang")
    assert err and "within" in err, err
    text, done, err = await collect(c1, "after")
    assert done == "Ответ на: after", (done, err)
    print("ok  timeout releases the queue")

    # 6. web pages may not connect (Origin check)
    try:
        bad = await websockets.connect(URL, origin="https://evil.example")
        await bad.send(json.dumps({"role": "browser"}))
        await bad.recv()
        raise AssertionError("web origin was accepted")
    except websockets.exceptions.ConnectionClosed as e:
        assert e.rcvd and e.rcvd.code == 4003, e
    assert server.is_browser_connected and server.browser_info == "mock"
    print("ok  web origin rejected")

    # 7. browser dies mid-answer -> client gets an error, not a hang
    text, done, err = await collect(c1, "die")
    assert err and "disconnected" in err.lower(), err
    print("ok  browser disconnect -> error")

    # 8. shutdown over the socket
    assert await c2.shutdown_daemon()
    await asyncio.wait_for(server_task, 3)
    await c1.close()
    await c2.close()
    print("ok  shutdown")
    print("ALL BRIDGE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
