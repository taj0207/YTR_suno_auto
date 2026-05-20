"""Suno HTTP client.

Approach (matching what suno_downloader Chrome extension does):
  1. Open suno.com in headless Playwright with our saved storage_state.
  2. Listen for any request to studio-api.* → extract Bearer token + apiBase.
  3. Close the browser. Use plain `requests` with that header for all API calls.

This lets us reuse Suno's real session without piping every call through the
browser process — fast, no JS round-trip, parallelisable.

Endpoint paths below are from the working extension. The /generate path is the
one endpoint the extension does NOT observe (it only handles download), so it's
a best-guess; verify with DevTools on suno.com if you see 404s in Step 4.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

# === Endpoint paths (relative to apiBase) ==================================
PATHS = {
    "generate":          "/generate/v2/",            # NOT observed — verify if 404
    "feed_by_ids_post":  "/feed/v3",                 # POST {ids:[...]}
    "feed_by_ids_get":   "/feed/v3",                 # GET  ?ids=X&page=0
    "clip":              "/clip/{id}",
    "convert_wav":       "/gen/{id}/convert_wav/",   # POST (returns 204)
    "wav_file":          "/gen/{id}/wav_file/",      # GET — JSON-with-URL or RIFF binary
    "audio_file":        "/gen/{id}/audio_file/",
    "increment_action":  "/gen/{id}/increment_action_count/",
}

DEFAULT_API_BASE = "https://studio-api.prod.suno.com/api"
DEFAULT_MV = "chirp-v4-5"
SUNO_APP = "https://suno.com"


class SunoError(RuntimeError):
    pass


# === Auth capture ==========================================================

def _capture(headless: bool = True, wait_secs: int = 30) -> tuple[str, str]:
    """Open suno.com with saved storage_state and capture (Bearer, apiBase)."""
    state_path = Path(os.environ.get("SUNO_STORAGE_STATE", "secrets/suno_storage_state.json"))
    if not state_path.exists():
        raise SunoError(
            f"missing {state_path}. run: python scripts/setup_suno_auth.py"
        )

    from playwright.sync_api import sync_playwright

    captured: dict[str, str | None] = {"auth": None, "api_base": None}

    def on_request(req) -> None:
        if captured["auth"]:
            return
        url = req.url
        if "studio-api" not in url:
            return
        auth = req.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            captured["auth"] = auth
            try:
                split = url.split("/api/", 1)
                if len(split) == 2:
                    captured["api_base"] = split[0] + "/api"
            except Exception:
                pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=str(state_path))
        page = ctx.new_page()
        page.on("request", on_request)
        try:
            page.goto(f"{SUNO_APP}/create", wait_until="domcontentloaded")
            deadline = time.monotonic() + wait_secs
            while time.monotonic() < deadline and not captured["auth"]:
                page.wait_for_timeout(250)
        finally:
            ctx.close()
            browser.close()

    if not captured["auth"]:
        raise SunoError(
            "could not capture Bearer token from suno.com — session may have "
            "expired. run: python scripts/setup_suno_auth.py"
        )
    api_base = (
        os.environ.get("SUNO_API_BASE")
        or captured["api_base"]
        or DEFAULT_API_BASE
    )
    return captured["auth"], api_base


@contextmanager
def session(headless: bool = True) -> Iterator["SunoClient"]:
    """Open one authenticated SunoClient and clean it up on exit."""
    auth, api_base = _capture(headless=headless)
    s = requests.Session()
    s.headers.update({
        "Authorization": auth,
        "Accept": "application/json, */*",
        "User-Agent": "YTR-suno-auto/1.0",
    })
    try:
        yield SunoClient(s, api_base)
    finally:
        s.close()


# === Client ================================================================

class SunoClient:
    def __init__(self, session: requests.Session, api_base: str):
        self.s = session
        self.api_base = api_base.rstrip("/")

    def _url(self, key: str, **kw) -> str:
        return self.api_base + PATHS[key].format(**kw)

    # --- generation ---------------------------------------------------------

    def submit_vocal(self, *, lyrics: str, styles: str, wid: str | None = None,
                     mv: str = DEFAULT_MV) -> list[str]:
        payload: dict = {
            "prompt": lyrics,
            "tags": styles,
            "make_instrumental": False,
            "mv": mv,
        }
        if wid:
            payload["workspace_id"] = wid
        return self._post_generate(payload)

    def submit_instrumental(self, *, description: str, wid: str | None = None,
                            mv: str = DEFAULT_MV) -> list[str]:
        payload: dict = {
            "gpt_description_prompt": description,
            "make_instrumental": True,
            "mv": mv,
        }
        if wid:
            payload["workspace_id"] = wid
        return self._post_generate(payload)

    def _post_generate(self, payload: dict) -> list[str]:
        r = self.s.post(self._url("generate"), json=payload, timeout=30)
        if not r.ok:
            raise SunoError(f"generate failed: {r.status_code} {r.text[:500]}")
        data = r.json()
        clips = data.get("clips") if isinstance(data, dict) else data
        if not clips:
            raise SunoError(f"no clips in response: {str(data)[:500]}")
        return [c["id"] for c in clips]

    # --- feed / status ------------------------------------------------------

    def fetch_feed(self, song_ids: list[str]) -> list[dict]:
        if not song_ids:
            return []
        try:
            r = self.s.post(self._url("feed_by_ids_post"),
                            json={"ids": song_ids}, timeout=30)
            if r.ok:
                data = r.json()
                return data if isinstance(data, list) else data.get("clips", [])
        except Exception:
            pass
        url = self._url("feed_by_ids_get") + "?ids=" + ",".join(song_ids) + "&page=0"
        r = self.s.get(url, timeout=30)
        if not r.ok:
            raise SunoError(f"feed failed: {r.status_code} {r.text[:500]}")
        data = r.json()
        return data if isinstance(data, list) else data.get("clips", [])

    # --- WAV (verified flow from suno_downloader extension) ----------------

    def increment_action(self, song_id: str) -> None:
        """Optional analytics ping the UI does before WAV download. Never fatal."""
        try:
            self.s.post(
                self._url("increment_action", id=song_id),
                json={"action": "download_audio_wav", "download_source": "workspace"},
                timeout=8,
            )
        except Exception:
            pass

    def trigger_wav(self, song_id: str) -> None:
        """POST convert_wav. 204 = queued. 403 = no Pro (sticky)."""
        url = self._url("convert_wav", id=song_id)
        r = self.s.post(url, data="", timeout=15)
        if r.status_code == 403:
            raise SunoError("forbidden (no Pro subscription)")
        if not r.ok and r.status_code != 204:
            raise SunoError(f"convert_wav failed: {r.status_code} {r.text[:500]}")

    def poll_wav(self, song_id: str, *, timeout: float = 300.0,
                 interval: float = 5.0) -> tuple[str | None, bytes | None]:
        """Poll wav_file. Returns (url, None) OR (None, bytes) on success.

        Suno can respond with either:
          - JSON containing the wav URL nested somewhere -> we walk to find it
          - The .wav binary directly (Content-Type non-JSON, RIFF/WAVE magic)
          - Empty JSON {} while transcoding -> retry
        401/403/404 are fatal.
        """
        url = self._url("wav_file", id=song_id)
        deadline = time.monotonic() + timeout
        last_status: int | None = None
        while time.monotonic() < deadline:
            r = self.s.get(url, timeout=20)
            last_status = r.status_code
            if r.status_code in (401, 403, 404):
                raise SunoError(f"wav_file fatal: {r.status_code}")
            if r.ok:
                ct = (r.headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    try:
                        found = _find_any_url(r.json())
                    except Exception:
                        found = None
                    if found:
                        return (found, None)
                else:
                    body = r.content
                    if _is_riff_wave(body):
                        return (None, body)
            time.sleep(interval)
        raise SunoError(f"poll_wav timeout after {timeout}s (last status {last_status})")

    def download_url(self, url: str, dest: Path, *, min_bytes: int = 1_000_000) -> int:
        """Stream a public CDN URL to disk. Used when poll_wav returns a URL."""
        r = self.s.get(url, stream=True, timeout=120)
        if not r.ok:
            raise SunoError(f"download failed: {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with dest.open("wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)
                    size += len(chunk)
        if size < min_bytes:
            raise SunoError(f"download too small ({size} bytes)")
        return size

    def save_bytes(self, buf: bytes, dest: Path, *, min_bytes: int = 1_000_000) -> int:
        """Write already-fetched bytes (when poll_wav streamed the binary directly)."""
        if len(buf) < min_bytes:
            raise SunoError(f"buffer too small ({len(buf)} bytes)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf)
        return len(buf)


# === helpers ===============================================================

def _is_riff_wave(buf: bytes) -> bool:
    return len(buf) > 12 and buf[0:4] == b"RIFF" and buf[8:12] == b"WAVE"


def _find_any_url(obj) -> str | None:
    """Walk a JSON-ish structure and return the first http(s) URL string found."""
    if obj is None:
        return None
    if isinstance(obj, str):
        if obj.startswith(("http://", "https://")):
            return obj
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_any_url(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_any_url(v)
            if found:
                return found
    return None


def is_complete(clip: dict) -> bool:
    s = (clip.get("status") or "").lower()
    return s in {"complete", "streamed", "finished"}


def is_failed(clip: dict) -> bool:
    return (clip.get("status") or "").lower() in {"error", "failed"}
