"""Canonical paths for pipeline artifacts. Centralised so renames are one-line."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("DATA_ROOT", REPO_ROOT / "data"))

LYRICS_RAW = DATA_ROOT / "lyrics" / "raw"
LYRICS_EN = DATA_ROOT / "lyrics" / "en"
LYRICS_AGG = DATA_ROOT / "lyrics" / "aggregated"
PROMPTS = DATA_ROOT / "prompts"
JOBS = DATA_ROOT / "jobs"
ALBUMS = DATA_ROOT / "albums"
CACHE = DATA_ROOT / ".cache"
SUNO_SUBMISSIONS = CACHE / "suno_submissions.jsonl"

WORKSPACES = REPO_ROOT / "workspaces"
SECRETS = REPO_ROOT / "secrets"


def lyric_raw(song: str) -> Path:
    return LYRICS_RAW / f"{song}.txt"


def lyric_en(song: str) -> Path:
    return LYRICS_EN / f"{song}.txt"


def prompt_file(batch: str, song: str, variant: str) -> Path:
    return PROMPTS / batch / f"{song}_{variant}.txt"


def job_dir(job: str) -> Path:
    return JOBS / job


def generation_log_path(job: str) -> Path:
    return job_dir(job) / "generation_log.json"


def job_downloads(job: str) -> Path:
    return job_dir(job) / "downloads"


def album_dir(album: str) -> Path:
    return ALBUMS / album


def album_manifest_path(album: str) -> Path:
    return album_dir(album) / "manifest.json"


def ensure_all() -> None:
    for d in (LYRICS_RAW, LYRICS_EN, LYRICS_AGG, PROMPTS, JOBS, ALBUMS, CACHE, SECRETS):
        d.mkdir(parents=True, exist_ok=True)
