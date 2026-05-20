"""Per-artist song catalog. Survives across runs; merges new discoveries in.

See docs/contracts.md section 5 for the schema.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import dedup, paths

CATALOG_DIR = paths.DATA_ROOT / "catalog"


@dataclass
class Song:
    slug: str
    title: str
    year: int | None = None
    kkbox_url: str | None = None
    first_seen_at: str = ""
    submitted: bool = False
    submitted_at: str | None = None
    jobs: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    artist: str
    artist_slug: str
    discovered_at: str
    songs: list[Song]


def _path(artist_slug: str) -> Path:
    return CATALOG_DIR / f"{artist_slug}.json"


def load(artist_slug: str) -> Catalog | None:
    p = _path(artist_slug)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return Catalog(
        artist=data["artist"],
        artist_slug=data["artist_slug"],
        discovered_at=data.get("discovered_at", ""),
        songs=[Song(**s) for s in data.get("songs", [])],
    )


def save(catalog: Catalog) -> Path:
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(catalog.artist_slug)
    p.write_text(
        json.dumps(
            {
                "artist": catalog.artist,
                "artist_slug": catalog.artist_slug,
                "discovered_at": catalog.discovered_at,
                "songs": [s.__dict__ for s in catalog.songs],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


_NON_ASCII = re.compile(r"[^a-z0-9]+")


def slugify(text: str, existing: set[str] | None = None) -> str:
    """ASCII-slugify a (probably Chinese) title. On collision, suffix _2, _3…"""
    # If text is mostly Chinese, slugify can produce empty — fall back to a hash.
    base = _NON_ASCII.sub("_", text.lower().strip("_")).strip("_")
    if not base:
        base = "song_" + dedup.content_hash([text])[:8]
    if existing is None or base not in existing:
        return base
    for i in range(2, 1000):
        candidate = f"{base}_{i}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("too many slug collisions")


def submitted_index() -> dict[str, list[str]]:
    """Read suno_submissions.jsonl → {source_song: [job, job, ...]}."""
    out: dict[str, list[str]] = {}
    for row in dedup.load_jsonl(paths.SUNO_SUBMISSIONS):
        src = row.get("source_song")
        job = row.get("job")
        if not src:
            continue
        out.setdefault(src, []).append(job)
    return out


def merge_discoveries(
    artist: str,
    artist_slug: str,
    titles: list[tuple[str, int | None]],
) -> Catalog:
    """Load existing catalog (or start empty), add new titles, recompute submitted flag."""
    existing = load(artist_slug)
    if existing:
        songs = list(existing.songs)
    else:
        songs = []
    by_slug = {s.slug: s for s in songs}
    now = dt.datetime.now().astimezone().isoformat()

    for title, year in titles:
        # If title already present (by exact title match), skip
        if any(s.title == title for s in songs):
            continue
        slug = slugify(title, existing={s.slug for s in songs})
        new_song = Song(
            slug=slug,
            title=title,
            year=year,
            first_seen_at=now,
        )
        songs.append(new_song)
        by_slug[slug] = new_song

    # Recompute submitted from ledger
    sub_idx = submitted_index()
    for s in songs:
        s.jobs = sub_idx.get(s.slug, [])
        s.submitted = bool(s.jobs)
        if s.submitted and not s.submitted_at:
            s.submitted_at = now

    cat = Catalog(
        artist=artist,
        artist_slug=artist_slug,
        discovered_at=now,
        songs=songs,
    )
    save(cat)
    return cat


def pending_songs(cat: Catalog, limit: int | None = None) -> list[Song]:
    out = [s for s in cat.songs if not s.submitted]
    if limit:
        out = out[:limit]
    return out
