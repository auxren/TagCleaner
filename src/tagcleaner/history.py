"""Across-run history for TagCleaner.

A history file (``tagcleaner-history.json`` by default, at the scan
root) records every folder TagCleaner has parsed, together with a
content fingerprint and the outcome of the last tag-writing pass. It
serves two purposes:

1. **Skip already-done work.** On subsequent runs the scanner skips any
   folder whose prior run was tagged successfully in the same mode and
   whose audio contents haven't changed. This lets you point the tool
   at a large library repeatedly without re-parsing everything.

2. **Training / audit data.** Every record captures what the parser
   decided so the file can be inspected, diffed, or fed back into
   improvements to the parser.

The skip decision is deliberately conservative:

* A prior dry-run never causes a real run to skip.
* Fingerprint changes (new files, removed files, resized files) force a
  re-parse so we don't miss added content.
* Prior failures (``tagging.failed > 0``) always re-try.
* Mode changes re-process the folder (e.g. in-place → copy-to, or a
  new ``--copy-to`` destination).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .drafts import concert_from_dict, concert_to_dict
from .models import Concert
from .tagger import Mode

HISTORY_FILENAME = "tagcleaner-history.json"
SCHEMA_VERSION = 1


@dataclass
class TaggingOutcome:
    mode: str                       # Mode.value: "dry-run" | "in-place" | "copy-to"
    applied_at: str                 # UTC ISO-8601
    applied: int = 0
    failed: int = 0
    skipped: int = 0
    copy_to: Optional[str] = None   # absolute path when mode == "copy_to"


@dataclass
class HistoryEntry:
    folder: str                     # absolute path (string for JSON-friendliness)
    scanned_at: str                 # UTC ISO-8601 of last parse
    fingerprint: str
    concert: dict[str, Any]         # drafts-shaped dict (see drafts.concert_to_dict)
    tagging: Optional[TaggingOutcome] = None


@dataclass
class History:
    schema: int = SCHEMA_VERSION
    entries: dict[str, HistoryEntry] = field(default_factory=dict)

    def get(self, folder: Path) -> Optional[HistoryEntry]:
        return self.entries.get(_key(folder))

    def record_scan(self, concert: Concert, fingerprint: str) -> None:
        key = _key(concert.folder)
        prior = self.entries.get(key)
        self.entries[key] = HistoryEntry(
            folder=key,
            scanned_at=_now_iso(),
            fingerprint=fingerprint,
            concert=concert_to_dict(concert),
            tagging=prior.tagging if prior else None,
        )

    def record_tagging(self, folder: Path, outcome: TaggingOutcome) -> None:
        key = _key(folder)
        entry = self.entries.get(key)
        if entry is not None:
            entry.tagging = outcome


def fingerprint(folder: Path, audio_files: list[Path], info_txt: Optional[Path]) -> str:
    """Cheap content hash: folder name + sorted (audio name, size) + info.txt size.

    Rename the folder, add/remove/resize any audio file, or swap the
    info.txt and the fingerprint changes. We deliberately skip hashing
    file bodies — opening every file would defeat the point of skipping.
    """
    h = hashlib.sha1()
    h.update(folder.name.encode("utf-8", "replace"))
    for f in sorted(audio_files, key=lambda p: p.name):
        h.update(b"\x00a:")
        h.update(f.name.encode("utf-8", "replace"))
        h.update(b"|")
        h.update(str(_safe_size(f)).encode("ascii"))
    if info_txt is not None:
        h.update(b"\x00i:")
        h.update(info_txt.name.encode("utf-8", "replace"))
        h.update(b"|")
        h.update(str(_safe_size(info_txt)).encode("ascii"))
    return h.hexdigest()


def should_skip(
    entry: Optional[HistoryEntry],
    current_fingerprint: str,
    mode: Mode,
    copy_to: Optional[Path],
) -> bool:
    if entry is None or entry.tagging is None:
        return False
    if entry.fingerprint != current_fingerprint:
        return False
    outcome = entry.tagging
    if outcome.failed > 0:
        return False
    prior_mode = outcome.mode
    if mode is Mode.DRY_RUN:
        # Any successful prior real run means there's nothing to preview.
        return prior_mode in (Mode.IN_PLACE.value, Mode.COPY_TO.value)
    if mode is Mode.IN_PLACE:
        return prior_mode == Mode.IN_PLACE.value
    if mode is Mode.COPY_TO:
        if prior_mode != Mode.COPY_TO.value or copy_to is None:
            return False
        return outcome.copy_to == str(copy_to.resolve())
    return False


def load_history(path: Path) -> History:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return History()
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        return History()
    entries: dict[str, HistoryEntry] = {}
    for key, rec in (raw.get("entries") or {}).items():
        try:
            tagging_raw = rec.get("tagging")
            tagging = TaggingOutcome(**tagging_raw) if tagging_raw else None
            entries[key] = HistoryEntry(
                folder=rec["folder"],
                scanned_at=rec["scanned_at"],
                fingerprint=rec["fingerprint"],
                concert=rec["concert"],
                tagging=tagging,
            )
        except (KeyError, TypeError):
            continue
    return History(entries=entries)


def save_history(history: History, path: Path) -> None:
    payload = {
        "schema": SCHEMA_VERSION,
        "entries": {k: _entry_to_dict(e) for k, e in history.entries.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def entry_to_concert(entry: HistoryEntry) -> Concert:
    """Rehydrate the recorded concert for display in the summary table."""
    return concert_from_dict(entry.concert)


def _entry_to_dict(entry: HistoryEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "folder": entry.folder,
        "scanned_at": entry.scanned_at,
        "fingerprint": entry.fingerprint,
        "concert": entry.concert,
    }
    if entry.tagging is not None:
        d["tagging"] = asdict(entry.tagging)
    return d


def _key(folder: Path) -> str:
    return str(folder.resolve())


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
