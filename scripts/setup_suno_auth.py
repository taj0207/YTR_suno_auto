"""One-time Suno login.

Approach: copy your daily Chrome profile to secrets/suno_chrome_profile/ then
launch Playwright against THAT copy. The copy keeps your Google cookies, so
Sign-in-with-Google works without Google's anti-automation block, but it lives
in an isolated location so Chrome won't IPC-route the launched window into
your daily Chrome process.

First run: copies the profile (one-time, ~50–200 MB, skips browser caches).
Later runs: reuses the copy.

Env overrides:
    SUNO_CHROME_USER_DATA  source Chrome User Data dir to copy from
    SUNO_CHROME_PROFILE    which profile inside it ('Default', 'Profile 1', ...)
    SUNO_USE_REAL_PROFILE=1   skip the copy and use the real profile directly
                              (must close Chrome first)

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


# Browser cache / DB / logs we never need for auth — skipping these saves a LOT.
SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "Service Worker", "Application Cache",
    "GrShaderCache", "ShaderCache", "VideoDecodeStats", "Crashpad",
    "Network Action Predictor", "blob_storage", "shared_proto_db",
    "optimization_guide_hint_cache_store", "Reporting and NEL",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
}
SKIP_FILES = {
    "History", "History-journal", "Visited Links",
    "Top Sites", "Top Sites-journal", "Favicons", "Favicons-journal",
}


def _copy_filter(_src, names):
    return [n for n in names if n in SKIP_DIRS or n in SKIP_FILES]


def ensure_isolated_profile(source_user_data: Path, profile: str) -> Path:
    """Mirror the user's Chrome profile to secrets/suno_chrome_profile so
    Playwright can launch against an isolated copy. Returns user_data_dir to
    point Playwright at. No-op on subsequent runs (idempotent)."""
    dest_root = paths.SECRETS / "suno_chrome_profile"
    dest_root.mkdir(parents=True, exist_ok=True)
    dest_profile = dest_root / profile

    if dest_profile.exists():
        print(f"[ok ] using existing isolated profile at {dest_profile}")
        return dest_root

    src_profile = source_user_data / profile
    if not src_profile.exists():
        raise RuntimeError(f"source profile not found: {src_profile}")

    # User-Data-level files that Chrome reads on startup
    for f in ("Local State", "First Run", "Last Version", "Last Browser"):
        s = source_user_data / f
        if s.exists():
            shutil.copy2(s, dest_root / f)

    print(f"copying profile '{profile}' → {dest_profile}")
    print("(one-time; skipping caches. Takes 10–60 seconds depending on profile size.)")
    shutil.copytree(src_profile, dest_profile, ignore=_copy_filter, dirs_exist_ok=False)
    print(f"[ok ] profile copied")
    return dest_root


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

    source_user_data = Path(
        os.environ.get("SUNO_CHROME_USER_DATA") or (default_chrome_user_data() or "")
    )
    profile = os.environ.get("SUNO_CHROME_PROFILE", "Default")
    use_real = os.environ.get("SUNO_USE_REAL_PROFILE") == "1"

    if not source_user_data or not source_user_data.exists():
        print(f"could not find Chrome User Data dir. Set SUNO_CHROME_USER_DATA to override.",
              file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    if use_real:
        if chrome_running():
            print("=" * 70)
            print("⚠  SUNO_USE_REAL_PROFILE=1 but Chrome is running.")
            print("   Close Chrome FULLY (including system-tray) and re-run.")
            print("=" * 70)
            return 1
        user_data_dir = source_user_data
        print(f"Using REAL profile '{profile}' at: {user_data_dir}")
    else:
        if chrome_running():
            print("[info] Chrome is running — that's fine, we'll use an isolated copy of your profile.")
        user_data_dir = ensure_isolated_profile(source_user_data, profile)
        print(f"Using isolated profile '{profile}' at: {user_data_dir}")

    print()

    with sync_playwright() as pw:
        ctx = open_context(pw, user_data_dir, profile)
        # Reuse Chrome's first existing tab so the user doesn't have to hunt
        # for a Playwright-spawned new tab among their restored-session tabs.
        import time as _t
        page = None
        for _ in range(20):
            if ctx.pages:
                page = ctx.pages[0]
                break
            _t.sleep(0.25)
        if page is None:
            page = ctx.new_page()
        try:
            page.bring_to_front()
        except Exception:
            pass
        page.goto(SUNO_HOME, wait_until="domcontentloaded")

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
