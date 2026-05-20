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


def chrome_running() -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            text=True, errors="ignore"
        )
        return "chrome.exe" in out.lower()
    except Exception:
        return False


def launch_chrome_with_cdp(user_data_dir: Path) -> None:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("chrome.exe not found in standard locations")

    # Critical: Chrome enforces single-process-per-User-Data-dir. If another
    # Chrome is already running with this profile, our spawn IPC-routes into
    # it and exits — leaving no CDP port. Bail with a clear message.
    if chrome_running():
        raise RuntimeError(
            "Chrome is already running. Because Chrome enforces one process per "
            "User Data dir, a new launch silently routes into the existing "
            "process and our --remote-debugging-port flag is ignored.\n\n"
            "Fix EITHER:\n"
            "  (a) Close ALL Chrome — main windows AND background/system-tray icon.\n"
            "      Verify with: Get-Process chrome  (must be empty)\n"
            "      Then re-run this script.\n\n"
            "  (b) Or start Chrome YOURSELF with the debug flag, then re-run:\n"
            f'      & "{chrome}" --remote-debugging-port=9222 '
            f'--user-data-dir="{user_data_dir}"\n'
        )

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
    print("[wait] for CDP endpoint at localhost:9222 (up to 30s)...")
    for i in range(60):
        if cdp_ready():
            print(f"[ok ] CDP endpoint reachable (after {i * 0.5:.1f}s)")
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome didn't start a CDP listener on {CDP_BASE} within 30s.\n"
        "Most likely Chrome silently routed into an already-running process. "
        "Close ALL Chrome (including system-tray icon) and try again."
    )


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

    print("[step] connecting to Chrome over CDP...")
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_BASE)
        print(f"[ok ] connected. contexts={len(browser.contexts)}")
        if not browser.contexts:
            print("[fail] no contexts in connected Chrome", file=sys.stderr)
            return 1
        ctx = browser.contexts[0]
        print(f"[ok ] using first context. pages={len(ctx.pages)}")
        for i, p in enumerate(ctx.pages):
            try:
                print(f"        tab {i}: {p.url[:90]}")
            except Exception:
                pass

        print()
        print("=" * 70)
        print("In the Chrome window that opened:")
        print()
        print("  1. Type / paste this URL into the address bar yourself:")
        print("       https://suno.com/create")
        print()
        print("  2. If not logged in, sign in (any method — Google, Email,")
        print("     Discord, whatever works for you).")
        print()
        print("  3. Confirm the address bar shows suno.com/create (not /sign-in).")
        print()
        print("Then come back here and press Enter to save the session.")
        print("=" * 70)
        try:
            input()
        except EOFError:
            pass

        # Find the suno tab to validate URL
        print("[step] checking Suno tab(s)...")
        suno_pages = [p for p in ctx.pages if "suno.com" in (p.url or "")]
        if not suno_pages:
            print("[fail] no suno.com tab found in Chrome.", file=sys.stderr)
            print("       Open suno.com in the Chrome window first.", file=sys.stderr)
            return 1
        target = suno_pages[-1]
        url = target.url
        print(f"[ok ] suno tab url: {url}")
        if "sign-in" in url or "login" in url:
            print(f"[fail] still on a sign-in page ({url}).", file=sys.stderr)
            print("       Finish login in Chrome, then re-run this script.", file=sys.stderr)
            return 1

        state_path.parent.mkdir(parents=True, exist_ok=True)
        print("[step] writing storage_state...")
        ctx.storage_state(path=str(state_path))
        print(f"[ok ] saved storage_state -> {state_path}")

    print()
    print("You can keep Chrome open. The pipeline (Step 4/5) only needs the")
    print("saved storage_state — it won't touch your live Chrome session again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
