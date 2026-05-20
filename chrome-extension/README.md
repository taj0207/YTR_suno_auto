# YTR Suno Bridge (Chrome extension)

The pipeline (Python) runs a small WebSocket server. This extension connects to
it from your daily Chrome, captures the Suno Bearer token from your real
session, and proxies authenticated API calls (`fetch`) inside an open
`suno.com` tab. The pipeline never touches Chrome directly — no CDP, no
`--remote-debugging-port`, no Playwright auth dance.

## Install (one-time)

1. `chrome://extensions/` → enable Developer mode.
2. **Load unpacked** → select `D:\github\taj0207\YTR_suno_auto\chrome-extension`.
3. Pin it. Badge:
   - empty grey = not connected to pipeline
   - blue `·` = connected, no Bearer captured yet (open suno.com)
   - green `✓` = connected and Bearer cached

## Use

1. Make sure **one tab in your Chrome is on `https://suno.com/...`** (any page
   that hits the API works — `/create`, `/me`, `/song/...`).
2. Run the pipeline. The Python side starts the bridge server on
   `127.0.0.1:18792`; this extension auto-connects within a couple of seconds
   and answers `getAuth` / `fetch` commands.
3. If Suno rotates the JWT mid-run, the next outgoing API request in your
   Chrome refreshes the cached Bearer automatically.

## Protocol

WebSocket JSON messages, 1:1 request/response keyed by `id`:

```
script -> ext:  { id, cmd: "ping" }                         -> { id, ok: true }
script -> ext:  { id, cmd: "getAuth" }                      -> { id, bearer, apiBase, age_ms }
script -> ext:  { id, cmd: "fetch", url, method, headers?, body? }
                                                            -> { id, status, headers, body_b64 }
```

`fetch` runs inside a `suno.com` tab via `chrome.scripting.executeScript` in
the MAIN world, so cookies + the current JWT attach naturally.

## Permissions explained

- `webRequest` (extraHeaders) — observe outgoing `Authorization: Bearer ...`
  on `studio-api.*` to learn the current token.
- `scripting` + `tabs` — run the fetch proxy inside the suno.com tab.
- `alarms` — keep the service worker awake so we keep reconnecting.
- `host_permissions` on suno.* and localhost — required for the above.
