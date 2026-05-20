"""One-time Suno login: opens your REAL daily Chrome (with your real profile)
so Google sign-in works without anti-automation trips. After login the script
saves a Playwright storage_state for headless reuse by the pipeline.

REQUIREMENT: close Chrome first. Chrome holds an exclusive lock on the profile
files; if it's open, Playwright will fail or silently route the window into
your already-running Chrome (which we can't control).

Override the profile if you want a separate one (so you don't have to close
Chrome):
    set SUNO_CHROME_USER_DATA=D:/some/other/dir
    set SUNO_CHROME_PROFILE=Default       (or 'Profile 1' etc.)

Run:
    python scripts/setup_suno_auth.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import paths  # noqa: E402

SUNO_HOME = "https://suno.com/"
SUNO_CREATE = "https://suno.com/create"


def default_chrome_user_data() -> Path | None:
    candidates = []
    if local := os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(local) / "Google" / "Chrome" / "User Data")
    candidates.append(Path(os.path.expanduser("~")) / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
    for c in candidates:
        if c.exists():
            return c
    return None


def chrome_running() -> bool:
    """Best-effort check for chrome.exe in tasklist."""
    try:
        out = subprocess.check_output(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                                      text=True, errors="ignore")
        return "chrome.exe" in out.lower()
    except Exception:
        return False


def open_context(pw, user_data_dir: Path, profile: str):
    common = {
        "user_data_dir": str(user_data_dir),
        "headless": False,
        "viewport": None,                    # use the real window size
        "args": [
            f"--profile-directory={profile}",
            "--disable-blink-features=AutomationControlled",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    for channel in ("chrome", "msedge"):
        try:
            ctx = pw.chromium.launch_persistent_context(channel=channel, **common)
            print(f"[ok ] launched {channel} with profile '{profile}' from {user_data_dir}")
            return ctx
        except Exception as e:  # noqa: BLE001
            print(f"[try] {channel} failed: {e}")
    raise RuntimeError(
        "couldn't launch Chrome or Edge. Make sure Chrome is fully closed "
        "(check the system tray too) and try again."
    )


def main() -> int:
    load_dotenv()
    paths.SECRETS.mkdir(parents=True, exist_ok=True)
    state_path = Path(
        os.environ.get("SUNO_STORAGE_STATE", paths.SECRETS / "suno_storage_state.json")
    )

    user_data_dir = Path(
        os.environ.get("SUNO_CHROME_USER_DATA") or (default_chrome_user_data() or "")
    )
    profile = os.environ.get("SUNO_CHROME_PROFILE", "Default")

    if not user_data_dir or not user_data_dir.exists():
        print(f"could not find Chrome User Data dir. Set SUNO_CHROME_USER_DATA to override.",
              file=sys.stderr)
        return 2

    if chrome_running():
        print("=" * 70)
        print("⚠  Chrome is currently running.")
        print(f"   Profile path:  {user_data_dir}")
        print(f"   Profile name:  {profile}")
        print()
        print("   Chrome locks the profile files while open. Close Chrome FULLY")
        print("   (including the system-tray icon) and re-run this script.")
        print("=" * 70)
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    print(f"Using profile '{profile}' at: {user_data_dir}")
    print("(your real Chrome session, with your real Google cookies — this is")
    print(" why Suno's Google sign-in will work here.)")
    print()

    with sync_playwright() as pw:
        ctx = open_context(pw, user_data_dir, profile)
        page = ctx.new_page()
        page.goto(SUNO_HOME)

        print("=" * 70)
        print("Log in to Suno in the opened window if you're not already.")
        print(f"When you're at {SUNO_CREATE} and signed in,")
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
