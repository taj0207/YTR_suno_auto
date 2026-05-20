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
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import paths, suno  # noqa: E402

MIN_WAV_BYTES = 1_000_000


def load_log(job: str) -> dict:
    path = paths.generation_log_path(job)
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def save_log(job: str, log: dict) -> None:
    paths.generation_log_path(job).write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def wav_local_path(job: str, song_id: str, variant: int) -> Path:
    return paths.job_downloads(job) / f"{song_id}_v{variant}.wav"


def needs_work(track: dict, job: str) -> bool:
    if track.get("wav_status") == "forbidden":
        return False
    if track.get("wav_status") == "ready" and track.get("local_path"):
        p = paths.job_dir(job) / track["local_path"]
        if p.exists() and p.stat().st_size >= MIN_WAV_BYTES:
            return False
    return True


def process_track(client: suno.SunoClient, track: dict, job: str, *, save_callback) -> None:
    sid = track["song_id"]
    now = dt.datetime.now().astimezone().isoformat()
    track["last_attempt_at"] = now
    track["attempts"] = track.get("attempts", 0) + 1
    track["error"] = None

    # 1. Refresh metadata; ensure generation is complete.
    clips = client.fetch_feed([sid])
    if not clips:
        track["error"] = "song_id not found in feed"
        return
    clip = clips[0]
    if suno.is_failed(clip):
        track["status"] = "failed"
        track["error"] = clip.get("error_message") or "Suno marked failed"
        return
    if not suno.is_complete(clip):
        track["status"] = "pending"
        track["error"] = f"still generating (suno status: {clip.get('status')})"
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

    # 5. Persist file
    dest = wav_local_path(job, sid, track["variant"])
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
    with suno.session(headless=True) as client:
        for track in targets:
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

    print(f"\nSummary: {n_ok} ready, {n_fail} need retry next run.", file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
