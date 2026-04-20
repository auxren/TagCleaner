# Patterns reference

This page documents every pattern the parser looks for, so you can tell
whether a given folder will parse cleanly or needs a manual nudge.

## Folder discovery

`scanner.py` treats a directory as a "concert folder" when either:

1. It directly contains at least one audio file
   (`.flac`, `.mp3`, `.m4a`, `.ogg`, `.opus`, `.wav`).
2. It contains exactly one subdirectory that itself contains audio. This
   covers the common archive-extraction pattern of
   `Show Name/Show Name/*.flac`.

Hidden folders (`.` prefix) and macOS resource-fork files (`._*`) are
ignored.

## Info-file selection

For each concert folder, `scanner.py` picks the largest `.txt` file whose
name doesn't look like a fingerprint/checksum manifest. Filenames containing
any of these tokens are skipped:

```
ffp  md5  sha  shntool  audiochecker  sbeok
```

If no suitable `.txt` exists, parsing continues using only folder name and
filenames.

## Artist

In priority order:

1. A labelled line in `info.txt`: `Artist: Some Band` or `Band: Some Band`.
2. The first non-blank line of `info.txt` that doesn't look like
   boilerplate (URLs, checksum logs, bitrate specs, the literal string
   `No errors occured.` from audiochecker, etc.).
3. A folder-name prefix matching the
   [etree band-abbreviation list](https://wiki.etree.org/index.php?page=BandAbbreviations):
   `gd1987-08-22…` → `Grateful Dead`.
4. The leading words of the folder name, if they're followed by a year:
   `Public Enemy 1994-12-09…` → `Public Enemy`.

## Date

`parser.py::parse_date` tries these patterns, in this order, taking the
first hit:

| Pattern | Example |
|---|---|
| ISO, dotted, or slashed | `1984-09-21`, `1984.09.21`, `1984/09/21` |
| Compact 8-digit | `19840921` |
| Split compact | `1985.0725`, `1985_0725` |
| Ordinal prose | `21st September 1984` |
| Month–day–year prose | `September 21, 1984` |
| Short year | `84-09-21` (years 60–99 → 19xx, 00–59 → 20xx) |

The folder name is consulted **before** the info.txt body on the theory that
folder names almost always record the concert date, while info.txt bodies
frequently mention transfer/mastering dates from years later.

## Venue / City / Region

Priority:

1. Labelled lines in `info.txt`: `Venue:`, `Location:`, `City:`.
2. Unlabelled lines in the first ~10 of `info.txt`, between artist and the
   first setlist marker. A line like `Wollman Rink, Central Park` becomes
   the venue, `New York, NY` becomes `city='New York', region='NY'`.
3. Folder-name extraction:
   - `Venue, City, ST` (`Springfield Civic Center, Springfield, MA`)
   - `City ST` (`Calaveras CA`)
   - `City, Country` (`London, England`)

US state codes (50 + DC) and a small country whitelist (England, Scotland,
Wales, Ireland, Canada, Germany, France, Netherlands, Italy, Spain, Japan,
Australia, UK, USA) are recognised.

## Sources and microphones

`sources.py` detects a source type and any mic model mentioned anywhere in
the combined (folder name + filenames + info.txt body) text.

| Source type | Matched tokens |
|---|---|
| `Pre-FM` | `pre fm`, `pre-fm`, `prefm` |
| `Matrix`  | `matrix`, `mtx` |
| `SBD`     | `sbd`, `soundboard`, `board` |
| `AUD`     | `aud`, `audience` |
| `FM`      | `fm`, `broadcast` |
| `DAT`     | `dat`, `digital master` |

Mic families (ordered most-specific first):

- Schoeps MK-series (MK4 / MK41 / MK5 / MK6 / …)
- AKG (C414, 451, 200E, 480, …)
- Neumann (KM184, U87, TLM series, …)
- Sennheiser (MKH20, MKH40, MKE2, …)
- DPA (4023, 4060, 4011, …)
- Nakamichi (CM-300, DR-3, …)
- Sony (PCM-D50, D3, D7, …)
- Core Sound / Binaural
- Shure (KSM-series, SM-series)

Bare family names (e.g. `AKG`) are dropped from the final label when a
specific model from the same family is also present.

The final bracketed label in the album name is:
`[<kind> <mic1> <mic2> …]` — empty when nothing was detected.

## Setlist

`parse_setlist` walks `info.txt` line by line. A line becomes a track when
it matches:

```
^ (optional disc/track prefix like d1t05 or t01) \s* NUMBER \s* [-.)\s] \s* TITLE
```

It is **not** treated as a track when it matches the skip regex for MD5 /
FFP / byte-count / SHA manifests.

Disc boundaries are set by any of these markers on their own line:

- `Disc One`, `Disc 1`, `CD1:`, `CD 2`, `Disk 1`
- `Set 1`, `Set II`, `Set Two`
- `Encore` (bumps disc number by 1)
- `Early Show` / `Matinee` (disc 1)
- `Late Show` / `Evening` (disc 2)

Track numbers restart at `01` at each disc boundary. If only one disc
marker appears, the show is treated as single-disc and no `DISCNUMBER`
tags are written.

## Extending the parser

All the patterns above live in `src/tagcleaner/parser.py` and
`src/tagcleaner/sources.py` as plain regexes and dict literals. Adding a new
artist abbreviation, a new venue-country keyword, or a new mic family is
usually a one-line change plus a test folder.
