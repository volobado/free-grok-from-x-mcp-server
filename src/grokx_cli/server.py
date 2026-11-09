"""WebSocket bridge daemon for GrokX.

Brokers messages between grokx clients (CLI, MCP server) and the browser side
(the GrokX Chrome extension's service worker). One browser connection at a time;
queries are serialized because one Grok tab can only answer one prompt at a time.

Protocol
  client -> daemon : {"role": "client"} then
                     {"action": "status"} | {"action": "query", "query_id", "text"}
                     {"action": "new_chat", "req_id"} | {"action": "stop"} | {"action": "shutdown"}
  daemon -> browser: {"action": "query" | "new_chat" | "stop", ...}
  browser -> daemon: {"role": "browser", "info"} then
                     {"type": "chunk" | "done" | "error", "query_id", ...}
                     {"type": "new_chat_done", "req_id", "ok", "error"} | {"type": "ping"}
  daemon -> client : {"type": "browser_status"} | {"type": "status_response"} | {"type": "info"}
                     chunk / done / error (forwarded) | {"type": "new_chat_done"}
"""
import asyncio
import json
import logging
import os
import signal
import sys

import websockets

from grokx_cli import __version__
from grokx_cli.config import (
    BROWSER_WAIT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    LOG_FILE,
    NEW_CHAT_TIMEOUT,
    PID_FILE,
    QUERY_TIMEOUT,
)

logger = logging.getLogger("grokx.server")


def is_ws_open(ws) -> bool:
    if ws is None:
        return False
    try:
        return ws.state.name == "OPEN"
    except Exception:
        return False


def origin_allowed(websocket) -> bool:
    """Only local programs (no Origin header) and browser extensions may connect.

    Without this check any web page open in the browser could connect to
    ws://127.0.0.1 and either read every prompt (posing as the browser) or
    drive the user's Grok session (posing as a client).
    """
    try:
        origin = websocket.request.headers.get("Origin")
    except Exception:
        origin = None
    if not origin:
        return True
    return origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")


async def _send(ws, obj) -> bool:
    if not is_ws_open(ws):
        return False
    try:
        await ws.send(json.dumps(obj, ensure_ascii=False))
        return True
    except Exception:
        return False


class GrokBridgeServer:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 browser_wait: float = BROWSER_WAIT, query_timeout: float = QUERY_TIMEOUT,
                 new_chat_timeout: float = NEW_CHAT_TIMEOUT):
        self.host = host
        self.port = port
        self.browser_wait = browser_wait
        self.query_timeout = query_timeout
        self.new_chat_timeout = new_chat_timeout
        self.browser_ws = None
        self.browser_info = ""
        self.clients = set()
        self.pending = {}      # query_id -> (client_ws, future)
        self.resets = {}       # req_id -> future
        self.lock = asyncio.Lock()
        self.server = None
        self._stop_event = asyncio.Event()
        self.ready = asyncio.Event()

    @property
    def is_browser_connected(self) -> bool:
        return is_ws_open(self.browser_ws)

    async def broadcast_status(self) -> None:
        msg = {"type": "browser_status", "connected": self.is_browser_connected}
        for client in list(self.clients):
            await _send(client, msg)

    async def _wait_browser(self) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.browser_wait
        while not self.is_browser_connected:
            if loop.time() > deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    # ---------------- connections ----------------
    async def handle_connection(self, websocket):
        if not origin_allowed(websocket):
            logger.warning("Rejected connection from origin %s", websocket.request.headers.get("Origin"))
            await websocket.close(4003, "origin not allowed")
            return
        role = None
        try:
            data = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
            role = data.get("role", "client")
            if role == "browser":
                await self._handle_browser(websocket, data)
            else:
                await self._handle_client(websocket)
        except asyncio.TimeoutError:
            logger.warning("Connection handshake timed out.")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.error("Error handling connection (%s): %s", role, e, exc_info=True)
        finally:
            if role == "browser" and self.browser_ws is websocket:
                self.browser_ws = None
                logger.info("Browser disconnected.")
                await self._fail_pending("Browser disconnected during the request.")
                await self.broadcast_status()
            self.clients.discard(websocket)

    async def _fail_pending(self, error: str):
        for qid, (client, fut) in list(self.pending.items()):
            await _send(client, {"type": "error", "query_id": qid, "error": error})
            if not fut.done():
                fut.set_result(None)
        for fut in self.resets.values():
            if not fut.done():
                fut.set_result({"ok": False, "error": error})

    async def _handle_browser(self, websocket, init_data: dict):
        old = self.browser_ws
        self.browser_ws = websocket
        self.browser_info = init_data.get("info", "unknown")
        if is_ws_open(old):
            logger.info("Another browser connected, replacing the old connection.")
            await self._fail_pending("Browser connection was replaced by another browser.")
            try:
                await old.close(4001, "replaced")
            except Exception:
                pass
        logger.info("Browser connected: %s", self.browser_info)
        await _send(websocket, {"type": "welcome", "message": "GrokX bridge connected"})
        await self.broadcast_status()

        async for message in websocket:
            try:
                msg = json.loads(message)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype in ("chunk", "done", "error"):
                entry = self.pending.get(msg.get("query_id"))
                if not entry:
                    continue
                client, fut = entry
                await _send(client, msg)
                if mtype in ("done", "error") and not fut.done():
                    fut.set_result(msg)
            elif mtype == "new_chat_done":
                fut = self.resets.get(msg.get("req_id"))
                if fut and not fut.done():
                    fut.set_result(msg)

    async def _handle_client(self, websocket):
        self.clients.add(websocket)
        tasks = set()
        await _send(websocket, {"type": "browser_status", "connected": self.is_browser_connected})
        try:
            async for message in websocket:
                try:
                    msg = json.loads(message)
                except Exception:
                    continue
                action = msg.get("action")
                if action == "status":
                    await _send(websocket, {
                        "type": "status_response",
                        "browser_connected": self.is_browser_connected,
                        "browser_info": self.browser_info if self.is_browser_connected else "",
                        "busy": self.lock.locked(),
                        "clients": len(self.clients),
                        "pid": os.getpid(),
                        "version": __version__,
                    })
                elif action == "query":
                    t = asyncio.create_task(self._run_query(websocket, msg.get("query_id") or "q", msg.get("text", "")))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                elif action == "new_chat":
                    t = asyncio.create_task(self._run_new_chat(websocket, msg.get("req_id") or "r"))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                elif action == "stop":
                    await _send(self.browser_ws, {"action": "stop"})
                elif action == "reload_extension":
                    ok = await _send(self.browser_ws, {"action": "reload_extension"})
                    await _send(websocket, {"type": "reload_sent", "ok": ok})
                elif action == "shutdown":
                    await _send(websocket, {"type": "info", "message": "shutting down"})
                    logger.info("Shutdown requested by client.")
                    self.stop()
        finally:
            # Client went away: abandon its requests (the running answer gets stopped).
            for t in tasks:
                t.cancel()

    # ---------------- requests ----------------
    async def _run_query(self, client, qid: str, text: str):
        if self.lock.locked():
            await _send(client, {"type": "info", "query_id": qid, "message": "Grok is busy with another request, queued."})
        async with self.lock:
            if not await self._wait_browser():
                await _send(client, {
                    "type": "error", "query_id": qid,
                    "error": "Browser is not connected: open https://x.com/i/grok in Chrome with the GrokX extension.",
                })
                return
            fut = asyncio.get_running_loop().create_future()
            self.pending[qid] = (client, fut)
            try:
                await _send(self.browser_ws, {"action": "query", "query_id": qid, "text": text})
                await asyncio.wait_for(fut, self.query_timeout)
            except asyncio.TimeoutError:
                await _send(self.browser_ws, {"action": "stop"})
                await _send(client, {"type": "error", "query_id": qid,
                                     "error": f"No complete answer within {int(self.query_timeout)} s."})
            except asyncio.CancelledError:
                await _send(self.browser_ws, {"action": "stop"})
                raise
            finally:
                self.pending.pop(qid, None)

    async def _run_new_chat(self, client, req_id: str):
        async with self.lock:
            if not await self._wait_browser():
                await _send(client, {"type": "new_chat_done", "req_id": req_id, "ok": False,
                                     "error": "Browser is not connected."})
                return
            fut = asyncio.get_running_loop().create_future()
            self.resets[req_id] = fut
            try:
                await _send(self.browser_ws, {"action": "new_chat", "req_id": req_id})
                res = await asyncio.wait_for(fut, self.new_chat_timeout)
            except asyncio.TimeoutError:
                res = {"ok": False, "error": "Browser did not confirm the new chat."}
            finally:
                self.resets.pop(req_id, None)
            # The tab reloads during /new; give the extension a moment to be ready.
            if res.get("ok"):
                await self._wait_browser()
            await _send(client, {"type": "new_chat_done", "req_id": req_id,
                                 "ok": bool(res.get("ok")), "error": res.get("error", "")})

    # ---------------- lifecycle ----------------
    async def start(self):
        self.server = await websockets.serve(
            self.handle_connection,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=20,
            max_size=20 * 1024 * 1024,
        )
        logger.info("GrokX bridge listening on ws://%s:%s", self.host, self.port)
        self.ready.set()
        await self._stop_event.wait()
        self.server.close()
        await self.server.wait_closed()
        logger.info("GrokX bridge stopped.")

    def stop(self):
        self._stop_event.set()


def setup_logging():
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    """Daemon entry point (python -m grokx_cli.server)."""
    setup_logging()
    server = GrokBridgeServer(host, port)

    async def main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(server.stop))
            except Exception:
                pass
        task = asyncio.create_task(server.start())
        ready = asyncio.create_task(server.ready.wait())
        await asyncio.wait({task, ready}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            ready.cancel()
            try:
                task.result()
            except OSError as e:
                logger.error("Cannot listen on %s:%s (%s). Is another grokx daemon running?", host, port, e)
            return
        try:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        await task

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("Server crashed: %s", e, exc_info=True)
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    run_server()
