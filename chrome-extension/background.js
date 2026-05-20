// YTR Suno Bridge — service worker.
//
// Connects out to ws://127.0.0.1:18792 (the Python bridge in the pipeline).
// Captures the Suno Bearer token + apiBase from outgoing API requests by
// observing webRequest. Responds to commands from the Python side:
//
//   { id, cmd: "ping" }            -> { id, ok: true }
//   { id, cmd: "getAuth" }          -> { id, bearer, apiBase, age_ms }
//   { id, cmd: "fetch", url, method, headers?, body? }
//                                   -> { id, status, headers, body }
//
// The fetch proxy runs the request inside one of the open suno.com tabs
// (via chrome.scripting.executeScript) so cookies + service-worker handling
// + freshly-rotated JWTs attach automatically. The script never needs to
// hold a long-lived Bearer.

const BRIDGE_HOST = "127.0.0.1";
const BRIDGE_PORT = 18792;
const RECONNECT_MS = 1500;

let ws = null;
let reconnectTimer = null;

// Auth lives in chrome.storage.session — MV3 service workers are killed
// after ~30s idle and module-level state is LOST on restart, so we cannot
// keep `bearer` in a plain variable. session storage persists across SW
// restarts but is wiped when Chrome closes. That's fine: user reopens
// suno.com on next session and the listener captures a fresh Bearer.

async function saveAuth(bearerVal, apiBaseVal) {
  await chrome.storage.session.set({
    bearer: bearerVal,
    apiBase: apiBaseVal,
    bearerCapturedAt: Date.now(),
  }).catch(() => {});
}

async function loadAuth() {
  const s = await chrome.storage.session.get(
    ["bearer", "apiBase", "bearerCapturedAt"]
  ).catch(() => ({}));
  return {
    bearer:           s.bearer || null,
    apiBase:          s.apiBase || null,
    bearerCapturedAt: s.bearerCapturedAt || 0,
  };
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text }).catch(() => {});
  if (color) chrome.action.setBadgeBackgroundColor({ color }).catch(() => {});
}

// ---- Bearer capture --------------------------------------------------------

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const auth = (details.requestHeaders || []).find(
      (h) => h.name.toLowerCase() === "authorization"
    );
    if (!auth || !/^Bearer\s+/i.test(auth.value || "")) {
      console.debug("[ytr-bridge] saw suno req (no Bearer):", details.method, details.url);
      return;
    }
    const m = details.url.match(/^(https?:\/\/[^/]+\/api)\b/i);
    const ab = m ? m[1] : null;
    saveAuth(auth.value, ab);
    setBadge("✓", "#16A34A");
    console.log("[ytr-bridge] captured Bearer for", ab, "from", details.url);
  },
  {
    urls: [
      "https://*.suno.com/*",
      "https://*.suno.ai/*",
      "https://suno.com/*",
      "https://suno.ai/*",
    ],
  },
  ["requestHeaders", "extraHeaders"]
);

// === Highlight POST requests to studio-api so we can see Suno's real
// generate endpoint when the user clicks "Create" manually ===============
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.method !== "POST") return;
    let bodySample = "";
    try {
      const rb = details.requestBody;
      if (rb?.raw && rb.raw[0]?.bytes) {
        const txt = new TextDecoder().decode(rb.raw[0].bytes);
        bodySample = txt.slice(0, 400);
      } else if (rb?.formData) {
        bodySample = JSON.stringify(rb.formData).slice(0, 400);
      }
    } catch (_) {}
    console.log(
      "%c[ytr-bridge] SUNO POST %s\n  body: %s",
      "color:#7c3aed;font-weight:bold",
      details.url,
      bodySample
    );
  },
  { urls: ["https://*.suno.com/api/*", "https://*.suno.ai/api/*"] },
  ["requestBody"]
);

// ---- WebSocket bridge ------------------------------------------------------

function send(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch (err) {
    console.warn("[ytr-bridge] send failed:", err);
    return false;
  }
}

async function findSunoTab() {
  const tabs = await chrome.tabs.query({ url: ["*://*.suno.com/*", "*://suno.com/*"] });
  if (tabs.length) return tabs[0];
  return null;
}

async function findKkboxTab() {
  const tabs = await chrome.tabs.query({ url: ["*://*.kkbox.com/*", "*://kkbox.com/*"] });
  if (tabs.length) return tabs[0];
  return null;
}

function bytesToB64(buf) {
  let s = "";
  const view = new Uint8Array(buf);
  for (let i = 0; i < view.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, view.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

async function swFetch(args) {
  // Cross-origin fetch in the SW. With host_permissions and
  // credentials:"include", the user's Chrome cookies for the target host
  // attach automatically (e.g. KKBox WAF cookie after user visits kkbox.com).
  const init = { method: args.method || "GET", credentials: "include" };
  if (args.headers) init.headers = args.headers;
  if (args.body !== undefined && args.body !== null) init.body = args.body;
  const r = await fetch(args.url, init);
  const buf = await r.arrayBuffer();
  const respHeaders = {};
  r.headers.forEach((v, k) => { respHeaders[k] = v; });
  return { status: r.status, headers: respHeaders, body_b64: bytesToB64(buf) };
}

async function tabMainWorldFetch(tab, args) {
  // Run fetch from inside a real page (MAIN world). Useful when an Authorization
  // header / SPA state is needed and only the page can produce it.
  const [{ result, error }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: async (url, method, headers, body) => {
      try {
        const init = { method: method || "GET", credentials: "include" };
        if (headers) init.headers = headers;
        if (body !== undefined && body !== null) init.body = body;
        const r = await fetch(url, init);
        const buf = await r.arrayBuffer();
        let s = "";
        const view = new Uint8Array(buf);
        for (let i = 0; i < view.length; i += 0x8000) {
          s += String.fromCharCode.apply(null, view.subarray(i, i + 0x8000));
        }
        const respHeaders = {};
        r.headers.forEach((v, k) => { respHeaders[k] = v; });
        return { status: r.status, headers: respHeaders, body_b64: btoa(s) };
      } catch (e) {
        return { __error: String(e) };
      }
    },
    args: [args.url, args.method, args.headers || null, args.body ?? null],
  });
  if (error) throw new Error(String(error));
  if (result && result.__error) throw new Error(result.__error);
  return result;
}

function utf8B64(s) {
  // proper UTF-8 → base64 (string from MAIN world can contain Chinese)
  return btoa(unescape(encodeURIComponent(s || "")));
}

function waitForTabLoad(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("tab load timeout"));
    }, timeoutMs);
    const listener = (id, info) => {
      if (id !== tabId) return;
      if (info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        // Give post-load JS (e.g. WAF challenge solver) a moment to finish
        // and the DOM to settle.
        setTimeout(resolve, 2500);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// Real navigation in a background tab so WAF JS challenges actually run.
// Disposes the tab when done.
async function navigateTabFetch(url) {
  const tab = await chrome.tabs.create({ url, active: false });
  try {
    await waitForTabLoad(tab.id, 40000);
    const [{ result, error }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => ({
        html: document.documentElement.outerHTML,
        url:  location.href,
      }),
    });
    if (error) throw new Error(String(error));
    if (!result) throw new Error("executeScript returned no result");
    return {
      status: 200,
      headers: { "x-final-url": result.url || "" },
      body_b64: utf8B64(result.html),
    };
  } finally {
    try { await chrome.tabs.remove(tab.id); } catch (_) {}
  }
}

async function proxyFetch(args) {
  let host = "";
  try { host = new URL(args.url).host.toLowerCase(); } catch (_) {}

  // Route Suno through an existing suno.com tab (need the SPA's JWT in
  // the same context that issued it).
  if (host.endsWith("suno.com") || host.endsWith("suno.ai")) {
    const tab = await findSunoTab();
    if (tab) return await tabMainWorldFetch(tab, args);
  }

  // KKBox: navigation in a background tab so AWS WAF challenge JS runs.
  // fetch() from MAIN world is treated as XHR by WAF — returns 202 with
  // empty body. Real navigation gets the actual document.
  if (host.endsWith("kkbox.com") && (args.method || "GET").toUpperCase() === "GET") {
    return await navigateTabFetch(args.url);
  }

  // Default: SW fetch (cross-origin OK with host_permissions). May still
  // be limited if SameSite=Lax cookies are required.
  return await swFetch(args);
}

async function handleCommand(msg) {
  const reply = (data) => send(Object.assign({ id: msg.id }, data));
  try {
    if (msg.cmd === "ping") return reply({ ok: true });
    if (msg.cmd === "getAuth") {
      const s = await loadAuth();
      return reply({
        bearer:  s.bearer,
        apiBase: s.apiBase,
        age_ms:  s.bearer ? Date.now() - s.bearerCapturedAt : null,
      });
    }
    if (msg.cmd === "fetch") {
      const result = await proxyFetch(msg);
      return reply(result);
    }
    reply({ error: `unknown cmd: ${msg.cmd}` });
  } catch (err) {
    reply({ error: err && err.message ? err.message : String(err) });
  }
}

function connect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
    return;
  }
  try {
    ws = new WebSocket(`ws://${BRIDGE_HOST}:${BRIDGE_PORT}/`);
  } catch (err) {
    scheduleReconnect();
    return;
  }
  ws.onopen = async () => {
    console.log("[ytr-bridge] connected to bridge");
    const s = await loadAuth();
    setBadge(s.bearer ? "✓" : "·", s.bearer ? "#16A34A" : "#2563EB");
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (typeof msg.id === "number" || typeof msg.id === "string") {
      handleCommand(msg);
    }
  };
  ws.onclose = () => {
    ws = null;
    setBadge("", "#000000");
    scheduleReconnect();
  };
  ws.onerror = () => {};
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, RECONNECT_MS);
}

// keep the SW alive enough to retry
chrome.alarms.create("ytr-bridge-poll", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "ytr-bridge-poll" && (!ws || ws.readyState !== WebSocket.OPEN)) {
    connect();
  }
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.action.onClicked.addListener(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    console.log("[ytr-bridge] already connected; auth=", !!bearer);
  } else {
    connect();
  }
});

connect();
