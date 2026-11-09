"""Configuration for GrokX CLI, bridge daemon and CDP tier."""
import os
from pathlib import Path

# Bridge daemon (the Chrome extension connects here; the port is hardcoded in extension/background.js)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("GROKX_PORT", 29191))
WS_URL = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}"

# CDP tier: a separate Chrome with its own profile. Not 9222: that is the port everyone's
# debug Chrome uses, and grokx must never attach to (and open tabs in) someone else's browser.
DEFAULT_CDP_PORT = int(os.environ.get("GROKX_CDP_PORT", 9333))

# User directory for history, logs, the CDP Chrome profile
GROKX_HOME = Path(os.environ.get("GROKX_HOME", Path.home() / ".grokx"))
GROKX_HOME.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = GROKX_HOME / "history.txt"
LOG_FILE = GROKX_HOME / "grokx.log"
PID_FILE = GROKX_HOME / "daemon.pid"
CHROME_PROFILE_DIR = GROKX_HOME / "chrome-profile"

# Timeouts (seconds)
BROWSER_WAIT = 20.0        # query arrived, waiting for the extension to (re)connect
QUERY_TIMEOUT = 600.0      # whole answer, Grok "Think" mode can take minutes
NEW_CHAT_TIMEOUT = 35.0
NO_STREAM_TIMEOUT = 45.0   # CDP: submit -> first sign of the answer stream

# Grok web URL (overridable for tests with a local mock page)
GROK_URL = os.environ.get("GROKX_GROK_URL", "https://x.com/i/grok")

# Project root (editable install: src/grokx_cli/config.py -> project)
PROJECT_DIR = Path(__file__).resolve().parents[2]
EXTENSION_DIR = PROJECT_DIR / "extension"
HOOK_JS = EXTENSION_DIR / "injected.js"
