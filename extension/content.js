/**
 * GrokX content script (isolated world) on x.com/i/grok.
 *
 * Talks to the extension service worker (background.js) over a runtime port.
 * The service worker owns the WebSocket to the local grokx daemon, so this
 * page never opens a localhost socket itself (x.com CSP and Chrome's Local
 * Network Access rules would get in the way).
 *
 * Answers are taken from the network stream reported by injected.js, not from
 * the DOM: x.com markup and button labels change and depend on UI language.
 */
(function () {
  if (window.__GROKX_CONTENT__) return;
  window.__GROKX_CONTENT__ = true;

  const NO_STREAM_TIMEOUT_MS = 45000;   // submit -> first sign of the answer stream
  const RESUBMIT_AFTER_MS = 1500;        // Enter did nothing -> try the send button
  const GROK_HOME = location.origin + '/i/grok';

  let port = null;
  let q = null;        // active query: {id, streamId, full, timers: []}
  let bridgeState = 'disconnected';
  let isActiveTab = false;

  // ---------- badge ----------
  const badge = document.createElement('div');
  badge.id = 'grokx-bridge-badge';
  const dot = document.createElement('span');
  dot.className = 'grokx-dot grokx-dot-disconnected';
  const label = document.createElement('span');
  label.textContent = 'GrokX: connecting...';
  badge.append(dot, label);
  badge.title = 'GrokX terminal bridge. Click to make this tab the active one.';
  badge.addEventListener('click', () => send({ type: 'focus', takeover: true }));

  function mountBadge() {
    if (!document.body || !attached) return;
    if (!badge.isConnected) document.body.appendChild(badge);
  }

  function renderBadge() {
    mountBadge();
    let cls = 'grokx-dot-disconnected';
    let text;
    if (q) {
      cls = 'grokx-dot-busy';
      text = q.streamId ? 'GrokX: streaming...' : 'GrokX: sending...';
    } else if (bridgeState === 'connected') {
      cls = isActiveTab ? 'grokx-dot-connected' : 'grokx-dot-idle';
      text = isActiveTab ? 'GrokX: ready' : 'GrokX: standby (other tab active)';
    } else if (bridgeState === 'replaced') {
      text = 'GrokX: another browser took over, click to reclaim';
    } else {
      text = 'GrokX: waiting for grokx daemon';
    }
    dot.className = 'grokx-dot ' + cls;
    label.textContent = text;
  }

  // ---------- port to service worker ----------
  function send(msg) {
    try {
      if (port) port.postMessage(msg);
    } catch (_) {}
  }

  function connectPort() {
    if (!chrome.runtime || !chrome.runtime.id) return; // extension reloaded/removed
    try {
      port = chrome.runtime.connect({ name: 'grokx-tab' });
    } catch (_) {
      port = null;
      return;
    }
    port.onMessage.addListener(onWorkerMessage);
    port.onDisconnect.addListener(() => {
      port = null;
      bridgeState = 'disconnected';
      renderBadge();
      setTimeout(connectPort, 1000);
    });
    if (document.visibilityState === 'visible') send({ type: 'focus' });
  }

  function onWorkerMessage(msg) {
    if (!msg) return;
    if (msg.type === 'status') {
      bridgeState = msg.bridge;
      isActiveTab = !!msg.active;
      renderBadge();
    } else if (msg.action === 'query') {
      startQuery(msg.query_id, msg.text || '');
    } else if (msg.action === 'new_chat') {
      newChat(msg.req_id);
    } else if (msg.action === 'stop') {
      stopGenerating();
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') send({ type: 'focus' });
  });
  window.addEventListener('focus', () => send({ type: 'focus' }));

  // ---------- DOM helpers ----------
  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 12) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  }

  function findInput() {
    const selectors = [
      'textarea[placeholder]',
      'textarea',
      'div[contenteditable="true"][role="textbox"]',
      'div[contenteditable="true"]'
    ];
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (!el.closest('#grokx-bridge-badge') && visible(el) && !el.disabled && !el.readOnly) return el;
      }
    }
    return null;
  }

  function inputValue(el) {
    return (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? el.value : (el.innerText || '');
  }

  function setInputText(el, text) {
    el.focus();
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
      el.select();
    } else {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    let ok = false;
    try { ok = document.execCommand('insertText', false, text); } catch (_) {}
    // Fallback only if nothing landed: rich editors normalise whitespace, so comparing the
    // value with the prompt gives false negatives, and a second insert doubled the prompt (seen live).
    if (!ok || !inputValue(el).trim()) {
      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);
      } else {
        el.textContent = text;
      }
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    }
  }

  function pressEnter(el) {
    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent('keydown', opts));
    el.dispatchEvent(new KeyboardEvent('keypress', opts));
    el.dispatchEvent(new KeyboardEvent('keyup', opts));
  }

  // No clicking of page buttons to submit: the Grok page also has upgrade / billing buttons,
  // and a loose "find the send button" search is not worth that risk. Enter only.

  const STOP_LABEL_RE = /^(stop|stop generating|остановить|зупинити|zatrzymaj|detener)\b/i;

  function stopGenerating() {
    // Only a button labelled Stop that sits in the composer next to the input.
    const input = findInput();
    let node = input ? input.parentElement : null;
    for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
      const btn = Array.from(node.querySelectorAll('button')).find((b) =>
        STOP_LABEL_RE.test((b.getAttribute('aria-label') || '').trim()) && visible(b));
      if (btn) { btn.click(); break; }
    }
    if (q) finishQuery(q.full, true);
  }

  // ---------- query lifecycle ----------
  function clearTimers() {
    if (!q) return;
    for (const t of q.timers) clearTimeout(t);
    q.timers = [];
  }

  function failQuery(error) {
    if (!q) return;
    clearTimers();
    send({ type: 'error', query_id: q.id, error: error });
    q = null;
    renderBadge();
  }

  function finishQuery(full, stopped) {
    if (!q) return;
    clearTimers();
    send({ type: 'done', query_id: q.id, full: full || '', stopped: !!stopped });
    q = null;
    renderBadge();
  }

  const INPUT_WAIT_MS = 20000;   // fresh page (after /new or auto-open): x.com renders the box late

  function startQuery(id, text) {
    if (q) failQuery('Interrupted by a newer query.');
    q = { id: id, streamId: null, full: '', timers: [] };
    renderBadge();
    const deadline = Date.now() + INPUT_WAIT_MS;
    (function waitInput() {
      if (!q || q.id !== id) return;
      const input = findInput();
      if (input) return submitQuery(id, text, input);
      if (Date.now() > deadline) {
        return failQuery('Grok input box not found on this page. Are you logged in to X and is x.com/i/grok fully loaded?');
      }
      q.timers.push(setTimeout(waitInput, 300));
    })();
  }

  const PAGE_SETTLE_MS = 3500;   // a just-loaded page shows the box before it can send (seen live)
  const SUBMIT_TRIES = 4;

  function clearInput(input) {
    try {
      input.focus();
      if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') input.select();
      else {
        const range = document.createRange();
        range.selectNodeContents(input);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
      document.execCommand('delete', false, null);
    } catch (_) {}
  }

  function submitQuery(id, text, input) {
    const since = Date.now() - 500;
    let tries = 0;

    function attempt() {
      if (!q || q.id !== id || q.streamId) return;
      if (tries > 0 && !inputValue(input).trim()) return;   // the last Enter went through
      if (tries >= SUBMIT_TRIES) return;
      tries++;
      // (Re)type every time: if the editor was not hydrated yet, its state may lack the text.
      setInputText(input, text);
      q.timers.push(setTimeout(() => {
        if (!q || q.id !== id) return;
        pressEnter(input);
        q.timers.push(setTimeout(attempt, RESUBMIT_AFTER_MS));
      }, 150));
    }

    const wait = Math.max(0, PAGE_SETTLE_MS - performance.now());
    q.timers.push(setTimeout(attempt, wait));

    q.timers.push(setTimeout(() => {
      if (!q || q.id !== id || q.streamId) return;
      const stillTyped = inputValue(input).trim().length > 0;
      if (stillTyped) clearInput(input);   // never leave our prompt behind for the next query
      failQuery((stillTyped
        ? 'Prompt was typed but Enter did not submit it (' + tries + ' tries).'
        : 'Prompt was submitted but no Grok answer stream appeared within 45 s.') + netReport(since));
    }, NO_STREAM_TIMEOUT_MS + wait));
  }

  const netLog = [];   // recent POSTs / sockets of the page, for diagnostics

  const wsLog = [];    // WebSocket frames: first ones kept, then a rolling tail

  function netReport(since) {
    const items = netLog.filter((n) => n.t >= since);
    let out = items.length
      ? ' Requests seen after submit:\n' + items.map((n) =>
        `  ${n.kind} ${n.method} ${n.status} ${n.url} [${n.ctype}]` +
        (n.sample ? '\n      ' + n.sample.replace(/\s+/g, ' ').slice(0, 300) : '')).join('\n')
      : ' No POST requests or sockets were seen after submitting.';
    const frames = wsLog.filter((f) => f.t >= since);
    if (frames.length) {
      const pick = frames.length > 40 ? frames.slice(0, 25).concat(frames.slice(-15)) : frames;
      out += `\n WebSocket frames after submit (${frames.length} total):\n` + pick.map((f) =>
        `  ${f.dir} ${f.kind} ${f.len}B ${f.url}\n      text: ${f.text}` + (f.b64 ? `\n      b64: ${f.b64}` : '')).join('\n');
    }
    return out;
  }

  window.addEventListener('__grokx_evt', (e) => {
    let evt;
    try { evt = JSON.parse(e.detail); } catch (_) { return; }
    if (evt && evt.type === 'GROKX_WSMSG') {
      wsLog.push(evt);
      if (wsLog.length > 400) wsLog.splice(25, 1);   // keep the first 25, roll the rest
      return;
    }
    if (evt && evt.type === 'GROKX_NET') {
      netLog.push(evt);
      if (netLog.length > 40) netLog.shift();
      return;
    }
    if (!q || !evt) return;

    if (evt.type === 'GROKX_STREAM_START') {
      if (!q.streamId) {
        q.streamId = evt.id;
        renderBadge();
      }
    } else if (evt.type === 'GROKX_STREAM_CHUNK') {
      if (evt.id === q.streamId && evt.delta) {
        q.full += evt.delta;
        send({ type: 'chunk', query_id: q.id, delta: evt.delta, full: q.full });
      }
    } else if (evt.type === 'GROKX_STREAM_DONE') {
      if (evt.id !== q.streamId) return;
      const full = evt.full || q.full;
      if (!full) {
        failQuery('Grok stream ended without answer text.' + (evt.error ? ' ' + evt.error : '') +
                  ' First lines: ' + JSON.stringify(evt.sample || []));
      } else if (!q.full && full) {
        // Strict token filter matched nothing during streaming: deliver the fallback text at once.
        send({ type: 'chunk', query_id: q.id, delta: full, full: full });
        finishQuery(full);
      } else {
        finishQuery(full);
      }
    } else if (evt.type === 'GROKX_STREAM_ERROR') {
      if (!q.streamId) failQuery('Grok returned HTTP ' + evt.status + ': ' + (evt.body || '').slice(0, 300));
    }
  });

  function newChat(reqId) {
    if (q) failQuery('Interrupted by /new.');
    // Tell the worker first: this tab will reload and reconnect with a fresh page.
    send({ type: 'new_chat_started', req_id: reqId });
    setTimeout(() => location.assign(GROK_HOME), 50);
  }

  // ---------- boot ----------
  // x.com is a single-page app: the user can walk into /i/grok without a page load,
  // so the script runs on all of x.com and attaches only while on the Grok page.
  function onGrokPage() {
    return /^\/i\/grok(\/|$)/.test(location.pathname);
  }

  let attached = false;
  function syncAttach() {
    const want = onGrokPage();
    if (want && attached && !badge.isConnected) {
      renderBadge();   // x.com re-rendered the body and dropped the badge
    }
    if (want && !attached) {
      attached = true;
      renderBadge();
      connectPort();
    } else if (!want && attached) {
      attached = false;
      if (q) failQuery('Navigated away from x.com/i/grok.');
      const p = port;
      port = null;
      try { if (p) p.disconnect(); } catch (_) {}
      badge.remove();
    }
  }

  // Reconnect after a worker restart only while still on the Grok page.
  const _connectPort = connectPort;
  connectPort = function () { if (attached) _connectPort(); };

  // Back/forward cache: the worker port was closed while the page was cached.
  window.addEventListener('pageshow', (e) => {
    if (e.persisted && attached && !port) connectPort();
  });

  syncAttach();
  setInterval(syncAttach, 1000);
})();
