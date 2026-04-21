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

# Microphone & recorder families. Each entry: (regex, canonical label template).
# The template can contain back-references; "{0}" inserts the whole match.
# List is mic-first, then recorder/preamp families added from etree corpus
# mining (Edirol, Tascam, Zoom, etc.). These aren't strictly microphones but
# they're part of the same rig identifier and users type them into info.txt
# alongside the mic model, so including them in the bracketed source label
# is what we want.
_MIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Schoeps: MK4, MK41, MK5, MK6, MK21, CCM4V, CMC4, CMC641
    (re.compile(r"\bschoeps\s*(ccm\s*\d+\w*|cmc\s*\d+\w*|mk\s*\d+\w*|m\s*\d+\w*)", re.I), "Schoeps {1}"),
    (re.compile(r"\bmk\s*(4|41|5|6|21|22|2s)\b", re.I), "Schoeps MK{1}"),
    # AKG: 414, 451, 460, 480, C414, 200E, CK-series capsules
    (re.compile(r"\bakg\s*(c?\s*\d{2,3}[a-z]*|ck\s*\d+\w*)", re.I), "AKG {1}"),
    (re.compile(r"\bakg\b", re.I), "AKG"),
    # Neumann: KM184, U87, KM140, TLM103
    (re.compile(r"\bneumann\s*(km\s*\d+\w*|u\s*\d+\w*|tlm\s*\d+)", re.I), "Neumann {1}"),
    (re.compile(r"\bneumann\b", re.I), "Neumann"),
    # Sennheiser: MKH40, MKH20, MKE2, MD421, MD441
    (re.compile(r"\bsennheiser\s*(mkh?\s*\d+\w*|mke\s*\d+|md\s*\d+\w*|\d{3,4})", re.I), "Sennheiser {1}"),
    (re.compile(r"\bmkh\s*(\d+\w*)", re.I), "Sennheiser MKH{1}"),
    # DPA: 4023, 4060, 4061, 4011
    (re.compile(r"\bdpa\s*(\d{4})", re.I), "DPA {1}"),
    (re.compile(r"\bdpa\b", re.I), "DPA"),
    # Nakamichi: CM-300, CM-100, CR-7A, DR-3, and the 'nak100'/'nak1k' shorthand
    (re.compile(r"\bnak(?:amichi)?\s*(cm-?\s*\d+\w*|dr-?\s*\d+\w*|cr-?\s*\d+\w*)", re.I), "Nak {1}"),
    (re.compile(r"\bnak\s*(\d+\w*)", re.I), "Nak {1}"),
    (re.compile(r"\bnak\b", re.I), "Nak"),
    # Sony: PCM-D50, PCM-D100, PCM-M10, TC-D5, TC-D5M, D3/D7/D8 DAT decks
    (re.compile(r"\bsony\s*(pcm-?[dm]\s*\d+\w*|tc-?d\s*\d+\w*|d\s*\d+)", re.I), "Sony {1}"),
    # Core Sound binaurals
    (re.compile(r"\bcore\s*sound\s*(\w+)?", re.I), "CoreSound {1}"),
    (re.compile(r"\bbinaural\b", re.I), "Binaural"),
    # Shure KSM / SM series
    (re.compile(r"\bshure\s*(ksm\d+|sm\d+)", re.I), "Shure {1}"),
    # Microtech Gefell (M300, UMT70S, etc.)
    (re.compile(r"\bmicrotech\s*gefell\s*([a-z]{0,4}\s*\d+\w*)", re.I), "MT Gefell {1}"),
    (re.compile(r"\bmicrotech\s*gefell\b", re.I), "MT Gefell"),
    # Milab: VM-44 Link is the community favourite
    (re.compile(r"\bmilab\s*(vm-?\s*\d+\w*(?:\s*link)?|[a-z]+\d+\w*)", re.I), "Milab {1}"),
    # Earthworks: SR40V, SR20, QTC-series
    (re.compile(r"\bearthworks\s*(sr\s*\d+\w*|qtc\s*\d+\w*|[a-z]{1,3}\s*\d+\w*)", re.I), "Earthworks {1}"),
    # Audio-Technica: AT4050, AT4053, AT853
    (re.compile(r"\baudio[-\s]?technica\s*(at\s*\d+\w*)", re.I), "AT {1}"),
    (re.compile(r"\bat\s*(\d{3,4}\w*)\b", re.I), "AT {1}"),
    # --- Recorders / preamps (not mics, but part of the rig identifier) ---
    # Tascam: DR-07, DR-40, DR-100, DR-680, DA-3000, DA-20, HD-P2, Portastudio
    (re.compile(r"\btascam\s*(dr-?\s*\d+\w*|da-?\s*\d+\w*|hd-?p?\s*\d+\w*|portastudio\s*\d+)", re.I), "Tascam {1}"),
    (re.compile(r"\btascam\b", re.I), "Tascam"),
    # Edirol / Roland handhelds: R-44, R-09, R-09HR, UA-5
    (re.compile(r"\bedirol\s*(r-?\s*\d+\w*|ua-?\s*\d+\w*)", re.I), "Edirol {1}"),
    (re.compile(r"\bedirol\b", re.I), "Edirol"),
    # Zoom: H1/H2/H4n/H5/H6/F3/F6/F8
    (re.compile(r"\bzoom\s*([hf]\s*\d+\w*)", re.I), "Zoom {1}"),
    # Sound Devices: 722, 744, MixPre
    (re.compile(r"\bsound\s*devices\s*(mix\s*pre\s*\d+\w*|\d{3,4}\w*)", re.I), "SD {1}"),
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
