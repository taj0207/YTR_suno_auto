"""End-to-end orchestrator for Step 0 → 5.

Step 0 discovers each artist's songs and emits a pending song_list, Step 1-5
process that list. Each step runs as a subprocess for cleaner error isolation.

Step 5.5 (make_album) and Step 6 (YouTube desc) are intentionally NOT run here
— album curation is a human decision.

Run (artist-driven, the normal case):
    python pipeline/run_all.py \
        --workspace billie_eilish_depressed \
        --artists artist_list.yaml \
        --batch 2026-05-17 \
        --mode vocal

Run (song list override, skip Step 0):
    python pipeline/run_all.py \
        --workspace billie_eilish_depressed \
        --songs song_list.yaml \
        --batch 2026-05-17 \
        --mode vocal

Flags:
    --skip-step N        skip a single step (e.g., --skip-step 1)
    --force-step N       pass --force-step N to step 3 (or 2)
    --refresh-lyrics     pass through to Step 1
    --refresh-catalog    pass --refresh to Step 0 (re-ask Gemini for songs)
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, label: str) -> int:
    print(f"\n{'=' * 70}\n[{label}] {' '.join(cmd)}\n{'=' * 70}")
    r = subprocess.run(cmd, cwd=str(REPO))
    if r.returncode != 0:
        print(f"[{label}] failed with exit {r.returncode}", file=sys.stderr)
    return r.returncode


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", required=True)
    p.add_argument("--artists", help="path to artist_list.yaml (drives Step 0 discovery)")
    p.add_argument("--songs", help="path to song_list.yaml (skips Step 0)")
    p.add_argument("--batch", default=dt.date.today().isoformat())
    p.add_argument("--mode", choices=["vocal", "instrumental"], default="vocal")
    p.add_argument("--variant", default=None)
    p.add_argument("--skip-step", action="append", type=int, default=[])
    p.add_argument("--force-step", type=int, default=0)
    p.add_argument("--refresh-lyrics", action="store_true")
    p.add_argument("--refresh-catalog", action="store_true")
    args = p.parse_args()

    if not args.artists and not args.songs:
        print("error: must supply --artists or --songs", file=sys.stderr)
        return 2

    py = sys.executable
    songs_path = args.songs

    # Step 0: discovery → emit pending song_list (unless --songs given)
    if args.artists and 0 not in args.skip_step:
        songs_path = "song_list_pending.yaml"
        rc = run([
            py, "pipeline/00_discover.py",
            "--artists", args.artists,
            "--out", songs_path,
            *(["--refresh"] if args.refresh_catalog else []),
        ], label="Step 0 discover")
        if rc != 0:
            return rc

    if not songs_path:
        print("error: no song list available after discovery", file=sys.stderr)
        return 2

    cfg = yaml.safe_load(Path(songs_path).read_text(encoding="utf-8"))
    songs = [s["name"] for s in cfg.get("songs", [])]
    if not songs:
        print(f"\nNo pending songs in {songs_path} — nothing to do.")
        return 0

    def song_args() -> list[str]:
        out = []
        for s in songs:
            out += ["--song", s]
        return out

    steps: list[tuple[int, str, list[str]]] = [
        (1, "Step 1 fetch_lyrics", [
            py, "pipeline/01_fetch_lyrics.py", "--songs", songs_path,
            *(["--refresh-lyrics"] if args.refresh_lyrics else []),
        ]),
        (2, "Step 2 translate", [
            py, "pipeline/02_translate.py", *song_args(),
            *(["--force-step", "2"] if args.force_step == 2 else []),
        ]),
        (3, "Step 3 gen_prompts", [
            py, "pipeline/03_gen_prompts.py",
            "--workspace", args.workspace,
            "--batch", args.batch,
            *(["--variant", args.variant] if args.variant else []),
            *song_args(),
            *(["--force-step", "3"] if args.force_step == 3 else []),
        ]),
        (4, "Step 4 suno_generate", [
            py, "pipeline/04_suno_generate.py",
            "--workspace", args.workspace,
            "--batch", args.batch,
            "--mode", args.mode,
            *(["--variant", args.variant] if args.variant else []),
            *song_args(),
        ]),
        (5, "Step 5 suno_download", [
            py, "pipeline/05_suno_download.py",
            "--job", f"{args.batch}_{args.workspace}",
        ]),
    ]

    for step_no, label, cmd in steps:
        if step_no in args.skip_step:
            print(f"\n[skip] {label}")
            continue
        rc = run(cmd, label=label)
        if rc != 0:
            print(f"\nStopping at {label}. Fix the error and re-run (idempotent).", file=sys.stderr)
            return rc

    print("\nAll stages complete. Now manually curate an album:")
    print(f"  python pipeline/make_album.py --name <album> --workspace {args.workspace} --add ...")
    print(f"  python pipeline/07_gen_youtube_desc.py --album <album>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
