"""Step 5: download WAVs for every track in a job's generation_log.

Verified flow (from suno_downloader extension):
    1. POST /api/gen/{id}/increment_action_count/   (analytics, optional)
    2. POST /api/gen/{id}/convert_wav/              (trigger, returns 204)
    3. GET  /api/gen/{id}/wav_file/                 (poll until JSON-with-URL or RIFF binary)

Default behaviour (retry-failed is default):
    - Skip tracks already with valid .wav on disk (>1MB).
    - Skip tracks with wav_status == 'forbidden' (no Pro, sticky).
    - For everything else: re-trigger and re-poll. Cheap, idempotent.

Run:
    python pipeline/05_suno_download.py --job 2026-05-17_billie_eilish_depressed
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import paths, progress, suno  # noqa: E402

MIN_WAV_BYTES = 1_000_000

# Filesystem-unsafe chars on Windows (also stripped on other OSes for portability).
_FS_INVALID = __import__("re").compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    s = _FS_INVALID.sub("_", name).strip().strip(". ")
    # Collapse whitespace runs to a single space
    s = " ".join(s.split())
    if not s:
        return ""
    up = s.upper()
    # Windows reserved device names
    if up in {"CON", "PRN", "AUX", "NUL"} or __import__("re").match(r"^(COM|LPT)\d$", up):
        s = "_" + s
    return s[:max_len]


def load_log(job: str) -> dict:
    path = paths.generation_log_path(job)
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_log(job: str, log: dict) -> None:
    paths.generation_log_path(job).write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def wav_local_path(job: str, song_id: str, variant: int, title: str | None = None,
                   existing: str | None = None) -> Path:
    """Name files as '<title>_v<n>.wav' when title is available; append a short
    song_id segment if a *different* clip already wrote to that filename. Falls
    back to '<song_id>_v<n>.wav' if no usable title.

    If `existing` (track.local_path from generation_log) is given, reuse that
    path — that file belongs to THIS clip from a previous run, so we want to
    rewrite it in place, not generate a parallel filename.
    """
    base_dir = paths.job_downloads(job)
    if existing:
        p = paths.job_dir(job) / existing
        return p
    safe_title = _sanitize_filename(title or "")
    if not safe_title:
        return base_dir / f"{song_id}_v{variant}.wav"
    primary = base_dir / f"{safe_title}_v{variant}.wav"
    if primary.exists():
        # Different clip already owns this filename — disambiguate with song_id.
        primary = base_dir / f"{safe_title}_{song_id[:8]}_v{variant}.wav"
    return primary


def needs_work(track: dict, job: str) -> bool:
    if track.get("wav_status") == "forbidden":
        return False
    if track.get("wav_status") == "ready" and track.get("local_path"):
        p = paths.job_dir(job) / track["local_path"]
        if p.exists() and p.stat().st_size >= MIN_WAV_BYTES:
            return False
    return True


def _existing_local_path(track: dict, job: str) -> Path | None:
    """Resolve the on-disk WAV file we previously wrote for this track,
    so we don't rewrite under a new name when the title finally arrives."""
    lp = track.get("local_path")
    if not lp:
        return None
    p = paths.job_dir(job) / lp
    return p if p.exists() else None


def process_track(client: suno.SunoClient, track: dict, job: str, *, save_callback) -> None:
    sid = track["song_id"]
    now = dt.datetime.now().astimezone().isoformat()
    track["last_attempt_at"] = now
    track["attempts"] = track.get("attempts", 0) + 1
    track["error"] = None

    # 1. Wait until clip is TRULY complete — Suno's /convert_wav/ rejects
    #    'streaming' with {"detail":"Clip must be complete."}.
    wait_max = float(os.environ.get("SUNO_WAV_WAIT_MAX_S", "420"))   # 7 min
    poll = float(os.environ.get("SUNO_WAV_POLL_S", "15"))
    deadline = time.monotonic() + wait_max
    clip = None
    while time.monotonic() < deadline:
        clips = client.fetch_feed([sid])
        if not clips:
            track["error"] = "song_id not found in feed"
            return
        clip = clips[0]
        if suno.is_failed(clip):
            track["status"] = "failed"
            track["error"] = clip.get("error_message") or "Suno marked failed"
            return
        if suno.is_truly_complete(clip):
            break
        s = (clip.get("status") or "").lower()
        print(f"[wait] {sid}: status={s}, waiting for 'complete' ({int(deadline - time.monotonic())}s left)")
        time.sleep(poll)
    else:
        track["status"] = "pending"
        track["wav_status"] = "timeout"
        track["error"] = f"clip never reached 'complete' (last: {clip and clip.get('status')})"
        return

    track["status"] = "complete"
    track["audio_url_mp3"] = clip.get("audio_url") or track.get("audio_url_mp3")
    track["title"] = clip.get("title") or track.get("title")
    md = clip.get("metadata") or {}
    track["duration_sec"] = md.get("duration") or clip.get("duration")
    track["suno_description"] = (
        md.get("prompt") or clip.get("prompt") or track.get("suno_description")
    )

    # 2. Analytics ping (matches UI; ignored if it fails)
    client.increment_action(sid)

    # 3. Trigger WAV (re-triggering an already-queued/ready WAV is harmless)
    try:
        print(f"[wav ] {sid}: trigger convert_wav")
        client.trigger_wav(sid)
        if track["wav_status"] == "not_requested":
            track["wav_status"] = "requested"
    except suno.SunoError as e:
        if "forbidden" in str(e):
            track["wav_status"] = "forbidden"
            track["error"] = str(e)
            print(f"[fbd ] {sid}: {e}")
            return
        track["wav_status"] = "failed"
        track["error"] = str(e)
        print(f"[fail] {sid}: trigger {e}", file=sys.stderr)
        return

    # 4. Poll wav_file → either a URL we download, or bytes we save directly.
    try:
        print(f"[poll] {sid}: wav_file")
        wav_url, wav_bytes = client.poll_wav(sid, timeout=300, interval=5)
        if wav_url:
            track["wav_url"] = wav_url
    except suno.SunoError as e:
        msg = str(e)
        track["wav_status"] = "timeout" if "timeout" in msg else "failed"
        track["error"] = msg
        print(f"[{track['wav_status'][:4]}] {sid}: {e}", file=sys.stderr)
        return

    # 5. Persist file (named after the Suno/Gemini title when available)
    dest = wav_local_path(job, sid, track["variant"],
                          title=track.get("title"),
                          existing=track.get("local_path"))
    try:
        if wav_url:
            print(f"[dl  ] {sid}: -> {dest.name}")
            size = client.download_url(wav_url, dest, min_bytes=MIN_WAV_BYTES)
        else:
            print(f"[byt ] {sid}: streamed directly -> {dest.name}")
            size = client.save_bytes(wav_bytes, dest, min_bytes=MIN_WAV_BYTES)
        track["wav_status"] = "ready"
        track["size_bytes"] = size
        track["local_path"] = str(dest.relative_to(paths.job_dir(job))).replace("\\", "/")
        print(f"[ok  ] {sid}: {size/1_000_000:.1f} MB")
    except suno.SunoError as e:
        track["wav_status"] = "failed"
        track["error"] = str(e)
        print(f"[fail] {sid}: write {e}", file=sys.stderr)
        return

    save_callback()


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", required=True)
    p.add_argument("--song-id", action="append", default=[], help="limit to specific song_ids")
    args = p.parse_args()

    log = load_log(args.job)
    targets = [t for t in log["tracks"]
               if (not args.song_id or t["song_id"] in args.song_id) and needs_work(t, args.job)]

    if not targets:
        print("nothing to do (all tracks ready or forbidden).")
        return 0

    def persist() -> None:
        save_log(args.job, log)

    print(f"processing {len(targets)} track(s)...")
    n_ok = n_fail = 0
    prog = progress.StepProgress("Step 5 suno_download", len(targets))
    with suno.session(headless=True) as client:
        for track in targets:
            prog.next(f"{track['source_song']}:{track['song_id'][:8]}")
            try:
                process_track(client, track, args.job, save_callback=persist)
                if track["wav_status"] == "ready":
                    n_ok += 1
                else:
                    n_fail += 1
            except Exception as e:  # noqa: BLE001
                print(f"[fail] {track['song_id']}: unexpected {e}", file=sys.stderr)
                track["error"] = str(e)
                n_fail += 1
            persist()

    prog.done(ok=n_ok, failed=n_fail)
    print(f"Summary: {n_ok} ready, {n_fail} need retry next run.", file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
