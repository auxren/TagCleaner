#!/usr/bin/env python3
"""Evaluate the TagCleaner parser against a harvested etree corpus.

For each item, we build a synthetic `Concert` the way the live CLI would
(folder name = IA identifier so etree-style prefix lookup fires; audio_files
synthesized from _audio.json; info_txt = the most info-shaped txt in the dir),
then compare the parsed result against IA's curated metadata fields:

    parser.artist  vs  ia.creator
    parser.date    vs  ia.date
    parser.venue   vs  ia.venue
    parser.city    vs  first comma-segment of ia.coverage

We report per-field coverage (% of items where parser produced a value),
match rate (% of items where parser's value matches IA's, fuzzy), and the
top failure buckets so it's clear what to tune next.

Usage:
    python scripts/eval_corpus.py --corpus corpus/etree
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tagcleaner.parser import build_concert  # noqa: E402

INFO_PRIORITY_RE = re.compile(r"info|notes?|readme|setlist", re.I)
SKIP_TXT_RE = re.compile(r"(ffp|md5|sha1|checksum|spectral|spek)\.txt$", re.I)


def pick_info_txt(item_dir: Path) -> Path | None:
    cands = [p for p in item_dir.glob("*.txt") if not p.name.startswith("_") and not SKIP_TXT_RE.search(p.name)]
    if not cands:
        return None
    # Prefer ones with info/notes/readme/setlist in name; fall back to largest.
    cands.sort(key=lambda p: (
        0 if INFO_PRIORITY_RE.search(p.name) else 1,
        -p.stat().st_size,
    ))
    return cands[0]


def synth_audio_paths(item_dir: Path) -> list[Path]:
    """Build Path objects matching the real audio filenames. We do not write
    bytes -- the parser only reads names + count, never opens the files."""
    p = item_dir / "_audio.json"
    if not p.exists():
        return []
    try:
        files = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    return [item_dir / f["name"] for f in files if f.get("name")]


def normalize(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"[\.\,&'\"\(\)\[\]/_]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    # strip very common articles for fuzzier compare
    s = re.sub(r"^(the |a |an )", "", s)
    return s.strip()


def fuzzy_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # one being a substring of the other is good enough for venues / artists
    if na in nb or nb in na:
        return True
    # token-set overlap
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and len(ta & tb) / max(len(ta), len(tb)) >= 0.75:
        return True
    return False


def first_segment(s: str | None) -> str | None:
    if not s:
        return None
    return s.split(",")[0].strip() or None


def first_of(field) -> str | None:
    if isinstance(field, list):
        return field[0] if field else None
    return field


def evaluate_one(item_dir: Path, ia: dict) -> dict:
    md = ia.get("metadata", {}) or {}
    audio = synth_audio_paths(item_dir)
    info_txt = pick_info_txt(item_dir)
    folder = Path(item_dir.name)  # identifier as folder name (drives prefix lookup)
    concert = build_concert(folder, audio, info_txt)

    truth = {
        "artist": first_of(md.get("creator")),
        "date": first_of(md.get("date")),
        "venue": first_of(md.get("venue")),
        "coverage": first_of(md.get("coverage")),
    }
    truth_city = first_segment(truth["coverage"])

    return {
        "identifier": item_dir.name,
        "had_info_txt": info_txt is not None,
        "audio_count": len(audio),
        "parser": {
            "artist": concert.artist,
            "date": concert.date,
            "venue": concert.venue,
            "city": concert.city,
            "region": concert.region,
            "tracks": len(concert.tracks),
            "source_kind": concert.source.kind if concert.source else None,
            "issues": concert.issues,
        },
        "truth": {**truth, "city": truth_city},
        "match": {
            "artist": fuzzy_match(concert.artist, truth["artist"]),
            "date": (concert.date or "") == (truth["date"] or "")[:10] if truth["date"] else False,
            "venue": fuzzy_match(concert.venue, truth["venue"]),
            "city": fuzzy_match(concert.city, truth_city),
        },
    }


def summarize(results: list[dict]) -> dict:
    n = len(results)
    fields = ("artist", "date", "venue", "city")
    summary = {"n": n}
    for f in fields:
        present = sum(1 for r in results if r["parser"].get(f))
        truthy = sum(1 for r in results if r["truth"].get(f))
        matched = sum(1 for r in results if r["match"][f])
        summary[f] = {
            "parser_filled": present,
            "parser_filled_pct": round(100 * present / n, 1) if n else 0,
            "truth_present": truthy,
            "matched": matched,
            "match_pct_of_truth": round(100 * matched / truthy, 1) if truthy else 0,
        }
    summary["had_info_txt"] = sum(1 for r in results if r["had_info_txt"])
    summary["had_info_txt_pct"] = round(100 * summary["had_info_txt"] / n, 1) if n else 0
    return summary


def failure_buckets(results: list[dict]) -> dict:
    buckets: dict[str, list[dict]] = {
        "no_info_txt": [],
        "artist_mismatch": [],
        "date_mismatch": [],
        "venue_mismatch": [],
        "city_mismatch": [],
        "no_tracks": [],
    }
    for r in results:
        if not r["had_info_txt"]:
            buckets["no_info_txt"].append(r["identifier"])
        if r["truth"]["artist"] and not r["match"]["artist"]:
            buckets["artist_mismatch"].append({
                "id": r["identifier"],
                "parser": r["parser"]["artist"],
                "truth": r["truth"]["artist"],
            })
        if r["truth"]["date"] and not r["match"]["date"]:
            buckets["date_mismatch"].append({
                "id": r["identifier"],
                "parser": r["parser"]["date"],
                "truth": r["truth"]["date"],
            })
        if r["truth"]["venue"] and not r["match"]["venue"]:
            buckets["venue_mismatch"].append({
                "id": r["identifier"],
                "parser": r["parser"]["venue"],
                "truth": r["truth"]["venue"],
            })
        if r["truth"]["city"] and not r["match"]["city"]:
            buckets["city_mismatch"].append({
                "id": r["identifier"],
                "parser": r["parser"]["city"],
                "truth": r["truth"]["city"],
            })
        if r["audio_count"] > 0 and r["parser"]["tracks"] == 0:
            buckets["no_tracks"].append(r["identifier"])
    return buckets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", type=Path, default=Path("corpus/etree"))
    ap.add_argument("--out", type=Path, default=None,
                    help="report path (defaults to <corpus>/_eval_report.json)")
    ap.add_argument("--examples", type=int, default=10,
                    help="number of example mismatches to print per bucket")
    args = ap.parse_args()

    log = args.corpus / "_harvest_log.jsonl"
    if not log.exists():
        print(f"no harvest log at {log}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    items: list[str] = []
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in rec:
            continue
        ident = rec.get("identifier")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        items.append(ident)

    results: list[dict] = []
    for i, ident in enumerate(items, 1):
        item_dir = args.corpus / ident
        if not item_dir.is_dir():
            continue
        ia_path = item_dir / "_ia.json"
        if not ia_path.exists():
            continue
        ia = json.loads(ia_path.read_text())
        try:
            r = evaluate_one(item_dir, ia)
        except Exception as e:
            print(f"  eval failed for {ident}: {e}", file=sys.stderr)
            continue
        results.append(r)

    summary = summarize(results)
    buckets = failure_buckets(results)

    report = {"summary": summary, "buckets": {k: v[: args.examples * 5] for k, v in buckets.items()}, "results": results}
    out = args.out or (args.corpus / "_eval_report.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}", file=sys.stderr)

    print(f"\n=== EVAL ({summary['n']} items) ===")
    print(f"  had info.txt:   {summary['had_info_txt']:5d}  ({summary['had_info_txt_pct']}%)")
    for f in ("artist", "date", "venue", "city"):
        s = summary[f]
        print(f"  {f:<7} parser={s['parser_filled']:4d} ({s['parser_filled_pct']}%)  "
              f"matched-of-truth={s['matched']}/{s['truth_present']} ({s['match_pct_of_truth']}%)")

    print("\n=== FAILURE BUCKETS ===")
    for k, items_b in buckets.items():
        print(f"  {k}: {len(items_b)}")
        for ex in items_b[: args.examples]:
            print(f"      {ex}")

    # Issue-tag frequency: which `issues` strings does the parser emit most?
    issue_counter = Counter()
    for r in results:
        for iss in r["parser"]["issues"]:
            # canonicalize 'track count mismatch: 12 parsed vs 10 audio files' -> 'track count mismatch'
            issue_counter[re.split(r":", iss)[0].strip()] += 1
    print("\n=== PARSER ISSUE FREQUENCY ===")
    for iss, n in issue_counter.most_common(10):
        print(f"  {n:4d}  {iss}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
