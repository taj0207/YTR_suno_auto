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
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import catalog as cat_lib, paths, suno_bridge  # noqa: E402

KKBOX_BASE = "https://www.kkbox.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}


def _waf_blocked(html: str) -> bool:
    return ("awsWafCookieDomainList" in html or
            "AwsWafIntegration" in html or
            len(html) < 200)


def kkbox_get(url: str) -> str:
    """GET a KKBox URL. Plain requests first; if WAF blocks (now common on
    artist pages too), route through the YTR Suno Bridge extension so the
    request runs as a real navigation in a kkbox.com tab.

    Forces UTF-8 decoding — KKBox serves UTF-8 HTML but doesn't always
    declare charset in Content-Type, which makes requests default to
    ISO-8859-1 and turns Chinese titles into mojibake.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.encoding = "utf-8"
        if r.ok and not _waf_blocked(r.text):
            return r.text
    except Exception:
        pass
    # Fall back to bridge / tab-navigate
    try:
        bridge = suno_bridge.get_bridge()
        bridge.wait_for_extension(timeout=30)
    except suno_bridge.SunoBridgeError as e:
        raise RuntimeError(
            f"KKBox blocked by WAF and bridge unavailable: {e}. "
            "Install the YTR Suno Bridge extension and keep Chrome open."
        ) from e
    status, _hdrs, body = bridge.fetch(url, method="GET", timeout=60)
    html = body.decode("utf-8", errors="replace")
    if status != 200 or _waf_blocked(html):
        raise RuntimeError(
            f"KKBox still WAF-blocking via bridge (status={status}). "
            "Open https://www.kkbox.com/ in your Chrome once, solve any "
            "CAPTCHA, then retry."
        )
    return html


def _path_segments(href: str) -> list[str]:
    """Get path segments from a KKBox link (may be relative or absolute)."""
    parsed = urlparse(href)
    path = parsed.path  # e.g. "/tw/tc/artist/<id>"
    return [seg for seg in path.split("/") if seg]


def search_artist_url(name: str) -> str | None:
    """Find a KKBox artist page URL by searching for the artist's name."""
    url = f"{KKBOX_BASE}/tw/tc/search?q={quote_plus(name)}"
    html = kkbox_get(url)
    soup = BeautifulSoup(html, "lxml")
    # Artist link path: /tw/tc/artist/<id>  (4 segments). Anchor may use
    # absolute (https://www.kkbox.com/...) or relative href.
    for a in soup.select('a[href*="/tw/tc/artist/"]'):
        href = (a.get("href") or "").split("?", 1)[0]
        segs = _path_segments(href)
        if len(segs) == 4 and segs[:3] == ["tw", "tc", "artist"]:
            return urljoin(KKBOX_BASE, href) if href.startswith("/") else href
    return None


def _extract_songs_from_html(html: str, seen: set[str],
                             out: list[tuple[str, str]],
                             max_songs: int) -> None:
    """Append (title, song_url) pairs extracted from `html` to `out`, in order,
    skipping ones already in `seen`. Mutates seen + out in place."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select('a[href*="/tw/tc/song/"]'):
        if len(out) >= max_songs:
            return
        href = (a.get("href") or "").split("?", 1)[0]
        segs = _path_segments(href)
        if len(segs) < 4 or segs[:3] != ["tw", "tc", "song"]:
            continue
        song_url = urljoin(KKBOX_BASE, href) if href.startswith("/") else href
        if song_url in seen:
            continue
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


def _extract_album_urls(html: str) -> list[str]:
    """Find /tw/tc/album/<id> links from an artist page (overview or albums tab)."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/tw/tc/album/"]'):
        href = (a.get("href") or "").split("?", 1)[0]
        segs = _path_segments(href)
        if len(segs) < 4 or segs[:3] != ["tw", "tc", "album"]:
            continue
        url = urljoin(KKBOX_BASE, href) if href.startswith("/") else href
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def fetch_artist_songs(artist_url: str, max_songs: int = 200,
                       sleep_s: float = 1.0) -> list[tuple[str, str]]:
    """Return [(title, song_url), ...] for an artist.

    KKBox's artist overview page only shows ~9 'top' songs. To get the full
    catalog we also walk every album the artist has and scrape its track list.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    overview = kkbox_get(artist_url)
    _extract_songs_from_html(overview, seen, out, max_songs)
    initial_n = len(out)

    # Find every album linked from the overview, plus any extra albums on
    # the dedicated /album sub-page when KKBox uses one.
    album_urls = _extract_album_urls(overview)
    try:
        time.sleep(sleep_s)
        albums_page = kkbox_get(artist_url.rstrip("/") + "/album")
        for u in _extract_album_urls(albums_page):
            if u not in album_urls:
                album_urls.append(u)
    except Exception:
        pass  # /album subpage is optional

    print(f"        discovered {len(album_urls)} album(s); top-9 gave {initial_n} song(s) — "
          f"walking albums for the rest")

    for i, album_url in enumerate(album_urls, start=1):
        if len(out) >= max_songs:
            break
        try:
            time.sleep(sleep_s)
            html = kkbox_get(album_url)
        except Exception as e:  # noqa: BLE001
            print(f"        [warn] album {i}/{len(album_urls)} {album_url}: {e}")
            continue
        before = len(out)
        _extract_songs_from_html(html, seen, out, max_songs)
        added = len(out) - before
        if added:
            print(f"        album {i}/{len(album_urls)}: +{added} song(s) "
                  f"(total {len(out)})")

    if not out:
        raise RuntimeError(
            f"could not extract song list from {artist_url}. "
            "KKBox markup may have changed — F12 the page and look for "
            "<a href=\".../song/...\"> elements."
        )
    return out


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
