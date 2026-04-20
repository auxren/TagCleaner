# TagCleaner

Bulk-clean metadata on folders full of live/bootleg/concert recordings. Point
it at a directory and it'll read `info.txt` files, folder names, and
filenames, then write consistent `ARTIST`, `ALBUMARTIST`, `ALBUM`, `DATE`,
`TRACKNUMBER`, `TITLE`, and `DISCNUMBER` tags to every audio file inside.

```
tagcleaner /path/to/concerts --dry-run      # see what would change
tagcleaner /path/to/concerts                # write tags in place
tagcleaner /path/to/concerts --copy-to /tagged   # keep originals pristine
```

Album names come out in a stable, Plex-friendly shape:

```
1987-08-22 Calaveras County Fairgrounds, Angel's Camp, CA [SBD]
1982-01-20 Springfield Civic Center, Springfield, MA [AUD AKG 200E]
1980-08-27 Wollman Rink, Central Park, New York City [SBD]
```

The bracketed suffix is the recording source (SBD / AUD / FM / Matrix) plus
any detected microphone model — so multiple transfers of the same show stay
distinct in your library instead of collapsing into one album.

---

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/auxren/TagCleaner.git
cd TagCleaner
python3 -m venv .venv
.venv/bin/pip install -e .
```

The `tagcleaner` command is now at `.venv/bin/tagcleaner`. You have three
choices for running it:

```bash
# 1. Run it directly from the venv
./.venv/bin/tagcleaner --dry-run /path/to/concerts

# 2. Activate the venv, then just use the name
source .venv/bin/activate
tagcleaner --dry-run /path/to/concerts

# 3. Symlink it onto your PATH (one-time setup)
sudo ln -s "$PWD/.venv/bin/tagcleaner" /usr/local/bin/tagcleaner
tagcleaner --dry-run /path/to/concerts
```

If you'd rather install globally and skip the venv dance:

```bash
pip install --user -e .        # installs to ~/.local/bin
# or on newer distros that block it:
pip install --break-system-packages -e .
```

Once published to PyPI, `pipx install tagcleaner` will Just Work.

## Quickstart

### 1. See what it would do (always safe)

```bash
tagcleaner /mnt/music/concerts --dry-run
```

Prints a table like:

```
┏━━━━━━┳────────────────────┳─────────────────────────────────────┳────────┳────────┓
┃ Conf ┃ Folder             ┃ Album                               ┃ Tracks ┃ Issues ┃
┡━━━━━━╇────────────────────╇─────────────────────────────────────╇────────╇────────┩
│ 1.00 │ rush1984-09-21…    │ 1984-09-21 Maple Leaf Gardens,      │  14/14 │ ok     │
│      │                    │ Toronto, Canada [SBD]               │        │        │
│ 0.80 │ bw1982-10-30…      │ 1982-10-30 Mesa Community Center…   │  21/20 │ track  │
│      │                    │ [AUD]                               │        │ count  │
│      │                    │                                     │        │ mismatch │
└──────┴────────────────────┴─────────────────────────────────────┴────────┴────────┘
```

Nothing is written to disk. A `tagcleaner-drafts.json` file is saved so you
can inspect or hand-edit the plan.

### 2. Apply the tags

```bash
tagcleaner /mnt/music/concerts           # in place
tagcleaner /mnt/music/concerts --yes     # skip the confirm prompt
```

### 3. Write tagged copies instead of mutating originals

```bash
tagcleaner /mnt/music/concerts --copy-to /mnt/music/concerts-tagged
```

Mirrors the source tree into the target directory, copies each audio file,
then tags the copy. The originals are never touched.

### 4. Review then apply

```bash
tagcleaner /mnt/music/concerts --dry-run --drafts /tmp/drafts.json
#  ... edit /tmp/drafts.json by hand ...
tagcleaner /mnt/music/concerts --load-drafts /tmp/drafts.json --yes
```

## Run it again: the history file

On every run TagCleaner writes `tagcleaner-history.json` next to the scanned
root (override with `--history FILE`, or turn it off with `--no-history`).
The file has two jobs:

1. **Skip work you've already finished.** On the next run, any folder whose
   last pass tagged successfully *and* whose audio contents haven't changed
   (a SHA-1 of filenames + sizes) is skipped. Running TagCleaner over a
   100k-folder library a second time only re-parses the folders that actually
   changed.
2. **Keep an audit / training log.** Each entry records what the parser
   decided — artist, date, venue, setlist, confidence, issues — plus the
   mode and outcome of the last tagging pass. That's the raw material for
   improving the parser against a real library.

Skip logic is conservative on purpose:

- Dry-runs never cause a real run to be skipped.
- If the last run had any failures, the folder is re-tried.
- Changing `--copy-to` to a new destination re-processes everything.
- `--rescan-all` forces a full re-parse for one run without touching history.

```bash
tagcleaner /mnt/music/concerts               # writes/reads tagcleaner-history.json
tagcleaner /mnt/music/concerts --rescan-all  # re-parse everything (still updates history)
tagcleaner /mnt/music/concerts --no-history  # one-off run, nothing persisted
tagcleaner /mnt/music/concerts --history /tmp/tc.json
```

Delete the file any time to start fresh.

If the animated scan panel misrenders in your terminal (e.g. panels duplicating
in scrollback over ssh / tmux / some emulators), pass `--plain` to fall back to
a simple one-line progress bar.

## Enrich with setlist.fm (optional)

If a folder is missing venue/city/setlist information, TagCleaner can query
[setlist.fm](https://api.setlist.fm/docs/1.0/index.html) by artist + date to
fill the gaps and/or confirm the parsed setlist.

```bash
export SETLISTFM_API_KEY=your-key-here
tagcleaner /mnt/music/concerts --dry-run --enrich-setlistfm
```

Local data always wins on fields the parser already filled — setlist.fm only
fills gaps. Pass `--setlistfm-overwrite` to let the API replace a parsed
setlist when its song count matches the audio file count (useful when the
local `info.txt` is garbled but setlist.fm has an accurate listing).

Rate-limited to ~1.8 req/sec automatically (setlist.fm allows 2).

## What TagCleaner understands

### Folder shapes

```
concerts/
├── 1980-08-27 Talking Heads Wollman Rink/
│   ├── 01 Psycho Killer.flac
│   ├── 02 Warning Signs.flac
│   └── info.txt
├── rush1984-09-21.sbd.fear.flac16/      # etree-style short prefix
│   ├── 01 The Spirit of Radio.flac
│   └── Rush 1984-09-21 - Fear.txt
└── SRV_1985.0725_Ottawa_1644/           # compact YYYY.MMDD date
    └── <audio files>
```

Nested layouts like `folder/folder/*.flac` (common with extracted archives)
are handled automatically.

### Dates

All of these parse correctly:

| Pattern | Example |
|---|---|
| ISO | `1984-09-21`, `1984/09/21`, `1984.09.21` |
| Compact | `19840921` |
| Split compact | `1985.0725`, `1985_0725` |
| Prose | `August 22, 1987` / `22nd Aug 1987` |
| Short-year | `84-09-21` |
| Glued to prefix | `los1996-03-20` (Los Lobos, 1996-03-20) |

### Artist abbreviations

TagCleaner ships with the standard
[etree band-abbreviation list](https://wiki.etree.org/index.php?page=BandAbbreviations),
so prefixes like `gd`, `ph`, `abb`, `wsp`, `dso`, `moe`, `sci`, `ymsb`,
`mule`, `rush`, etc., expand to full artist names automatically.

### Sources and microphones

Detected from folder names, filenames, and info.txt bodies:

| Source | Matches |
|---|---|
| `SBD` | `sbd`, `soundboard`, `board` |
| `AUD` | `aud`, `audience` |
| `FM` | `fm`, `broadcast` |
| `Pre-FM` | `pre-fm`, `prefm` |
| `Matrix` | `matrix`, `mtx` |

Mic families: AKG, Schoeps, Neumann, Sennheiser, DPA, Nakamichi, Sony, Shure,
Core Sound, plus the family-only fallback when no model is named.

### Multi-set shows (disc numbers)

A Grateful Dead night with two sets + encore becomes three discs: tracks
restart at `01` on each disc and `DISCNUMBER` / `DISCTOTAL` are set. The
parser recognises all of:

- `Set 1:` / `Set 2:` / `Encore:`
- `Disc One` / `CD 1` / `CD2:`
- `Early Show` / `Late Show` / `Matinee` / `Evening Show`

Single-disc shows (even ones labelled `Disc One`) get no disc tags.

## Tag layout

| Tag | Source |
|---|---|
| `ARTIST` | info.txt first line, or labelled `Artist:` line, or folder-name abbreviation lookup |
| `ALBUMARTIST` | same as `ARTIST` |
| `ALBUM` | `YYYY-MM-DD Venue, City, Region [source]` |
| `DATE` | ISO `YYYY-MM-DD` |
| `TRACKNUMBER` | `01`, `02`, … restarting per disc |
| `TITLE` | parsed setlist line |
| `DISCNUMBER` / `DISCTOTAL` | only when the show has ≥ 2 sets/discs |

Supports FLAC (Vorbis comments) and MP3 (ID3 via EasyID3). Other formats in
the audio-file scan (`.m4a`, `.ogg`, `.opus`, `.wav`) are discovered but not
yet tagged — opening an issue or PR is welcome.

## Confidence scoring

Every concert gets a `0.00`–`1.00` confidence score:

| Points | Field |
|---|---|
| 0.25 | artist resolved |
| 0.25 | date resolved |
| 0.15 | venue resolved |
| 0.10 | city resolved |
| 0.25 | track count matches audio file count exactly |
| 0.05 | tracks parsed but count differs |

By default, `--min-confidence 0.5` skips anything below that threshold when
writing tags. Use `--min-confidence 0` to force everything through, or raise
it to `0.75` for a strictly-automatic pass that punts manual work to a second
review round.

## Safety model

- **`--dry-run`** never touches audio files. Use it freely.
- **`--copy-to DIR`** never touches the source tree. Originals are read-only
  as far as TagCleaner is concerned.
- **in-place mode** is the only one that mutates originals; it prompts for
  confirmation unless you pass `--yes`, and skips any folder whose parsed
  track count doesn't match its audio file count.

The drafts JSON is the source of truth between runs. Edit it, reload it with
`--load-drafts`, and you'll always know exactly what's about to happen.

## Contributing / roadmap

- [ ] M4A / Ogg / Opus tagging (currently FLAC + MP3)
- [ ] Cover-art extraction from `folder.jpg`
- [ ] Plex-specific album/sort-order hints
- [ ] MusicBrainz as a second enrichment backend

See [`docs/PATTERNS.md`](docs/PATTERNS.md) for the full list of recognised
naming patterns and how to extend them.

## License

MIT — see [LICENSE](LICENSE).
