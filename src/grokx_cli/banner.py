"""ASCII Banner and terminal styling for GrokX."""
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

BANNER_TEXT = r"""
  ____ ____   ___  _  __ __  __
 / ___|  _ \ / _ \| |/ / \ \/ /
| |  _| |_) | | | | ' /   \  / 
| |_| |  _ <| |_| | . \   /  \ 
 \____|_| \_\\___/|_|\_\ /_/\_\
"""

def print_banner(console: Console, backend_name: str = "none", bridge_port: int = 29191, cdp_port: int = 9333) -> None:
    """Prints styled banner with multi-tier connection indicators."""
    banner = Text(BANNER_TEXT.strip("\n"), style="bold cyan")

    subtext = Text()
    subtext.append(" GrokX-Cli AI Terminal Assistant (Multi-Tier Bridge)\n", style="bright_black")

    # Connection Status
    subtext.append(" Status: ", style="bright_black")
    if backend_name == "bridge":
        subtext.append("● Connected via Web Bridge (Extension / Userscript)", style="bold green")
    elif backend_name == "cdp":
        subtext.append(f"● Connected via Chrome CDP (Port {cdp_port})", style="bold green")
    else:
        subtext.append("○ Waiting for x.com/i/grok (Bridge or CDP)", style="bold yellow")

    subtext.append("\n Channels: ", style="bright_black")
    subtext.append(f"WebSocket :{bridge_port}", style="white" if backend_name == "bridge" else "dim")
    subtext.append("  |  ", style="bright_black")
    subtext.append(f"Chrome CDP :{cdp_port}", style="white" if backend_name == "cdp" else "dim")
    subtext.append("  |  ", style="bright_black")
    subtext.append("Auto-Failover: ON", style="bold cyan")

    subtext.append("\n Commands: ", style="bright_black")
    subtext.append("/new", style="bold white")
    subtext.append(" (reset), ", style="bright_black")
    subtext.append("/copy", style="bold white")
    subtext.append(" (clipboard), ", style="bright_black")
    subtext.append("/status", style="bold white")
    subtext.append(", ", style="bright_black")
    subtext.append("/help", style="bold white")
    subtext.append(", ", style="bright_black")
    subtext.append("/exit", style="bold white")

    panel = Panel(
        Text.assemble(banner, "\n\n", subtext),
        border_style="bright_blue",
        padding=(1, 2),
        subtitle="[dim]x.com Web Bridge & CDP Engine[/dim]",
        subtitle_align="right"
    )
    console.print(panel)
