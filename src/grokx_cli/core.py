"""GrokX orchestrator: picks the channel (extension bridge or CDP Chrome) and fails over."""
import asyncio
import logging
from typing import AsyncGenerator, Dict, Optional, Tuple

from grokx_cli.cdp import GrokCDPClient, cdp_grok_tabs, is_cdp_listening
from grokx_cli.client import GrokClient, ensure_server_running, is_port_open
from grokx_cli.config import DEFAULT_CDP_PORT, DEFAULT_HOST, DEFAULT_PORT, GROK_URL

logger = logging.getLogger("grokx.core")


class GrokOrchestrator:
    def __init__(self, bridge_port: int = DEFAULT_PORT, cdp_port: int = DEFAULT_CDP_PORT,
                 mode: str = "auto"):
        """mode: "auto" (bridge first, CDP as fallback) | "bridge" | "cdp"."""
        self.bridge_port = bridge_port
        self.cdp_port = cdp_port
        self.mode = mode
        self.ws_client = GrokClient(ws_url=f"ws://{DEFAULT_HOST}:{bridge_port}")
        self.cdp_client = GrokCDPClient(port=cdp_port)
        self.active_tier: str = "none"   # "bridge" | "cdp" | "none"

    def _order(self):
        if self.mode == "bridge":
            return ["bridge"]
        if self.mode == "cdp":
            return ["cdp"]
        return ["bridge", "cdp"]

    async def _bridge_ready(self, wait: float) -> bool:
        was_running = is_port_open(DEFAULT_HOST, self.bridge_port)
        if not await asyncio.to_thread(ensure_server_running, DEFAULT_HOST, self.bridge_port):
            return False
        if not await self.ws_client.connect():
            return False
        # A daemon that just started needs a couple of seconds until the extension reconnects.
        # Measured on the live setup: ~6.5 s from daemon start to extension connect.
        return await self.ws_client.wait_browser(wait if was_running else max(wait, 12.0))

    async def _cdp_ready(self) -> bool:
        if not is_cdp_listening(self.cdp_port):
            return False
        return await self.cdp_client.connect() and await self.cdp_client.is_ready()

    async def detect_best_tier(self, wait: float = 0.0) -> str:
        for tier in self._order():
            if tier == "bridge" and await self._bridge_ready(wait):
                self.active_tier = "bridge"
                return tier
            if tier == "cdp" and await self._cdp_ready():
                self.active_tier = "cdp"
                return tier
        self.active_tier = "none"
        return "none"

    def _no_channel_error(self) -> Dict[str, str]:
        if self.mode == "cdp":
            hint = f"Chrome with CDP port {self.cdp_port} is not running: grokx --launch"
        else:
            hint = ("Open https://x.com/i/grok in Chrome with the GrokX extension loaded, "
                    "or start the grokx Chrome: grokx --launch")
        return {"type": "error", "error": "No Grok channel is available. " + hint}

    async def query_stream(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        if self.active_tier == "none":
            await self.detect_best_tier()
        tiers = [self.active_tier] if self.active_tier != "none" else []
        tiers += [t for t in self._order() if t not in tiers]

        last_error: Optional[Dict[str, str]] = None
        for tier in tiers:
            if tier != self.active_tier:
                ok = await (self._bridge_ready(0.0) if tier == "bridge" else self._cdp_ready())
                if not ok:
                    continue
                logger.info("Switching to %s channel", tier)
                yield {"type": "info", "message": f"switched to {tier}"}
                self.active_tier = tier
            client = self.ws_client if tier == "bridge" else self.cdp_client
            got_text = False
            async for chunk in client.stream_query(prompt):
                ctype = chunk.get("type")
                if ctype == "error" and not got_text:
                    last_error = chunk      # nothing shown yet: try the next channel
                    break
                if ctype == "chunk":
                    got_text = True
                yield chunk
                if ctype in ("done", "error"):
                    return
            else:
                return
        self.active_tier = "none"
        yield last_error or self._no_channel_error()

    async def query_full(self, prompt: str) -> Tuple[bool, str]:
        text = ""
        async for chunk in self.query_stream(prompt):
            t = chunk.get("type")
            if t == "chunk":
                text += chunk.get("delta", "")
            elif t == "done":
                return True, chunk.get("full") or text
            elif t == "error":
                return False, chunk.get("error", "Unknown error")
        return (True, text) if text else (False, "Empty response")

    async def new_chat(self) -> Tuple[bool, str]:
        if self.active_tier == "none":
            await self.detect_best_tier()
        if self.active_tier == "bridge":
            return await self.ws_client.send_new_chat()
        if self.active_tier == "cdp":
            return await self.cdp_client.send_new_chat()
        return False, self._no_channel_error()["error"]

    async def stop(self):
        if self.active_tier == "bridge":
            await self.ws_client.stop()
        elif self.active_tier == "cdp":
            await self.cdp_client.stop()

    async def get_health_status(self) -> Dict:
        daemon_up = is_port_open(DEFAULT_HOST, self.bridge_port)
        st = await self.ws_client.status() if daemon_up else None
        cdp_up = is_cdp_listening(self.cdp_port)
        return {
            "active_tier": self.active_tier,
            "mode": self.mode,
            "bridge": {
                "port": self.bridge_port,
                "daemon_running": daemon_up,
                "daemon_pid": (st or {}).get("pid"),
                "browser_connected": bool(st and st.get("browser_connected")),
                "browser_info": (st or {}).get("browser_info", ""),
                "busy": bool(st and st.get("busy")),
            },
            "cdp": {
                "port": self.cdp_port,
                "port_listening": cdp_up,
                "grok_tabs": cdp_grok_tabs(self.cdp_port) if cdp_up else [],
            },
            "grok_url": GROK_URL,
        }

    async def close(self):
        await self.ws_client.close()
        await self.cdp_client.close()
