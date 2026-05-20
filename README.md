# YTR_suno_auto

Pipeline: artist → discover songs → KKBox partial lyrics → translate → AI production-note prompt → Suno generate → WAV download → curate album → YouTube description.

See `docs/contracts.md` for data schemas.

## Pipeline stages

| Step | Script | Output |
|---|---|---|
| 0 | `pipeline/00_discover.py` | `data/catalog/{artist_slug}.json` + `song_list_pending.yaml` |
| 1 | `pipeline/01_fetch_lyrics.py` | `data/lyrics/raw/{song}.txt` |
| 1.5 | `pipeline/02b_make_docx.py` | `data/lyrics/aggregated/{batch}.docx` |
| 2 | `pipeline/02_translate.py` | `data/lyrics/en/{song}.txt` |
| 3 | `pipeline/03_gen_prompts.py` | `data/prompts/{batch}/{song}_3_{1,2}.txt` |
| 4 | `pipeline/04_suno_generate.py` | `data/jobs/{date_ws}/generation_log.json` |
| 5 | `pipeline/05_suno_download.py` | `data/jobs/{date_ws}/downloads/*.wav` |
| 5.5 | `pipeline/make_album.py` | `data/albums/{name}/manifest.json` |
| 6 | `pipeline/07_gen_youtube_desc.py` | `data/albums/{name}/youtube_description.txt` |

Step 0 takes an artist list and emits only songs NOT yet submitted to Suno (checked against `data/.cache/suno_submissions.jsonl`). Each subsequent run picks up where the previous left off.

`pipeline/run_all.py` runs Step 0→5 in order. All steps are idempotent (hash-based dedup); Step 5 auto-retries previously-failed WAV downloads.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env  # fill in keys
python scripts\setup_suno_auth.py  # manual Suno login -> secrets/suno_storage_state.json
```

## Run

Edit `artist_list.yaml` (copy from `artist_list.example.yaml`) with the artists and per-artist `limit` (how many *new* songs to process per run). Then:

```powershell
python pipeline\run_all.py --workspace billie_eilish_depressed --artists artist_list.yaml --mode vocal
```

Re-running picks up where you left off — Step 0 filters out songs already submitted to Suno. To re-ask Gemini for an artist's catalog, add `--refresh-catalog`.

To bypass Step 0 entirely with a hand-picked song list:

```powershell
python pipeline\run_all.py --workspace billie_eilish_depressed --songs song_list.yaml --mode vocal
```
