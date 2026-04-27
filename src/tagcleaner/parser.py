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
#   01 - Title / 01. Title / 01) Title / 01 Title / t01 Title / d1t05 Title /
#   1: Title (etree-with-colon, used by some old taper info.txt) /
#   01_ Title (etree-with-underscore, seen on Bowie bootleg trees)
TRACK_LINE = re.compile(
    r"^\s*(?:d\d+)?[ts]?(\d{1,3})(?:\s*[-.)_\s]\s*|:\s+)(.+?)\s*$", re.I,
)

# Vinyl side-letter tracks — ``A1 Title``, ``B-2 Title``, ``D5 Title``.
# The side letter maps to a disc number (A=1, B=2, C=3, …). Caller treats
# this as a supplementary form when TRACK_LINE misses.
VINYL_TRACK_LINE = re.compile(
    r"^\s*([A-H])\s*[-.]?\s*(\d{1,2})(?:\s*[-.)_\s]\s*|:\s+)(.+?)\s*$",
)

# Lines we treat as "not a track" even if they look like one:
TRACK_SKIP = re.compile(
    r"^\s*\d+\.?\s*(?:md5|ffp|sha\d+|bytes?|samples?|kb|mb|gb)\b", re.I,
)

# Trailing "  1:09" / "  12:34" / "  :47" duration on a captured title —
# strip it so the title isn't polluted with the etree-style length suffix.
# Also handles bare "  :47" (no leading minutes), seen on intros/outros.
TRAILING_DURATION = re.compile(r"\s+\d{0,3}:\d{2}\s*$")


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

    Strips leading/trailing separator junk, leading sequencing numbers
    (``01 Godcaster`` → ``Godcaster``), splits at the first ` - ` or
    ` (`, and returns the first chunk if it looks plausibly like a
    band name.
    """
    t = text.strip(" -,_()[]\t")
    # Strip a leading 1-3 digit sequencing prefix ("01 Godcaster" →
    # "Godcaster"). Used by tapers to order opener/headliner folders.
    # Don't strip a digit-only or 4+ digit prefix (those are real
    # band-name fragments like "311" or "10,000 Maniacs").
    t = re.sub(r"^\d{1,3}\s+(?=[A-Za-z])", "", t)
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
    """Strong first pass: only fires when the folder name has a clear
    artist-before-date shape (``Talking Heads 1980-08-27 …``,
    ``Grateful Dead - 1987-08-22 - Calaveras``) or a registered etree
    prefix (``gd67-08-05``).

    Deliberately does NOT try to salvage ``1969 Black Sabbath`` (artist
    after a leading date) or bare ``All Them Witches`` folder names here —
    those belong to :func:`weak_artist_from_folder`, which is consulted
    only after the lexicon and parent-trust walks have had a chance to
    provide a more trustworthy answer.
    """
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


def _ancestor_is_various_artists(folder: Path) -> bool:
    """True if any ancestor folder is literally named 'Various Artists'
    (case-insensitive). Signal that *folder* sits inside a compilation
    layout and its own name is almost certainly the per-set artist.
    """
    current = folder.parent
    for _ in range(_ANCESTOR_DEPTH):
        if current is None or current == current.parent:
            return False
        if current.name.lower() in ("various artists", "various"):
            return True
        current = current.parent
    return False


def weak_artist_from_folder(folder_name: str) -> str | None:
    """Last-resort artist extraction from the folder name alone.

    Two shapes we handle:
      * Artist AFTER a leading date (``1969 Black Sabbath``,
        ``20150515 U2 Vancouver``): take the tail after the date.
      * Folder name IS the artist (``All Them Witches``, ``Howard Jones``):
        accept when the name has no numbers, no venue keywords, ≤5 words,
        mostly-capitalised.

    Called only when :func:`guess_artist_from_folder`, the lexicon walk,
    and ``_trust_parent_artist`` all came up empty. Returns a candidate
    that's better than nothing — still OK if it's wrong, since the
    ``--prompt-unknown`` flow will ask the user anyway.
    """
    pos = _first_date_position(folder_name)
    if pos == 0:
        tail = _artist_after_leading_date(folder_name)
        if tail:
            return tail
    if pos is None:
        return _artist_from_bare_folder(folder_name)
    return None


def _first_date_match_end(text: str) -> int | None:
    """Return the end position of the earliest date-like match in *text*,
    or None if no date is found. Companion to ``_first_date_position``.
    """
    best_start: int | None = None
    best_end: int | None = None
    for pat in _DATE_FINDERS:
        m = pat.search(text)
        if m is None:
            continue
        if best_start is None or m.start() < best_start:
            best_start, best_end = m.start(), m.end()
    m = YEAR_ONLY.search(text)
    if m is not None and (best_start is None or m.start() < best_start):
        best_start, best_end = m.start(), m.end()
    return best_end


# Generic placeholder tokens that appear after a leading date and DO NOT
# name an artist — "1999-01-01 Show A", "2001-05-05 Set 1", etc. Used
# to suppress test-shaped or truly generic tails.
_GENERIC_TAIL_TOKENS = frozenset({
    "show", "set", "concert", "live", "audio", "recording", "recordings",
    "tape", "tapes", "untitled", "unknown", "misc", "disc", "cd",
})


def _artist_after_leading_date(folder_name: str) -> str | None:
    end = _first_date_match_end(folder_name)
    if end is None:
        return None
    tail = folder_name[end:].strip(" -,_.()\t")
    if not tail:
        return None
    # Stop at first ' - ' or ' (' (venue/annotation boundary). Same cut
    # logic as _clean_artist_candidate.
    head = re.split(r"\s+-\s+|\s+\(", tail, maxsplit=1)[0].strip(" -,_.()\t")
    if not head:
        return None
    if _VENUE_KEYWORDS.search(head):
        return None
    if _MIC_PLACEMENT.search(head):
        return None
    # "Black Sabbath" or "The Strokes" — must start with a capital letter.
    if not re.match(r"^[A-Z0-9]", head):
        return None
    if len(head) < 2 or len(head) > 60:
        return None
    if YEAR_ONLY.search(head):
        return None
    words = head.split()
    # Reject generic placeholder shapes — "Show A", "Set 1", "Concert".
    # If every word is either a single character OR one of the generic
    # tokens, it's not a real artist name.
    lower_words = [w.lower().rstrip(".,") for w in words]
    if all(
        w in _GENERIC_TAIL_TOKENS or (len(w) == 1 and not w.isdigit())
        for w in lower_words
    ):
        return None
    return head


_US_STATE_SUFFIX = re.compile(
    r"(?<!\w)(?:A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|"
    r"M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|"
    r"W[AIVY])\.?\s*$",
    re.I,
)
_CA_PROVINCE_SUFFIX = re.compile(
    r"(?<!\w)(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\.?\s*$", re.I,
)


def _artist_from_bare_folder(folder_name: str) -> str | None:
    """Return folder_name itself when it reads as a plausible artist name
    and nothing else — otherwise None.

    Real library shape: ``/Tapes/Howard Jones/`` with audio directly inside
    is treated as a concert folder (no show subdirectory). The folder name
    IS the artist. Guard against venue/prose folder names by rejecting any
    name containing venue keywords, mic-placement jargon, digits, or more
    than 5 words.
    """
    cleaned = folder_name.strip(" -,_.()[]")
    if not cleaned or len(cleaned) < 2 or len(cleaned) > 60:
        return None
    # Must start with a capital letter, or a digit when followed by a
    # capitalised word ("3 Doors Down", "10,000 Maniacs").
    if not re.match(r"^[A-Z]|^\d+\s+[A-Z]", cleaned):
        return None
    # Reject 4-digit year (clearly a date-shaped folder, not a pure artist
    # name). Short numeric tokens are fine — "U2", "Sum 41", "Blink-182",
    # "3 Doors Down" all have digits and are legitimate artist names.
    if re.search(r"\b\d{4}\b", cleaned):
        return None
    # Reject date-like runs anywhere in the string (91-11-03, 7-8-80, etc.).
    if re.search(r"\d{1,2}[-._/]\d{1,2}[-._/]\d{1,4}", cleaned):
        return None
    # "Boston MA", "Dallas TX", "Toronto ON" are location strings, not artists.
    if _US_STATE_SUFFIX.search(cleaned) or _CA_PROVINCE_SUFFIX.search(cleaned):
        return None
    if _VENUE_KEYWORDS.search(cleaned):
        return None
    if _MIC_PLACEMENT.search(cleaned):
        return None
    if _SOURCE_CODE_TOKEN.search(cleaned):
        return None
    # Quality/format tokens — "FLAC 24bit", "Master of Reality FLAC".
    if re.search(r"\b(?:flac|mp3|wav|shn|24bit|16bit|lossless|master(?:ed)?)\b",
                 cleaned, re.I):
        return None
    # "City, ST" or "City ST" shape — mostly venue/location strings.
    if _looks_like_city(cleaned):
        return None
    # Must look like a short name (≤5 words) — prose gets longer.
    words = cleaned.split()
    if len(words) > 5:
        return None
    # At least half the words should start with a capital (band names are
    # title-cased; "music" or "hottest live from exotic honolulu" fail).
    caps = sum(1 for w in words if w[:1].isupper())
    if caps / max(len(words), 1) < 0.5:
        return None
    return cleaned


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


# Section headers that introduce an unnumbered setlist. After one of these
# we accept subsequent non-noise non-blank lines as track titles until we
# hit another disc marker or a line that's clearly not a title.
#
# The header line is permissive about trailing punctuation and stray words
# so "Tracklist .", "Track List:", "Setlist:" all match. We require the
# trigger word(s) to be the dominant content of the line, though — a bare
# "Tracklist" in the middle of a sentence should not count.
_SETLIST_HEADER_RE = re.compile(
    r"^\s*(?:set[\s-]*list|track[\s-]*list(?:ing)?|tracks|songs|songlist|"
    r"playlist)\s*[:.\-]?\s*$",
    re.I,
)


def parse_setlist(body: str) -> list[tuple[int | None, str]]:
    """Return list of (disc_number_or_None, track_title) in order.
    Disc is None for single-disc shows; otherwise 1-based or -1 for Encore.

    Two-pass strategy: first pass only accepts numbered tracks
    (``01. Title`` / ``d1t05 Title`` / etc.). If that yields at least 3
    tracks, we return those — the overwhelmingly common case. If the
    numbered pass comes up short we run a second pass with unnumbered
    mode enabled — it fires on an explicit ``Setlist`` / disc marker, or
    (as of the streak-trigger below) after 5+ consecutive plausible
    title-shape lines appear without any header.
    """
    first, _ = _setlist_pass(body, allow_unnumbered=False)
    if len(first) >= 3:
        return first
    second, _ = _setlist_pass(body, allow_unnumbered=True)
    return second if len(second) > len(first) else first


# Streak length that promotes a run of title-shape lines to tracks even
# without a preceding ``Setlist:`` / ``Disc N`` trigger. Deliberately on
# the high side — 5 consecutive short, capitalised, non-prose lines is
# a strong signal; lower thresholds over-fire on credit blocks.
_STREAK_TRIGGER = 5


def _setlist_pass(
    body: str, *, allow_unnumbered: bool,
) -> tuple[list[tuple[int | None, str]], bool]:
    """Core setlist scanner. Returns ``(results, saw_trigger)`` where
    *saw_trigger* records whether any unnumbered-mode trigger (header,
    disc marker, or streak) fired — useful context for callers that
    want to distinguish "no setlist" from "parser punted".
    """
    lines = body.splitlines()
    current_disc: int | None = None
    results: list[tuple[int | None, str]] = []
    saw_disc_marker = False
    pending_encore = False
    unnumbered_mode = False
    saw_trigger = False
    pending_titles: list[str] = []  # streak buffer for auto-trigger

    def _emit_unnumbered(title: str) -> None:
        title = TRAILING_DURATION.sub("", title).strip()
        # Strip "Track01" / "Track 1" / "Tr. 1" / "Tk 1" prefixes that
        # tools like Traders Little Helper inject when auto-naming tracks.
        title = re.sub(
            r"^\s*(?:track|tr|tk)\s*\.?\s*\d{1,3}[\s._:\-]+", "", title, flags=re.I,
        ).strip()
        if not title or len(title) > 200:
            return
        disc_val = current_disc
        if pending_encore:
            disc_val = (current_disc or 1) + 1
        results.append((disc_val if saw_disc_marker else None, title))

    for raw in lines:
        line = raw.strip()
        if not line:
            # Blank lines DON'T break the streak — info files commonly
            # leave a blank between header and setlist block.
            continue
        if TRACK_SKIP.match(line):
            pending_titles = []
            continue
        disc = _disc_from_marker(line)
        if disc is not None:
            pending_titles = []
            saw_disc_marker = True
            saw_trigger = True
            unnumbered_mode = allow_unnumbered
            if disc == -1:
                pending_encore = True
            else:
                current_disc = disc
                pending_encore = False
            continue
        if _SETLIST_HEADER_RE.match(line):
            pending_titles = []
            saw_trigger = True
            unnumbered_mode = allow_unnumbered
            continue
        m = TRACK_LINE.match(line)
        if not m:
            # Vinyl side-letter tracks — ``A1 Live Wire`` → disc A, track 1.
            vm = VINYL_TRACK_LINE.match(line)
            if vm:
                pending_titles = []
                side = vm.group(1).upper()
                v_title = vm.group(3).strip().strip(":-").strip()
                v_title = TRAILING_DURATION.sub("", v_title).strip()
                if v_title and len(v_title) <= 200:
                    v_disc = ord(side) - ord("A") + 1
                    results.append((v_disc, v_title))
                    saw_disc_marker = True
                    current_disc = v_disc
                continue
            if allow_unnumbered and _looks_like_unnumbered_title(line):
                if unnumbered_mode:
                    _emit_unnumbered(line)
                else:
                    pending_titles.append(line)
                    if len(pending_titles) >= _STREAK_TRIGGER:
                        # Promote to unnumbered mode. Drop any leading
                        # single-word line (likely an artist header
                        # like "AC/DC") before backfilling.
                        start = 1 if len(pending_titles[0].split()) == 1 else 0
                        for buf in pending_titles[start:]:
                            _emit_unnumbered(buf)
                        pending_titles = []
                        unnumbered_mode = True
                        saw_trigger = True
            else:
                pending_titles = []
            continue
        # Matched TRACK_LINE — reset streak and record.
        pending_titles = []
        title = m.group(2).strip().strip(":-").strip()
        title = TRAILING_DURATION.sub("", title).strip()
        if not title or len(title) > 200:
            continue
        if re.search(r"\b[a-f0-9]{16,}\b", title.lower()):
            continue
        disc_val = current_disc
        if pending_encore:
            disc_val = (current_disc or 1) + 1
        results.append((disc_val if saw_disc_marker else None, title))
    return results, saw_trigger


def _looks_like_unnumbered_title(line: str) -> bool:
    """True when *line* plausibly names a track in an unnumbered setlist.

    Guards against picking up lineage lines, credit blocks, and prose that
    sit mixed in with the tracks in some info.txt files. Keep the shape
    restrictive — a false positive pollutes the track list with junk.
    """
    if len(line) < 3 or len(line) > 80:
        return False
    if _NOISE_FIRST_LINE.match(line):
        return False
    if _SENTENCE_OPENER.match(line):
        return False
    if _MIC_PLACEMENT.search(line):
        return False
    if _LINEAGE_CHAIN.search(line):
        return False
    if _SOURCE_CODE_TOKEN.search(line):
        return False
    if parse_date(line):
        return False
    if _EMBEDDED_DATE_SHAPE.search(line):
        return False
    # Must contain letters (not just punctuation / numbers).
    if sum(ch.isalpha() for ch in line) < 2:
        return False
    # "Key: value" labelled lines are metadata, not titles.
    if re.match(r"^[A-Za-z][A-Za-z ]{0,20}:\s", line):
        return False
    # Credit / personnel lines — "John Wetton - bass & lead vocals".
    if re.search(r"\s-\s+(?:bass|drums|guitar|keyboards?|vocals|lead vocals|"
                 r"percussion|piano|organ|saxophone|harmonica|violin|cello)\b",
                 line, re.I):
        return False
    # Dense-digit lines (>30% digits) are stamps or IDs, not titles.
    digits = sum(ch.isdigit() for ch in line)
    if digits and digits / max(len(line), 1) > 0.3:
        return False
    # Quality / format metadata — "Bit / 96Khz HD Audio", "16/44.1 FLAC".
    if re.search(
        r"\b(?:kbps|khz|bit|hd\s+audio|16/44|24/96|lossless|"
        r"flac|mp3|wav|shn|dvd-a|sacd)\b",
        line, re.I,
    ):
        return False
    # Self-referential section labels that sometimes appear inline
    # ("Set List", "Setlist:", "Tracklisting:").
    if _SETLIST_HEADER_RE.match(line):
        return False
    if re.match(
        r"^\s*(?:set\s*list|tracklist(?:ing)?|playlist)\s*[:.\-]?\s*$",
        line, re.I,
    ):
        return False
    return True


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

    # Labeled fields take priority. Anchor each pattern to the start of
    # a line (re.MULTILINE) so we don't match the label word inside
    # prose sentences — "let's talk about the venue -- Manny's Carwash
    # was a TINY TINY place..." used to be parsed as a venue value.
    for pat, key in [
        (r"^artist\s*[:\-]\s*(.+)", "artist"),
        (r"^band\s*[:\-]\s*(.+)", "artist"),
        (r"^venue\s*[:\-]\s*(.+)", "venue"),
        (r"^location\s*[:\-]\s*(.+)", "venue"),
        (r"^city\s*[:\-]\s*(.+)", "city"),
        (r"^date\s*[:\-]\s*(.+)", "_datetxt"),
    ]:
        m = re.search(pat, body, re.I | re.MULTILINE)
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

    # Some old taper info files use space-separated labels with no colon
    # or dash: "Venue Concertgebouw\nCity Amsterdam\nState Netherlands".
    # The label word IS the field name and the rest of the line IS the
    # value. Line-anchored to avoid catching prose like "Venue is on the
    # corner..."; value must start with a capital letter.
    for pat, key in [
        (r"^[Aa]rtist\s+([A-Z][^\n]{1,80})\s*$", "artist"),
        (r"^[Bb]and\s+([A-Z][^\n]{1,80})\s*$", "artist"),
        (r"^[Vv]enue\s+([A-Z][^\n]{1,80})\s*$", "venue"),
        (r"^[Ll]ocation\s+([A-Z][^\n]{1,80})\s*$", "venue"),
        (r"^[Cc]ity\s+([A-Z][^\n]{1,80})\s*$", "city"),
        (r"^[Ss]tate\s+([A-Z][^\n]{1,80})\s*$", "region"),
        (r"^[Cc]ountry\s+([A-Z][^\n]{1,80})\s*$", "region"),
    ]:
        m = re.search(pat, body, re.MULTILINE)
        if m and key not in data:
            val = m.group(1).strip().strip(",")
            if not val:
                continue
            if key in ("venue", "artist") and _MIC_PLACEMENT.search(val):
                continue
            if key == "venue" and (
                _LINEAGE_CHAIN.search(val) or _NON_VENUE_PREFIX.match(val)
            ):
                continue
            data[key] = val

    # If _first_artist_line picked a line that turned out to be a
    # space-labeled venue/city/state row ("Venue Concertgebouw"), clear
    # the artist so build_concert can fall back to parent-trust.
    if data.get("artist") and re.match(
        r"^(?:venue|city|state|country|location|source|lineage)\s+",
        data["artist"], re.I,
    ):
        data.pop("artist", None)

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
        # Reject prose sentences ("An interesting SB Taj solo..."): these
        # have just enough structure (mixed case, commas) to fool
        # _looks_like_venue, and we'd rather have a missing venue than a
        # review sentence stamped into the album tag.
        if _SENTENCE_OPENER.match(ln) or _looks_like_prose_artist(ln):
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
    r"(?:all|most)\s+(?:song|track)s?|"
    # "An interesting SB Taj solo", "A great show" — common opinion
    # lines in taper notes. Match when "an/a" is followed by an adjective
    # word class (descriptor before a noun).
    r"an?\s+(?:interesting|excellent|incredible|amazing|great|wonderful|"
    r"awesome|fantastic|beautiful|nice|good|decent|solid|killer|"
    r"classic|rare|unique|special|legendary|superb|fine)|"
    # Contractions and interrogatives — clear prose openers in taper
    # notes. "We're all looking for upgrades", "What I mean by this",
    # "I'm not sure of the source", "You'll hear some hiss".
    r"we['’](?:re|ve|ll|d)|"
    r"i['’](?:m|ve|ll|d)|"
    r"you['’](?:re|ve|ll|d)|"
    r"(?:what|where|when|why|how|who)\s+(?:i|we|you|they|the|this|that|"
    r"is|was|are|were|to|do|did|can|could|should|would)|"
    # Common credit/sentence openings.
    r"from\s+(?:bootleg|tape|cassette|cd|the|a)|"
    r"audience\s+recording|complete\s+show|partial\s+show)\b",
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
    # `fairgrounds` and the plural `grounds` only — singular `ground`
    # alone gets used as a band-name word ("Solid Ground", "Common
    # Ground", "Higher Ground") that we don't want to flag as venue.
    r"fairgrounds|civic|gardens|grounds|bowl|palace|"
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
            # Don't give up yet — rich taper headers pack "Artist, City,
            # date, source" into one line. Try to salvage the artist token
            # from before the first comma.
            salvaged = _salvage_artist_before_comma(line)
            if salvaged:
                return salvaged
            continue
        if DISC_MARKER.search(line):
            break
        stripped = line.strip("*#=-_ \t")
        if not stripped:
            continue
        # Track lines ("01. Sweet Home Chicago") are clearly not the artist,
        # even if they're otherwise clean enough to pass every other filter.
        if TRACK_LINE.match(stripped):
            continue
        letters = sum(ch.isalpha() for ch in stripped)
        if letters < 2 or len(stripped) > 100:
            # Too long to be a name, but might have a clean artist prefix.
            salvaged = _salvage_artist_before_comma(stripped)
            if salvaged:
                return salvaged
            continue
        # Reject descriptive sentences ("This is an incredible show...") and
        # taper-metadata lines ("Steel Pulse - Sunsplash - JA 8-21-87 SBD4").
        # These slip past _NOISE_FIRST_LINE because they start with an
        # arbitrary word but are never an artist name.
        if _SENTENCE_OPENER.match(stripped):
            continue
        # Lineage / signal-chain lines: "Transfer: SDHC > wav > FLAC".
        # Without this filter they slip through as plausible-looking
        # artists (well-formed words, capital starts, no source code).
        if _LINEAGE_CHAIN.search(stripped):
            continue
        # Lines starting with a labeled metadata field — "Transfer:",
        # "Recording source:", "Sound quality:", "Taping gear:", etc.
        # Match `<TitleCase prefix>:` where the prefix contains one of
        # the known label words. Up to 3 words before the colon allows
        # multi-word labels like "Recording source", "Taping equipment".
        if _is_metadata_label_line(stripped):
            continue
        # Musician-credit lines — "Tom Petty — guitar, vocals" — appear
        # in the personnel roster section of nearly every taper note.
        # Without this filter they leak as ARTIST tags.
        if _is_personnel_credit(stripped):
            continue
        # Lines that begin with multiple quoted song titles —
        # "“Warm Ways,” “Over My Head,” …" — are setlist enumerations.
        # Match two consecutive quoted phrases (any quote variant).
        if re.match(
            r'^[\"“][^\"”]{2,80}[\"”][^\"“]{1,5}[\"“]',
            stripped,
        ):
            continue
        if _SOURCE_CODE_TOKEN.search(stripped):
            salvaged = _salvage_artist_before_comma(stripped)
            if salvaged:
                return salvaged
            continue
        if _EMBEDDED_DATE_SHAPE.search(stripped):
            salvaged = _salvage_artist_before_comma(stripped)
            if salvaged:
                return salvaged
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
        # Strip a trailing "(...)" parenthetical context — "Mountain
        # (opening for Deep Purple)" → "Mountain". Only when the head
        # before the paren is a clean artist-shape (≥2 chars, alphabetic).
        cleaned = _TRAILING_PAREN_RE.sub("", stripped).strip()
        if cleaned != stripped and 2 <= len(cleaned) <= 60 and re.match(
            r"^[A-Z]", cleaned
        ):
            stripped = cleaned
        return _strip_date_suffix(stripped)
    return None


_PROSE_VERB_TOKEN = re.compile(
    r"\b(?:is|was|are|were|be|been|being|has|have|had|"
    r"came|comes|come|went|goes|going|did|does|done|doing|"
    r"plays|played|playing|play|"
    r"check|checked|checking|"
    r"includes|included|including|include|"
    r"recorded|recording|taped|taping|"
    r"sounds|sounded|sound|"
    r"appears|appeared|appearing)\b",
    re.I,
)


def _looks_like_prose_artist(candidate: str) -> bool:
    """True if *candidate* reads as a prose sentence rather than an artist
    name.

    Used as a last-line defence in ``build_concert``: when body parsing
    hands us something that plainly isn't a name — typically because a
    descriptive line slipped past ``_first_artist_line`` — prefer the
    folder-name fallback.

    Heuristics:
      * Over 60 chars OR 8+ words is prose territory (longest real artist
        names we've seen top out around "The Nitty Gritty Dirt Band Featuring
        John Denver" — 48 chars).
      * 5+ words AND contains a common sentence verb / connector.
      * 4+ words AND mostly uppercase (>70% of letters are uppercase) —
        bootleg banner titles like ``SEE IF WE CAN WAKE UP EDDIE``.
        Short all-caps names (AC/DC, ELP, ABBA) are unaffected because
        they have <4 words.
    """
    if not candidate:
        return False
    c = candidate.strip()
    if len(c) > 60:
        return True
    words = c.split()
    if len(words) >= 8:
        return True
    if len(words) >= 5 and _PROSE_VERB_TOKEN.search(c):
        return True
    if len(words) >= 4:
        letters = [ch for ch in c if ch.isalpha()]
        if letters:
            upper = sum(1 for ch in letters if ch.isupper())
            if upper / len(letters) > 0.7:
                return True
    return False


# Words that, when found in a labeled prefix `Foo Bar:`, mark the line
# as technical metadata not an artist. Match against the lower-cased
# prefix as a substring — covers "Recording source:", "Transfer info:",
# "Sound quality:", "Tape source:", "Taping gear:", etc.
_METADATA_LABEL_WORDS = (
    "source", "transfer", "lineage", "recording", "recorded",
    "taper", "taping", "seeded", "info", "gear", "equipment",
    "quality", "format", "encode", "encoded", "ripped", "lineage",
    "playback", "noise", "track[\\s-]*list", "track\\s*split",
    "uploaded", "shared", "originally",
)
_METADATA_LABEL_RE = re.compile(
    # Optional `<TitleCase prefix> ` before the keyword. Lets bare
    # ``Taper:`` match alongside multi-word ``Recording source:``.
    r"^(?:[A-Z][^:\n]{0,40}\s+)?(?:" + "|".join(_METADATA_LABEL_WORDS)
    + r")[^:\n]{0,15}:\s",
    re.I,
)


def _is_metadata_label_line(s: str) -> bool:
    """True if *s* starts with a Title-cased labeled metadata prefix
    like ``Recording source:``, ``Transfer:`` or ``Sound quality:``."""
    return bool(_METADATA_LABEL_RE.match(s))


# Personnel-credit shape: "<Name> [— / - / (] (extra word){0,3}
# (guitar|vocals|...)". Catches the musician-roster lines that taper
# notes always include and that used to leak into ARTIST tags:
# "Tom Petty—guitar and lead vocals", "Bruce Springsteen (vocals,
# guitar, harmonica)", "John Wetton - bass & lead vocals".
_PERSONNEL_CREDIT = re.compile(
    r"^[A-Z][\w\s.,'&\"]+?\s*[—\-(]\s*"
    r"(?:[a-z]+\s+){0,3}"
    r"(?:guitars?|vocals?|drums?|bass|keyboards?|piano|organ|"
    r"saxophone|sax|harmonica|violin|cello|mandolin|banjo|"
    r"trumpet|trombone|percussion|backing|lead\s+vocals?|"
    r"rhythm\s+guitar|lead\s+guitar)\b",
    re.I,
)


def _is_personnel_credit(s: str) -> bool:
    """True if *s* looks like a musician-credit line —
    ``Tom Petty — guitar``, ``Bruce Springsteen (vocals, guitar)``."""
    return bool(_PERSONNEL_CREDIT.match(s))


# Parenthetical context that should be stripped from an otherwise-clean
# artist line: "(opening for Deep Purple)", "(with Ginger Baker)",
# "(special guest Stevie Wonder)", "(Acoustic Reckoning)".
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


_MONTH_PREFIX = re.compile(
    r"^\s*(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\b",
    re.I,
)
_NUMERIC_DATE_PREFIX = re.compile(r"^\s*\d{1,4}[-./]")


def _strip_date_suffix(s: str) -> str:
    """If *s* has a `" - "` separator followed by a date-shaped tail
    (month name, year, or numeric date), return the clean head — else
    return *s* unchanged. Catches ``"Robert Plant - December 13"`` →
    ``"Robert Plant"``.
    """
    if not s or " - " not in s:
        return s
    head, tail = s.split(" - ", 1)
    head = head.strip()
    if not head:
        return s
    tail_lstripped = tail.lstrip(" \t-_,.")
    if (parse_date(tail_lstripped[:40]) or
            _MONTH_PREFIX.match(tail_lstripped) or
            _NUMERIC_DATE_PREFIX.match(tail_lstripped) or
            re.match(r"^(?:19|20)\d{2}", tail_lstripped)):
        return head
    return s


def _salvage_artist_before_comma(line: str) -> str | None:
    """Return the clean artist token from a noisy ``Artist, City, date,
    source`` header, or None if the head before the first comma doesn't
    look artist-shaped.

    Real-world shapes we catch::

        Taj MAHAL, Bologna-Italy, 9 april 1978, 2d gen SB + Bonus FM
        Bob Dylan, Manchester UK, 17 May 1966, Free Trade Hall SBD

    The full line fails the strict filters (date, source code) but the
    prefix is the artist and losing it costs us correct tagging on
    hundreds of etree-style headers.
    """
    if "," not in line:
        return None
    # Full-line mic-placement signal rules out salvaging — "Main Stage, at
    # the SBD, ROC" has a deceptively clean head but the whole line is
    # placement jargon, not artist metadata. Venue keywords in the tail
    # (Hall, Garden, Arena) are fine — they're the expected venue field
    # that follows a real artist prefix.
    if _MIC_PLACEMENT.search(line):
        return None
    if _LINEAGE_CHAIN.search(line):
        return None
    head = line.split(",", 1)[0].strip("*#=-_ \t")
    if not head:
        return None
    # Strip a trailing " - <date>" before applying the strict filters —
    # ``Robert Plant - December 13`` and
    # ``Bruce Springsteen - 11-16-1990`` should yield the artist alone.
    head = _strip_date_suffix(head)
    # Reject heads that are just a month-day or a date by themselves —
    # ``April 7`` from ``April 7, 2017``, ``December 13`` from
    # ``December 13, 1983``. These slip past parse_date when truncated.
    if re.fullmatch(
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
        r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept|"
        r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}",
        head, re.I,
    ):
        return None
    letters = sum(ch.isalpha() for ch in head)
    if letters < 2:
        return None
    # 2-60 chars keeps us inside plausible artist-name length. The full-line
    # check above already let us through with longer strings — this is the
    # tight guard for the salvaged head.
    if len(head) > 60:
        return None
    if _SENTENCE_OPENER.match(head):
        return None
    if _SOURCE_CODE_TOKEN.search(head):
        return None
    if _EMBEDDED_DATE_SHAPE.search(head):
        return None
    if parse_date(head):
        return None
    if _VENUE_KEYWORDS.search(head):
        return None
    if _MIC_PLACEMENT.search(head):
        return None
    if _looks_like_city(head):
        return None
    return _strip_date_suffix(head)


_ATTRIBUTION_CREDIT_RE = re.compile(
    r"^(?:taped|recorded|recorded|tracked|seeded|mastered|"
    r"transferred|transfer|edited|mixed|engineered|produced|"
    r"presented|uploaded|shared|posted|encoded|ripped)"
    r"(?:\s*&\s*\w+)?"  # Allow "&" connector: "Tracked & Seeded by ..."
    r"\s+(?:by|on)\b",
    re.I,
)


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
    # Attribution credits — "Taped by Joe Blow", "Tracked & Seeded by
    # Bill Graves", "Mastered by Engineer X". Not venues.
    if _ATTRIBUTION_CREDIT_RE.match(line):
        return False
    # Personnel-credit lines ("Tom Petty — guitar, vocals") aren't
    # venues either.
    if _is_personnel_credit(line):
        return False
    # Metadata-label lines ("Source: SBD", "Recording source: ...").
    if _is_metadata_label_line(line):
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
                # Reject release-code brackets / parens — "[VGP-330]",
                # "(SODD)", "[GBR-XX]" are bootleg release identifiers
                # that don't belong in artist names.
                if "[" in cand or "(" in cand:
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
    # Cut at the first '[' or '(' — bracket release codes (e.g. "[VGP-330]",
    # "(SODD)") and parenthesised release-title trailers are not part of
    # the artist name. "Rolling Stones [VGP-330] Front Row" → "Rolling Stones".
    for sep in ("[", "("):
        if sep in name_for_split:
            head = name_for_split.split(sep, 1)[0].strip()
            if head and " " in head or len(head.split()) >= 1:
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
    # If the body yielded a prose-shaped "artist" (sentence verbs, too many
    # words), discard it so parent-trust / lexicon can rescue the right
    # name from the folder tree. Prevents "An interesting SB Taj solo…"
    # from becoming the ARTIST tag on 23 FLACs.
    if artist and _looks_like_prose_artist(artist):
        artist = guess_artist_from_folder(folder_name)
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
    # Compilation override: when any ancestor folder is literally
    # "Various Artists", the leaf folder's own name is almost always the
    # per-set artist (festival layout:
    # ``/Various Artists/<Compilation>/<Artist>/``). Prefer the leaf's
    # name over either "Various Artists" itself or the compilation name
    # picked up by ``_trust_parent_artist``. Without this, dozens of
    # Concert For Amnesty / Two of Us subfolders stay tagged with the
    # compilation name and end up in the Plex VA bucket.
    if _ancestor_is_various_artists(folder):
        candidate = weak_artist_from_folder(folder_name)
        if candidate:
            artist = candidate
    if not artist:
        # Last-resort: folder name as a weak artist signal. Covers lone
        # "Howard Jones" folders and "1969 Black Sabbath" leading-date
        # shapes that the strong fallbacks can't handle.
        artist = weak_artist_from_folder(folder_name)

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
