#!/usr/bin/env python3
"""Mine a harvested etree corpus for lookup-table expansion candidates.

Reads a corpus produced by harvest_etree.py and emits suggestions for the
parser's hard-coded tables in src/tagcleaner/parser.py and sources.py:

  - artist abbreviations:  identifier prefix (^[a-z]+ before a digit)  ->
                           IA `creator` field, ranked by occurrence count.
                           Only suggests prefixes that map to ONE creator with
                           high consistency (no ambiguous prefixes).
  - mic models:            mic-like tokens found in info.txt bodies that the
                           current sources.py regexes don't already match.
  - source markers:        frequency of SBD/AUD/FM/Matrix/etc. in IA `source`
                           field + info.txt bodies; flags any unrecognized
                           short-form labels.
  - venues / cities:       top venues + cities by frequency (data for a future
                           fuzzy-normalization pass).

Output:
    <corpus>/_mining_report.json   -- full structured report
    stdout                         -- human-readable summary
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Make `tagcleaner` importable when run from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tagcleaner.parser import ARTIST_PREFIX_MAP, read_info_txt  # noqa: E402
from tagcleaner.sources import _KIND_PATTERNS, _MIC_PATTERNS, detect_source  # noqa: E402

PREFIX_RE = re.compile(r"^([a-z]+)\d")

# Loose mic-token regex: a brand word followed by a model-like token.
# Catches things like "Schoeps CMC641", "Neumann KM84", "AKG C480", "DPA 4023",
# "Sennheiser MKH8040", "Earthworks SR40V", "Audio-Technica AT853", "Avenson STO-2".
MIC_BRANDS = (
    r"schoeps|neumann|akg|sennheiser|dpa|nakamichi|nak|sony|shure|core\s*sound|"
    r"earthworks|audio[-\s]?technica|at\d{2,}|avenson|microtech|gefell|royer|"
    r"oktava|busman|milab|peluso|cad|samson|behringer|tascam|edirol|zoom|m-?audio"
)
MIC_LINE_RE = re.compile(rf"\b({MIC_BRANDS})[\s/-]*[A-Za-z0-9-]{{0,12}}", re.I)

SOURCE_TOKEN_RE = re.compile(
    r"\b(sbd|aud|fm|mtx|matrix|board|soundboard|broadcast|pre[\s._-]*fm|"
    r"dat|digital\s*master|hi[\s._-]*md|minidisc|md|cassette|cdr|cd-r|"
    r"sound[\s_-]*?board|in[\s_-]*ear|line[\s_-]*in|aux[\s_-]*board)\b",
    re.I,
)

# Tokens we do NOT want to nominate as new abbreviation prefixes (look like
# format/source/dataset markers, not artist abbreviations).
PREFIX_DENY = {
    "sbd", "aud", "fm", "mtx", "dat", "flac", "shn", "mp3", "wav",
    "cd", "cdr", "live", "set", "disc", "vol", "n", "e", "m", "ny",
    "jam", "the", "a",
}


def load_summary_log(corpus: Path) -> list[dict]:
    log = corpus / "_harvest_log.jsonl"
    out = []
    if not log.exists():
        return out
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_ia_meta(item_dir: Path) -> dict:
    p = item_dir / "_ia.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def info_text_for(item_dir: Path) -> str:
    """Concatenate all txt info files in an item dir."""
    chunks: list[str] = []
    for txt in sorted(item_dir.glob("*.txt")):
        if txt.name.startswith("_"):
            continue
        chunks.append(read_info_txt(txt))
    return "\n\n".join(chunks)


def _mic_already_matched(token: str) -> bool:
    for pat, _ in _MIC_PATTERNS:
        if pat.search(token):
            return True
    return False


def mine_artist_prefixes(items: list[dict]) -> dict:
    """For each lowercase prefix preceding a digit in the identifier, group all
    creator values seen. Suggest the prefix if it points to one dominant creator
    and isn't already in ARTIST_PREFIX_MAP."""
    prefix_to_creators: dict[str, Counter[str]] = defaultdict(Counter)
    for it in items:
        ident = it.get("identifier", "")
        m = PREFIX_RE.match(ident.lower())
        if not m:
            continue
        prefix = m.group(1)
        if prefix in PREFIX_DENY or len(prefix) < 2 or len(prefix) > 12:
            continue
        creator = it.get("creator")
        if isinstance(creator, list):
            creator = creator[0] if creator else None
        if not creator:
            continue
        prefix_to_creators[prefix][creator.strip()] += 1

    known = {k.lower() for k in ARTIST_PREFIX_MAP}
    suggestions = []
    ambiguous = []
    for prefix, counts in sorted(prefix_to_creators.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(counts.values())
        top, top_n = counts.most_common(1)[0]
        purity = top_n / total
        record = {
            "prefix": prefix,
            "creator": top,
            "count": top_n,
            "total_with_prefix": total,
            "purity": round(purity, 2),
            "already_known": prefix in known,
            "known_value": ARTIST_PREFIX_MAP.get(prefix),
            "all_creators": counts.most_common(5),
        }
        if prefix in known:
            # Sanity check: does our existing mapping match what etree thinks?
            if ARTIST_PREFIX_MAP[prefix].lower() != top.lower():
                record["disagrees_with_known"] = True
                suggestions.append(record)
            continue
        if purity >= 0.8 and top_n >= 2:
            suggestions.append(record)
        else:
            ambiguous.append(record)
    return {"suggestions": suggestions, "ambiguous": ambiguous}


def mine_mics(items: list[dict], corpus: Path) -> dict:
    unmatched: Counter[str] = Counter()
    matched: Counter[str] = Counter()
    for it in items:
        ident = it.get("identifier")
        if not ident:
            continue
        item_dir = corpus / ident
        if not item_dir.is_dir():
            continue
        body = info_text_for(item_dir)
        if not body:
            continue
        for m in MIC_LINE_RE.finditer(body):
            tok = re.sub(r"\s+", " ", m.group(0).strip()).lower()
            # Trim trailing junk tokens
            tok = re.sub(r"[^a-z0-9 \-/]+$", "", tok)
            if not tok:
                continue
            if _mic_already_matched(tok):
                matched[tok] += 1
            else:
                unmatched[tok] += 1
    return {
        "unmatched_top": unmatched.most_common(50),
        "matched_top": matched.most_common(20),
    }


def mine_sources(items: list[dict], corpus: Path) -> dict:
    """Frequency of source markers in IA's `source` field + info.txt bodies.
    Anything in the body that doesn't get matched by sources.py is a candidate."""
    ia_source = Counter()
    body_tokens = Counter()
    parser_kinds = Counter()
    for it in items:
        ident = it.get("identifier")
        if not ident:
            continue
        item_dir = corpus / ident
        ia = load_ia_meta(item_dir).get("metadata", {})
        src = ia.get("source")
        if isinstance(src, list):
            src = src[0] if src else None
        if src:
            ia_source[str(src).strip().lower()] += 1
        body = info_text_for(item_dir)
        if body:
            for m in SOURCE_TOKEN_RE.finditer(body):
                body_tokens[m.group(0).lower()] += 1
            si = detect_source(body)
            if si.kind:
                parser_kinds[si.kind] += 1
            else:
                parser_kinds["__none__"] += 1
    return {
        "ia_source_field": ia_source.most_common(30),
        "body_tokens": body_tokens.most_common(30),
        "parser_resolved": parser_kinds.most_common(),
    }


def mine_venues(items: list[dict]) -> dict:
    venues = Counter()
    coverage = Counter()
    for it in items:
        v = it.get("venue")
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            venues[v.strip()] += 1
        c = it.get("coverage")
        if isinstance(c, list):
            c = c[0] if c else None
        if c:
            coverage[c.strip()] += 1
    return {
        "top_venues": venues.most_common(30),
        "top_coverage": coverage.most_common(30),
        "unique_venues": len(venues),
        "unique_coverage": len(coverage),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", type=Path, default=Path("corpus/etree"))
    ap.add_argument("--out", type=Path, default=None,
                    help="report path (defaults to <corpus>/_mining_report.json)")
    args = ap.parse_args()

    items = load_summary_log(args.corpus)
    items = [it for it in items if "error" not in it]
    if not items:
        print(f"no harvest log entries in {args.corpus}", file=sys.stderr)
        return 1
    print(f"mining {len(items)} items from {args.corpus}", file=sys.stderr)

    report = {
        "n_items": len(items),
        "artist_prefixes": mine_artist_prefixes(items),
        "mics": mine_mics(items, args.corpus),
        "sources": mine_sources(items, args.corpus),
        "venues": mine_venues(items),
    }

    out = args.out or (args.corpus / "_mining_report.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}", file=sys.stderr)

    # Human-readable digest
    print("\n=== ARTIST PREFIX SUGGESTIONS ===")
    sugg = report["artist_prefixes"]["suggestions"]
    for s in sugg[:25]:
        marker = "  (DISAGREE)" if s.get("disagrees_with_known") else ""
        print(f'  "{s["prefix"]}": "{s["creator"]}",   # n={s["count"]}/{s["total_with_prefix"]} purity={s["purity"]}{marker}')
    if len(sugg) > 25:
        print(f"  ... +{len(sugg) - 25} more")

    print("\n=== UNMATCHED MIC TOKENS (parser regex doesn't catch) ===")
    for tok, n in report["mics"]["unmatched_top"][:25]:
        print(f"  {n:4d}  {tok}")

    print("\n=== SOURCE FIELD TOKENS (IA `source` metadata) ===")
    for tok, n in report["sources"]["ia_source_field"][:15]:
        print(f"  {n:4d}  {tok}")

    print("\n=== TOP VENUES ===")
    for v, n in report["venues"]["top_venues"][:15]:
        print(f"  {n:4d}  {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
