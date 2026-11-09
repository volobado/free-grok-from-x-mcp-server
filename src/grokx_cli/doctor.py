"""grokx doctor: checks every piece and says what to do about it."""
import asyncio
import json
import shutil
import sys

from rich.console import Console
from rich.table import Table

from grokx_cli.cdp import CHROME_PROFILE_DIR, cdp_grok_tabs, find_browser_executable, is_cdp_listening
from grokx_cli.client import GrokClient, is_port_open
from grokx_cli.config import DEFAULT_CDP_PORT, DEFAULT_HOST, DEFAULT_PORT, EXTENSION_DIR, GROKX_HOME, HOOK_JS, LOG_FILE

console = Console()
OK, WARN, BAD, OFF = "[green]✓ OK[/green]", "[yellow]⚠[/yellow]", "[red]✗[/red]", "[dim]○[/dim]"


def _daemon_status():
    async def go():
        c = GrokClient(f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}")
        try:
            return await c.status()
        finally:
            await c.close()
    try:
        return asyncio.run(go())
    except Exception:
        return None


def run_doctor():
    t = Table(title="grokx doctor", show_lines=True)
    t.add_column("Что", style="bold white", width=26)
    t.add_column("", width=6)
    t.add_column("Подробности", style="white")

    t.add_row("Python", OK, f"{sys.version.split()[0]} ({sys.executable})")

    missing = []
    for mod in ("websockets", "rich", "prompt_toolkit", "pyperclip", "playwright", "mcp"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    t.add_row("Библиотеки", BAD if missing else OK,
              f"нет: {', '.join(missing)}; запустите setup.bat" if missing else "все на месте")

    cmd = shutil.which("grokx")
    t.add_row("Команда grokx в PATH", OK if cmd else WARN, cmd or "нет; setup.bat копирует её в ~/.local/bin")

    manifest = EXTENSION_DIR / "manifest.json"
    ext_ok = manifest.exists() and HOOK_JS.exists()
    version = json.loads(manifest.read_text(encoding="utf-8")).get("version") if manifest.exists() else "?"
    t.add_row("Файлы расширения", OK if ext_ok else BAD,
              f"{EXTENSION_DIR} (v{version})\nChrome: chrome://extensions, режим разработчика, "
              "«Загрузить распакованное», выбрать эту папку")

    if is_port_open(port=DEFAULT_PORT):
        st = _daemon_status()
        if st is None:
            t.add_row(f"Демон-мост :{DEFAULT_PORT}", BAD, "порт занят чем-то, что не отвечает как grokx")
        else:
            t.add_row(f"Демон-мост :{DEFAULT_PORT}", OK, f"PID {st.get('pid')}, v{st.get('version')}")
            if st.get("browser_connected"):
                t.add_row("Браузер с расширением", OK, st.get("browser_info") or "подключён")
            else:
                t.add_row("Браузер с расширением", OFF,
                          "не подключён: откройте https://x.com/i/grok в Chrome, где загружено расширение")
    else:
        t.add_row(f"Демон-мост :{DEFAULT_PORT}", OFF, "не запущен; стартует сам при первом вопросе")

    exe = find_browser_executable()
    t.add_row("Chrome / Edge", OK if exe else WARN, exe or "не найден (нужен только для grokx --launch)")
    if is_cdp_listening(DEFAULT_CDP_PORT):
        tabs = cdp_grok_tabs(DEFAULT_CDP_PORT)
        t.add_row(f"CDP Chrome :{DEFAULT_CDP_PORT}", OK,
                  f"работает, вкладок Grok: {len(tabs)}; профиль {CHROME_PROFILE_DIR}")
    else:
        t.add_row(f"CDP Chrome :{DEFAULT_CDP_PORT}", OFF,
                  "не запущен (запасной канал): grokx --launch, затем один раз войти в X в этом окне")

    t.add_row("Данные", OK, f"{GROKX_HOME} (лог: {LOG_FILE.name})")
    console.print(t)
