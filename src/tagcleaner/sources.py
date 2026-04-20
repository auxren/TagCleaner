"""Detect the recording source and microphone model from folder/file names or info.txt bodies.

The goal is to produce a short, stable bracketed label appended to the album
name so that multiple transfers of the same show (e.g. SBD + AUD) don't
collide into a single album in a library.
"""
from __future__ import annotations

import re

from .models import SourceInfo

# Canonical source-type markers. Order matters: longer/more-specific first.
_KIND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpre[\s._-]*fm\b", re.I), "Pre-FM"),
    (re.compile(r"\bmtx\b|\bmatrix\b", re.I), "Matrix"),
    (re.compile(r"\bsbd\b|\bsoundboard\b|\bboard\b", re.I), "SBD"),
    (re.compile(r"\baud\b|\baudience\b", re.I), "AUD"),
    (re.compile(r"\bfm\b(?!.*broadcast)|\bbroadcast\b|\bpre-?broadcast\b", re.I), "FM"),
    (re.compile(r"\bdigital\s*master\b|\bdat\b", re.I), "DAT"),
]

# Microphone families. Each entry: (regex, canonical label template).
# The template can contain back-references; "{0}" inserts the whole match.
_MIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Schoeps: MK4, MK41, MK5, MK6, MK21
    (re.compile(r"\bschoeps\s*(mk\s*\d+\w*)", re.I), "Schoeps {1}"),
    (re.compile(r"\bmk\s*(4|41|5|6|21|22|2s)\b", re.I), "Schoeps MK{1}"),
    # AKG: 414, 451, 460, 480, C414, 200E
    (re.compile(r"\bakg\s*(c?\s*\d{2,3}[a-z]*)", re.I), "AKG {1}"),
    (re.compile(r"\bakg\b", re.I), "AKG"),
    # Neumann: KM184, U87, KM140, etc.
    (re.compile(r"\bneumann\s*(km\s*\d+\w*|u\s*\d+\w*|tlm\s*\d+)", re.I), "Neumann {1}"),
    (re.compile(r"\bneumann\b", re.I), "Neumann"),
    # Sennheiser: MKH40, MKH20, MKE2, etc.
    (re.compile(r"\bsennheiser\s*(mkh?\s*\d+\w*|mke\s*\d+)", re.I), "Sennheiser {1}"),
    (re.compile(r"\bmkh\s*(\d+\w*)", re.I), "Sennheiser MKH{1}"),
    # DPA: 4023, 4060, 4061, 4011
    (re.compile(r"\bdpa\s*(\d{4})", re.I), "DPA {1}"),
    (re.compile(r"\bdpa\b", re.I), "DPA"),
    # Nakamichi: CM-300, CM-100, DR-3
    (re.compile(r"\bnak(?:amichi)?\s*(cm-?\s*\d+|dr-?\s*\d+)", re.I), "Nak {1}"),
    (re.compile(r"\bnak\b", re.I), "Nak"),
    # Sony: PCM-D50, PCM-D100, D3, D7, D8 (DAT decks)
    (re.compile(r"\bsony\s*(pcm-?d\s*\d+|d\s*\d+)", re.I), "Sony {1}"),
    # Core Sound binaurals
    (re.compile(r"\bcore\s*sound\s*(\w+)?", re.I), "CoreSound {1}"),
    (re.compile(r"\bbinaural\b", re.I), "Binaural"),
    # Shure KSM / SM series
    (re.compile(r"\bshure\s*(ksm\d+|sm\d+)", re.I), "Shure {1}"),
]


def _normalize(label: str) -> str:
    return re.sub(r"\s+", " ", label).strip()


def detect_source(*texts: str) -> SourceInfo:
    """Scan one or more strings (folder name, filenames, info.txt body) and
    return a merged SourceInfo. Later sources refine earlier ones but don't
    override when non-empty fields already exist."""
    blob = "  ".join(t or "" for t in texts)

    kind: str | None = None
    for pat, label in _KIND_PATTERNS:
        if pat.search(blob):
            kind = label
            break

    mics: list[str] = []
    seen: set[str] = set()
    for pat, template in _MIC_PATTERNS:
        for m in pat.finditer(blob):
            groups = [m.group(0)] + list(m.groups())
            try:
                label = template.format(*groups)
            except IndexError:
                label = template
            label = _normalize(label)
            key = label.lower()
            if key in seen or not label:
                continue
            seen.add(key)
            mics.append(label)

    mics = _dedupe_mic_families(mics)

    return SourceInfo(kind=kind, mics=mics)


def _dedupe_mic_families(mics: list[str]) -> list[str]:
    """Drop bare family names when a more specific model from the same family
    is present. 'AKG' is dropped if 'AKG 414' is also present."""
    specific = [m for m in mics if re.search(r"\d", m)]
    families_with_spec = {m.split()[0].lower() for m in specific}
    kept = [m for m in mics if re.search(r"\d", m) or m.split()[0].lower() not in families_with_spec]
    return kept
