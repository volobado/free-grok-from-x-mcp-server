"""GrokX CLI: interactive REPL and one-shot queries to Grok on x.com."""
import argparse
import asyncio
import signal
import sys
import time
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from grokx_cli.banner import print_banner
from grokx_cli.config import DEFAULT_CDP_PORT, DEFAULT_PORT, HISTORY_FILE
from grokx_cli.core import GrokOrchestrator

console = Console()
err_console = Console(stderr=True)


def read_piped_stdin() -> str:
    """Reads stdin to the end when it is a pipe or a file (not an interactive console)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        data = sys.stdin.buffer.read()
    except Exception:
        return ""
    if not data:
        return ""
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("mbcs" if sys.platform == "win32" else "latin-1", errors="replace")


class GrokCLI:
    def __init__(self, bridge_port: int = DEFAULT_PORT, cdp_port: int = DEFAULT_CDP_PORT, mode: str = "auto"):
        self.orchestrator = GrokOrchestrator(bridge_port=bridge_port, cdp_port=cdp_port, mode=mode)
        self.last_response: Optional[str] = None
        self._query_task: Optional[asyncio.Task] = None

    def channel_label(self) -> str:
        t = self.orchestrator.active_tier
        return "extension" if t == "bridge" else f"CDP :{self.orchestrator.cdp_port}" if t == "cdp" else "?"

    # ---------------- query ----------------
    async def _stream(self, prompt: str, plain: bool) -> bool:
        full = ""
        start = time.time()
        error = None
        stopped = False

        def tail_view() -> Text:
            lines = full.splitlines() or [""]
            height = max(5, console.size.height - 3)
            return Text("\n".join(lines[-height:]))

        spinner = Spinner("dots", text=Text(" Grok думает...", style="bright_black"))
        live = None if plain else Live(spinner, console=console, refresh_per_second=12, transient=True)
        try:
            if live:
                live.start()
            async for chunk in self.orchestrator.query_stream(prompt):
                ctype = chunk.get("type")
                if ctype == "chunk":
                    full += chunk.get("delta", "")
                    if live:
                        live.update(tail_view())
                elif ctype == "done":
                    full = chunk.get("full") or full
                    stopped = chunk.get("stopped", False)
                    break
                elif ctype == "error":
                    error = chunk.get("error")
                    break
                elif ctype == "info" and live:
                    spinner.text = Text(f" Grok думает... ({chunk.get('message')})", style="bright_black")
        except asyncio.CancelledError:
            await self.orchestrator.stop()
            stopped = True
            raise
        finally:
            if live:
                live.stop()
            if full:
                self.last_response = full
                if plain:
                    sys.stdout.write(full.rstrip("\n") + "\n")
                    sys.stdout.flush()
                else:
                    console.print(Markdown(full))
            if not plain and (full or stopped):
                note = " • прервано" if stopped else ""
                console.print(f"[dim]─── {time.time() - start:.1f}s • {self.channel_label()}{note} ───[/dim]\n")

        if error:
            err_console.print(f"[bold red]✗ Ошибка:[/bold red] {error}")
            return bool(full)
        if not full:
            err_console.print("[yellow]Grok ничего не ответил.[/yellow]")
            return False
        return True

    async def execute_query(self, prompt: str, plain: bool = False) -> bool:
        if not prompt.strip():
            return False
        self._query_task = asyncio.current_task()
        try:
            return await self._stream(prompt, plain)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.uncancel() == 0:
                console.print("[dim]Ответ прерван (Ctrl+C).[/dim]\n")
                return False
            raise
        finally:
            self._query_task = None

    def _on_sigint(self, *_):
        task = self._query_task
        if task is not None and not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)
        else:
            raise KeyboardInterrupt

    # ---------------- REPL ----------------
    async def handle_slash_command(self, cmd: str) -> bool:
        base = cmd.strip().split(maxsplit=1)[0].lower()
        if base in ("/exit", "/quit"):
            return False
        if base in ("/help", "/?"):
            self.print_help()
        elif base == "/new":
            with console.status("[bright_black]Новый чат...[/bright_black]"):
                ok, msg = await self.orchestrator.new_chat()
            console.print("[green]✓ Новый чат.[/green]\n" if ok else f"[red]✗ Не удалось: {msg}[/red]\n")
        elif base == "/copy":
            if not self.last_response:
                console.print("[yellow]Пока нечего копировать.[/yellow]\n")
            else:
                try:
                    import pyperclip
                    pyperclip.copy(self.last_response)
                    console.print("[green]✓ Ответ скопирован в буфер обмена.[/green]\n")
                except Exception as e:
                    console.print(f"[red]✗ Буфер обмена недоступен: {e}[/red]\n")
        elif base == "/clear":
            console.clear()
            print_banner(console, self.orchestrator.active_tier, self.orchestrator.bridge_port, self.orchestrator.cdp_port)
        elif base == "/status":
            await self.show_status()
        elif base == "/doctor":
            from grokx_cli.doctor import run_doctor
            await asyncio.to_thread(run_doctor)
        else:
            console.print(f"[yellow]Неизвестная команда {base}. /help покажет список.[/yellow]\n")
        return True

    async def show_status(self):
        h = await self.orchestrator.get_health_status()
        b, c = h["bridge"], h["cdp"]
        t = Text()
        t.append("Каналы GrokX\n\n", style="bold cyan")
        t.append(" 1. Расширение Chrome (мост)\n", style="bold white")
        t.append(f"   демон ws://127.0.0.1:{b['port']}: ", style="bright_black")
        t.append(f"работает, PID {b['daemon_pid']}\n" if b["daemon_running"] else "не запущен\n",
                 style="green" if b["daemon_running"] else "yellow")
        t.append("   браузер с расширением: ", style="bright_black")
        t.append(f"подключён ({b['browser_info']})\n" if b["browser_connected"] else "не подключён\n",
                 style="green" if b["browser_connected"] else "yellow")
        t.append("\n 2. Отдельный Chrome по CDP\n", style="bold white")
        t.append(f"   порт {c['port']}: ", style="bright_black")
        t.append("открыт\n" if c["port_listening"] else "закрыт (grokx --launch)\n",
                 style="green" if c["port_listening"] else "yellow")
        if c["port_listening"]:
            t.append("   вкладка Grok: ", style="bright_black")
            t.append("есть\n" if c["grok_tabs"] else "нет (откроется сама)\n",
                     style="green" if c["grok_tabs"] else "yellow")
        t.append("\n Активный канал: ", style="bold white")
        tier = h["active_tier"]
        t.append({"bridge": "расширение", "cdp": f"CDP :{c['port']}"}.get(tier, "не выбран"),
                 style="bold green" if tier != "none" else "bold yellow")
        console.print(Panel(t, border_style="cyan", padding=(1, 2)))

    def print_help(self):
        console.print(Panel("""[bold cyan]Команды:[/bold cyan]
  [bold]/new[/bold]     новый чат в Grok
  [bold]/copy[/bold]    скопировать последний ответ
  [bold]/status[/bold]  состояние каналов
  [bold]/doctor[/bold]  полная диагностика
  [bold]/clear[/bold]   очистить экран
  [bold]/exit[/bold]    выход (или Ctrl+D)

[bold cyan]Ввод:[/bold cyan] Enter отправляет, Alt+Enter или \\ в конце строки даёт новую строку.
Ctrl+C во время ответа прерывает генерацию.""", border_style="cyan", padding=(0, 2)))

    async def repl(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        with console.status("[bright_black]Ищу Grok...[/bright_black]"):
            await self.orchestrator.detect_best_tier(wait=3.0)
        print_banner(console, self.orchestrator.active_tier, self.orchestrator.bridge_port, self.orchestrator.cdp_port)
        if self.orchestrator.active_tier == "none":
            console.print("[yellow]Grok пока не найден.[/yellow] Откройте https://x.com/i/grok в Chrome с расширением "
                          "GrokX или запустите отдельный Chrome: [bold]grokx --launch[/bold]. "
                          "Первый вопрос повторит поиск.\n")

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        session = PromptSession(history=FileHistory(str(HISTORY_FILE)), key_bindings=kb,
                                style=Style.from_dict({"prompt": "ansicyan bold", "arrow": "ansibrightblack bold"}))
        prompt_tokens = [("class:prompt", "grokx"), ("class:arrow", " > ")]

        signal.signal(signal.SIGINT, self._on_sigint)
        try:
            while True:
                try:
                    text = await session.prompt_async(prompt_tokens)
                    while text.rstrip().endswith("\\"):
                        text = text.rstrip()[:-1] + "\n" + await session.prompt_async("... ")
                except (KeyboardInterrupt, EOFError):
                    break
                text = text.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if not await self.handle_slash_command(text):
                        break
                else:
                    await self.execute_query(text)
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            console.print("[dim]До встречи![/dim]")
            await self.orchestrator.close()


def main_sync():
    parser = argparse.ArgumentParser(
        prog="grokx",
        description="GrokX: терминал для Grok на x.com (через расширение Chrome или отдельный Chrome по CDP)",
        epilog="Подкоманды: grokx doctor | grokx mcp | grokx daemon [start|stop|restart|status|logs]",
    )
    parser.add_argument("prompt", nargs="*", help="Вопрос к Grok (без него запускается интерактивный режим)")
    parser.add_argument("-n", "--new", action="store_true", help="Начать новый чат перед вопросом")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"Порт демона-моста ({DEFAULT_PORT})")
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT, help=f"Порт CDP Chrome ({DEFAULT_CDP_PORT})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cdp", action="store_true", help="Только через отдельный Chrome по CDP")
    mode.add_argument("--bridge", action="store_true", help="Только через расширение")
    parser.add_argument("--launch", action="store_true", help="Запустить отдельный Chrome для grokx (порт CDP)")
    parser.add_argument("--status", action="store_true", help="Показать состояние каналов")
    parser.add_argument("--stop-daemon", action="store_true", help="Остановить демон-мост")
    args = parser.parse_args()

    tokens = args.prompt
    first = tokens[0].lower() if tokens else ""

    if first == "doctor" and len(tokens) == 1:
        from grokx_cli.doctor import run_doctor
        run_doctor()
        return
    if first == "mcp" and len(tokens) == 1:
        from grokx_cli.mcp_server import main as run_mcp
        run_mcp()
        return
    if first == "daemon" and len(tokens) <= 2:
        from grokx_cli import daemon
        sub = tokens[1].lower() if len(tokens) > 1 else "status"
        actions = {"start": lambda: daemon.start_daemon(args.port), "stop": lambda: daemon.stop_daemon(args.port),
                   "restart": lambda: daemon.restart_daemon(args.port), "logs": daemon.show_daemon_logs,
                   "status": lambda: daemon.show_daemon_status(args.port)}
        actions.get(sub, actions["status"])()
        return
    if first == "reload-extension" and len(tokens) == 1:
        from grokx_cli.client import GrokClient

        async def _reload():
            c = GrokClient(f"ws://127.0.0.1:{args.port}")
            try:
                return await c.reload_extension()
            finally:
                await c.close()
        ok = asyncio.run(_reload())
        console.print("[green]✓ Расширение перезагружается вместе с вкладками Grok.[/green]" if ok
                      else "[red]✗ Расширение не подключено к демону.[/red]")
        sys.exit(0 if ok else 1)
    if args.stop_daemon:
        from grokx_cli import daemon
        daemon.stop_daemon(args.port)
        return
    if args.launch:
        from grokx_cli.cdp import launch_chrome_cdp
        ok, msg = launch_chrome_cdp(port=args.cdp_port)
        console.print(("[green]✓ " if ok else "[red]✗ ") + msg + ("[/green]" if ok else "[/red]"))
        if ok:
            console.print("Если в этом окне Chrome вы ещё не вошли в X, войдите один раз: профиль сохранится.")
        sys.exit(0 if ok else 1)

    cli = GrokCLI(bridge_port=args.port, cdp_port=args.cdp_port,
                  mode="cdp" if args.cdp else "bridge" if args.bridge else "auto")

    if args.status:
        async def _status():
            await cli.show_status()
            await cli.orchestrator.close()
        asyncio.run(_status())
        return

    piped = read_piped_stdin()
    prompt_arg = " ".join(tokens).strip()
    full_prompt = "\n\n".join(p for p in (piped.strip(), prompt_arg) if p)

    if not full_prompt:
        if piped == "" and not sys.stdin.isatty():
            err_console.print("[yellow]Пустой ввод: нечего спрашивать.[/yellow]")
            sys.exit(2)
        try:
            asyncio.run(cli.repl())
        except KeyboardInterrupt:
            pass
        return

    plain = not sys.stdout.isatty()
    if plain:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    async def _once() -> int:
        try:
            if await cli.orchestrator.detect_best_tier(wait=3.0) == "none":
                err_console.print("[yellow]⚠ Grok не найден.[/yellow] Откройте https://x.com/i/grok в Chrome с "
                                  "расширением GrokX или запустите: grokx --launch")
                return 1
            if args.new:
                ok, msg = await cli.orchestrator.new_chat()
                if not ok:
                    err_console.print(f"[red]✗ Новый чат не создан: {msg}[/red]")
                    return 1
            return 0 if await cli.execute_query(full_prompt, plain=plain) else 1
        finally:
            await cli.orchestrator.close()

    try:
        code = asyncio.run(_once())
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main_sync()
