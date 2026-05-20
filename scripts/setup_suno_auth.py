"""One-time Suno login: opens a real Chrome (not bundled Chromium) so Google
sign-in works without tripping anti-automation checks. After you log in we save
storage_state for headless reuse by the pipeline.

If Google still refuses to let you in:
  - Easier: log in to Suno via Email (magic link) or Discord in this window
    instead. Suno supports both. No Google needed.

Run:
    python scripts/setup_suno_auth.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import paths  # noqa: E402

SUNO_HOME = "https://suno.com/"
SUNO_CREATE = "https://suno.com/create"


def open_context(pw):
    """Try real Chrome with a persistent profile first (best for Google login).
    Fall back to bundled Chromium if Chrome isn't installed.
    """
    profile_dir = paths.SECRETS / "suno_chrome_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    common = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": {"width": 1280, "height": 800},
        "args": [
            "--disable-blink-features=AutomationControlled",
        ],
    }
    last_err = None
    for channel in ("chrome", "msedge", None):
        try:
            if channel:
                ctx = pw.chromium.launch_persistent_context(channel=channel, **common)
                print(f"[ok ] launched real {channel} with persistent profile {profile_dir}")
            else:
                ctx = pw.chromium.launch_persistent_context(**common)
                print(f"[ok ] launched bundled chromium with persistent profile {profile_dir}")
            return ctx
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[try] {channel or 'chromium'} failed: {e}")
    raise RuntimeError(f"could not launch any browser. last error: {last_err}")


def main() -> int:
    load_dotenv()
    paths.SECRETS.mkdir(parents=True, exist_ok=True)
    state_path = Path(
        os.environ.get("SUNO_STORAGE_STATE", paths.SECRETS / "suno_storage_state.json")
    )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.new_page()
        page.goto(SUNO_HOME)

        print("=" * 70)
        print("Log in to Suno in the opened window.")
        print("Recommended: use **Email** (magic link) or **Discord** — Google")
        print("often blocks automated browsers even with a real Chrome profile.")
        print(f"When you're on {SUNO_CREATE} and signed in,")
        print("come back here and press Enter to save the session.")
        print("=" * 70)
        try:
            input()
        except EOFError:
            pass

        page.goto(SUNO_CREATE, wait_until="domcontentloaded")
        if "sign-in" in page.url or "login" in page.url:
            print(f"Login didn't complete (page is at {page.url}). Aborting save.",
                  file=sys.stderr)
            ctx.close()
            return 1

        state_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(state_path))
        print(f"saved storage_state -> {state_path}")
        ctx.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
