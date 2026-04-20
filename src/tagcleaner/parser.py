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

from .models import Concert, Track
from .sources import detect_source

# Dates can sit flush against an artist abbreviation ('los1996-03-20') or
# inside an underscore-joined filename ('SRV_1985.0725'), so we don't require
# a word boundary before the year.
ISO_DATE = re.compile(r"(?<!\d)(19|20)\d{2}[-._/](0?[1-9]|1[0-2])[-._/](0?[1-9]|[12]\d|3[01])(?!\d)")
SHORT_DATE = re.compile(r"(?<!\d)(\d{2})[-._](0?[1-9]|1[0-2])[-._](0?[1-9]|[12]\d|3[01])(?!\d)")
COMPACT_DATE = re.compile(r"(?<!\d)(19|20)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
# 'YYYY.MMDD' or 'YYYY_MMDD' (seen in filenames like SRV_1985.0725)
SPLIT_COMPACT = re.compile(r"(?<!\d)(19|20)(\d{2})[._](0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
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
            y = int(m.group(1) + text[m.start() + 2: m.start() + 4])
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
    m = PROSE_DATE.search(text) or MONTH_DAY_YEAR.search(text)
    if m:
        try:
            dt = dateparser.parse(m.group(0), fuzzy=True)
            return dt.strftime("%Y-%m-%d")
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


def guess_artist_from_folder(folder_name: str) -> str | None:
    art = _expand_artist_prefix(folder_name)
    if art:
        return art
    # "Artist YYYY-MM-DD ..." or "Artist - YYYY-MM-DD ..."
    m = re.match(r"^([A-Za-z][A-Za-z0-9&'. ]+?)\s+[-,(]?\s*(?:19|20)\d{2}", folder_name)
    if m:
        return m.group(1).strip(" -,")
    # "YYYY-MM-DD - Artist - Venue"
    m = re.match(r"^(?:19|20)\d{2}[-.]\d{2}[-.]\d{2}\s*-\s*([^-]+?)\s*-", folder_name)
    if m:
        return m.group(1).strip()
    return None


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
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    sample = raw[:1024]
    if len(sample) >= 4:
        odd_zeros = sample[1::2].count(0)
        even_zeros = sample[0::2].count(0)
        half = len(sample) // 2
        if half and odd_zeros / half > 0.3 and odd_zeros > even_zeros:
            return raw.decode("utf-16-le", errors="replace")
        if half and even_zeros / half > 0.3 and even_zeros > odd_zeros:
            return raw.decode("utf-16-be", errors="replace")
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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
            data[key] = m.group(1).strip().strip(",")

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
        if "venue" not in data or "city" not in data:
            v, c, r = _split_venue_city_region(ln)
            if v and c:
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
        if _VENUE_KEYWORDS.search(stripped):
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
    if re.search(r"\d{5,}", line):
        return False
    low = line.lower()
    if any(tok in low for tok in ("http://", "https://", "@", "flac ", ".flac", ".shn", ".wav", "kbps", "khz")):
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


def build_concert(
    folder: Path,
    audio_files: Iterable[Path],
    info_txt: Path | None,
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
