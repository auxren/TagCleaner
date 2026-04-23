"""Parse concert metadata out of info.txt files, folder names, and filenames.

The parser is intentionally forgiving: it tries several strategies and returns
a best-effort `Concert` with `issues` listing anything it couldn't resolve.
The caller (CLI) shows these to the user before any tags are written.
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from dateutil import parser as dateparser

from .lexicon import Lexicon
from .models import Concert, Track
from .sources import detect_source

# Parent-folder names that look tempting to treat as an artist but almost
# never are in a bootleg library ("/Music/Tapes/<concert>/" → parent is the
# library root). Matched case-insensitively against the normalized parent
# name before consulting the lexicon.
_NOT_AN_ARTIST = frozenset({
    "tapes", "bootlegs", "concerts", "live", "shows", "downloads",
    "music", "archive", "archives", "audio", "recordings", "files",
    "sbd", "aud", "flac", "mp3", "shn", "wav", "cd", "dvd",
    "new", "unsorted", "misc", "various", "various artists",
    "library", "collection",
})

# Dates can sit flush against an artist abbreviation ('los1996-03-20') or
# inside an underscore-joined filename ('SRV_1985.0725'), so we don't require
# a word boundary before the year.
ISO_DATE = re.compile(r"(?<!\d)(19|20)\d{2}[-._/](0?[1-9]|1[0-2])[-._/](0?[1-9]|[12]\d|3[01])(?!\d)")
SHORT_DATE = re.compile(r"(?<!\d)(\d{2})[-._](0?[1-9]|1[0-2])[-._](0?[1-9]|[12]\d|3[01])(?!\d)")
COMPACT_DATE = re.compile(r"(?<!\d)(19|20)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
# 'YYYY.MMDD' or 'YYYY_MMDD' (seen in filenames like SRV_1985.0725)
SPLIT_COMPACT = re.compile(r"(?<!\d)(19|20)(\d{2})[._](0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
# US-style MM/DD/YYYY with full 4-digit year ('02/19/2010', '11.10.2023')
US_FULL_DATE = re.compile(
    r"(?<!\d)(0?[1-9]|1[0-2])[-._/](0?[1-9]|[12]\d|3[01])[-._/]((?:19|20)\d{2})(?!\d)"
)
# US-style MM_DD_YY with 2-digit year ('11_10_23', '09-01-89'). Year ≥ 60 is
# interpreted as 19xx, otherwise 20xx — matches the etree/archive convention.
US_SHORT_DATE = re.compile(
    r"(?<!\d)(0?[1-9]|1[0-2])[-._/](0?[1-9]|[12]\d|3[01])[-._/](\d{2})(?!\d)"
)
PROSE_DATE = re.compile(
    r"\b(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+)?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?[,\s]+(\d{4})",
    re.I,
)
MONTH_DAY_YEAR = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?[,\s]+(\d{4})",
    re.I,
)
# Bare year (used only as an artist/date boundary in folder names, never as a
# full parsed date — "1985" alone gives you a year, not a calendar date).
YEAR_ONLY = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

# Artist-abbreviation prefixes used in etree-style filenames like `gd67-08-05`.
# Baseline is the community list at https://wiki.etree.org/index.php?page=BandAbbreviations
# plus common prefixes seen in the wild. Keep keys lowercase; lookups downcase.
ARTIST_PREFIX_MAP: dict[str, str] = {
    # etree wiki
    "3ah": "3 Apples High", "bog": "A Band of Gypsys", "ahf": "Alan Hertz and Friends",
    "abb": "Allman Brothers Band", "amf": "Amfibian", "bcue": "Barbara Cue",
    "bfm": "Barefoot Manner", "beanland": "Beanland", "bfft": "Bela Fleck & the Flecktones",
    "bftt": "Bela Fleck & Tony Trischka", "bh": "Ben Harper",
    "bhic": "Ben Harper and the Innocent Criminals", "bij": "Big In Japan",
    "bs": "Big Smith", "wu": "The Big Wu", "bmelon": "Blind Melon",
    "bgug": "Blueground Undergrass", "bt": "Blues Traveler", "be": "Bockman's Euphio",
    "bp": "Brothers Past", "bruce": "Bruce Hornsby", "buho": "El Buho",
    "bts": "Built to Spill", "bspear": "Burning Spear", "bnb": "Burt Neilson Band",
    "cvb": "Camper van Beethoven", "ccity": "Cerulean City", "ch": "Charlie Hunter",
    "cbobb": "Col. Claypool's Bucket of Bernie Brains", "cc": "Counting Crows",
    "cj": "Cowboy Junkies", "cracker": "Cracker", "cb": "Critters Buggin",
    "dso": "Dark Star Orchestra", "d&t": "Dave Matthews & Tim Reynolds",
    "dm": "Dave Matthews", "dmb": "Dave Matthews Band", "dgray": "David Gray",
    "dnb": "David Nelson Band", "dbr": "Day by the River", "dead": "The Dead",
    "dbb": "Deep Banana Blackout", "dt": "Derek Trucks Band",
    "ddbb": "Dirty Dozen Brass Band", "db": "Disco Biscuits", "disp": "Dispatch",
    "logic": "DJ Logic", "dtb": "Donna the Buffalo", "dbt": "Drive-By Truckers",
    "eb": "Edie Brickell", "eh": "Ekoostik Hookah", "farrah": "Farrah",
    "fhg": "Fareed Haque Group", "glove": "G. Love and Special Sauce",
    "gal": "Galactic", "garaj": "Garaj Mahal", "garcia": "Jerry Garcia",
    "jgb": "Jerry Garcia Band", "porter": "George Porter, Jr. & Runnin' Pardners",
    "gt": "Ghost Trane", "gsw": "God Street Wine", "mule": "Gov't Mule",
    "gtb": "Grand Theft Bus", "gd": "Grateful Dead", "gba": "GreyBoy AllStars",
    "gus": "Guster", "guymalone": "Guy Malone", "ht": "Hot Tuna",
    "hday": "Howie Day", "itf": "Indiana Trip Factory", "ig": "Indigo Girls",
    "jj": "Jack Johnson", "jmp": "Jazz Mandolin Project", "jt": "Jeff Tweedy",
    "jhe": "Jimi Hendrix Experience", "jmayer": "John Mayer",
    "jm3": "John Mayer Trio", "sco": "John Scofield", "jol": "Jolene",
    "jk": "Jorma Kaukonen", "kaikln": "Kai Kln", "kdcw": "Karl Denson and Chris Wood",
    "kdtu": "Karl Denson's Tiny Universe", "kw": "Keller Williams",
    "kwi": "Keller Williams w/ String Cheese Incident", "sk": "Kimock",
    "kk": "Kudzu Kings", "kvhw": "KVHW", "lt": "Lake Trout",
    "ls": "Leftover Salmon", "lom": "Legion of Mary",
    "fb": "Les Claypool's Fearless Flying Frog Brigade", "laf": "Life After Failing",
    "lf": "Little Feat", "ld": "Living Daylights", "lfb": "Lo Faber Band",
    "mammals": "The Mammals", "mel": "Marcus Eaton & The Lobby", "marlow": "Marlow",
    "mf": "Marc Ford", "mmw": "Medeski Martin & Wood",
    "mfs": "Michael Franti & Spearhead", "mcrx": "Mike Clark's Prescription Renewal",
    "moe": "moe.", "tn": "The Nadas", "nd": "The New Deal",
    "nmas": "North Mississippi Allstars", "oar": "O.A.R.",
    "too": "The Other Ones", "or": "Oregon", "oh": "Oysterhead",
    "par": "Particle", "metheny": "Pat Metheny", "pm": "Pat Metheny",
    "pmg": "Pat Metheny Group", "pmt": "Pat Metheny Trio", "pmb": "Pat McGee Band",
    "pj": "Pearl Jam", "phil": "Phil Lesh & Friends", "ph": "Phish",
    "phish": "Phish", "pb": "Psychedelic Breakfast", "rad": "Radiators",
    "rre": "Railroad Earth", "raq": "Raq", "ratdog": "RatDog",
    "rg": "Reid Genauer", "reservoir": "Reservoir", "rezi": "Rezi",
    "rr": "Robert Randolph & the Family Band", "rwtc": "Robert Walter's 20th Congress",
    "schas": "Santa Cruz Hemp Allstars", "ho": "Schleigho",
    "schwillb": "The Schwillbillies", "amendola": "Scott Amendola Band",
    "spod": "Serial Pod", "sy": "Seth Yacovone Band", "sexmob": "Sex Mob",
    "st": "Shaking Tree", "slip": "The Slip", "soulive": "Soulive",
    "sts9": "Sound Tribe Sector 9", "spin": "Spin Doctors",
    "kmck": "Steve Kimock & Friends", "skb": "Steve Kimock Band",
    "ss": "Stockholm Syndrome", "sf": "Strangefolk", "sci": "String Cheese Incident",
    "tlg": "Tea Leaf Green", "tend": "Tenacious D", "tr": "Tim Reynolds",
    "tortoise": "Tortoise", "hip": "The Tragically Hip",
    "trey": "Trey Anastasio", "um": "Umphrey's McGee", "us": "Uncle Sammy",
    "vb": "Vida Blue", "wh": "Warren Haynes", "ween": "Ween",
    "wsp": "Widespread Panic", "wilco": "Wilco", "wb4t": "Will Bernard 4tet",
    "willyp": "Willy Porter", "word": "The Word",
    "ymsb": "Yonder Mountain String Band", "zero": "Zero", "zm": "Zony Mash",
    "zwan": "Zwan", "bc": "Black Crowes",
    # Extras commonly seen in Tapes/etree trees beyond the wiki.
    "los": "Los Lobos", "bw": "Bob Weir", "bob": "Bob Dylan",
    "bd": "Bob Dylan", "rush": "Rush", "srv": "Stevie Ray Vaughan",
    "u2": "U2", "rhcp": "Red Hot Chili Peppers",
    "nrps": "New Riders of the Purple Sage", "fur": "Furthur",
    "rp": "Robert Plant", "tp": "Tom Petty",
    "zzt": "ZZ Top", "vm": "Van Morrison", "pf": "Pink Floyd",
    "lz": "Led Zeppelin", "rem": "R.E.M.",
    # Mined from a 1000-item random sample of archive.org/etree (April 2026).
    # Only prefixes with high purity in the corpus (all-or-nearly-all pointing
    # to a single creator) are added here; ambiguous ones (dtb, los, bs) are
    # deliberately left as whatever the original wiki list said.
    "ttb": "Tedeschi Trucks Band", "furthur": "Furthur", "yarn": "Yarn",
    "jrad": "Joe Russo's Almost Dead", "twiddle": "Twiddle",
    "gsbg": "Greensky Bluegrass", "rd": "RatDog", "loslobos": "Los Lobos",
    "jjj": "Jerry Joseph and the Jackmormons", "jauntee": "The Jauntee",
    "nma": "North Mississippi Allstars", "bloodkin": "Bloodkin",
    "tsp": "The Smashing Pumpkins", "hbr": "Hot Buttered Rum",
    "plf": "Phil Lesh & Friends", "paf": "Phil Lesh & Friends",
    "dubapoc": "Dub Apocalypse", "billystrings": "Billy Strings",
    "eggy": "Eggy", "pgroove": "Perpetual Groove", "rh": "Robert Hunter",
    "deadco": "Dead & Company", "hyryder": "Hyryder", "spafford": "Spafford",
    "zendog": "ZenDog", "mudhoney": "Mudhoney",
    # N=2 in sample but the prefix is the band's own name or an
    # unambiguous community shorthand.
    "aq": "Aqueous", "breakfast": "The Breakfast", "goose": "Goose",
    "danieldonato": "Daniel Donato", "galactic": "Galactic",
    "particle": "Particle", "guster": "Guster", "radiators": "Radiators",
    "isd": "Infamous Stringdusters", "sts": "Sound Tribe Sector 9",
    "fru": "Fruition", "osp": "Ominous Seapods",
    "bhtm": "Big Head Todd & the Monsters", "jm": "John Mayer",
    "tbt": "Trampled by Turtles",
}

DISC_MARKER = re.compile(
    r"^\s*(?:"
    r"(?:disc|cd|disk)\s*(?P<num1>[0-9]+|one|two|three|four|five|six|seven|eight)\b"
    r"|(?:set)\s*(?P<num2>[0-9]+|i{1,3}|iv|v|one|two|three|four)\b"
    r"|(?P<encore>encore)\b"
    r"|(?P<early>early\s*show)\b"
    r"|(?P<late>late\s*show)\b"
    r"|(?P<matinee>matinee(?:\s+show)?)\b"
    r"|(?P<evening>evening(?:\s+show)?)\b"
    r")",
    re.I,
)

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}

# Track line forms:
#   01 - Title / 01. Title / 01) Title / 01 Title / t01 Title / d1t05 Title
TRACK_LINE = re.compile(
    r"^\s*(?:d\d+)?[ts]?(\d{1,3})\s*[-.)\s]\s*(.+?)\s*$", re.I,
)

# Lines we treat as "not a track" even if they look like one:
TRACK_SKIP = re.compile(
    r"^\s*\d+\.?\s*(?:md5|ffp|sha\d+|bytes?|samples?|kb|mb|gb)\b", re.I,
)


def parse_date(text: str) -> str | None:
    """Return ISO YYYY-MM-DD from *text* or None. Prefers ISO format hits."""
    from datetime import date as _date
    m = ISO_DATE.search(text)
    if m:
        try:
            y = int(text[m.start(): m.start() + 4])
            mo = int(m.group(2))
            d = int(m.group(3))
            return _date(y, mo, d).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            pass
    for pat in (COMPACT_DATE, SPLIT_COMPACT):
        m = pat.search(text)
        if m:
            try:
                y = int(text[m.start(): m.start() + 4])
                mo = int(m.group(3))
                d = int(m.group(4))
                return _date(y, mo, d).strftime("%Y-%m-%d")
            except (ValueError, OverflowError):
                pass
    m = US_FULL_DATE.search(text)
    if m:
        try:
            mo = int(m.group(1))
            d = int(m.group(2))
            y = int(m.group(3))
            return _date(y, mo, d).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            pass
    m = PROSE_DATE.search(text) or MONTH_DAY_YEAR.search(text)
    if m:
        try:
            dt = dateparser.parse(m.group(0), fuzzy=True)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            pass
    m = US_SHORT_DATE.search(text)
    if m:
        try:
            mo = int(m.group(1))
            d = int(m.group(2))
            yy = int(m.group(3))
            year = 1900 + yy if yy >= 60 else 2000 + yy
            return _date(year, mo, d).strftime("%Y-%m-%d")
        except (ValueError, OverflowError):
            pass
    m = SHORT_DATE.search(text)
    if m:
        yy = int(m.group(1))
        year = 1900 + yy if yy >= 60 else 2000 + yy
        mo, day = int(m.group(2)), int(m.group(3))
        try:
            return _date(year, mo, day).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _expand_artist_prefix(folder_name: str) -> str | None:
    """'gd67-08-05.sbd...' -> 'Grateful Dead'."""
    m = re.match(r"^([a-z]+)\d", folder_name.lower())
    if not m:
        return None
    return ARTIST_PREFIX_MAP.get(m.group(1))


_DATE_FINDERS = (
    ISO_DATE, COMPACT_DATE, SPLIT_COMPACT,
    US_FULL_DATE, US_SHORT_DATE, SHORT_DATE,
    PROSE_DATE, MONTH_DAY_YEAR,
)


def _first_date_position(text: str) -> int | None:
    """Position of the first date-like or year-only substring in *text*.

    Returns the earliest match across every supported date format, falling
    back to a bare four-digit year. Used by folder-name parsing to locate the
    artist/date boundary without being picky about which format is in play.
    """
    best: int | None = None
    for pat in _DATE_FINDERS:
        m = pat.search(text)
        if m is not None and (best is None or m.start() < best):
            best = m.start()
    m = YEAR_ONLY.search(text)
    if m is not None and (best is None or m.start() < best):
        best = m.start()
    return best


def _clean_artist_candidate(text: str) -> str | None:
    """Normalise a raw 'everything before the date' string into an artist.

    Strips leading/trailing separator junk, splits at the first ` - ` or
    ` (`, and returns the first chunk if it looks plausibly like a band name.
    """
    t = text.strip(" -,_()[]\t")
    # Cut at the first ' - ' or ' (' so compound prefixes like
    # "Steel Pulse - The Palace, Hollywood" reduce to just "Steel Pulse".
    t = re.split(r"\s+-\s+|\s+\(", t, maxsplit=1)[0]
    t = t.strip(" -,_()[]\t")
    if not t or not any(ch.isalpha() for ch in t):
        return None
    if len(t) < 2 or len(t) > 60:
        return None
    return t


def guess_artist_from_folder(folder_name: str) -> str | None:
    art = _expand_artist_prefix(folder_name)
    if art:
        return art
    # "YYYY-MM-DD - Artist - Venue" style: artist lives between the date and
    # the venue separator.
    m = re.match(r"^(?:19|20)\d{2}[-.]\d{2}[-.]\d{2}\s*-\s*([^-]+?)\s*-", folder_name)
    if m:
        return m.group(1).strip()
    pos = _first_date_position(folder_name)
    if pos is None or pos == 0:
        return None
    return _clean_artist_candidate(folder_name[:pos])


def _disc_from_marker(line: str) -> int | None:
    m = DISC_MARKER.search(line.strip())
    if not m:
        return None
    if m.group("encore"):
        return -1  # sentinel handled by caller
    if m.group("early") or m.group("matinee"):
        return 1
    if m.group("late") or m.group("evening"):
        return 2
    raw = (m.group("num1") or m.group("num2") or "").lower()
    if raw.isdigit():
        return int(raw)
    return _ROMAN.get(raw) or _WORDNUM.get(raw)


def parse_setlist(body: str) -> list[tuple[int | None, str]]:
    """Return list of (disc_number_or_None, track_title) in order.
    Disc is None for single-disc shows; otherwise 1-based or -1 for Encore."""
    lines = body.splitlines()
    current_disc: int | None = None
    results: list[tuple[int | None, str]] = []
    saw_disc_marker = False
    pending_encore = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if TRACK_SKIP.match(line):
            continue
        disc = _disc_from_marker(line)
        if disc is not None:
            saw_disc_marker = True
            if disc == -1:
                pending_encore = True
            else:
                current_disc = disc
                pending_encore = False
            continue
        m = TRACK_LINE.match(line)
        if not m:
            continue
        title = m.group(2).strip().strip(":-").strip()
        if not title or len(title) > 200:
            continue
        # Drop anything that's clearly a hash or md5 line
        if re.search(r"\b[a-f0-9]{16,}\b", title.lower()):
            continue
        disc_val = current_disc
        if pending_encore:
            # Encore becomes current_disc+1 once we decide totals later
            disc_val = (current_disc or 1) + 1
        results.append((disc_val if saw_disc_marker else None, title))
    return results


def read_info_txt(path: Path) -> str:
    """Read *path* with best-effort encoding detection.

    Real-world info.txt files show up in UTF-8, UTF-16 (Notepad's default on
    older Windows), and occasionally cp1252. BOM sniffing handles the explicit
    cases; a zero-byte-density heuristic catches UTF-16-LE files saved without
    a BOM, which would otherwise come out as mojibake.

    Mac TextEdit defaults to RTF when users save a file as `.txt` from the GUI,
    so info.txt files in the wild sometimes arrive as RTF. We detect the
    `{\\rtf` magic and strip control words / groups back to plain text.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16-le", errors="replace")
    elif raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16-be", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        sample = raw[:1024]
        text = None
        if len(sample) >= 4:
            odd_zeros = sample[1::2].count(0)
            even_zeros = sample[0::2].count(0)
            half = len(sample) // 2
            if half and odd_zeros / half > 0.3 and odd_zeros > even_zeros:
                text = raw.decode("utf-16-le", errors="replace")
            elif half and even_zeros / half > 0.3 and even_zeros > odd_zeros:
                text = raw.decode("utf-16-be", errors="replace")
        if text is None:
            for enc in ("utf-8", "cp1252", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = raw.decode("utf-8", errors="replace")
    if text.lstrip().startswith("{\\rtf"):
        text = _strip_rtf(text)
    return text


def _strip_rtf(text: str) -> str:
    """Convert RTF source to plain text. Drops control words, font/color
    tables, and unbalanced braces; keeps paragraph breaks via \\par/\\line."""
    # Drop entire \fonttbl, \colortbl, \stylesheet, etc. groups (one level of
    # nested braces supported). Optional \* destination marker handled.
    text = re.sub(
        r"\{\\(?:\*\\)?(?:fonttbl|colortbl|stylesheet|listtable|listoverridetable|rsidtbl|generator|info|filetbl|pict|themedata|datastore|latentstyles|xmlnstbl|revtbl|fldrslt|bkmkstart|bkmkend)\b[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
        "",
        text,
    )
    # Convert paragraph/line breaks to newlines.
    text = re.sub(r"\\(?:par|line|pard)\b\s?", "\n", text)
    # Decode \uXXXX? unicode escapes (RTF stores codepoint as decimal, optional ?).
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), text)
    # Decode \'hh hex byte escapes (assume cp1252).
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", errors="replace"), text)
    # Drop remaining control words like \ansi, \cocoartf1138, \fs24, \cf2, etc.
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)
    # Drop control symbols like \\ \{ \} (we already handled escapes we care about).
    text = re.sub(r"\\[^a-zA-Z]", "", text)
    # Strip leftover braces.
    text = text.replace("{", "").replace("}", "")
    # Collapse triple+ blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_info_txt(body: str) -> dict:
    """Extract artist/date/venue/city/region plus the raw setlist from *body*.
    Returns a dict with keys artist, date, venue, city, region, setlist.
    Any field may be missing."""
    lines = [ln.rstrip() for ln in body.splitlines()]
    nonblank = [ln.strip() for ln in lines if ln.strip()]
    data: dict = {"setlist": parse_setlist(body)}

    artist = _first_artist_line(nonblank)
    if artist:
        data["artist"] = artist

    # Labeled fields take priority.
    for pat, key in [
        (r"artist\s*[:\-]\s*(.+)", "artist"),
        (r"band\s*[:\-]\s*(.+)", "artist"),
        (r"venue\s*[:\-]\s*(.+)", "venue"),
        (r"location\s*[:\-]\s*(.+)", "venue"),
        (r"city\s*[:\-]\s*(.+)", "city"),
        (r"date\s*[:\-]\s*(.+)", "_datetxt"),
    ]:
        m = re.search(pat, body, re.I)
        if m:
            val = m.group(1).strip().strip(",")
            if not val:
                continue
            # Taper "Location:" lines almost always describe mic placement,
            # not a venue. Reject values that look like placement jargon so
            # they don't win over real venue lines parsed later.
            if key in ("venue", "artist") and _MIC_PLACEMENT.search(val):
                continue
            # "Venue: dsp-quattro > MBIT+ > flac" is a labelled lineage chain,
            # not a venue. Same for "(D-sbd), recorded from...".
            if key == "venue" and (
                _LINEAGE_CHAIN.search(val) or _NON_VENUE_PREFIX.match(val)
            ):
                continue
            data[key] = val

    data["date"] = parse_date(body)

    # Venue + city: look at the first ~10 lines, skipping the line we used as
    # artist. We scan from position 0 (not 1) because the artist may have been
    # rejected — in which case the venue is the first line.
    for ln in nonblank[:10]:
        if artist and ln == artist:
            continue
        if parse_date(ln):  # date line
            continue
        if DISC_MARKER.search(ln):
            break
        # Reject setlist entries like "01. Steel Pulse - intro" — those slip
        # past _looks_like_venue (they have uppercase, no source code, etc.)
        # and then get promoted to venue simply because they come first.
        if TRACK_LINE.match(ln):
            continue
        if "venue" not in data or "city" not in data:
            v, c, r = _split_venue_city_region(ln)
            if v and c and _looks_like_venue(v):
                data.setdefault("venue", v)
                data.setdefault("city", c)
                if r:
                    data.setdefault("region", r)
                continue
        if "city" not in data and _looks_like_city(ln):
            city, region = _split_city_region(ln)
            data["city"] = city
            if region:
                data["region"] = region
            continue
        if "venue" not in data and _looks_like_venue(ln):
            data["venue"] = ln.strip(",")
            continue
    return data


_NOISE_FIRST_LINE = re.compile(
    r"^(?:no\s+errors?|errors?\s+found|\s*$|"
    r"https?://|www\.|"
    r"\d+\s*(?:kbps|khz|bit|byte|mb|gb)|"
    r"flac\s+fingerprint|md5|sha\d+|"
    r"tracklist|setlist|recording\s+info|lineage|source|notes?|file\s*info|"
    r"audiochecker|shntool|sbe(?:ok)?|ffp|checksum)",
    re.I,
)

# Descriptive-sentence openers that almost always introduce a note or
# comment about the recording, not the artist name. "This is an incredible
# show…", "Recorded from the soundboard…", "Taped by…", etc.
_SENTENCE_OPENER = re.compile(
    r"^(?:this\s+(?:is|was|recording|show|tape)|these\s+are|"
    r"recorded|taped|taping|transferred|transfer\s+from|mastered|"
    r"dedicated|thanks|thank\s+you|please|note|the\s+following|"
    r"here\s+(?:is|are)|my\s+(?:cassette|tape|copy)|"
    r"(?:all|most)\s+(?:song|track)s?)\b",
    re.I,
)

# Source / lineage codes that, when present in a line, signal it's taper
# metadata rather than an artist name. Matches 'SBD', 'SBD4', 'AUD2', 'DAT',
# 'FM', 'MTX', 'MATRIX' as whole tokens (with optional trailing digits).
_SOURCE_CODE_TOKEN = re.compile(
    r"\b(?:SBD|AUD|DAT|MTX|MATRIX|DAUD|ALD|FOB|ROIO|FLAC|SHN)\d*\b",
    re.I,
)

# Any 2+ dot/dash/slash-separated numeric run — catches dates that
# parse_date misses because of surrounding noise ('8-21-87', '10/9/09').
_EMBEDDED_DATE_SHAPE = re.compile(
    r"\b\d{1,4}[-._/]\d{1,2}[-._/]\d{1,4}\b"
)

# Words that strongly suggest the line is a venue, not an artist. Used to stop
# venue-only first lines (e.g. "Henry J. Kaiser Convention Center, Oakland, CA")
# from getting classified as the artist.
_VENUE_KEYWORDS = re.compile(
    r"\b(?:arena|stadium|amphitheat(?:re|er)|coliseum|colosseum|"
    r"cent(?:er|re)|theat(?:re|er)|auditorium|hall|club|pavilion|"
    r"fairgrounds?|civic|gardens?|grounds?|bowl|palace|"
    r"rink|ballroom|casino|university|college|dome|forum|lounge|"
    r"speedway|festival|tabernacle)\b",
    re.I,
)

# Mic-placement shorthand — these lines describe where the taper stood, not a
# venue or artist. ("FOB, 10ft DIN, mics clamped to the rail", "DFC / ORTF",
# "Main Stage, at the SBD, ROC"). Reject any line whose dominant signal is
# placement jargon.
_MIC_PLACEMENT = re.compile(
    r"\b(?:FOB|DFC|OTS|ROC|DIN|ORTF|NOS|XY|AB|M/?S|PAS|"
    r"mics?|capsules?|pair|clamped|stand|stands|stack|stacks|"
    r"omni|omnis|cards?|cardioid|hypercard|subcard|"
    r"\d+\s*(?:ft|feet|'|\u2019|m|meters?)\s+(?:from|high|tall|up|back|off)|"
    r"from\s+(?:the\s+)?(?:stage|soundboard|sbd|board)|"
    r"(?:on|at)\s+(?:the\s+)?(?:stage|sbd|soundboard|board|rail)|"
    r"row\s+[a-z]\b|right\s+of\s+center|left\s+of\s+center|"
    r"right\s+stack|left\s+stack|balcony)\b",
    re.I,
)

# Signal-chain / lineage notation — "dsp-quattro 3 > MBIT+ > 16/44.1 wav > flac"
# or "AKG C568 → Sound Devices 722 → WAV". These describe transfer chains, not
# venues. Any whitespace-flanked arrow is enough; real venue names don't carry
# arrow separators.
_LINEAGE_CHAIN = re.compile(r"\s[>→]\s|\s->\s")

# Taper-notes section headers and parenthesised source-kind prefixes that
# sometimes lead a standalone line: "Transfer Info: ...", "The Recording: ...",
# "(D-sbd), recorded from the board". These are never venue names.
_NON_VENUE_PREFIX = re.compile(
    r"^\s*(?:"
    r"transfer(?:\s+info)?|recording(?:\s+info|\s+notes)?|the\s+recording|"
    r"lineage|source(?:\s+info)?|"
    r"tap(?:er|ed(?:\s+by)?)|transferred|mastered|setup|equipment|"
    r"\(\s*[A-Za-z][A-Za-z0-9\-\s]{0,14}\s*\)[,\s]"
    r")",
    re.I,
)


def _first_artist_line(nonblank: list[str]) -> str | None:
    """Return the first line that plausibly names the artist.
    Skips obvious boilerplate (URLs, checksum logs, 'No errors occured.', etc.)
    and lines that look like a venue or 'City, ST'.
    """
    for line in nonblank[:6]:
        if _NOISE_FIRST_LINE.match(line):
            continue
        if parse_date(line):
            continue
        if DISC_MARKER.search(line):
            break
        stripped = line.strip("*#=-_ \t")
        if not stripped:
            continue
        letters = sum(ch.isalpha() for ch in stripped)
        if letters < 2 or len(stripped) > 100:
            continue
        # Reject descriptive sentences ("This is an incredible show...") and
        # taper-metadata lines ("Steel Pulse - Sunsplash - JA 8-21-87 SBD4").
        # These slip past _NOISE_FIRST_LINE because they start with an
        # arbitrary word but are never an artist name.
        if _SENTENCE_OPENER.match(stripped):
            continue
        if _SOURCE_CODE_TOKEN.search(stripped):
            continue
        if _EMBEDDED_DATE_SHAPE.search(stripped):
            continue
        if _VENUE_KEYWORDS.search(stripped):
            continue
        if _MIC_PLACEMENT.search(stripped):
            continue
        if _looks_like_city(stripped):
            continue
        # "Venue, City, ST" three-comma shape — also a venue line.
        if _split_venue_city_region(stripped)[0]:
            continue
        return stripped
    return None


def _looks_like_venue(line: str) -> bool:
    """Very loose filter for lines that could name a venue. Bails out on
    obvious boilerplate (audiochecker logs, lineage lines, URLs, etc.)."""
    if len(line) < 3 or len(line) > 120:
        return False
    if _NOISE_FIRST_LINE.match(line):
        return False
    if _NON_VENUE_PREFIX.match(line):
        return False
    if _LINEAGE_CHAIN.search(line):
        return False
    if re.search(r"\d{5,}", line):
        return False
    low = line.lower()
    if any(tok in low for tok in ("http://", "https://", "@", "flac ", ".flac", ".shn", ".wav", "kbps", "khz")):
        return False
    # Dense-digit lines (>30% digits) are typically mangled date/time stamps,
    # not venues. Catches 'MM/DD/YYYY - Friday' that slipped through parse_date.
    digits = sum(ch.isdigit() for ch in line)
    if digits and digits / max(len(line), 1) > 0.3:
        return False
    if _MIC_PLACEMENT.search(line):
        return False
    return bool(re.search(r"[A-Z]", line))


def _looks_like_city(line: str) -> bool:
    # City or "City, ST" or "City, Country"
    return bool(re.match(r"^[A-Z][A-Za-z.'\- ]+,\s*[A-Za-z][A-Za-z.'\- ]{1,}$", line))


def _split_city_region(line: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in line.split(",", 1)]
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], None


# Deep box-set trees (``/Artist/Artist - Archives-NN/archives.vols_01-17/Volume XV/disc/<show>/CD 2``)
# push the artist folder 6–8 levels above the leaf. Cap the walk so we don't
# climb forever into unrelated trees; ``_NOT_AN_ARTIST`` stops us earlier
# when we reach the library root.
_ANCESTOR_DEPTH = 8


def _lexicon_artist_from_parent(folder: Path, lexicon: Lexicon) -> str | None:
    """Walk up to ``_ANCESTOR_DEPTH`` ancestors looking for a known artist.

    Date-first folders (``/Tapes/Black Sabbath/1969-XX-XX Show/``) leave the
    parser without anything to guess from, but somewhere up the chain the
    artist usually appears as a folder name. For each ancestor we try the
    bare name plus a couple of cleanups (``Bruce Springsteen 1978-...``,
    ``Bob Dylan - Tour Title``, ``06 Bob Dylan-...``) before consulting the
    lexicon. We stop the moment we hit an organisational wrapper like
    ``Tapes`` — that means we've left the artist's tree.
    """
    current = folder.parent
    for _ in range(_ANCESTOR_DEPTH):
        if current is None or current == current.parent:
            return None
        name = current.name
        if not name or name in (".", "/"):
            return None
        if name.lower() in _NOT_AN_ARTIST:
            # "Various Artists" is a real artist tag for compilation albums —
            # treat the wrapper as the artist instead of stopping the walk.
            if name.lower() in ("various artists", "various"):
                return "Various Artists"
            return None
        # Year-only or date-first wrappers ("1987/", "2003-12-17 Show") aren't
        # artists — skip them but keep walking to the next ancestor.
        if YEAR_ONLY.fullmatch(name.strip()) or _first_date_position(name) == 0:
            current = current.parent
            continue
        for candidate in _ancestor_candidates(name):
            match = lexicon.match_artist(candidate)
            if match:
                return match
        current = current.parent
    return None


# Format/container wrapper names that we *skip past* when looking for a
# trustworthy artist ancestor (vs. _NOT_AN_ARTIST which marks the library
# root and terminates the walk).
_FORMAT_ANCESTORS = frozenset({
    "audio", "files", "recordings", "tracks", "music", "songs",
    "flac", "flac files", "mp3", "wav", "shn", "shnf",
    "cd", "cd1", "cd2", "cd3", "dvd", "disc", "disc 1", "disc 2",
    "sbd", "aud",
})


def _trust_parent_artist(folder: Path) -> str | None:
    """Walk up the ancestors to find a clean artist-shape name when the
    lexicon couldn't confirm one.

    Catches two cases:
      * brand-new ``Tapes/Artist/Show/`` libraries where the artist has zero
        prior history (e.g. ``Oysterhead``, ``Frank Sinatra``);
      * deep ``Show/Show/Audio/CD1`` wrappers where the artist sits 3+ levels
        up — we skip format containers (Audio, FLAC) and walk further.

    For each ancestor we try the bare name and the ``_ancestor_candidates``
    splits (so ``"Eric Clapton and Dr. John - 1996-01-13 - London"`` yields
    ``"Eric Clapton and Dr. John"``).
    """
    current = folder.parent
    for _ in range(_ANCESTOR_DEPTH):
        if current is None or current == current.parent:
            return None
        name = current.name
        if not name or name in (".", "/"):
            return None
        nlow = name.lower()
        # Format containers — skip past them (checked before _NOT_AN_ARTIST
        # because "flac"/"audio" appear in both sets).
        if nlow in _FORMAT_ANCESTORS:
            current = current.parent
            continue
        if nlow in _NOT_AN_ARTIST:
            if nlow in ("various artists", "various"):
                return "Various Artists"
            return None
        if name.startswith(("[", "(", "_", ".")):
            return None
        if YEAR_ONLY.fullmatch(name.strip()) or _first_date_position(name) == 0:
            current = current.parent
            continue
        # When the name has a " - " separator, only trust the head if what
        # follows looks like a date (`Artist - 1996-01-13 - Venue` pattern).
        # `Cajun - Zydeco` (no date in tail) means this is a category folder
        # and we should walk up to find a real artist ancestor.
        if " - " in name:
            head, rest = name.split(" - ", 1)
            head = head.strip()
            if rest and (YEAR_ONLY.search(rest[:30]) or _first_date_position(rest) is not None):
                if (2 <= len(head) <= 60
                        and re.match(r"^[A-Za-z0-9]", head)
                        and not YEAR_ONLY.search(head)):
                    return head
            current = current.parent
            continue
        # Bare clean name — but if it embeds a year, try to cut the artist
        # off the front ("BHIC 1998.08.08 Camden ..." -> "BHIC",
        # "Black_Sabbath_1974-02-21" -> "Black_Sabbath"). Use the same
        # cut-at-year/cut-at-hyphen logic as the lexicon walk.
        if YEAR_ONLY.search(name):
            # Try _ancestor_candidates' splits first.
            for candidate in _ancestor_candidates(name):
                cand = candidate.strip(" -,_([{")
                if not cand or cand == name or " - " in cand:
                    continue
                if YEAR_ONLY.search(cand):
                    continue
                if not re.match(r"^[A-Za-z0-9]", cand):
                    continue
                if 2 <= len(cand) <= 60:
                    return cand
            # Fallback: cut at the year position even when preceded by `_`
            # (which `_ancestor_candidates`' \b boundary doesn't catch — e.g.
            # "Black_Sabbath_1974-02-21").
            m = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", name)
            if m:
                head = name[: m.start()].strip(" -_,.()[]")
                if (head and " - " not in head and 2 <= len(head) <= 60
                        and re.match(r"^[A-Za-z0-9]", head)):
                    return head
            current = current.parent
            continue
        if not re.match(r"^[A-Za-z0-9]", name):
            return None
        if 2 <= len(name) <= 60:
            return name
        current = current.parent
    return None


def _ancestor_candidates(name: str) -> list[str]:
    """Variants of *name* worth probing against the lexicon.

    Folder names often pack the artist plus tour/year/disc trailers
    (``Bruce Springsteen 1978-1978 The Unbroken Promise``,
    ``Blackmore, Gillan, Glover, Lord, Paice (1970)``,
    ``06 Bob Dylan-Highlights from Temples in Flames Tour``); strip the
    trailers so the lexicon can hit the bare artist name.
    """
    seen: list[str] = []

    def _push(s: str | None) -> None:
        if not s:
            return
        s = s.strip(" -,_([{")
        if s and s not in seen:
            seen.append(s)

    _push(name)
    # Strip a leading track number ("06 Bob Dylan-..." → "Bob Dylan-...").
    no_track = re.sub(r"^\d{1,3}[\s._-]+", "", name)
    if no_track != name:
        _push(no_track)
        name_for_split = no_track
    else:
        name_for_split = name
    # Cut at the first 4-digit year ("Bruce Springsteen 1978-..." → "Bruce Springsteen").
    year = re.search(r"\b(19|20)\d{2}\b", name_for_split)
    if year and year.start() > 0:
        _push(name_for_split[: year.start()])
    # Cut at " - " separator ("Bob Dylan - Flames..." → "Bob Dylan").
    if " - " in name_for_split:
        _push(name_for_split.split(" - ", 1)[0])
    # Cut at "-" separator only when the head has spaces (avoids splitting
    # hyphenated band names like "Crosby-Nash" or single tokens).
    if "-" in name_for_split:
        head = name_for_split.split("-", 1)[0]
        if " " in head:
            _push(head)
    return seen


def build_concert(
    folder: Path,
    audio_files: Iterable[Path],
    info_txt: Path | None,
    *,
    lexicon: Lexicon | None = None,
) -> Concert:
    audio = list(audio_files)
    body = read_info_txt(info_txt) if info_txt else ""
    parsed = parse_info_txt(body) if body else {}

    folder_name = folder.name
    filenames = "  ".join(f.name for f in audio)

    artist = parsed.get("artist") or guess_artist_from_folder(folder_name)
    # Folder-name dates are almost always the concert date; info.txt bodies
    # often mention mastering/transfer dates that shouldn't win.
    date = parse_date(folder_name) or parse_date(filenames) or parsed.get("date")

    venue = parsed.get("venue")
    city = parsed.get("city")
    region = parsed.get("region")

    # Fall back to folder-name city guessing when info.txt was silent.
    if not city:
        city, region2 = _city_from_folder(folder_name)
        if city and not region:
            region = region2

    if lexicon is not None:
        if not artist:
            artist = _lexicon_artist_from_parent(folder, lexicon)
        else:
            match = lexicon.match_artist(artist)
            if match:
                artist = match
        if venue:
            match = lexicon.match_venue(venue)
            if match:
                venue = match
    if not artist:
        artist = _trust_parent_artist(folder)

    source = detect_source(folder_name, filenames, body)

    tracks = _finalize_tracks(parsed.get("setlist", []))

    issues: list[str] = []
    if not artist:
        issues.append("artist unknown")
    if not date:
        issues.append("date unknown")
    if not tracks and audio:
        issues.append("no setlist found")
    elif tracks and len(tracks) != len(audio):
        issues.append(f"track count mismatch: {len(tracks)} parsed vs {len(audio)} audio files")

    concert = Concert(
        folder=folder,
        artist=(artist or "").strip() or None,
        date=date,
        venue=(venue or "").strip() or None,
        city=(city or "").strip() or None,
        region=(region or "").strip() or None,
        source=source,
        tracks=tracks,
        audio_files=audio,
        info_txt=info_txt,
        issues=issues,
    )
    return concert


US_STATE_CODE = re.compile(r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")

_COUNTRY_NAMES = frozenset({
    "England", "Scotland", "Wales", "Ireland", "Canada", "Germany", "France",
    "Netherlands", "Italy", "Spain", "Japan", "Australia", "UK", "USA",
    "Mexico", "Belgium", "Sweden", "Norway", "Denmark", "Switzerland",
    "Austria", "Finland", "Poland", "Greece", "Portugal", "Brazil",
    "Argentina", "Russia", "China", "India",
})


def _split_venue_city_region(line: str) -> tuple[str | None, str | None, str | None]:
    """If *line* is 'Venue, City, ST' or 'Venue, City, Country', return
    (venue, city, region). Otherwise (None, None, None).

    The region is gated on a known state code or country so that generic
    three-comma lines ('Track 01, Part 1, extended') don't match.
    """
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None, None, None
    region = parts[-1]
    city = parts[-2]
    venue = ", ".join(parts[:-2]).strip()
    if not (venue and city and region):
        return None, None, None
    if US_STATE_CODE.fullmatch(region) or region in _COUNTRY_NAMES:
        return venue, city, region
    return None, None, None


def _city_from_folder(name: str) -> tuple[str | None, str | None]:
    """Extract (city, region) from a folder name. Handles:
      - 'Venue, City, ST'   (comma-separated)
      - 'City ST'           (bare two-letter state code)
      - 'City, Country'
    """
    m = re.search(r",\s*([A-Z][A-Za-z.'\- ]+?),\s*([A-Z]{2})\b", name)
    if m:
        return m.group(1).strip(), m.group(2)
    m = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+(" + US_STATE_CODE.pattern[2:-2] + r")\b", name)
    if m:
        return m.group(1).strip(), m.group(2)
    m = re.search(r",\s*([A-Z][A-Za-z.'\- ]+?),\s*(England|Scotland|Wales|Ireland|Canada|Germany|France|Netherlands|Italy|Spain|Japan|Australia|UK|USA)\b", name)
    if m:
        return m.group(1).strip(), m.group(2)
    return None, None


def _finalize_tracks(raw: list[tuple[int | None, str]]) -> list[Track]:
    """Number tracks (restart per disc) and fill disc_total.

    Single-disc setlists (even those introduced by a lone 'Disc One' marker)
    get no disc tags — disc tags are only meaningful when the show really
    spans multiple sets or CDs.
    """
    if not raw:
        return []
    distinct = {d for d, _ in raw if d is not None}
    # Single-disc case: flatten.
    if len(distinct) <= 1:
        return [Track(number=i + 1, title=t) for i, (_, t) in enumerate(raw)]
    # Renumber disc ids to 1..N in order of appearance.
    seen: dict[int, int] = {}
    for d, _ in raw:
        if d is None or d in seen:
            continue
        seen[d] = len(seen) + 1
    total = len(seen)
    tracks: list[Track] = []
    counter: dict[int, int] = {}
    fallback = next(iter(seen))
    for d, title in raw:
        disc = seen.get(d, seen[fallback])
        counter[disc] = counter.get(disc, 0) + 1
        tracks.append(Track(number=counter[disc], title=title, disc=disc, disc_total=total))
    return tracks
