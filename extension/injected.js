/**
 * GrokX network hook (runs in the page MAIN world).
 *
 * Shared by both tiers:
 *   - the Chrome extension injects it at document_start (manifest "world": "MAIN");
 *   - the CDP tier injects the same file via Playwright add_init_script.
 *
 * Grok on x.com streams answers from .../2/grok/add_response.json as NDJSON:
 *   {"conversationId": "..."}
 *   {"result": {"sender": "ASSISTANT", "message": "tok", "isThinking": false, ...}}
 * We read a clone of that response (the page keeps the original untouched),
 * pick the answer tokens and report them as "__grokx_evt" CustomEvents
 * (detail = JSON string, so it crosses into the extension's isolated world)
 * and through window.__grokxEmit when the CDP tier exposed that binding.
 */
(function () {
  if (window.__GROKX_HOOK__) return;
  window.__GROKX_HOOK__ = true;

  const GROK_URL_RE = /grok/i;              // which requests are worth reading at all
  const ANSWER_URL_RE = /add_response/i;   // the answer stream endpoint (HTTP errors reported)
  const SAMPLE_LINES = 6;
  let seq = 0;

  function emit(evt) {
    try {
      window.dispatchEvent(new CustomEvent('__grokx_evt', { detail: JSON.stringify(evt) }));
    } catch (_) {}
    try {
      if (typeof window.__grokxEmit === 'function') window.__grokxEmit(evt);
    } catch (_) {}
  }

  function urlOf(input) {
    try {
      if (typeof input === 'string') return input;
      if (input instanceof URL) return input.href;
      if (input && typeof input.url === 'string') return input.url;
    } catch (_) {}
    return '';
  }

  /** Incremental NDJSON / SSE parser for one response. */
  function makeParser(url) {
    const id = 'g' + (++seq) + '_' + Date.now();
    let started = false;
    let strict = '';   // tokens that look like the visible answer
    let loose = '';    // any non-thinking "message" text (fallback if the format shifted)
    let buf = '';
    let raw = '';
    const sample = [];
    let finished = false;

    function start() {
      if (started) return;
      started = true;
      emit({ type: 'GROKX_STREAM_START', id: id, url: url });
    }

    function handleObj(obj) {
      if (!obj || typeof obj !== 'object') return;
      const r = (obj.result && typeof obj.result === 'object') ? obj.result : obj;
      if (typeof r.message !== 'string') {
        if (obj.conversationId || r.conversationId || obj.userChatItemId || r.userChatItemId) start();
        return;
      }
      start();
      if (r.isThinking) return;
      const msg = r.message;
      if (!msg) return;
      loose += msg;
      const sender = String(r.sender || '').toUpperCase();
      const tag = r.messageTag;
      if ((!sender || sender === 'ASSISTANT') && (!tag || tag === 'final')) {
        strict += msg;
        emit({ type: 'GROKX_STREAM_CHUNK', id: id, delta: msg });
      }
    }

    function handleLine(line) {
      let t = line.trim();
      if (!t) return;
      if (sample.length < SAMPLE_LINES) sample.push(t.slice(0, 300));
      if (t.startsWith('data:')) t = t.slice(5).trim();
      if (!t || t === '[DONE]') return;
      if (t[0] !== '{') return;
      try { handleObj(JSON.parse(t)); } catch (_) {}
    }

    return {
      feed(text) {
        if (!text || finished) return;
        if (raw.length < 400000) raw += text;
        buf += text;
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const l of lines) handleLine(l);
      },
      end(error) {
        if (finished) return;
        finished = true;
        if (buf) { handleLine(buf); buf = ''; }
        // Whole body may be one pretty-printed JSON object instead of NDJSON.
        if (!started && raw.trim().startsWith('{')) {
          try { handleObj(JSON.parse(raw)); } catch (_) {}
        }
        if (!started) return; // not a Grok answer stream, stay silent
        emit({
          type: 'GROKX_STREAM_DONE',
          id: id,
          full: strict || loose,
          strict: !!strict,
          sample: sample,
          error: error ? String(error) : ''
        });
      }
    };
  }

  function reportHttpError(url, status, bodyText) {
    emit({
      type: 'GROKX_STREAM_ERROR',
      url: url,
      status: status,
      body: String(bodyText || '').slice(0, 500)
    });
  }

  async function consumeBody(body, url) {
    const parser = makeParser(url);
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8');
    let err = '';
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        parser.feed(decoder.decode(value, { stream: true }));
      }
      parser.feed(decoder.decode());
    } catch (e) {
      err = e && e.message ? e.message : String(e);
    }
    parser.end(err);
  }

  // ---- diagnostics: every POST / socket the page makes, with the start of the answer ----
  // Lets the content script explain "no answer stream" with real data instead of guesses.
  function netEvent(kind, method, url, status, ctype, sample) {
    emit({
      type: 'GROKX_NET', kind: kind, method: method, url: String(url).slice(0, 220),
      status: status, ctype: ctype || '', sample: sample || '', t: Date.now()
    });
  }

  async function sampleBody(res, max) {
    try {
      const reader = res.clone().body.getReader();
      const dec = new TextDecoder('utf-8');
      let out = '';
      const deadline = Date.now() + 8000;
      while (out.length < max && Date.now() < deadline) {
        const { value, done } = await reader.read();
        if (done) break;
        out += dec.decode(value, { stream: true });
      }
      reader.cancel().catch(() => {});
      return out.slice(0, max);
    } catch (e) {
      return '<unreadable: ' + (e && e.message) + '>';
    }
  }

  // WebSocket frames, for diagnostics: text as is, binary as printable text + base64 head.
  function b64(bytes) {
    let s = '';
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  async function frameInfo(data) {
    try {
      if (typeof data === 'string') return { kind: 'text', len: data.length, text: data.slice(0, 600) };
      let buf = null;
      if (data instanceof ArrayBuffer) buf = new Uint8Array(data);
      else if (ArrayBuffer.isView(data)) buf = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      else if (data instanceof Blob) buf = new Uint8Array(await data.arrayBuffer());
      if (!buf) return { kind: typeof data, len: 0, text: '' };
      const text = new TextDecoder('utf-8').decode(buf).replace(/[\x00-\x08\x0b-\x1f�]+/g, '·');
      return { kind: 'binary', len: buf.length, text: text.slice(0, 600), b64: b64(buf.slice(0, 150)) };
    } catch (e) {
      return { kind: 'err', len: 0, text: String(e) };
    }
  }

  function wsFrame(dir, url, data) {
    frameInfo(data).then((f) => emit(Object.assign({ type: 'GROKX_WSMSG', dir: dir, url: String(url).slice(0, 120), t: Date.now() }, f)));
  }

  // ---- Grok "AI agent" protocol over wss://grok.com/ws/... (seen live on x.com, Nov 2026) ----
  //   in  {"event":{"type":"response.created","response":{"id":R}}}
  //   in  {"event":{"type":"response.chunk","response_id":R,"chunk":{"text":{"text":"tok","channel":"CHANNEL_ASSISTANT_RESPONSE"}}}}
  //       (other channels: NOTETAKER_HEADER "Thinking about your request", thinking, phase markers)
  //   in  {"event":{"type":"response.output_text.done","response_id":R,...}}
  // "conversation.live.updated streaming:false" arrives BEFORE the last chunks, so it is not an end marker.
  // Ending: "output_text.done" also fires for intermediate agent steps (web / X search) BEFORE the
  // answer exists (seen live), so it only ends the response once answer text has arrived.
  const WS_DONE_GRACE_MS = 2500;      // after output_text.done with text: wait for more steps
  const WS_IDLE_DONE_MS = 180000;     // no event at all for this response this long -> finish
  const wsResponses = new Map();      // response_id -> {strict, loose, timer, doneSeen, trace}

  function wsResp(id) {
    let r = wsResponses.get(id);
    if (!r) {
      r = { id: 'ws_' + id, strict: '', loose: '', timer: null, finished: false, doneSeen: false, trace: [] };
      wsResponses.set(id, r);
      emit({ type: 'GROKX_STREAM_START', id: r.id, url: 'websocket' });
    }
    return r;
  }

  function wsFinish(rid, error) {
    const r = wsResponses.get(rid);
    if (!r || r.finished) return;
    r.finished = true;
    clearTimeout(r.timer);
    emit({ type: 'GROKX_STREAM_DONE', id: r.id, full: r.strict || r.loose, strict: !!r.strict,
           sample: r.trace, error: error || '' });
    setTimeout(() => wsResponses.delete(rid), 60000);
  }

  // Re-arm the end timer after every event of this response.
  function wsArm(rid) {
    const r = wsResponses.get(rid);
    if (!r || r.finished) return;
    clearTimeout(r.timer);
    const ms = (r.doneSeen && (r.strict || r.loose)) ? WS_DONE_GRACE_MS : WS_IDLE_DONE_MS;
    r.timer = setTimeout(() => wsFinish(rid), ms);
  }

  function traceEvent(r, ev) {
    if (r.trace.length >= 60) return;
    let d = ev.type;
    const c = ev.chunk;
    if (c) {
      const keys = Object.keys(c).filter((k) => k !== 'metadata');
      d += ' ' + keys.join(',');
      if (c.text) d += ' [' + (c.text.channel || '') + '] ' + String(c.text.text || '').slice(0, 60);
    }
    if (ev.item && ev.item.type) d += ' item=' + ev.item.type + (ev.item.role ? '/' + ev.item.role : '');
    if (!/^response\.(chunk|created|content_part\.added|output_item\.added)$/.test(ev.type)) {
      const rest = Object.assign({}, ev);
      delete rest.type; delete rest.event_id; delete rest.response_id; delete rest.item_id;
      d += ' ' + JSON.stringify(rest).slice(0, 700);
    }
    r.trace.push(d);
  }

  function onGrokWsText(data) {
    if (typeof data !== 'string' || data.indexOf('"response') < 0) return;
    let ev;
    try { ev = JSON.parse(data).event; } catch (_) { return; }
    if (!ev || typeof ev.type !== 'string') return;
    const t = ev.type;
    const rid = ev.response_id || (ev.response && ev.response.id);
    if (!rid) return;

    const r = wsResp(rid);
    if (r.finished) return;
    traceEvent(r, ev);

    if (t === 'response.chunk') {
      const tx = ev.chunk && ev.chunk.text;
      if (tx && typeof tx.text === 'string' && tx.text) {
        const ch = String(tx.channel || '');
        if (ch === 'CHANNEL_ASSISTANT_RESPONSE') {
          r.strict += tx.text;
          emit({ type: 'GROKX_STREAM_CHUNK', id: r.id, delta: tx.text });
        } else if (!/HEADER|THINK|REASON|NOTETAKER|TOOL|SEARCH/i.test(ch)) {
          r.loose += tx.text;
        }
      }
    } else if (t === 'response.output_text.delta' && typeof ev.delta === 'string') {
      r.strict += ev.delta;
      emit({ type: 'GROKX_STREAM_CHUNK', id: r.id, delta: ev.delta });
    } else if (t === 'response.grok.output' && ev.output && ev.output.stream_error) {
      // e.g. {"kind":"usage_limit_reached","message":"You've reached your usage limit..."} (seen live)
      const se = ev.output.stream_error;
      r.streamError = 'GROK_ERROR ' + (se.kind || 'error') + ': ' + (se.message || '');
    } else if (t === 'response.output_text.done' || t === 'response.content_part.done') {
      r.doneSeen = true;
    } else if (t === 'response.done' || t === 'response.completed') {
      const resp = ev.response || {};
      if (!r.strict && !r.loose && !r.streamError && resp.status && resp.status !== 'completed') {
        r.streamError = 'GROK_ERROR ' + resp.status + ': ' + JSON.stringify(resp.status_details || {});
      }
      return wsFinish(rid, r.streamError);
    } else if (/failed|error|cancel/i.test(t)) {
      return wsFinish(rid, t + ': ' + JSON.stringify(ev.error || ev.response || {}).slice(0, 300));
    }
    wsArm(rid);
  }

  const OrigWS = window.WebSocket;
  if (typeof OrigWS === 'function') {
    const WrappedWS = function (url, protocols) {
      netEvent('websocket', 'WS', url, 0, '', '');
      const sock = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      try {
        sock.addEventListener('message', (ev) => {
          wsFrame('in', url, ev.data);
          try { onGrokWsText(ev.data); } catch (_) {}
        });
        const origSend = sock.send;
        sock.send = function (data) {
          wsFrame('out', url, data);
          return origSend.call(this, data);
        };
      } catch (_) {}
      return sock;
    };
    WrappedWS.prototype = OrigWS.prototype;
    Object.assign(WrappedWS, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
    window.WebSocket = WrappedWS;
  }

  // ---- fetch ----
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (...args) {
      const p = origFetch.apply(this, args);
      const url = urlOf(args[0]);
      let method = 'GET';
      try {
        method = String((args[1] && args[1].method) || (args[0] && args[0].method) || 'GET').toUpperCase();
      } catch (_) {}
      if (method !== 'GET') {
        p.then((res) => {
          const ctype = res.headers.get('content-type') || '';
          if (res.body) sampleBody(res, 600).then((s) => netEvent('fetch', method, url, res.status, ctype, s));
          else netEvent('fetch', method, url, res.status, ctype, '');
        }, (e) => netEvent('fetch', method, url, -1, '', String(e)));
      }
      if (!GROK_URL_RE.test(url)) return p;
      return p.then((res) => {
        try {
          if (ANSWER_URL_RE.test(url) && !res.ok) {
            res.clone().text().then((t) => reportHttpError(url, res.status, t), () => reportHttpError(url, res.status, ''));
          } else if (res.body) {
            consumeBody(res.clone().body, url);
          }
        } catch (_) {}
        return res;
      });
    };
  }

  // ---- XMLHttpRequest (in case x.com moves the stream to XHR) ----
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { this.__grokxUrl = String(url); this.__grokxMethod = String(method).toUpperCase(); } catch (_) {}
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    const url = this.__grokxUrl || '';
    if (this.__grokxMethod && this.__grokxMethod !== 'GET') {
      const xhr = this;
      xhr.addEventListener('loadend', () => {
        let s = '';
        try { s = (xhr.responseType === '' || xhr.responseType === 'text') ? xhr.responseText.slice(0, 600) : '<' + xhr.responseType + '>'; } catch (_) {}
        netEvent('xhr', xhr.__grokxMethod, url, xhr.status, xhr.getResponseHeader('content-type'), s);
      });
    }
    if (GROK_URL_RE.test(url)) {
      const xhr = this;
      const parser = makeParser(url);
      let pos = 0;
      const pump = () => {
        if (xhr.responseType !== '' && xhr.responseType !== 'text') return;
        try {
          const t = xhr.responseText || '';
          if (t.length > pos) { parser.feed(t.slice(pos)); pos = t.length; }
        } catch (_) {}
      };
      xhr.addEventListener('progress', pump);
      xhr.addEventListener('loadend', () => {
        pump();
        if (ANSWER_URL_RE.test(url) && xhr.status >= 400) {
          reportHttpError(url, xhr.status, (xhr.responseType === '' || xhr.responseType === 'text') ? xhr.responseText : '');
        } else {
          parser.end(xhr.status === 0 ? 'network error' : '');
        }
      });
    }
    return XS.apply(this, arguments);
  };
})();
