"""CDP tier: drive Grok in a dedicated Chrome through the DevTools protocol.

Chrome 136+ ignores --remote-debugging-port for the default profile, so grokx
runs its own Chrome with its own profile (~/.grokx/chrome-profile) on port 9333.
Log in to X once in that window; the session is kept in the profile.

Answers are read with the same network hook the extension uses (extension/injected.js),
delivered through a Playwright binding.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from typing import AsyncGenerator, Dict, Optional, Tuple

from grokx_cli.config import (
    CHROME_PROFILE_DIR,
    DEFAULT_CDP_PORT,
    DEFAULT_HOST,
    GROK_URL,
    HOOK_JS,
    NO_STREAM_TIMEOUT,
    QUERY_TIMEOUT,
)

GROK_PATH_RE = re.compile(r"^https?://[^/]+/i/grok(?:[/?#]|$)", re.I)
INPUT_SELECTOR = 'textarea, div[contenteditable="true"]'
STOP_LABEL_RE = re.compile(r"^(stop|stop generating|остановить|зупинити|zatrzymaj)\b", re.I)
PAGE_SETTLE_S = 3.5   # a just-loaded Grok page shows the input before it can send
SUBMIT_TRIES = 4


def cdp_url(port: int = DEFAULT_CDP_PORT) -> str:
    return f"http://{DEFAULT_HOST}:{port}"


def find_browser_executable() -> Optional[str]:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _get_json(url: str, timeout: float = 0.7):
    req = urllib.request.Request(url, headers={"User-Agent": "grokx"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_cdp_listening(port: int = DEFAULT_CDP_PORT) -> bool:
    try:
        _get_json(f"{cdp_url(port)}/json/version")
        return True
    except Exception:
        return False


def cdp_grok_tabs(port: int = DEFAULT_CDP_PORT) -> list:
    """URLs of Grok tabs in the CDP Chrome (cheap check, no Playwright)."""
    try:
        return [t.get("url", "") for t in _get_json(f"{cdp_url(port)}/json/list")
                if t.get("type") == "page" and GROK_PATH_RE.match(t.get("url", ""))]
    except Exception:
        return []


def launch_chrome_cdp(port: int = DEFAULT_CDP_PORT, url: str = GROK_URL, wait: float = 15.0) -> Tuple[bool, str]:
    """Starts the dedicated grokx Chrome. Returns (ok, message)."""
    if is_cdp_listening(port):
        return True, f"Chrome is already listening on port {port}."
    exe = find_browser_executable()
    if not exe:
        return False, "Chrome / Edge / Brave not found."
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
    except OSError as e:
        return False, f"Failed to start {exe}: {e}"
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_cdp_listening(port):
            return True, f"Chrome started on port {port} with profile {CHROME_PROFILE_DIR}."
        time.sleep(0.3)
    return False, f"Chrome started but port {port} did not open within {int(wait)} s."


class GrokCDPClient:
    """Playwright connection to the grokx Chrome over CDP."""

    def __init__(self, port: int = DEFAULT_CDP_PORT, grok_url: str = GROK_URL):
        self.port = port
        self.grok_url = grok_url
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._queue: Optional[asyncio.Queue] = None
        self._hooked_pages = set()
        self._lock = asyncio.Lock()
        self._loaded_at = 0.0

    @property
    def connected(self) -> bool:
        try:
            return bool(self.browser and self.browser.is_connected() and self.page and not self.page.is_closed())
        except Exception:
            return False

    async def connect(self) -> bool:
        if self.connected:
            return True
        await self.close()
        if not is_cdp_listening(self.port):
            return False
        try:
            from playwright.async_api import async_playwright

            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url(self.port), timeout=10000)
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.page = await self._find_or_open_grok_page()
            await self._install_hook(self.page)
            return True
        except Exception:
            await self.close()
            return False

    async def _find_or_open_grok_page(self):
        for p in self.context.pages:
            if GROK_PATH_RE.match(p.url or ""):
                return p
        p = await self.context.new_page()
        await p.goto(self.grok_url, wait_until="domcontentloaded")
        self._loaded_at = time.monotonic()
        return p

    def _on_emit(self, evt):
        if self._queue is not None and isinstance(evt, dict):
            self._queue.put_nowait(evt)

    async def _install_hook(self, page):
        if id(page) in self._hooked_pages:
            return
        hook = HOOK_JS.read_text(encoding="utf-8")
        await page.expose_function("__grokxEmit", self._on_emit)
        await page.add_init_script(hook)       # future navigations / reloads
        if not await page.evaluate("!!window.__GROKX_HOOK__"):
            # Grok's answer WebSocket is opened during page load, so the hook must be in
            # place before it: reload instead of injecting into the running page.
            await page.reload(wait_until="domcontentloaded")
            self._loaded_at = time.monotonic()
        self._hooked_pages.add(id(page))

    async def is_ready(self) -> bool:
        return self.connected and bool(GROK_PATH_RE.match(self.page.url or ""))

    async def _find_input(self, timeout: float = 15.0):
        loc = self.page.locator(INPUT_SELECTOR).filter(visible=True).first
        try:
            await loc.wait_for(state="visible", timeout=timeout * 1000)
            return loc
        except Exception:
            return None

    async def send_new_chat(self) -> Tuple[bool, str]:
        if not await self.connect():
            return False, "CDP Chrome is not reachable."
        async with self._lock:
            try:
                await self.page.goto(self.grok_url, wait_until="domcontentloaded")
                self._loaded_at = time.monotonic()
                if await self._find_input() is None:
                    return False, "Grok page loaded without an input box (logged out of X?)."
                return True, "New chat started."
            except Exception as e:
                return False, str(e)

    async def stop(self):
        if not self.connected:
            return
        try:
            btns = self.page.get_by_role("button", name=STOP_LABEL_RE)
            if await btns.count():
                await btns.first.click(timeout=2000)
        except Exception:
            pass

    async def stream_query(self, prompt: str) -> AsyncGenerator[Dict[str, str], None]:
        if not await self.connect():
            yield {"type": "error", "error": f"CDP Chrome is not reachable on port {self.port} (run: grokx --launch)."}
            return
        async with self._lock:
            async for item in self._stream_locked(prompt):
                yield item

    async def _stream_locked(self, prompt: str):
        page = self.page
        if not GROK_PATH_RE.match(page.url or ""):
            await page.goto(self.grok_url, wait_until="domcontentloaded")
            self._loaded_at = time.monotonic()
        inp = await self._find_input()
        if inp is None:
            hint = " The tab looks logged out of X: log in once in the grokx Chrome window." \
                if re.search(r"login|signin|flow", page.url or "", re.I) else ""
            yield {"type": "error", "error": "Grok input box not found." + hint}
            return

        self._queue = asyncio.Queue()
        try:
            settle = PAGE_SETTLE_S - (time.monotonic() - self._loaded_at)
            if settle > 0:
                await asyncio.sleep(settle)
            await inp.fill(prompt)
            await inp.press("Enter")
            tries = 1
            loop = asyncio.get_running_loop()
            submitted = loop.time()
            stream_id = None
            full = ""

            while True:
                if stream_id is None:
                    wait = NO_STREAM_TIMEOUT - (loop.time() - submitted)
                    if wait <= 0:
                        yield {"type": "error", "error": "No Grok answer stream within 45 s after submitting."}
                        return
                    wait = min(wait, 1.5)
                else:
                    wait = QUERY_TIMEOUT
                try:
                    evt = await asyncio.wait_for(self._queue.get(), timeout=wait)
                except asyncio.TimeoutError:
                    if stream_id is None and tries < SUBMIT_TRIES and await self._still_typed(inp):
                        tries += 1
                        await inp.fill(prompt)   # retype: the editor may not have been hydrated
                        await inp.press("Enter")
                        continue
                    if stream_id is not None:
                        yield {"type": "error", "error": "Grok answer timed out."}
                        return
                    continue
                etype = evt.get("type")
                if etype == "GROKX_STREAM_START" and stream_id is None:
                    stream_id = evt.get("id")
                elif etype == "GROKX_STREAM_CHUNK" and evt.get("id") == stream_id:
                    delta = evt.get("delta", "")
                    full += delta
                    yield {"type": "chunk", "delta": delta, "full": full}
                elif etype == "GROKX_STREAM_DONE" and evt.get("id") == stream_id:
                    final = evt.get("full") or full
                    if not final and str(evt.get("error", "")).startswith("GROK_ERROR "):
                        yield {"type": "error", "error": evt["error"]}
                        return
                    if not final:
                        yield {"type": "error", "error": "Grok stream ended without text. Sample: "
                                                         + json.dumps(evt.get("sample", []), ensure_ascii=False)}
                        return
                    if not full:
                        yield {"type": "chunk", "delta": final, "full": final}
                    yield {"type": "done", "full": final}
                    return
                elif etype == "GROKX_STREAM_ERROR" and stream_id is None:
                    yield {"type": "error", "error": f"Grok returned HTTP {evt.get('status')}: {evt.get('body', '')[:300]}"}
                    return
        except Exception as e:
            yield {"type": "error", "error": f"CDP error: {e}"}
        finally:
            self._queue = None

    async def _still_typed(self, inp) -> bool:
        try:
            value = await inp.evaluate("el => (el.value !== undefined ? el.value : el.innerText) || ''")
            return bool(value.strip())
        except Exception:
            return False

    async def close(self):
        """Disconnects Playwright; the Chrome window itself keeps running."""
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.page = self.context = self.browser = self.playwright = None
        self._hooked_pages.clear()
