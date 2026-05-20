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

let bearer = null;
let apiBase = null;
let bearerCapturedAt = 0;

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
    if (!auth || !/^Bearer\s+/i.test(auth.value || "")) return;
    bearer = auth.value;
    bearerCapturedAt = Date.now();
    const m = details.url.match(/^(https?:\/\/[^/]+\/api)/i);
    if (m) apiBase = m[1];
    setBadge("✓", "#16A34A");
    console.log("[ytr-bridge] captured Bearer for", apiBase);
  },
  {
    urls: [
      "https://studio-api.prod.suno.com/api/*",
      "https://studio-api.suno.com/api/*",
      "https://studio-api.suno.ai/api/*",
    ],
  },
  ["requestHeaders", "extraHeaders"]
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

async function proxyFetch(args) {
  let host = "";
  try { host = new URL(args.url).host.toLowerCase(); } catch (_) {}

  // Route through a same-origin tab whenever possible — SW cross-origin
  // fetch loses SameSite=Lax cookies, which kills KKBox's WAF cookie.
  if (host.endsWith("suno.com") || host.endsWith("suno.ai")) {
    const tab = await findSunoTab();
    if (tab) return await tabMainWorldFetch(tab, args);
  }
  if (host.endsWith("kkbox.com")) {
    const tab = await findKkboxTab();
    if (!tab) {
      throw new Error(
        "no kkbox.com tab open in this Chrome — open https://www.kkbox.com/ " +
        "so the WAF cookie can attach to subsequent fetches"
      );
    }
    return await tabMainWorldFetch(tab, args);
  }
  // Other origins: try SW fetch as a generic fallback.
  return await swFetch(args);
}

async function handleCommand(msg) {
  const reply = (data) => send(Object.assign({ id: msg.id }, data));
  try {
    if (msg.cmd === "ping") return reply({ ok: true });
    if (msg.cmd === "getAuth")
      return reply({
        bearer,
        apiBase,
        age_ms: bearer ? Date.now() - bearerCapturedAt : null,
      });
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
  ws.onopen = () => {
    console.log("[ytr-bridge] connected to bridge");
    setBadge(bearer ? "✓" : "·", bearer ? "#16A34A" : "#2563EB");
    if (bearer) send({ method: "auth", bearer, apiBase });
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
