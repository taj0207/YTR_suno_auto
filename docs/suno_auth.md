# Suno authentication

The pipeline talks to Suno's internal HTTP API the same way suno.com's web app
does — by piggybacking on a real browser session. Playwright loads a saved
storage state (cookies + localStorage), and HTTP calls are routed through the
browser's request context so the auth header is attached automatically.

There is no Suno-issued API key for end-users. The "credential" is a long-lived
storage_state JSON captured from a manual login.

## One-time setup

```powershell
python scripts\setup_suno_auth.py
```

This opens a real Chrome window (with a dedicated profile under
`secrets/suno_chrome_profile/`). Falls back to Edge or bundled Chromium if
Chrome isn't installed.

**Login method matters:** Google actively blocks automated browsers from
signing in. Use one of these instead:

| Method | Works in automated browser? |
|---|---|
| Email magic link | ✅ Always works |
| Discord | ✅ Works |
| Microsoft | ✅ Works |
| Google | ❌ Usually blocked by Google's anti-automation |

If your Suno account is currently Google-only, log in to suno.com from your
daily Chrome **first**, go to Settings → add an email login method, then run
the setup script and sign in with email.

After login, the script saves:
- `secrets/suno_storage_state.json` — cookies + localStorage for headless reuse.
- `secrets/suno_chrome_profile/` — a persistent Chrome profile so re-running
  the setup script doesn't ask you to log in again.

## When to re-run

- Session expired (usually weeks). You'll see 401s in Step 4/5 logs.
- You log out / clear cookies on suno.com.
- Suno migrates auth providers.

## How the auth handshake works

Suno's web app puts a Bearer JWT in every `/api/` request's `Authorization`
header. Cookies + localStorage from `storage_state.json` are what let suno.com
mint that JWT on page load. So our flow:

1. Playwright opens `https://suno.com/create` with the saved storage_state.
2. We register a `page.on("request", ...)` listener.
3. The SPA fires its first request to `studio-api.*` within ~5s — we grab the
   `Authorization` header AND derive `apiBase = <origin>/api`.
4. Close the browser. Reuse the captured Bearer + apiBase via plain `requests`
   for all subsequent API calls.

The same pattern is used by the `suno_downloader` Chrome extension — it
intercepts `fetch`/`XMLHttpRequest` in the page context. We do it from the
outside via Playwright; same result.

## Endpoint registry

All Suno endpoint paths live in `pipeline/_lib/suno.py:PATHS`. Most paths are
**verified by observation** from `suno_downloader`:

| Key | Verified? | Purpose |
|---|---|---|
| `convert_wav`       | ✅ | `POST /api/gen/{id}/convert_wav/` — trigger WAV (204) |
| `wav_file`          | ✅ | `GET /api/gen/{id}/wav_file/` — poll WAV (JSON-with-URL or RIFF binary) |
| `audio_file`        | ✅ | `GET /api/gen/{id}/audio_file/` — MP3 fallback |
| `increment_action`  | ✅ | `POST /api/gen/{id}/increment_action_count/` — analytics ping |
| `feed_by_ids_post`  | ✅ | `POST /api/feed/v3 {ids:[...]}` |
| `feed_by_ids_get`   | ✅ | `GET /api/feed/v3?ids=X&page=0` |
| `clip`              | ✅ | `GET /api/clip/{id}` |
| `generate`          | ❓ | `POST /api/generate/v2/` — **NOT observed by extension** |

The `generate` endpoint isn't exercised by suno_downloader (it's a download
tool). If Step 4 returns 404, open DevTools → Network on suno.com → Generate a
song manually → grep the network panel for the request that posts your
lyrics. Copy that path into `PATHS["generate"]`.

## Override via env

- `SUNO_API_BASE=https://studio-api.suno.ai/api` — force a specific origin if
  auto-discovery picks the wrong one.
- `SUNO_STORAGE_STATE=secrets/...json` — point to a different session file.
