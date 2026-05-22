"""Step 2: translate Chinese lyrics to English via Google Translate REST API.

Auth: API key from GOOGLE_TRANSLATE_API_KEY (.env)
Endpoint: https://translation.googleapis.com/language/translate/v2

Reads:  data/lyrics/raw/{song}.txt
Writes: data/lyrics/en/{song}.txt   (+ .hash sidecar)

Dedup: sha256(raw_text). If raw hasn't changed, skip.

Run:
    python pipeline/02_translate.py --song lingering_perfection --song wheel_hush
    python pipeline/02_translate.py --all
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import dedup, paths, progress  # noqa: E402

ENDPOINT = "https://translation.googleapis.com/language/translate/v2"


def translate(text: str, api_key: str, *, source: str = "zh-TW", target: str = "en") -> str:
    r = requests.post(
        ENDPOINT,
        params={"key": api_key},
        data={"q": text, "source": source, "target": target, "format": "text"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data["data"]["translations"][0]["translatedText"]


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--song", action="append", default=[])
    p.add_argument("--all", action="store_true", help="translate every file in data/lyrics/raw/")
    p.add_argument("--force-step", type=int, default=0)
    args = p.parse_args()

    api_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
    if not api_key:
        print("GOOGLE_TRANSLATE_API_KEY not set", file=sys.stderr)
        return 2

    paths.ensure_all()

    if args.all:
        songs = [f.stem for f in sorted(paths.LYRICS_RAW.glob("*.txt"))]
    else:
        songs = args.song
    if not songs:
        print("nothing to translate (use --song or --all)", file=sys.stderr)
        return 2

    force = args.force_step == 2
    n_ok = n_skip = n_fail = 0

    prog = progress.StepProgress("Step 2 translate", len(songs))
    for song in songs:
        prog.next(song)
        src = paths.lyric_raw(song)
        if not src.exists():
            print(f"[skip] {song}: missing {src}", file=sys.stderr)
            n_fail += 1
            continue

        raw = src.read_text(encoding="utf-8")
        digest = dedup.content_hash([raw])
        out = paths.lyric_en(song)

        if not force and dedup.is_cached(out, digest):
            print(f"[skip] {song}: cache hit")
            n_skip += 1
            continue

        try:
            print(f"[trn ] {song}: translating {len(raw)} chars...")
            en = translate(raw, api_key)
            out.write_text(en, encoding="utf-8")
            dedup.write_hash(out, digest)
            print(f"[ok  ] {song}: wrote {out} ({len(en)} chars)")
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {song}: {e}", file=sys.stderr)
            n_fail += 1

    prog.done(ok=n_ok, failed=n_fail)
    print(f"Summary: {n_ok} ok, {n_skip} skipped, {n_fail} failed.", file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
