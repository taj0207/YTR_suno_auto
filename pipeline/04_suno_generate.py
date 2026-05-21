"""Step 4: submit prompts to Suno (advanced vocal or simple instrumental).

Reads:
    data/prompts/{batch}/{song}_{variant}.txt   (from Step 3)
    workspaces/{ws}/config.yaml                 (for wid + default style)
    data/.cache/suno_submissions.jsonl          (dedup ledger)

Writes:
    data/jobs/{job}/generation_log.json
    appends to data/.cache/suno_submissions.jsonl

Dedup: prompt_hash = sha256(prompt_text, mode, wid). Skip if already submitted
       unless --regenerate.

Run:
    python pipeline/04_suno_generate.py \
        --workspace billie_eilish_depressed \
        --batch 2026-05-17 \
        --song lingering_perfection --song wheel_hush \
        --mode vocal

    # instrumental mode reuses a different prompt file convention if you like;
    # by default it sends the prompt body as gpt_description_prompt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import dedup, paths, suno, workspace as ws_lib  # noqa: E402

LYRICS_HEADER_RE = re.compile(r"(?im)^\s*(lyrics?|歌詞)\s*:?\s*$")
STYLES_HEADER_RE = re.compile(r"(?im)^\s*(styles?|style|風格)\s*:?\s*$")
NOTES_HEADER_RE = re.compile(r"(?im)^\s*(studio\s*production\s*notes?|production\s*notes?)\s*:?\s*$")
TITLE_LINE_RE = re.compile(r"(?im)^\s*title\s*[:：]\s*(.+?)\s*$")
# First lyric section header — used to strip Gemini preamble like
# "Here are the lyrics for a...:" that would otherwise be sung by Suno.
SECTION_HEADER_RE = re.compile(
    r"\[\s*(?:intro|verse|pre[-\s]?chorus|chorus|bridge|hook|refrain|"
    r"outro|drop|build[-\s]?up|breakdown|interlude|coda|instrumental|"
    r"post[-\s]?chorus)[^\]]*\]",
    re.IGNORECASE,
)


def split_vocal_prompt(text: str) -> tuple[str, str, str]:
    """Split a Gemini response into (title, lyrics, styles) for Suno.

    Strategy:
      1. Pull a 'Title: ...' line from the top if present.
      2. Look for a 'Studio Production Notes' header — content after it is the styles block.
      3. Everything above the notes header (and any 'Lyrics:' section above it) is lyrics.
      4. Strip any preamble before the first [Verse]/[Intro]/etc. section header
         — otherwise Suno will sing things like "Here are the lyrics...".
      5. Fallback: split at the last paragraph that looks like style descriptors.
    """
    # Extract title from the first matching "Title:" line (case-insensitive)
    title = ""
    m_title = TITLE_LINE_RE.search(text)
    if m_title:
        title = m_title.group(1).strip().strip('"\'')
        # Remove the title line from the body so it doesn't end up in lyrics
        text = (text[:m_title.start()] + text[m_title.end():]).strip()

    notes_match = NOTES_HEADER_RE.search(text)
    if notes_match:
        lyrics = text[:notes_match.start()].strip()
        styles = text[notes_match.end():].strip()
    else:
        parts = re.split(r"\n\s*\n", text.strip())
        if len(parts) >= 2:
            lyrics = "\n\n".join(parts[:-1]).strip()
            styles = parts[-1].strip()
        else:
            lyrics = text.strip()
            styles = ""

    lyrics = LYRICS_HEADER_RE.sub("", lyrics).strip()
    styles = STYLES_HEADER_RE.sub("", styles).strip()

    # Strip Gemini preamble: drop everything before the first section header.
    sec = SECTION_HEADER_RE.search(lyrics)
    if sec and sec.start() > 0:
        lyrics = lyrics[sec.start():].strip()

    return title, lyrics, styles


def load_submissions_index() -> dict[str, dict]:
    idx = {}
    for row in dedup.load_jsonl(paths.SUNO_SUBMISSIONS):
        idx[row["prompt_hash"]] = row
    return idx


def job_name(workspace: str) -> str:
    return f"{dt.date.today().isoformat()}_{workspace}"


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", required=True)
    p.add_argument("--batch", required=True)
    p.add_argument("--song", action="append", default=[])
    p.add_argument("--mode", choices=["vocal", "instrumental"], required=True)
    p.add_argument("--variant", default=None, help="prompt variant (3_1/3_2). default: workspace setting")
    p.add_argument("--job", default=None, help="job folder name (default: today_workspace)")
    p.add_argument("--regenerate", action="store_true", help="ignore Suno submission dedup")
    p.add_argument("--dry-run", action="store_true", help="don't actually submit, just print")
    args = p.parse_args()

    paths.ensure_all()
    ws = ws_lib.load(args.workspace)
    variant = args.variant or ws.default_prompt_variant
    job = args.job or job_name(args.workspace)
    job_dir = paths.job_dir(job)
    job_dir.mkdir(parents=True, exist_ok=True)

    if not args.song:
        print("error: no --song specified", file=sys.stderr)
        return 2

    sub_idx = load_submissions_index()

    # Load existing generation_log if resuming
    log_path = paths.generation_log_path(job)
    if log_path.exists():
        gen_log = json.loads(log_path.read_text(encoding="utf-8"))
    else:
        gen_log = {
            "job": job,
            "workspace": args.workspace,
            "created_at": dt.datetime.now().astimezone().isoformat(),
            "playlist_id": None,
            "tracks": [],
        }
    gen_log.setdefault("playlist_id", None)
    existing_ids = {t["song_id"] for t in gen_log["tracks"]}

    pending = []
    for song in args.song:
        prompt_path = paths.prompt_file(args.batch, song, variant)
        if not prompt_path.exists():
            print(f"[skip] {song}: missing {prompt_path}", file=sys.stderr)
            continue
        prompt_text = prompt_path.read_text(encoding="utf-8")
        prompt_hash = dedup.content_hash([prompt_text, args.mode, ws.wid])

        if not args.regenerate and prompt_hash in sub_idx:
            prior = sub_idx[prompt_hash]
            print(f"[cached] {song}: prompt already submitted, song_ids={prior['song_ids']}")
            continue

        pending.append((song, prompt_path, prompt_text, prompt_hash))

    if not pending:
        print("nothing to submit.")
        return 0

    if args.dry_run:
        for song, prompt_path, _, h in pending:
            print(f"[dry] would submit {song} ({args.mode}) hash={h[:12]} from {prompt_path}")
        return 0

    n_ok = n_fail = 0
    submit_delay_s = float(os.environ.get("SUNO_SUBMIT_DELAY_S", "5"))
    job_wait_max_s = float(os.environ.get("SUNO_JOB_WAIT_MAX_S", "300"))
    job_poll_s = float(os.environ.get("SUNO_JOB_POLL_S", "10"))
    in_flight: list[str] = []  # song_ids from the most recent submission

    def wait_for_jobs(ids: list[str]) -> None:
        if not ids:
            return
        print(f"[wait] for previous {len(ids)} variant(s) to finish (Suno 限制不允許並行) — polling /feed/v3...")
        deadline = time.monotonic() + job_wait_max_s
        while time.monotonic() < deadline:
            try:
                clips = client.fetch_feed(ids)
            except Exception as e:  # noqa: BLE001
                print(f"        feed poll error: {e}")
                time.sleep(job_poll_s)
                continue
            # Only count the clips we actually asked about.
            byid = {c.get("id"): c for c in clips if c.get("id") in set(ids)}
            done = 0
            shown = []
            for sid in ids:
                c = byid.get(sid)
                if c is None:
                    shown.append("missing")
                    continue
                s = (c.get("status") or "").lower()
                shown.append(s or "?")
                if suno.is_complete(c) or suno.is_failed(c):
                    done += 1
            print(f"        {done}/{len(ids)} done — {shown}")
            if done >= len(ids):
                return
            time.sleep(job_poll_s)
        print(f"[warn] previous job didn't complete within {job_wait_max_s:.0f}s, proceeding anyway")

    with suno.session(headless=True) as client:
        # Create (or reuse) the Suno playlist for this job
        if not gen_log.get("playlist_id") and os.environ.get("SUNO_NO_PLAYLIST") != "1":
            tpl_ctx = {
                "date":         dt.date.today().isoformat(),
                "batch":        args.batch,
                "workspace":    args.workspace,
                "display_name": ws.config.get("display_name", args.workspace),
                "mode":         args.mode,
                "job":          job,
            }
            name_tpl = ws.config.get("playlist_name_template", "YTR {job}")
            desc_tpl = ws.config.get(
                "playlist_description_template",
                "YTR_suno_auto batch — workspace {workspace}, batch {batch}, mode {mode}",
            )
            try:
                playlist_name = name_tpl.format(**tpl_ctx)
                playlist_desc = desc_tpl.format(**tpl_ctx)
            except KeyError as e:
                print(f"[plst] template error ({e}) — falling back to default name", file=sys.stderr)
                playlist_name = f"YTR {job}"
                playlist_desc = f"YTR_suno_auto batch — workspace {args.workspace}"
            try:
                pid = client.create_playlist(playlist_name, description=playlist_desc)
                gen_log["playlist_id"] = pid
                log_path.write_text(json.dumps(gen_log, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                print(f"[plst] created Suno playlist '{playlist_name}' id={pid}")
            except Exception as e:  # noqa: BLE001
                print(f"[plst] could not create playlist: {e}", file=sys.stderr)

        for idx, (song, prompt_path, prompt_text, prompt_hash) in enumerate(pending):
            if in_flight:
                wait_for_jobs(in_flight)
                in_flight = []
            elif idx > 0 and submit_delay_s > 0:
                time.sleep(submit_delay_s)
            try:
                if args.mode == "vocal":
                    title, lyrics, styles = split_vocal_prompt(prompt_text)
                    if not lyrics or not styles:
                        raise RuntimeError(
                            "could not split prompt into (lyrics, styles). "
                            "Check that the Gemini output has a 'Studio Production Notes' section."
                        )
                    # Always prepend the configured vocal qualifier so Suno
                    # respects gender even if Gemini's notes don't mention it.
                    vocal = (ws.config.get("vocal") or "").strip()
                    vocal_style = (ws.config.get("vocal_style") or "").strip()
                    voice_tag = f"{vocal_style} {vocal} vocal".strip().replace("  ", " ")
                    if voice_tag and voice_tag.lower() not in styles.lower()[:60]:
                        styles = f"{voice_tag}, {styles}"
                    persona_id = (ws.config.get("suno") or {}).get("persona_id")
                    vocal_gender_raw = (ws.config.get("vocal") or "").strip().lower()
                    suno_input = {"title": title, "lyrics": lyrics, "styles": styles,
                                  "persona_id": persona_id, "vocal_gender": vocal_gender_raw}
                    print(f"[gen ] {song}: vocal (title={title!r} voice={voice_tag!r} "
                          f"gender={vocal_gender_raw!r} persona_id={persona_id!r} "
                          f"lyrics={len(lyrics)} styles={len(styles)})")
                    song_ids = client.submit_vocal(lyrics=lyrics, styles=styles, title=title,
                                                   wid=ws.wid, persona_id=persona_id,
                                                   vocal_gender=vocal_gender_raw)
                else:
                    description = prompt_text.strip()
                    suno_input = {"description": description}
                    print(f"[gen ] {song}: instrumental (desc={len(description)})")
                    song_ids = client.submit_instrumental(description=description, wid=ws.wid)

                print(f"[ok  ] {song}: song_ids={song_ids}")
                in_flight = list(song_ids)
                now = dt.datetime.now().astimezone().isoformat()

                # Add the new clips to this job's Suno playlist
                pid = gen_log.get("playlist_id")
                if pid:
                    try:
                        client.add_to_playlist(pid, song_ids)
                        print(f"[plst] added {len(song_ids)} clip(s) to playlist {pid}")
                    except Exception as e:  # noqa: BLE001
                        print(f"[plst] add_to_playlist failed: {e}", file=sys.stderr)

                # Append to suno_submissions ledger
                dedup.append_jsonl(paths.SUNO_SUBMISSIONS, {
                    "prompt_hash":  prompt_hash,
                    "workspace":    args.workspace,
                    "wid":          ws.wid,
                    "mode":         args.mode,
                    "submitted_at": now,
                    "source_song":  song,
                    "prompt_file":  str(prompt_path),
                    "suno_input":   suno_input,
                    "song_ids":     song_ids,
                    "job":          job,
                })

                # Add tracks to generation_log
                for i, sid in enumerate(song_ids, start=1):
                    if sid in existing_ids:
                        continue
                    gen_log["tracks"].append({
                        "song_id":          sid,
                        "variant":          i,
                        "source_song":      song,
                        "mode":             args.mode,
                        "prompt_hash":      prompt_hash,
                        "prompt_file":     str(prompt_path),
                        "suno_input":       suno_input,
                        "status":           "pending",
                        "audio_url_mp3":    None,
                        "wav_status":       "not_requested",
                        "wav_url":          None,
                        "local_path":       None,
                        "size_bytes":       None,
                        "duration_sec":     None,
                        "suno_song_url":    f"https://suno.com/song/{sid}",
                        "suno_description": None,
                        "title":            None,
                        "last_attempt_at":  None,
                        "attempts":         0,
                        "error":            None,
                    })
                    existing_ids.add(sid)
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"[fail] {song}: {e}", file=sys.stderr)
                n_fail += 1

    log_path.write_text(json.dumps(gen_log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {log_path} ({len(gen_log['tracks'])} tracks total)")
    print(f"Summary: {n_ok} submitted, {n_fail} failed.", file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
