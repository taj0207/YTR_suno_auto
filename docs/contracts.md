# Data Contracts

Three JSON schemas + one hash spec. Every pipeline script reads/writes these — change them here first, never inline.

---

## 1. `suno_submissions.jsonl`

**Path:** `data/.cache/suno_submissions.jsonl`
**Writer:** Step 4 (`04_suno_generate.py`)
**Reader:** Step 4 (dedup), debugging
**Purpose:** Global append-only ledger of every Suno submission. Prevents re-submitting the same prompt (Suno credits cost real money).

One JSON object per line:

```json
{
  "prompt_hash":   "a1b2c3...",
  "workspace":     "billie_eilish_depressed",
  "wid":           "84cca4a7-1ed8-43eb-984a-94d451900800",
  "mode":          "vocal",
  "submitted_at":  "2026-05-17T14:23:01+08:00",
  "source_song":   "lingering_perfection",
  "prompt_file":   "data/prompts/2026-05-17/lingering_perfection_3_2.txt",
  "suno_input":    { "lyrics": "...", "styles": "...", "description": null },
  "song_ids":      ["abc-uuid", "def-uuid"],
  "job":           "2026-05-17_billie_eilish_depressed"
}
```

| Field | Type | Note |
|---|---|---|
| `prompt_hash` | hex string | See **Hash spec** below. Dedup key. |
| `workspace` | string | Workspace folder name. |
| `wid` | uuid string | Suno workspace id (from workspace `config.yaml`). |
| `mode` | `"vocal"` \| `"instrumental"` | |
| `submitted_at` | ISO 8601 with tz | |
| `source_song` | string | Filename stem in `data/lyrics/`. |
| `prompt_file` | path | Relative to repo root. |
| `suno_input` | object | Exact fields sent to Suno (`description` for instrumental, `lyrics` + `styles` for vocal). |
| `song_ids` | array of uuid | Suno returns N variants (usually 2). |
| `job` | string | Which job folder this submission landed in. |

**Dedup rule:** before submitting, scan jsonl for matching `prompt_hash`. If found and `--regenerate` not set → skip and reuse existing `song_ids`.

---

## 2. `generation_log.json`

**Path:** `data/jobs/{job}/generation_log.json`
**Writer:** Step 4 (initial), Step 5 (status + wav fields)
**Reader:** Step 5, Step 5.5 (`make_album.py`)
**Purpose:** Per-job record of every candidate variant produced and its download state.

```json
{
  "job":           "2026-05-17_billie_eilish_depressed",
  "workspace":     "billie_eilish_depressed",
  "created_at":    "2026-05-17T14:23:01+08:00",
  "tracks": [
    {
      "song_id":          "abc-uuid",
      "variant":          1,
      "source_song":      "lingering_perfection",
      "mode":             "vocal",
      "prompt_hash":      "a1b2c3...",
      "prompt_file":      "data/prompts/2026-05-17/lingering_perfection_3_2.txt",
      "suno_input":       { "lyrics": "...", "styles": "..." },
      "status":           "complete",
      "audio_url_mp3":    "https://cdn1.suno.ai/abc.mp3",
      "wav_status":       "ready",
      "wav_url":          "https://cdn1.suno.ai/abc.wav",
      "local_path":       "downloads/abc-uuid_v1.wav",
      "size_bytes":       41234567,
      "duration_sec":     305,
      "suno_song_url":    "https://suno.com/song/abc-uuid",
      "suno_description": "Vocal Texture: ...",
      "title":            "Lingering Perfection",
      "last_attempt_at":  "2026-05-17T14:48:11+08:00",
      "attempts":         1,
      "error":            null
    }
  ]
}
```

### Status state machines

`status` (Suno generation):
```
pending → complete | failed
```

`wav_status` (WAV export, only meaningful when status == complete):
```
not_requested → requested → ready
                         → timeout    (retried next run)
                         → failed     (retried next run)
                         → forbidden  (sticky — no Pro subscription)
```

| Field | Note |
|---|---|
| `song_id` | Suno's id. Unique across all jobs. |
| `variant` | Suno returns 2 per prompt by default. |
| `local_path` | Relative to job folder. `null` until WAV downloaded. |
| `last_attempt_at` / `attempts` | For retry-failed logic in Step 5. |
| `error` | Last error message if `wav_status` in {timeout, failed}. |
| `title` | Suno auto-titles; copied here for album manifest convenience. |

**Step 5 retry rule (default):** every run reprocesses every track where
`wav_status in {timeout, failed}` (NOT `forbidden`, which is sticky).

---

## 3. `album manifest.json`

**Path:** `data/albums/{album}/manifest.json`
**Writer:** Step 5.5 (`make_album.py`)
**Reader:** Step 6 (`07_gen_youtube_desc.py`)
**Purpose:** Curated subset of generated tracks forming a publishable album. Tracks may come from multiple jobs.

```json
{
  "album_name":  "star_etched_skin",
  "workspace":   "billie_eilish_depressed",
  "created_at":  "2026-05-17T18:00:00+08:00",
  "tracks": [
    {
      "track_no":         1,
      "title":            "Lingering Perfection",
      "src_job":          "2026-05-17_billie_eilish_depressed",
      "src_song_id":      "abc-uuid",
      "src_variant":      2,
      "local_path":       "tracks/01_lingering_perfection.wav",
      "duration_sec":     305,
      "suno_song_url":    "https://suno.com/song/abc-uuid",
      "suno_description": "Vocal Texture: ..."
    }
  ],
  "timestamps_for_youtube": [
    "00:00 1. Lingering Perfection",
    "05:05 2. Wheel Hush"
  ]
}
```

| Field | Note |
|---|---|
| `tracks[].local_path` | Relative to album folder. Real file lives in job folder; album folder uses symlink (or copy on Windows). |
| `tracks[].suno_description` / `suno_song_url` | Copied from `generation_log.json` at album-creation time so the album is self-contained. |
| `timestamps_for_youtube` | Pre-computed running-time tracklist for the YouTube description prompt. |

**Step 5.5 invariants:**
- Every `src_song_id` must point to a track in `generation_log.json` with `wav_status == "ready"` and a non-null `local_path`.
- No duplicate `src_song_id` within an album.
- `track_no` is contiguous starting at 1.

---

## 4. Hash spec

Used to keep dedup decisions reproducible across runs and machines.

```python
def content_hash(parts: list[str | bytes]) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, str):
            p = p.encode("utf-8")
        h.update(b"\x1f")        # unit separator between parts
        h.update(p)
    return h.hexdigest()
```

Always feed a **list of parts** (not one concatenated string) so that
`["abc", "def"]` and `["ab", "cdef"]` hash to different values.

### Per-step inputs

| Step | Hash input parts | Stored at |
|---|---|---|
| 2 translate | `[raw_chinese_text]` | `data/lyrics/en/{song}.txt.hash` |
| 3 prompt-gen | `[lyrics_en, template_text, workspace_config_yaml]` | `data/prompts/{batch}/{song}_3_{n}.txt.hash` |
| 4 Suno submit | `[prompt_text, mode, wid]` | `prompt_hash` in `suno_submissions.jsonl` |
| 6 YouTube desc | `[album_manifest_json_canonical, template_text]` | `data/albums/{album}/youtube_description.txt.hash` |

Canonical JSON for hashing: `json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.

---

## 5. `artist catalog.json`

**Path:** `data/catalog/{artist_slug}.json`
**Writer:** Step 0 (`00_discover.py`)
**Reader:** Step 0 (merge with prior state), Step 1 (`--artist` mode)
**Purpose:** Per-artist registry of songs we've ever seen for this artist, with
status. Survives across runs. Step 0 merges new discoveries in; existing
entries keep their status.

```json
{
  "artist":       "陳奕迅",
  "artist_slug":  "eason_chan",
  "discovered_at": "2026-05-17T...",
  "songs": [
    {
      "slug":          "fuji_san_xia",
      "title":         "富士山下",
      "year":          2006,
      "first_seen_at": "2026-05-17T...",
      "submitted":     true,
      "submitted_at":  "2026-05-17T...",
      "jobs":          ["2026-05-17_billie_eilish_depressed"]
    }
  ]
}
```

| Field | Note |
|---|---|
| `slug` | filesystem-safe identifier (same one used as `data/lyrics/raw/{slug}.txt`). Generated by ASCII-slugifying the title; if collision, append a counter. |
| `submitted` | true iff this song appears in `suno_submissions.jsonl` with matching `source_song`. Step 0 recomputes this every run by scanning the ledger. |
| `jobs` | list of jobs that have submitted this song. |

### Step 0 algorithm

```
load_catalog(artist_slug)                     # may be empty
artist_url = artist.kkbox_url OR search_kkbox(artist.display_name)
new_titles_and_urls = scrape_artist_songs(artist_url, max=max_fetch)

for (title, song_url) in new_titles_and_urls:
    if title not in catalog.songs:
        catalog.songs.append({
            slug:        slugify(title),
            title,
            kkbox_url:   song_url,
            first_seen_at: now,
            submitted:   false,
        })
    else:
        # backfill kkbox_url onto existing entry if it didn't have one
        existing.kkbox_url = existing.kkbox_url or song_url

# Recompute submitted flag from suno_submissions.jsonl
done_slugs = {row.source_song for row in suno_submissions}
for s in catalog.songs:
    s.submitted = s.slug in done_slugs
    s.jobs = [row.job for row in suno_submissions if row.source_song == s.slug]

save_catalog(catalog)

pending = [s for s in catalog.songs if not s.submitted][:artist.limit]
emit song_list_pending.yaml with kkbox_url attached so Step 1 doesn't re-search
```

## File layout reminder

```
data/
├── catalog/{artist_slug}.json             # per-artist song registry
├── lyrics/
│   ├── raw/{song}.txt
│   ├── en/{song}.txt           (+ .hash)
│   └── aggregated/{batch}.docx
├── prompts/{batch}/{song}_3_{1,2}.txt    (+ .hash)
├── jobs/{job}/
│   ├── generation_log.json
│   └── downloads/{song_id}_v{n}.wav
├── albums/{album}/
│   ├── manifest.json
│   ├── tracks/{NN}_{slug}.wav
│   └── youtube_description.txt          (+ .hash)
└── .cache/
    └── suno_submissions.jsonl
```
