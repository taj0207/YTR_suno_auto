"""Step 3: turn translated lyrics into Suno production-note prompts via Gemini.

Reads:  data/lyrics/en/{song}.txt
Writes: data/prompts/{batch}/{song}_{variant}.txt  (+ .hash sidecar)

Dedup key (see docs/contracts.md):
    sha256( lyrics_en, template_text, workspace_config_yaml )

Run:
    python pipeline/03_gen_prompts.py \
        --workspace billie_eilish_depressed \
        --song lingering_perfection \
        --song wheel_hush \
        --variant 3_2 \
        --batch 2026-05-17

  --force-step 3   ignore cache, regenerate
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from dotenv import load_dotenv

# Allow `python pipeline/03_gen_prompts.py` invocation
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline._lib import dedup, gemini, paths, workspace as ws_lib  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", required=True)
    p.add_argument(
        "--song",
        action="append",
        default=[],
        help="song stem (filename without .txt). repeat for multiple.",
    )
    p.add_argument(
        "--variant",
        default=None,
        help="'3_1' or '3_2'. defaults to workspace.default_prompt_variant.",
    )
    p.add_argument(
        "--batch",
        default=dt.date.today().isoformat(),
        help="batch folder name under data/prompts/. defaults to today's date.",
    )
    p.add_argument("--force-step", type=int, default=0, help="set to 3 to ignore cache.")
    return p.parse_args()


def render_one(ws: ws_lib.Workspace, variant: str, lyrics_en: str) -> tuple[str, str]:
    """Return (rendered_prompt, hash_digest)."""
    template_text = ws.template_text(variant)
    config_text = ws.config_text()
    digest = dedup.content_hash([lyrics_en, template_text, config_text])
    rendered = ws.render(variant, lyrics_en=lyrics_en)
    return rendered, digest


def main() -> int:
    load_dotenv()
    args = parse_args()
    paths.ensure_all()

    ws = ws_lib.load(args.workspace)
    variant = args.variant or ws.default_prompt_variant
    force = args.force_step == 3

    if not args.song:
        print("error: no --song specified", file=sys.stderr)
        return 2

    n_skipped = 0
    n_written = 0
    n_failed = 0

    for song in args.song:
        lyrics_path = paths.lyric_en(song)
        if not lyrics_path.exists():
            print(f"[skip] {song}: missing {lyrics_path}", file=sys.stderr)
            n_failed += 1
            continue

        lyrics_en = lyrics_path.read_text(encoding="utf-8").strip()
        rendered, digest = render_one(ws, variant, lyrics_en)

        out_path = paths.prompt_file(args.batch, song, variant)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if not force and dedup.is_cached(out_path, digest):
            print(f"[skip] {song}: cache hit")
            n_skipped += 1
            continue

        try:
            print(f"[gen ] {song}: calling Gemini ({variant})...")
            response = gemini.generate(rendered)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {song}: {e}", file=sys.stderr)
            n_failed += 1
            continue

        out_path.write_text(response, encoding="utf-8")
        dedup.write_hash(out_path, digest)
        # Save the prompt we sent to Gemini next to the response so we can
        # audit "what we asked" vs "what we got" without re-rendering.
        in_path = out_path.with_suffix(".input.txt")
        in_path.write_text(rendered, encoding="utf-8")
        print(f"[ok  ] {song}: wrote {out_path} ({len(response)} chars), input -> {in_path.name}")
        n_written += 1

    print(
        f"\nSummary: {n_written} written, {n_skipped} skipped (cached), {n_failed} failed.",
        file=sys.stderr,
    )
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
