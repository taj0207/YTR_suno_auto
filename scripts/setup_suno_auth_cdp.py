"""Alternative auth flow: attach to your running Chrome via CDP.

Use this if the isolated-profile-copy approach in setup_suno_auth.py doesn't
preserve your Suno login (Chrome 127+ binds cookies to runtime state).

Steps:

  1. Close ALL Chrome instances (including system-tray icon).

  2. Start Chrome with a debugging port, pointing at your real profile:

       & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
         --remote-debugging-port=9222 `
         --user-data-dir="$env:LOCALAPPDATA\\Google\\Chrome\\User Data"

     (Or use the helper below: this script can do it for you if Chrome isn't
     already running with a debug port.)

  3. In that Chrome window, log in to suno.com (you'll see your Google account
     options because it's your real profile and process).

  4. Run this script:

       python scripts/setup_suno_auth_cdp.py

     It connects to localhost:9222, grabs cookies + localStorage from your
     Suno tab, and writes secrets/suno_storage_state.json.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import paths  # noqa: E402

CDP_BASE = "http://localhost:9222"
CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> Path | None:
    for p in CHROME_PATHS:
        if p.exists():
            return p
    return None


def cdp_ready() -> bool:
    try:
        r = requests.get(f"{CDP_BASE}/json/version", timeout=2)
        return r.ok
    except Exception:
        return False


def launch_chrome_with_cdp(user_data_dir: Path) -> None:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("chrome.exe not found in standard locations")
    print(f"starting Chrome at {chrome}")
    print(f"  --remote-debugging-port=9222")
    print(f"  --user-data-dir={user_data_dir}")
    subprocess.Popen([
        str(chrome),
        "--remote-debugging-port=9222",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ])
    # Wait for CDP to come up
    for _ in range(30):
        if cdp_ready():
            print("[ok ] CDP endpoint reachable")
            return
        time.sleep(0.5)
    raise RuntimeError(f"Chrome didn't start a CDP listener on {CDP_BASE} within 15s")


def main() -> int:
    load_dotenv()
    paths.SECRETS.mkdir(parents=True, exist_ok=True)
    state_path = Path(
        os.environ.get("SUNO_STORAGE_STATE", paths.SECRETS / "suno_storage_state.json")
    )

    if not cdp_ready():
        print("[info] no Chrome with debug port found; starting one now…")
        user_data_dir = Path(
            os.environ.get("SUNO_CHROME_USER_DATA")
            or (Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data")
        )
        if not user_data_dir.exists():
            print(f"can't find Chrome User Data at {user_data_dir}", file=sys.stderr)
            return 2
        try:
            launch_chrome_with_cdp(user_data_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {e}", file=sys.stderr)
            print("If Chrome was already running with a different profile, "
                  "close ALL Chrome windows (and system-tray icon) and re-run.",
                  file=sys.stderr)
            return 1
    else:
        print("[ok ] using existing Chrome with debug port at localhost:9222")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed.", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_BASE)
        if not browser.contexts:
            print("[fail] no contexts in connected Chrome", file=sys.stderr)
            return 1
        ctx = browser.contexts[0]

        # Find or create a Suno tab in the user's Chrome
        suno_page = None
        for p in ctx.pages:
            if "suno.com" in p.url:
                suno_page = p
                break
        if suno_page is None:
            suno_page = ctx.new_page()
            suno_page.goto("https://suno.com/", wait_until="domcontentloaded")
        try:
            suno_page.bring_to_front()
        except Exception:
            pass

        print("=" * 70)
        print("In the Chrome window:")
        print("  - Make sure you're logged in to Suno (any method — your real")
        print("    Google account works here because this is your real Chrome).")
        print(f"  - Confirm the URL is suno.com (not a sign-in page).")
        print("Then return here and press Enter to save the session.")
        print("=" * 70)
        try:
            input()
        except EOFError:
            pass

        # Validate logged in
        suno_page.goto("https://suno.com/create", wait_until="domcontentloaded")
        if "sign-in" in suno_page.url or "login" in suno_page.url:
            print(f"Looks like login didn't complete (page is at {suno_page.url}).",
                  file=sys.stderr)
            return 1

        state_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(state_path))
        print(f"saved storage_state -> {state_path}")

    print()
    print("You can keep Chrome open. The pipeline (Step 4/5) only needs the")
    print("saved storage_state — it won't touch your live Chrome session again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
