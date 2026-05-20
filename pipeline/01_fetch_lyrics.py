"""Step 1: fetch partial Chinese lyrics from KKBox.

Reads:  a song_list.yaml like:
    songs:
      - name: lingering_perfection        # slug used as filename
        title: "戀無可戀"                   # Chinese title used for search
        artist: "陳奕迅"                    # optional, only used as fallback search
        kkbox_url: "https://..."           # optional — skips search entirely
Writes: data/lyrics/raw/{name}.txt

When kkbox_url is present (typical, since Step 0 supplies it), we skip the
search step and go directly to the song page — faster and no risk of picking
the wrong match.

Dedup: existing file is kept unless --refresh-lyrics is set.

Run:
    python pipeline/01_fetch_lyrics.py --songs song_list_pending.yaml
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline._lib import gemini, paths  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}
KKBOX_BASE = "https://www.kkbox.com"


def guess_artist(title: str) -> str:
    prompt = (
        f"歌曲名稱:「{title}」。請只回覆這首歌主要演唱者的中文姓名(一個名字),"
        f"不要任何說明、引號、標點。若無法判斷請只回覆: unknown"
    )
    out = gemini.generate(prompt, temperature=0.0).strip()
    # take first line, strip punctuation
    first = out.splitlines()[0].strip().strip("「」\"'。 ")
    return first or "unknown"


def search_song_url(title: str, artist: str) -> str | None:
    """Return the canonical KKBox song page URL for (title, artist), or None."""
    q = f"{artist} {title}".strip()
    url = f"{KKBOX_BASE}/tw/tc/search?q={quote_plus(q)}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # KKBox search results contain <a href="/tw/tc/song/..."> links.
    for a in soup.select('a[href*="/tw/tc/song/"]'):
        href = a.get("href", "")
        if href.startswith("/tw/tc/song/"):
            return KKBOX_BASE + href.split("?", 1)[0]
    return None


def fetch_partial_lyrics(song_url: str) -> str:
    r = requests.get(song_url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # KKBox shows partial lyrics inside a container. Selectors below are best-guess
    # and may need tuning if KKBox changes their markup — check the actual HTML.
    candidates = [
        ".lyrics",                       # commonly seen class name
        '[data-testid="lyrics"]',
        "div.lyrics-container",
        "section[class*='lyric']",
    ]
    for sel in candidates:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            # Preserve line breaks: replace <br> with \n before extracting text.
            for br in node.find_all("br"):
                br.replace_with("\n")
            text = node.get_text("\n")
            return _clean(text)

    # Fallback: pull the largest <pre>/<div> that looks like lyrics.
    blocks = [
        n.get_text("\n") for n in soup.find_all(["pre", "div"]) if len(n.get_text(strip=True)) > 80
    ]
    if blocks:
        return _clean(max(blocks, key=len))
    return ""


def _clean(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--songs", required=True, help="path to song_list.yaml")
    parser.add_argument("--refresh-lyrics", action="store_true", help="overwrite existing files")
    parser.add_argument("--sleep", type=float, default=1.5, help="seconds between requests")
    args = parser.parse_args()

    paths.ensure_all()
    cfg = yaml.safe_load(Path(args.songs).read_text(encoding="utf-8"))
    songs = cfg.get("songs", [])
    if not songs:
        print("no songs in yaml", file=sys.stderr)
        return 2

    n_ok = n_skip = n_fail = 0
    for entry in songs:
        name = entry["name"]
        title = entry["title"]
        artist = entry.get("artist")

        out = paths.lyric_raw(name)
        if out.exists() and not args.refresh_lyrics:
            print(f"[skip] {name}: already fetched")
            n_skip += 1
            continue

        try:
            song_url = entry.get("kkbox_url")
            if not song_url:
                # Step 0 normally supplies kkbox_url. Fallback path: search.
                if not artist:
                    print(f"[ai  ] {name}: asking Gemini for artist of '{title}'...")
                    artist = guess_artist(title)
                    print(f"        -> {artist}")
                print(f"[srch] {name}: searching KKBox for {artist} - {title}")
                song_url = search_song_url(title, artist)
                if not song_url:
                    raise RuntimeError("no KKBox result")
                time.sleep(args.sleep)

            print(f"[scrp] {name}: {song_url}")
            lyrics = fetch_partial_lyrics(song_url)
            if not lyrics:
                raise RuntimeError("empty lyrics block (KKBox markup may have changed)")

            out.write_text(lyrics, encoding="utf-8")
            print(f"[ok  ] {name}: wrote {out} ({len(lyrics)} chars)")
            n_ok += 1
            time.sleep(args.sleep)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {name}: {e}", file=sys.stderr)
            n_fail += 1

    print(f"\nSummary: {n_ok} ok, {n_skip} skipped, {n_fail} failed.", file=sys.stderr)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
