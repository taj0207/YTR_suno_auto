"""Step 5.5: curate generated tracks into an album.

Two modes:

    # 1. Pick tracks by id from one or more jobs
    python pipeline/make_album.py --name star_etched_skin \
        --workspace billie_eilish_depressed \
        --add 2026-05-17_billie_eilish_depressed/abc-uuid:2 \
        --add 2026-05-10_billie_eilish_depressed/xyz-uuid:1

    # 2. Scan a folder you've manually populated with WAVs you want
    python pipeline/make_album.py --name star_etched_skin \
        --workspace billie_eilish_depressed --from-folder

Track-spec format for --add:   {job}/{song_id}:{variant}

Writes: data/albums/{name}/manifest.json + populates tracks/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import paths  # noqa: E402

SPEC_RE = re.compile(r"^(?P<job>[^/]+)/(?P<sid>[^:]+):(?P<var>\d+)$")


def parse_spec(spec: str) -> tuple[str, str, int]:
    m = SPEC_RE.match(spec)
    if not m:
        raise ValueError(f"bad --add spec '{spec}'. expected: job/song_id:variant")
    return m["job"], m["sid"], int(m["var"])


def load_gen_log(job: str) -> dict:
    p = paths.generation_log_path(job)
    if not p.exists():
        raise FileNotFoundError(p)
    return json.loads(p.read_text(encoding="utf-8"))


def find_track(log: dict, song_id: str, variant: int) -> dict:
    for t in log["tracks"]:
        if t["song_id"] == song_id and t["variant"] == variant:
            return t
        # fallback: variant unspecified in log? match by id only if unique
    matching = [t for t in log["tracks"] if t["song_id"] == song_id]
    if len(matching) == 1:
        return matching[0]
    raise KeyError(f"track {song_id}:v{variant} not found in {log['job']}")


def slugify(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title.lower())
    s = re.sub(r"[\s_-]+", "_", s).strip("_")
    return s or "untitled"


def hhmmss(total_sec: float) -> str:
    total_sec = int(total_sec or 0)
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_manifest(album: str, workspace: str, picks: list[tuple[str, str, int]]) -> dict:
    tracks_out = []
    running_sec = 0
    timestamps = []
    album_dir = paths.album_dir(album)
    tracks_dir = album_dir / "tracks"
    tracks_dir.mkdir(parents=True, exist_ok=True)

    for idx, (job, sid, variant) in enumerate(picks, start=1):
        log = load_gen_log(job)
        t = find_track(log, sid, variant)
        if t.get("wav_status") != "ready" or not t.get("local_path"):
            raise RuntimeError(f"{job}/{sid}:v{variant} is not ready (wav_status={t.get('wav_status')})")

        src = paths.job_dir(job) / t["local_path"]
        if not src.exists():
            raise FileNotFoundError(src)

        title = t.get("title") or t.get("source_song") or sid
        dest_name = f"{idx:02d}_{slugify(title)}.wav"
        dest = tracks_dir / dest_name
        if not dest.exists():
            shutil.copy2(src, dest)   # symlink on Windows is admin-only; copy is safer

        tracks_out.append({
            "track_no":         idx,
            "title":            title,
            "src_job":          job,
            "src_song_id":      sid,
            "src_variant":      variant,
            "local_path":       f"tracks/{dest_name}",
            "duration_sec":     t.get("duration_sec"),
            "suno_song_url":    t.get("suno_song_url"),
            "suno_description": t.get("suno_description"),
        })
        timestamps.append(f"{hhmmss(running_sec)} {idx}. {title}")
        running_sec += int(t.get("duration_sec") or 0)

    manifest = {
        "album_name":  album,
        "workspace":   workspace,
        "created_at":  dt.datetime.now().astimezone().isoformat(),
        "tracks":      tracks_out,
        "timestamps_for_youtube": timestamps,
    }
    return manifest


def from_folder(album: str, workspace: str) -> dict:
    """Scan data/albums/{album}/tracks/*.wav and best-effort match back to jobs."""
    tracks_dir = paths.album_dir(album) / "tracks"
    wavs = sorted(tracks_dir.glob("*.wav"))
    if not wavs:
        raise RuntimeError(f"no .wav files in {tracks_dir}")

    # Build reverse index: song_id -> (job, track)
    index = {}
    for job_dir in paths.JOBS.iterdir():
        log_path = job_dir / "generation_log.json"
        if not log_path.exists():
            continue
        log = json.loads(log_path.read_text(encoding="utf-8"))
        for t in log["tracks"]:
            index[(t["song_id"], t["variant"])] = (log["job"], t)

    tracks_out = []
    running_sec = 0
    timestamps = []
    for idx, wav in enumerate(wavs, start=1):
        # Filename convention from Step 5: {song_id}_v{n}.wav OR from this script: NN_slug.wav
        m = re.match(r"^(?P<sid>[0-9a-f-]{8,})_v(?P<var>\d+)\.wav$", wav.name)
        if m:
            key = (m["sid"], int(m["var"]))
            job, t = index.get(key, (None, {}))
        else:
            t = {}; job = None
        title = t.get("title") or wav.stem
        tracks_out.append({
            "track_no":         idx,
            "title":            title,
            "src_job":          job,
            "src_song_id":      t.get("song_id"),
            "src_variant":      t.get("variant"),
            "local_path":       f"tracks/{wav.name}",
            "duration_sec":     t.get("duration_sec"),
            "suno_song_url":    t.get("suno_song_url"),
            "suno_description": t.get("suno_description"),
        })
        timestamps.append(f"{hhmmss(running_sec)} {idx}. {title}")
        running_sec += int(t.get("duration_sec") or 0)

    return {
        "album_name":  album,
        "workspace":   workspace,
        "created_at":  dt.datetime.now().astimezone().isoformat(),
        "tracks":      tracks_out,
        "timestamps_for_youtube": timestamps,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--add", action="append", default=[], help="job/song_id:variant")
    p.add_argument("--from-folder", action="store_true",
                   help="scan data/albums/<name>/tracks/ instead of using --add")
    args = p.parse_args()

    paths.ensure_all()

    if args.from_folder:
        manifest = from_folder(args.name, args.workspace)
    else:
        if not args.add:
            print("error: use --add or --from-folder", file=sys.stderr)
            return 2
        picks = [parse_spec(s) for s in args.add]
        seen = set()
        for _, sid, _ in picks:
            if sid in seen:
                print(f"error: duplicate song_id {sid} in --add list", file=sys.stderr)
                return 2
            seen.add(sid)
        manifest = build_manifest(args.name, args.workspace, picks)

    out = paths.album_manifest_path(args.name)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({len(manifest['tracks'])} tracks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
