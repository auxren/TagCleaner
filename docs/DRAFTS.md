# Drafts JSON schema

Every run of TagCleaner (including `--dry-run`) writes a `tagcleaner-drafts.json`
file capturing exactly what the parser decided. You can inspect it, hand-edit
it, and then replay it with `--load-drafts FILE`.

## Top-level shape

```jsonc
[
  {
    "folder": "/mnt/music/concerts/rush1984-09-21.sbd.fear.flac16",
    "artist": "Rush",
    "date": "1984-09-21",
    "venue": "Maple Leaf Gardens",
    "city": "Toronto",
    "region": "Canada",
    "source": {
      "kind": "SBD",
      "mics": [],
      "taper": null
    },
    "album": "1984-09-21 Maple Leaf Gardens, Toronto, Canada [SBD]",
    "confidence": 1.0,
    "issues": [],
    "audio_files": [
      "/mnt/music/concerts/rush1984-09-21.sbd.fear.flac16/01 Spirit of Radio.flac",
      "/mnt/music/concerts/rush1984-09-21.sbd.fear.flac16/02 Enemy Within.flac"
    ],
    "tracks": [
      { "number": 1, "title": "The Spirit of Radio", "disc": null, "disc_total": null },
      { "number": 2, "title": "The Enemy Within", "disc": null, "disc_total": null }
    ]
  }
]
```

## Fields

| Field | Type | Notes |
|---|---|---|
| `folder` | string | Absolute path to the concert folder. |
| `artist` | string \| null | `ARTIST` and `ALBUMARTIST` tag value. |
| `date` | string \| null | ISO `YYYY-MM-DD`. |
| `venue` | string \| null | First component of the album name after the date. |
| `city` | string \| null | |
| `region` | string \| null | US state code, UK country, or non-US country. |
| `source.kind` | string \| null | `SBD` / `AUD` / `FM` / `Pre-FM` / `Matrix` / `DAT`. |
| `source.mics` | string[] | Ordered list of detected mic models. |
| `source.taper` | string \| null | Reserved; not populated by the parser yet. |
| `album` | string | Computed; editing this field alone is ignored — edit the parts. |
| `confidence` | number | `0.0`–`1.0`. Computed from other fields. |
| `issues` | string[] | Human-readable warnings (track-count mismatch, etc.). |
| `audio_files` | string[] | Ordered list of audio files pairing with `tracks`. |
| `tracks` | object[] | One entry per audio file. |
| `tracks[].number` | integer | 1-based, restarts per disc. |
| `tracks[].title` | string | `TITLE` tag value. |
| `tracks[].disc` | integer \| null | 1-based. Null means single-disc show. |
| `tracks[].disc_total` | integer \| null | Total disc count for this show. |

## Hand-editing workflow

1. Run a dry-run: `tagcleaner /mnt/music --dry-run --drafts /tmp/d.json`
2. Open `/tmp/d.json`, fix anything the parser got wrong:
   - Correct a misspelled venue.
   - Resolve track-count mismatches by removing an extra parsed track.
   - Flip a disc number if Set 1/Set 2 came out the wrong way round.
3. Replay: `tagcleaner /mnt/music --load-drafts /tmp/d.json --yes`

When `--load-drafts` is used, the parser is skipped entirely — TagCleaner
trusts the JSON as ground truth.

## Gotchas

- `album` is recomputed from `date` / `venue` / `city` / `region` / `source`
  on load, so editing just the `album` field has no effect. Edit the
  components instead.
- `audio_files` and `tracks` must be the same length or the tagger will
  skip that concert.
- Absolute paths are expected in both `folder` and `audio_files`; the
  in-place and copy-to logic depend on that.
