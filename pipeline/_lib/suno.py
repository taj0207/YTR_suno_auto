"""Suno HTTP client.

Auth source: the YTR Suno Bridge Chrome extension (chrome-extension/) connects
to a local WebSocket server (pipeline/_lib/suno_bridge.py) and supplies the
Bearer token + apiBase captured live from the user's logged-in Chrome.
We never run Playwright or open Chrome ourselves — Chrome 136+ blocks that
anyway. Just use `requests` with the captured token.

Endpoint paths are from the suno_downloader extension's observed traffic.
The /generate path is the one endpoint that extension does NOT exercise (it's
a download tool), so it's a best-guess; verify with DevTools if Step 4 404s.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

import requests

from . import suno_bridge

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


# === Auth source ============================================================

@contextmanager
def session(headless: bool = True) -> Iterator["SunoClient"]:
    """Open one authenticated SunoClient by pulling auth from the bridge
    extension. `headless` is ignored (kept for API compat)."""
    del headless  # unused; the extension lives in the user's real Chrome
    bridge = suno_bridge.get_bridge()
    auth = bridge.wait_for_auth(timeout=int(os.environ.get("SUNO_AUTH_WAIT", "180")))
    api_base = os.environ.get("SUNO_API_BASE") or auth.api_base or DEFAULT_API_BASE
    s = requests.Session()
    s.headers.update({
        "Authorization": auth.bearer,
        "Accept": "application/json, */*",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    })
    try:
        yield SunoClient(s, api_base, bridge=bridge)
    finally:
        s.close()


# === Client ================================================================

class SunoClient:
    def __init__(self, session: requests.Session, api_base: str,
                 bridge: "suno_bridge.SunoBridge | None" = None):
        self.s = session
        self.api_base = api_base.rstrip("/")
        self.bridge = bridge

    def refresh_auth(self) -> bool:
        """Refetch Bearer from bridge (call this on 401). Returns True if changed."""
        if not self.bridge:
            return False
        auth = self.bridge.get_auth()
        if not auth or not auth.bearer:
            return False
        if self.s.headers.get("Authorization") == auth.bearer:
            return False
        self.s.headers["Authorization"] = auth.bearer
        return True

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
