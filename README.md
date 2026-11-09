# Free Grok from X: terminal CLI and MCP server

Use the Grok that comes with your X account from the terminal and from AI agents
(Claude Code, Antigravity, any MCP client). No API key and no paid xAI API: `grokx` drives
the Grok page in **your own logged-in browser**, so you get exactly what x.com/i/grok gives you,
with the same limits.

```text
$ grokx "What is the capital of Poland? One sentence."
The capital of Poland is Warsaw.

$ Get-Content main.py | grokx "Review the error handling"
$ grokx -n "Start a fresh chat and list 3 ideas"
```

[Русская версия](README.ru.md)

## How it works

Two channels; the CLI picks whichever works and fails over to the other:

| Channel | Needs | Good for |
| :--- | :--- | :--- |
| **1. Chrome extension** (main) | The extension from `extension/` in your normal Chrome, logged in to X | Your everyday browser |
| **2. Separate Chrome over CDP** (fallback) | `grokx --launch`, log in to X once in that window | No extension; own profile |

```
grokx (CLI / MCP) ──ws://127.0.0.1:29191──> grokx daemon ──> extension service worker ──> x.com/i/grok tab
                  └──CDP 127.0.0.1:9333──> separate Chrome (~/.grokx/chrome-profile) ──> x.com/i/grok tab
```

The answer is read from Grok's network stream, not from the page markup, so layout changes and
UI language do not matter. Grok on X (the "AI agent" version, November 2026) streams over a
WebSocket to `wss://grok.com/ws/mgw/`: `response.chunk` events carry the answer text on
`CHANNEL_ASSISTANT_RESPONSE` (service channels such as "Thinking about your request" are skipped),
and `response.output_text.done` ends it. The older HTTP stream (`add_response.json`) is supported
too. One hook (`extension/injected.js`) serves both channels.

If X changes the protocol again, the "no Grok answer stream" error lists the page's requests and
WebSocket frames after the prompt was sent, which shows what to adjust.

Safety by design:

- The prompt is typed into the input box and sent with **Enter only**. grokx never clicks page
  buttons (the Grok page also has billing and upgrade buttons).
- The local daemon accepts only local programs and browser extensions; web pages are rejected,
  so no website can read your prompts or drive your Grok session.
- The CDP channel only uses its own Chrome on port 9333, never your debug Chrome on 9222.

## Install (Windows)

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Chrome.

1. `git clone https://github.com/volobado/free-grok-from-x-mcp-server` and run `setup.bat`
   (dependencies, `grokx` / `grokx-cli` / `grokx-mcp` commands in `~/.local/bin`, self-check).
2. Chrome: `chrome://extensions` → Developer mode → Load unpacked → the `extension` folder.
3. Be logged in to X in that Chrome. Opening https://x.com/i/grok is optional: the extension
   opens a background tab by itself on the first question. A **GrokX: ready** badge shows in the
   bottom right corner of the Grok page. If Chrome asks about access to devices on the local
   network, allow it.

While the daemon is not running, the extension's error list collects `ERR_CONNECTION_REFUSED`
entries. They are harmless: the extension simply keeps knocking.

## Usage

```powershell
grokx                                   # interactive mode
grokx "Explain transformers simply"
grokx -n "Question in a new chat"
Get-Content main.py | grokx "Check the error handling"
grokx "Summary" > answer.md             # piped/redirected output is plain text
grokx --cdp "..."                       # separate Chrome only
grokx --bridge "..."                    # extension only
grokx --launch                          # start the separate Chrome (log in to X there once)
grokx --status                          # channel status
grokx doctor                            # full self-check
grokx daemon start|stop|restart|status|logs
grokx reload-extension                  # reload the extension from disk after edits
```

Interactive mode: `/new`, `/copy`, `/status`, `/doctor`, `/clear`, `/help`, `/exit`.
Enter sends, Alt+Enter or a trailing `\` adds a line, Ctrl+C stops the answer (in Grok too),
Ctrl+D exits. The CLI's own messages are in Russian.

## MCP

```powershell
claude mcp add -s user grokx -- <path-to-repo>\.venv\Scripts\python.exe -m grokx_cli.mcp_server
```

Tools: `grok_query(prompt, new_chat=False)`, `grok_new_chat()`, `grok_status()`.

## Files

- `~/.grokx/grokx.log` daemon log, `history.txt` input history, `chrome-profile/` the CDP Chrome profile.
- Environment: `GROKX_PORT` (29191, the extension uses the same), `GROKX_CDP_PORT` (9333), `GROKX_HOME`.

## Tests

```powershell
.venv\Scripts\python.exe tests\test_bridge.py   # daemon protocol
.venv\Scripts\python.exe tests\test_e2e.py      # real Chromium + extension, and real Chrome over CDP, on a mock Grok page
```

`test_e2e.py` runs a copy of the extension on its own port, so a running daemon and browser are not
disturbed. The mock replays the protocol captured from live x.com, including the page ignoring Enter
for the first second after load. Verified live as well: answers, new chat, follow-up context, pipe, MCP.

## Limitations and disclaimer

- You need to be logged in to X; Grok's limits for your account apply as in the browser.
- Images and attachments in answers come back as text only.
- A daemon started from an MCP host exits together with it and restarts on the next question.
- This is an unofficial personal tool, not affiliated with X or xAI. It automates the web page you
  are logged in to, which X's terms may not allow; use it at your own risk and in moderation.
  A protocol change on X's side can break it at any time.

## License

MIT
