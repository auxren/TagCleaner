"""Self-bootstrapping artist + venue lexicon.

The lexicon is a count of every artist and venue TagCleaner has successfully
parsed in a library, persisted as ``tagcleaner-lexicon.json`` alongside the
history file. It feeds back into the parser:

* **Parent-folder artist fallback** — when a folder's name is date-first
  (``1987-12-17 Chestnut Cabaret, Philly``) the parser can't extract an
  artist from the name alone. If the parent folder name is in the lexicon
  ("Grateful Dead" seen 400 times), we adopt it.
* **Spelling canonicalization** — ``talking heads`` and ``Talking Heads``
  are merged into one canonical spelling (the most-seen form).

The lexicon never invents an artist or venue. It only confirms ones the
library itself has already voted for.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Iterable, Optional

LEXICON_FILENAME = "tagcleaner-lexicon.json"
SCHEMA_VERSION = 1

# Default minimum occurrences before a lexicon entry is trusted to confirm
# a parser guess. Low-count entries are almost always typos or one-off
# mis-parses, so a threshold keeps them from polluting downstream runs.
DEFAULT_MIN_COUNT = 2

# Fuzzy cutoff for difflib.get_close_matches. Tuned on real-world data:
# 0.88 catches ``Talking Heads`` vs ``The Talking Heads`` and
# ``Grateful Dead`` vs ``grateful dead.`` while rejecting ``Phish`` /
# ``Pish``. Short candidates are handled separately (see match_*).
FUZZY_CUTOFF = 0.88


def normalize_name(s: str) -> str:
    """Fold *s* for equality: lowercase, strip ``The `` prefix, drop
    punctuation, collapse whitespace. Returns an empty string when *s*
    has no letters."""
    if not s:
        return ""
    t = s.strip().lower()
    t = re.sub(r"^the\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


@dataclass
class Lexicon:
    """Per-library counts of every artist and venue seen by the parser.

    ``artists`` / ``venues`` map canonical display forms to counts. The
    canonical form is the most-frequently-seen spelling among all inputs
    that normalize to the same key.
    """
    artists: dict[str, int] = field(default_factory=dict)
    venues: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._artist_index = _build_index(self.artists)
        self._venue_index = _build_index(self.venues)

    def match_artist(
        self, candidate: Optional[str], *, min_count: int = DEFAULT_MIN_COUNT,
    ) -> Optional[str]:
        return _match(candidate, self.artists, self._artist_index, min_count)

    def match_venue(
        self, candidate: Optional[str], *, min_count: int = DEFAULT_MIN_COUNT,
    ) -> Optional[str]:
        return _match(candidate, self.venues, self._venue_index, min_count)

    def add_artist(self, name: str, count: int = 1) -> str:
        """Add *name* to the lexicon (or bump its count) and return the
        canonical spelling now stored. A user-supplied name that
        normalises to an existing entry merges with it; a brand-new name
        is kept verbatim."""
        return _add(name, count, self.artists, self._artist_index)

    def add_venue(self, name: str, count: int = 1) -> str:
        return _add(name, count, self.venues, self._venue_index)

    def save(self, path: Path) -> None:
        # Merge with existing file on disk so externally-imported entries
        # (e.g. from Qobuz, MusicBrainz validation) survive the round-trip.
        existing = Lexicon.load(path)
        merged_artists = dict(existing.artists)
        for name, count in self.artists.items():
            merged_artists[name] = max(merged_artists.get(name, 0), count)
        merged_venues = dict(existing.venues)
        for name, count in self.venues.items():
            merged_venues[name] = max(merged_venues.get(name, 0), count)
        payload = {
            "schema": SCHEMA_VERSION,
            "artists": _sort_by_count(merged_artists),
            "venues": _sort_by_count(merged_venues),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8", "replace"))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
            return cls()
        artists = {k: int(v) for k, v in (raw.get("artists") or {}).items() if isinstance(v, (int, float))}
        venues = {k: int(v) for k, v in (raw.get("venues") or {}).items() if isinstance(v, (int, float))}
        return cls(artists=artists, venues=venues)

    @classmethod
    def load_or_seed(cls, path: Path) -> "Lexicon":
        """Load lexicon from *path*, or seed *path* with the bundled
        starter-lexicon if the file is missing/empty. First-run UX: a
        brand-new library gets a curated artist + venue list out of the
        box instead of starting empty.

        The starter is bundled at ``src/tagcleaner/data/starter-lexicon.json``
        and represents ~8500 artists + ~2900 venues from a real live-music
        library, junk-pruned and MusicBrainz-validated, with all counts
        normalised to 1 (so first-run users do not inherit someone else's
        library distribution as their canonicalisation prior).
        """
        lex = cls.load(path)
        if lex.artists or lex.venues:
            return lex
        starter = Path(__file__).parent / "data" / LEXICON_FILENAME.replace(
            "tagcleaner-lexicon.json", "starter-lexicon.json"
        )
        if not starter.exists():
            return lex
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy(str(starter), str(path))
        except OSError:
            return lex
        return cls.load(path)

    @classmethod
    def from_concert_dicts(cls, concerts: Iterable[dict]) -> "Lexicon":
        """Build a lexicon from drafts/history-shaped concert dicts.

        Fields ``artist`` and ``venue`` are counted; missing/blank entries
        are skipped. Multiple spellings that normalize identically get
        merged under the most-frequent display form.
        """
        artist_counts = _CaseFolder()
        venue_counts = _CaseFolder()
        for c in concerts:
            artist_counts.add(c.get("artist"))
            venue_counts.add(c.get("venue"))
        return cls(artists=artist_counts.resolve(), venues=venue_counts.resolve())

    @classmethod
    def from_history(cls, history) -> "Lexicon":
        """Build from a ``History`` object. Accepts anything with an
        ``entries`` dict of objects whose ``.concert`` is a drafts dict."""
        dicts = [e.concert for e in history.entries.values() if getattr(e, "concert", None)]
        return cls.from_concert_dicts(dicts)


def _match(
    candidate: Optional[str],
    table: dict[str, int],
    index: dict[str, str],
    min_count: int,
) -> Optional[str]:
    if not candidate:
        return None
    key = normalize_name(candidate)
    if not key:
        return None
    canonical = index.get(key)
    if canonical and table.get(canonical, 0) >= min_count:
        return canonical
    # Fuzzy pass — only for candidates long enough that typo-collapse is
    # informative. Short keys like "moe" are too generic for difflib.
    if len(key) < 5:
        return None
    close = get_close_matches(key, index.keys(), n=1, cutoff=FUZZY_CUTOFF)
    if not close:
        return None
    canonical = index[close[0]]
    if table.get(canonical, 0) >= min_count:
        return canonical
    return None


def _add(name: str, count: int, table: dict[str, int], index: dict[str, str]) -> str:
    """Add *name* to *table* and update *index* in place. Returns the
    canonical spelling (the most-seen form after the add)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("cannot add empty name to lexicon")
    key = normalize_name(name)
    if not key:
        raise ValueError(f"name normalises to empty: {name!r}")
    existing = index.get(key)
    if existing is None:
        table[name] = table.get(name, 0) + count
        index[key] = name
        return name
    new_total = table.get(existing, 0) + count
    # When the incoming spelling and the stored one disagree, keep whichever
    # has the higher total count as canonical.
    if name != existing and count >= new_total - count:
        table.pop(existing, None)
        table[name] = new_total
        index[key] = name
        return name
    table[existing] = new_total
    return existing


def _build_index(table: dict[str, int]) -> dict[str, str]:
    """Map normalized key -> canonical display form. Ties broken by count."""
    idx: dict[str, str] = {}
    best: dict[str, int] = {}
    for name, count in table.items():
        key = normalize_name(name)
        if not key:
            continue
        if count > best.get(key, -1):
            idx[key] = name
            best[key] = count
    return idx


def _sort_by_count(table: dict[str, int]) -> dict[str, int]:
    return dict(sorted(table.items(), key=lambda kv: (-kv[1], kv[0].lower())))


class _CaseFolder:
    """Counts raw name occurrences, folding case-insensitive duplicates
    under whichever spelling is most common."""

    def __init__(self) -> None:
        self._groups: dict[str, dict[str, int]] = {}

    def add(self, name: Optional[str]) -> None:
        if not name:
            return
        name = name.strip()
        if not name:
            return
        key = normalize_name(name)
        if not key:
            return
        self._groups.setdefault(key, {}).setdefault(name, 0)
        self._groups[key][name] += 1

    def resolve(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for spellings in self._groups.values():
            total = sum(spellings.values())
            # Canonical = most-seen spelling; tie-break by longer form
            # (preserves casing/diacritics over a shorter variant).
            canonical = max(
                spellings.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]),
            )[0]
            out[canonical] = total
        return out
