"""Step 0: discover an artist's songs by scraping KKBox, merge into catalog.

For each artist in artist_list.yaml:
  1. Search KKBox for the artist's name → land on the artist page.
  2. Scrape the artist's songs list (title + song URL).
  3. Merge into data/catalog/{artist_slug}.json. Existing entries keep their
     status; new titles get appended.
  4. Recompute the 'submitted' flag from suno_submissions.jsonl.
  5. Emit song_list_pending.yaml of songs NOT yet submitted (≤ artist.limit).

Run:
    python pipeline/00_discover.py --artists artist_list.yaml \
        --out song_list_pending.yaml
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import catalog as cat_lib, paths  # noqa: E402

KKBOX_BASE = "https://www.kkbox.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}


def search_artist_url(name: str) -> str | None:
    """Find a KKBox artist page URL by searching for the artist's name."""
    url = f"{KKBOX_BASE}/tw/tc/search?q={quote_plus(name)}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    # An artist link looks like /tw/tc/artist/<id> (no trailing path).
    for a in soup.select('a[href^="/tw/tc/artist/"]'):
        href = a.get("href", "").split("?", 1)[0]
        parts = [p for p in href.split("/") if p]
        # /tw/tc/artist/<id>   -> ["tw","tc","artist","<id>"]  (4 segments)
        if len(parts) == 4 and parts[2] == "artist":
            return urljoin(KKBOX_BASE, href)
    return None


def fetch_artist_songs(artist_url: str, max_songs: int = 100) -> list[tuple[str, str]]:
    """Return [(title, song_url), ...] for the artist. Stops at max_songs."""
    # KKBox usually has both /artist/<id> and /artist/<id>/songs — try both.
    tried = []
    for path in ("/songs", ""):
        u = artist_url + path
        tried.append(u)
        r = requests.get(u, headers=HEADERS, timeout=20)
        if not r.ok:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for a in soup.select('a[href^="/tw/tc/song/"]'):
            href = a.get("href", "").split("?", 1)[0]
            song_url = urljoin(KKBOX_BASE, href)
            if song_url in seen:
                continue
            # Title: <a>'s text, or a child element's text. KKBox markup varies.
            title = a.get_text(strip=True)
            if not title:
                el = a.find(["span", "div", "h1", "h2", "h3"])
                if el:
                    title = el.get_text(strip=True)
            if not title:
                title = (a.get("title") or "").strip()
            if not title:
                continue
            seen.add(song_url)
            out.append((title, song_url))
            if len(out) >= max_songs:
                break
        if out:
            return out
    raise RuntimeError(
        f"could not extract song list from artist page. tried: {tried}. "
        "KKBox markup may have changed — F12 the artist page and update the "
        "selectors in pipeline/00_discover.py:fetch_artist_songs()."
    )


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--artists", required=True, help="path to artist_list.yaml")
    p.add_argument("--out", default="song_list_pending.yaml",
                   help="write pending song list here (consumed by Step 1)")
    p.add_argument("--refresh", action="store_true",
                   help="re-scrape KKBox even if catalog already exists for this artist")
    p.add_argument("--max-fetch", type=int, default=200,
                   help="cap how many songs we scrape per artist (default 200)")
    p.add_argument("--sleep", type=float, default=1.5, help="seconds between KKBox requests")
    args = p.parse_args()

    paths.ensure_all()
    cfg = yaml.safe_load(Path(args.artists).read_text(encoding="utf-8"))
    artists = cfg.get("artists", [])
    if not artists:
        print("no artists in yaml", file=sys.stderr)
        return 2

    pending_for_yaml: list[dict] = []

    for a in artists:
        slug = a["slug"]
        name = a["display_name"]
        limit = int(a.get("limit", 20))
        # Allow user to skip the search step by hardcoding the artist URL.
        artist_url_override = a.get("kkbox_url")

        existing = cat_lib.load(slug)

        if existing and not args.refresh:
            print(f"[skip] {slug}: catalog exists, recomputing submitted only "
                  f"(use --refresh to re-scrape KKBox)")
            titles_and_urls: list[tuple[str, str]] = []
        else:
            try:
                if artist_url_override:
                    artist_url = artist_url_override
                    print(f"[ws  ] {slug}: using override kkbox_url={artist_url}")
                else:
                    print(f"[srch] {slug}: searching KKBox for '{name}'")
                    artist_url = search_artist_url(name)
                    if not artist_url:
                        raise RuntimeError("no KKBox artist page found")
                    print(f"        -> {artist_url}")
                time.sleep(args.sleep)
                print(f"[scrp] {slug}: fetching songs (max {args.max_fetch})")
                titles_and_urls = fetch_artist_songs(artist_url, max_songs=args.max_fetch)
                print(f"[ok  ] {slug}: got {len(titles_and_urls)} song(s) from KKBox")
                time.sleep(args.sleep)
            except Exception as e:  # noqa: BLE001
                print(f"[fail] {slug}: {e}", file=sys.stderr)
                continue

        # merge_discoveries currently takes (title, year) tuples. We carry
        # KKBox URL separately and stash it on the catalog after merging.
        titles = [(t, None) for t, _ in titles_and_urls]
        urls_by_title = {t: u for t, u in titles_and_urls}

        cat = cat_lib.merge_discoveries(name, slug, titles)
        # Attach kkbox_url to any song that has one
        for s in cat.songs:
            if not s.kkbox_url and s.title in urls_by_title:
                s.kkbox_url = urls_by_title[s.title]
        cat_lib.save(cat)

        pending = cat_lib.pending_songs(cat, limit=limit)
        print(f"[plan] {slug}: catalog has {len(cat.songs)} total, "
              f"{len(pending)} pending (taking {min(len(pending), limit)})")

        for s in pending:
            pending_for_yaml.append({
                "name":      s.slug,
                "title":     s.title,
                "artist":    cat.artist,
                "kkbox_url": s.kkbox_url,
            })

    if not pending_for_yaml:
        print("\nNothing pending. (Catalogs may already be fully submitted.)")
        Path(args.out).write_text(yaml.safe_dump({"songs": []}, allow_unicode=True),
                                  encoding="utf-8")
        return 0

    Path(args.out).write_text(
        yaml.safe_dump({"songs": pending_for_yaml}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"\nWrote {args.out} with {len(pending_for_yaml)} pending songs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
