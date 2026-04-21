#!/usr/bin/env python3
"""Harvest a random sample of the archive.org Live Music Archive (etree).

For each sampled item we save:
  - _ia.json    : the full IA metadata response (creator, date, venue, coverage,
                  notes, source, lineage, …) -- used as ground truth for eval
  - _audio.json : list of audio filenames (no bytes) -- lets the parser see
                  what filenames it would have on disk
  - <name>.txt  : every original txt/md/nfo file uploaded with the item --
                  this is what the parser actually ingests as info.txt

Usage:
    python scripts/harvest_etree.py --count 50 --out corpus/etree --seed 42
    python scripts/harvest_etree.py --count 1000 --out corpus/etree --seed 42

The script is resumable: items already present in the output dir are skipped.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "TagCleaner-research/0.1 (https://github.com/auxren/TagCleaner)"
SCRAPE_URL = "https://archive.org/services/search/v1/scrape"
META_URL = "https://archive.org/metadata/{id}"
DOWNLOAD_URL = "https://archive.org/download/{id}/{name}"

# IA's published rate limit for unauthenticated requests is generous but we
# still want to be polite; 0.4s ~ 2.5 req/s sustained.
DEFAULT_SLEEP = 0.4

TXT_EXTS = {".txt", ".md", ".nfo"}
AUDIO_EXTS = {".flac", ".shn", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aif", ".aiff"}
# Files we never want even if they end in .txt
SKIP_NAMES = {"checksum.txt", "md5.txt", "sha1.txt", "ffp.txt"}


def http_get_json(url: str, params: dict | None = None, retries: int = 3) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  retry {attempt+1}/{retries} after {wait}s: {e}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"http_get_json failed for {url}: {last_err}")


def http_get_bytes(url: str, retries: int = 3, max_bytes: int = 2_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(max_bytes)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  retry {attempt+1}/{retries} after {wait}s: {e}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"http_get_bytes failed for {url}: {last_err}")


def scrape_all_identifiers(collection: str = "etree", page_size: int = 10_000) -> list[str]:
    """Page through the IA scraping API to collect every item identifier in a
    collection. The etree collection is ~280k items; expect ~30 pages."""
    ids: list[str] = []
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        params = {
            "q": f"collection:{collection}",
            "fields": "identifier",
            "count": str(page_size),
        }
        if cursor:
            params["cursor"] = cursor
        data = http_get_json(SCRAPE_URL, params)
        items = data.get("items", [])
        ids.extend(it["identifier"] for it in items if "identifier" in it)
        cursor = data.get("cursor")
        print(f"scrape page {page}: +{len(items)} (total {len(ids)})", file=sys.stderr)
        if not cursor or not items:
            break
        time.sleep(DEFAULT_SLEEP)
    return ids


def cached_identifiers(out_root: Path, collection: str) -> list[str] | None:
    cache = out_root / f"_identifiers_{collection}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    return None


def save_identifiers(out_root: Path, collection: str, ids: list[str]) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    cache = out_root / f"_identifiers_{collection}.json"
    cache.write_text(json.dumps(ids))


def harvest_one(identifier: str, out_dir: Path, sleep: float) -> dict:
    """Fetch IA metadata for one item and download its txt info files.
    Returns a small summary dict (used for the harvest log)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = http_get_json(META_URL.format(id=identifier))
    (out_dir / "_ia.json").write_text(json.dumps(meta, indent=2))

    files = meta.get("files", [])
    txt_files: list[dict] = []
    audio_files: list[dict] = []
    for f in files:
        name = f.get("name", "")
        if not name:
            continue
        # Originals only -- skip derivatives like _vbr.mp3, png spectrograms, etc.
        if f.get("source") and f.get("source") != "original":
            continue
        ext = Path(name).suffix.lower()
        low = name.lower()
        if ext in TXT_EXTS and low not in SKIP_NAMES and not low.endswith(("ffp.txt", "md5.txt", "sha1.txt")):
            txt_files.append(f)
        elif ext in AUDIO_EXTS:
            audio_files.append({"name": name, "size": f.get("size"), "format": f.get("format")})

    (out_dir / "_audio.json").write_text(json.dumps(audio_files, indent=2))

    saved_txt: list[str] = []
    for f in txt_files:
        name = f["name"]
        # Sanitize to a flat filename inside out_dir
        safe = name.replace("/", "__")
        target = out_dir / safe
        if target.exists() and target.stat().st_size > 0:
            saved_txt.append(safe)
            continue
        try:
            data = http_get_bytes(DOWNLOAD_URL.format(id=identifier, name=urllib.parse.quote(name)))
        except RuntimeError as e:
            print(f"  txt fetch failed for {identifier}/{name}: {e}", file=sys.stderr)
            continue
        target.write_bytes(data)
        saved_txt.append(safe)
        time.sleep(sleep)

    md = meta.get("metadata", {}) or {}
    return {
        "identifier": identifier,
        "creator": md.get("creator"),
        "date": md.get("date"),
        "venue": md.get("venue"),
        "coverage": md.get("coverage"),
        "source_field": md.get("source"),
        "txt_files": saved_txt,
        "audio_count": len(audio_files),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=50, help="number of items to sample")
    ap.add_argument("--out", type=Path, default=Path("corpus/etree"))
    ap.add_argument("--collection", default="etree")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible sampling")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    ap.add_argument("--refresh-index", action="store_true",
                    help="re-scrape the identifier list even if cached")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    ids = None if args.refresh_index else cached_identifiers(args.out, args.collection)
    if ids is None:
        print(f"scraping identifiers for collection={args.collection!r}…", file=sys.stderr)
        ids = scrape_all_identifiers(args.collection)
        save_identifiers(args.out, args.collection, ids)
        print(f"cached {len(ids)} identifiers", file=sys.stderr)
    else:
        print(f"using cached identifier list ({len(ids)} items)", file=sys.stderr)

    rng = random.Random(args.seed)
    sample = rng.sample(ids, min(args.count, len(ids)))
    print(f"sampled {len(sample)} items (seed={args.seed})", file=sys.stderr)

    log_path = args.out / "_harvest_log.jsonl"
    summaries: list[dict] = []
    done_ids: set[str] = set()
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done_ids.add(rec["identifier"])
                summaries.append(rec)
            except (json.JSONDecodeError, KeyError):
                pass

    with log_path.open("a", encoding="utf-8") as log:
        for i, ident in enumerate(sample, 1):
            if ident in done_ids:
                continue
            item_dir = args.out / ident
            print(f"[{i}/{len(sample)}] {ident}", file=sys.stderr)
            try:
                summary = harvest_one(ident, item_dir, args.sleep)
            except Exception as e:
                print(f"  FAIL: {e}", file=sys.stderr)
                summary = {"identifier": ident, "error": str(e)}
            log.write(json.dumps(summary) + "\n")
            log.flush()
            summaries.append(summary)
            time.sleep(args.sleep)

    ok = sum(1 for s in summaries if "error" not in s)
    print(f"done: {ok}/{len(summaries)} succeeded; corpus at {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
