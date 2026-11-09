/**
 * GrokX service worker.
 *
 * Keeps one WebSocket to the local grokx daemon (ws://127.0.0.1:29191) for as long as
 * Chrome runs, and routes daemon requests to one x.com/i/grok tab (the last focused one).
 * If no Grok tab is open when a request arrives, it opens one in the background.
 *
 * While the daemon is not running, connection attempts back off to once per
 * RECONNECT_MAX_MS; Chrome logs each refused attempt as ERR_CONNECTION_REFUSED, harmless.
 */
const WS_URL = 'ws://127.0.0.1:29191';
const GROK_URL = 'https://x.com/i/grok';
const RECONNECT_MIN_MS = 2000;
const RECONNECT_MAX_MS = 10000;
const KEEPALIVE_MS = 20000;        // WebSocket traffic keeps an MV3 worker alive
const TAB_WAIT_MS = 30000;         // request arrived: wait this long for a ready Grok tab
const OPEN_TAB_AFTER_MS = 1500;    // ...and open one ourselves if none shows up by then
const RESET_RELOAD_MS = 25000;     // /new: wait for the tab to come back

const tabs = new Map();            // tabId -> {port, lastFocus}
const queryTab = new Map();        // query_id -> tabId
const pendingReset = new Map();    // tabId -> {req_id, timer}
let activeTabId = null;
let ws = null;
let bridge = 'disconnected';       // connected | disconnected | replaced
let reconnectTimer = null;
let reconnectDelay = RECONNECT_MIN_MS;

function log(...a) { console.log('[GrokX]', ...a); }

// ---------------- WebSocket ----------------
function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  return false;
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(ensureWs, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

function ensureWs() {
  if (bridge === 'replaced') return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(reconnectTimer);
  let sock;
  try {
    sock = new WebSocket(WS_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws = sock;
  sock.onopen = () => {
    bridge = 'connected';
    reconnectDelay = RECONNECT_MIN_MS;
    sock.send(JSON.stringify({ role: 'browser', info: 'GrokX Chrome extension ' + chrome.runtime.getManifest().version }));
    log('connected to daemon');
    pushStatus();
  };
  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    onDaemonMessage(msg);
  };
  sock.onclose = (ev) => {
    if (ws !== sock) return;
    ws = null;
    // 4001 = another browser connection replaced us; reconnect only when a tab asks.
    bridge = ev.code === 4001 ? 'replaced' : 'disconnected';
    failAllQueries('Browser bridge lost connection to the daemon.');
    pushStatus();
    if (bridge !== 'replaced') scheduleReconnect();
  };
  sock.onerror = () => {};
}

setInterval(() => wsSend({ type: 'ping' }), KEEPALIVE_MS);

// The worker is stopped by Chrome when idle (e.g. while the daemon is down);
// the alarm wakes it up so it can try again.
chrome.alarms.create('grokx-reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => ensureWs());
chrome.runtime.onStartup.addListener(() => ensureWs());

// ---------------- tabs ----------------
function pickActiveTab() {
  let best = null;
  for (const [id, t] of tabs) {
    if (best === null || t.lastFocus > tabs.get(best).lastFocus) best = id;
  }
  activeTabId = best;
}

function pushStatus() {
  for (const [id, t] of tabs) {
    try { t.port.postMessage({ type: 'status', bridge: bridge, active: id === activeTabId }); } catch (_) {}
  }
}

function failAllQueries(error) {
  for (const qid of queryTab.keys()) wsSend({ type: 'error', query_id: qid, error: error });
  queryTab.clear();
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'grokx-tab' || !port.sender || !port.sender.tab) return;
  const tabId = port.sender.tab.id;
  tabs.set(tabId, { port: port, lastFocus: port.sender.tab.active ? Date.now() : 0 });
  if (activeTabId === null || port.sender.tab.active) activeTabId = tabId;

  port.onMessage.addListener((msg) => onTabMessage(tabId, msg));
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;   // e.g. "page moved into back/forward cache"
    const t = tabs.get(tabId);
    if (t && t.port === port) tabs.delete(tabId);
    if (!pendingReset.has(tabId)) {
      for (const [qid, tid] of queryTab) {
        if (tid === tabId) {
          wsSend({ type: 'error', query_id: qid, error: 'Grok tab was closed or navigated away during the answer.' });
          queryTab.delete(qid);
        }
      }
    }
    if (activeTabId === tabId && !pendingReset.has(tabId)) pickActiveTab();
    pushStatus();
  });

  // A tab that reloaded for /new is back: the reset is complete.
  const pr = pendingReset.get(tabId);
  if (pr) {
    clearTimeout(pr.timer);
    pendingReset.delete(tabId);
    activeTabId = tabId;
    wsSend({ type: 'new_chat_done', req_id: pr.req_id, ok: true });
  }

  ensureWs();
  pushStatus();
});

function onTabMessage(tabId, msg) {
  if (!msg) return;
  const t = tabs.get(tabId);
  if (msg.type === 'focus') {
    if (t) t.lastFocus = Date.now();
    activeTabId = tabId;
    if (msg.takeover && bridge === 'replaced') bridge = 'disconnected';
    ensureWs();
    pushStatus();
  } else if (msg.type === 'chunk') {
    wsSend(msg);
  } else if (msg.type === 'done' || msg.type === 'error') {
    queryTab.delete(msg.query_id);
    wsSend(msg);
  } else if (msg.type === 'new_chat_started') {
    const timer = setTimeout(() => {
      pendingReset.delete(tabId);
      wsSend({ type: 'new_chat_done', req_id: msg.req_id, ok: false, error: 'Grok tab did not come back after reload.' });
    }, RESET_RELOAD_MS);
    pendingReset.set(tabId, { req_id: msg.req_id, timer: timer });
  }
}

// After install / update / reload, already open Grok tabs have no (or orphaned) content
// scripts: reload them so the user never has to press F5.
chrome.runtime.onInstalled.addListener(async () => {
  try {
    const open = await chrome.tabs.query({ url: ['https://x.com/i/grok*', 'https://twitter.com/i/grok*'] });
    for (const t of open) chrome.tabs.reload(t.id);
  } catch (e) {
    log('could not reload Grok tabs', e);
  }
});

// ---------------- daemon -> tab ----------------
function readyTab() {
  if (activeTabId === null || !tabs.has(activeTabId)) pickActiveTab();
  return activeTabId !== null && tabs.has(activeTabId) && !pendingReset.has(activeTabId) ? activeTabId : null;
}

function waitForTab(timeoutMs) {
  return new Promise((resolve) => {
    const start = Date.now();
    let opened = false;
    (function poll() {
      const id = readyTab();
      if (id !== null) return resolve(id);
      if (!opened && pendingReset.size === 0 && Date.now() - start > OPEN_TAB_AFTER_MS) {
        opened = true;
        log('no Grok tab, opening one in the background');
        chrome.tabs.create({ url: GROK_URL, active: false }).catch(() => {});
      }
      if (Date.now() - start > timeoutMs) return resolve(null);
      setTimeout(poll, 250);
    })();
  });
}

async function onDaemonMessage(msg) {
  if (msg.action === 'query') {
    const tabId = await waitForTab(TAB_WAIT_MS);
    if (tabId === null) {
      wsSend({ type: 'error', query_id: msg.query_id,
               error: 'No x.com/i/grok tab became ready (opened one, but it did not load; logged in to X?).' });
      return;
    }
    queryTab.set(msg.query_id, tabId);
    tabs.get(tabId).port.postMessage({ action: 'query', query_id: msg.query_id, text: msg.text });
  } else if (msg.action === 'new_chat') {
    const tabId = await waitForTab(TAB_WAIT_MS);
    if (tabId === null) {
      wsSend({ type: 'new_chat_done', req_id: msg.req_id, ok: false, error: 'No x.com/i/grok tab became ready.' });
      return;
    }
    tabs.get(tabId).port.postMessage({ action: 'new_chat', req_id: msg.req_id });
  } else if (msg.action === 'reload_extension') {
    // Picks up edited files from disk; onInstalled then reloads the Grok tabs.
    wsSend({ type: 'reload_ack' });
    setTimeout(() => chrome.runtime.reload(), 200);
  } else if (msg.action === 'stop') {
    for (const tabId of new Set(queryTab.values())) {
      const t = tabs.get(tabId);
      if (t) t.port.postMessage({ action: 'stop' });
    }
  }
}

ensureWs();
