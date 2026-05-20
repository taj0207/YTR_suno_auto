"""Step 6: generate a YouTube post description for an album using Gemini.

Reads:
    data/albums/{name}/manifest.json
    workspaces/{ws}/config.yaml + prompt_7.j2

Writes:
    data/albums/{name}/youtube_description.txt   (+ .hash sidecar)

Dedup: sha256(canonical(album_manifest), template_text). Skip if unchanged.

Run:
    python pipeline/07_gen_youtube_desc.py --album star_etched_skin
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import dedup, gemini, paths, workspace as ws_lib  # noqa: E402


def pick_samples(tracks: list[dict], idx_a: int, idx_b: int) -> list[str]:
    sample_a = tracks[idx_a].get("suno_description") or ""
    sample_b = tracks[idx_b].get("suno_description") or ""
    return [s for s in (sample_a, sample_b) if s.strip()]


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--album", required=True)
    p.add_argument("--sample-tracks", default="1,2",
                   help="1-indexed track numbers to quote descriptions from. default '1,2'")
    p.add_argument("--force-step", type=int, default=0)
    args = p.parse_args()

    manifest_path = paths.album_manifest_path(args.album)
    if not manifest_path.exists():
        print(f"missing {manifest_path}. run make_album.py first.", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    ws = ws_lib.load(manifest["workspace"])
    template_text = ws.template_text("7")
    canonical = dedup.canonical_json(manifest)
    digest = dedup.content_hash([canonical, template_text])

    out_path = paths.album_dir(args.album) / "youtube_description.txt"
    if args.force_step != 6 and dedup.is_cached(out_path, digest):
        print(f"[skip] cache hit -> {out_path}")
        return 0

    a, b = (int(x) - 1 for x in args.sample_tracks.split(","))
    samples = pick_samples(manifest["tracks"], a, b)
    if len(samples) < 2:
        print(f"warning: only {len(samples)} sample description(s) usable", file=sys.stderr)

    rendered = ws.render(
        "7",
        sample_descriptions=samples,
        tracklist_with_timestamps=manifest["timestamps_for_youtube"],
        workspace_youtube=ws.config.get("youtube", {}),
    )

    print(f"[gen ] calling Gemini for {args.album}...")
    response = gemini.generate(rendered, temperature=0.8)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(response, encoding="utf-8")
    dedup.write_hash(out_path, digest)
    print(f"[ok  ] wrote {out_path} ({len(response)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
