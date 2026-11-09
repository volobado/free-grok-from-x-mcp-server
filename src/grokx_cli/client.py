"""Client for the GrokX bridge daemon, plus daemon auto-start."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator, Callable, Dict, Optional, Tuple

import websockets

from grokx_cli.config import DEFAULT_HOST, DEFAULT_PORT, NEW_CHAT_TIMEOUT, PROJECT_DIR, QUERY_TIMEOUT, WS_URL


def is_ws_open(ws) -> bool:
    if ws is None:
        return False
    try:
        return ws.state.name == "OPEN"
    except Exception:
        return False


def is_port_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _daemon_python() -> str:
    """pythonw.exe next to the current interpreter: no console window for the daemon."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(exe)


def spawn_daemon(port: int = DEFAULT_PORT) -> bool:
    """Starts `python -m grokx_cli.server` fully detached from the caller."""
    cmd = [_daemon_python(), "-m", "grokx_cli.server"]
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                  cwd=str(PROJECT_DIR), close_fds=True)
    env = dict(os.environ, GROKX_PORT=str(port)) if port != DEFAULT_PORT else None
    if sys.platform == "win32":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # Break away from the caller's job object (MCP hosts kill their children's job on exit).
        for flags in (base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base):
            try:
                subprocess.Popen(cmd, creationflags=flags, env=env, **kwargs)
                return True
            except OSError:
                continue
        return False
    try:
        subprocess.Popen(cmd, start_new_session=True, env=env, **kwargs)
        return True
    except OSError:
        return False


def ensure_server_running(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 6.0) -> bool:
    """Returns True if the daemon answers on the port, starting it if needed."""
    if is_port_open(host, port):
        return True
    if not spawn_daemon(port):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_open(host, port):
            return True
        time.sleep(0.1)
    return is_port_open(host, port)


class GrokClient:
    """One WebSocket connection to the daemon (role "client")."""

    def __init__(self, ws_url: str = WS_URL):
        self.ws_url = ws_url
        self.ws = None
        self.browser_connected = False

    async def connect(self) -> bool:
        if is_ws_open(self.ws):
            return True
        await self.close()
        try:
            self.ws = await websockets.connect(self.ws_url, open_timeout=3, ping_interval=20,
                                               ping_timeout=20, max_size=20 * 1024 * 1024)
            await self.ws.send(json.dumps({"role": "client"}))
            await self._recv_until(lambda m: m.get("type") == "browser_status", 3.0)
            return True
        except Exception:
            await self.close()
            return False

    async def _recv_until(self, pred: Callable[[dict], bool], timeout: Optional[float]) -> Optional[dict]:
        """Reads messages until pred matches; tracks browser_status on the way."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            if remaining == 0.0:
                return None
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "browser_status":
                self.browser_connected = bool(msg.get("connected"))
            if pred(msg):
                return msg

    async def status(self) -> Optional[dict]:
        if not await self.connect():
            return None
        try:
            await self.ws.send(json.dumps({"action": "status"}))
            msg = await self._recv_until(lambda m: m.get("type") == "status_response", 3.0)
        except Exception:
            return None
        if msg:
            self.browser_connected = bool(msg.get("browser_connected"))
        return msg

    async def is_browser_ready(self) -> bool:
        st = await self.status()
        return bool(st and st.get("browser_connected"))

    async def wait_browser(self, timeout: float) -> bool:
        """Polls until the extension is connected (it reconnects every ~2 s)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if await self.is_browser_ready():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def send_new_chat(self, timeout: float = NEW_CHAT_TIMEOUT + 30) -> Tuple[bool, str]:
        if not await self.connect():
            return False, "Not connected to the grokx daemon."
        req_id = uuid.uuid4().hex[:8]
        try:
            await self.ws.send(json.dumps({"action": "new_chat", "req_id": req_id}))
            msg = await self._recv_until(lambda m: m.get("type") == "new_chat_done" and m.get("req_id") == req_id,
                                         timeout)
        except websockets.exceptions.ConnectionClosed:
            return False, "Daemon connection closed."
        if not msg:
            return False, "No confirmation from the browser."
        return bool(msg.get("ok")), msg.get("error") or "New chat started."

    async def stop(self):
        if is_ws_open(self.ws):
            try:
                await self.ws.send(json.dumps({"action": "stop"}))
            except Exception:
                pass

    async def reload_extension(self) -> bool:
        """Asks the connected extension to reload itself from disk (and its Grok tabs)."""
        if not await self.connect():
            return False
        await self.ws.send(json.dumps({"action": "reload_extension"}))
        msg = await self._recv_until(lambda m: m.get("type") == "reload_sent", 3.0)
        return bool(msg and msg.get("ok"))

    async def shutdown_daemon(self) -> bool:
        if not await self.connect():
            return False
        try:
            await self.ws.send(json.dumps({"action": "shutdown"}))
            await self._recv_until(lambda m: m.get("type") == "info", 2.0)
            return True
        except Exception:
            return True  # the daemon closed the socket while stopping

    async def stream_query(self, prompt: str, timeout: float = QUERY_TIMEOUT + 60) -> AsyncGenerator[Dict[str, str], None]:
        """Yields {"type": "chunk", "delta", "full"} ... then {"type": "done", "full"} or {"type": "error"}.

        Also yields {"type": "info", "message"} for queue notices.
        """
        if not await self.connect():
            yield {"type": "error", "error": "Not connected to the grokx daemon."}
            return
        qid = uuid.uuid4().hex[:10]
        try:
            await self.ws.send(json.dumps({"action": "query", "query_id": qid, "text": prompt}, ensure_ascii=False))
        except websockets.exceptions.ConnectionClosed:
            yield {"type": "error", "error": "Daemon connection closed."}
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        full = ""
        try:
            while True:
                msg = await self._recv_until(lambda m: m.get("query_id") == qid,
                                             max(0.1, deadline - loop.time()))
                if msg is None:
                    yield {"type": "error", "error": "Timed out waiting for Grok."}
                    return
                mtype = msg.get("type")
                if mtype == "chunk":
                    delta = msg.get("delta", "")
                    full += delta
                    yield {"type": "chunk", "delta": delta, "full": msg.get("full") or full}
                elif mtype == "done":
                    yield {"type": "done", "full": msg.get("full") or full, "stopped": bool(msg.get("stopped"))}
                    return
                elif mtype == "error":
                    yield {"type": "error", "error": msg.get("error", "Unknown error.")}
                    return
                elif mtype == "info":
                    yield {"type": "info", "message": msg.get("message", "")}
        except websockets.exceptions.ConnectionClosed:
            yield {"type": "error", "error": "Daemon connection closed."}

    async def close(self):
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
