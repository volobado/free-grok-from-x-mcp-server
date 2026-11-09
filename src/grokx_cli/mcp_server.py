"""MCP server (stdio) exposing Grok to Claude Code, agy and other MCP clients.

stdout belongs to the JSON-RPC protocol: nothing else may print there.
"""
import logging
import sys

from mcp.server.fastmcp import FastMCP

from grokx_cli.core import GrokOrchestrator

logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

mcp = FastMCP("grokx", log_level="WARNING")
_orchestrator = None


def orchestrator() -> GrokOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = GrokOrchestrator()
    return _orchestrator


@mcp.tool()
async def grok_query(prompt: str, new_chat: bool = False) -> str:
    """Ask Grok (x.com/i/grok, the user's logged-in X session) and return the full answer.

    Args:
        prompt: The question or task for Grok.
        new_chat: Start a fresh Grok conversation first (otherwise the current chat context continues).
    """
    o = orchestrator()
    if new_chat:
        ok, msg = await o.new_chat()
        if not ok:
            return f"Error: could not start a new Grok chat: {msg}"
    ok, result = await o.query_full(prompt)
    return result if ok else f"Error communicating with Grok: {result}"


@mcp.tool()
async def grok_new_chat() -> str:
    """Start a fresh Grok conversation."""
    ok, msg = await orchestrator().new_chat()
    return "New Grok chat started." if ok else f"Failed to start a new chat: {msg}"


@mcp.tool()
async def grok_status() -> dict:
    """Connectivity of the GrokX channels (extension bridge daemon, CDP Chrome)."""
    return await orchestrator().get_health_status()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
