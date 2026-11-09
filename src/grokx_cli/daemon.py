"""grokx daemon start/stop/status/logs."""
import asyncio
import time

from rich.console import Console

from grokx_cli.client import GrokClient, ensure_server_running, is_port_open
from grokx_cli.config import DEFAULT_HOST, DEFAULT_PORT, LOG_FILE

console = Console()


def _status(port: int):
    async def go():
        c = GrokClient(f"ws://{DEFAULT_HOST}:{port}")
        try:
            return await c.status()
        finally:
            await c.close()
    return asyncio.run(go())


def start_daemon(port: int = DEFAULT_PORT):
    if is_port_open(port=port):
        st = _status(port) or {}
        console.print(f"[yellow]Демон уже работает (PID {st.get('pid', '?')}, порт {port}).[/yellow]")
        return
    if ensure_server_running(port=port):
        st = _status(port) or {}
        console.print(f"[green]✓ Демон запущен (PID {st.get('pid', '?')}, ws://127.0.0.1:{port}).[/green]")
    else:
        console.print(f"[red]✗ Демон не запустился. Лог: {LOG_FILE}[/red]")


def stop_daemon(port: int = DEFAULT_PORT):
    """Stops the daemon by asking it over the socket (no killing of PIDs from a stale file)."""
    if not is_port_open(port=port):
        console.print("[dim]Демон не запущен.[/dim]")
        return

    async def go():
        c = GrokClient(f"ws://{DEFAULT_HOST}:{port}")
        try:
            return await c.shutdown_daemon()
        finally:
            await c.close()

    asyncio.run(go())
    for _ in range(30):
        if not is_port_open(port=port):
            console.print("[green]✓ Демон остановлен.[/green]")
            return
        time.sleep(0.1)
    console.print(f"[red]✗ Порт {port} всё ещё занят: возможно, это не демон grokx.[/red]")


def restart_daemon(port: int = DEFAULT_PORT):
    stop_daemon(port)
    start_daemon(port)


def show_daemon_status(port: int = DEFAULT_PORT):
    if not is_port_open(port=port):
        console.print(f"[dim]○ Демон выключен (порт {port} свободен). Запустится сам при первом вопросе.[/dim]")
        return
    st = _status(port)
    if not st:
        console.print(f"[red]Порт {port} занят, но это не отвечает как демон grokx.[/red]")
        return
    browser = f"браузер подключён ({st.get('browser_info')})" if st.get("browser_connected") else "браузер не подключён"
    console.print(f"[green]● Демон работает[/green] (PID {st.get('pid')}, v{st.get('version')}, порт {port}), {browser}")


def show_daemon_logs(lines: int = 30):
    if not LOG_FILE.exists():
        console.print("[dim]Лога пока нет.[/dim]")
        return
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    console.print(f"[bold cyan]{LOG_FILE}[/bold cyan] (последние {min(lines, len(content))} строк)\n")
    for line in content[-lines:]:
        console.print(line, markup=False, highlight=False)
