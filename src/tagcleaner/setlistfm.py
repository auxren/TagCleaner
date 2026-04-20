"""Optional enrichment via the setlist.fm API.

When enabled, TagCleaner queries setlist.fm by (artist, date) and merges
anything it returns — venue, city, country, song list — into the locally
parsed Concert. Local data always wins on fields the parser already filled;
the API fills gaps and can confirm the setlist track count when it matches.

API reference: https://api.setlist.fm/docs/1.0/index.html

Notes:
  - Requires a free API key (users request one from setlist.fm).
  - Rate limit is ~2 req/sec; we serialise all calls and enforce a delay.
  - This module stays pure-stdlib so users without `requests` can still use
    the core tagger offline.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import Concert, Track
from .parser import _finalize_tracks

API_ROOT = "https://api.setlist.fm/rest/1.0"
USER_AGENT = "TagCleaner/0.1 (+https://github.com/auxren/TagCleaner)"
MIN_INTERVAL_S = 0.55  # ~1.8 req/sec, safely under the 2/s ceiling.


class SetlistFmError(RuntimeError):
    pass


@dataclass
class EnrichedResult:
    venue: str | None = None
    city: str | None = None
    region: str | None = None
    songs: list[tuple[int | None, str]] | None = None
    setlist_id: str | None = None
    url: str | None = None


class SetlistFmClient:
    def __init__(self, api_key: str, *, min_interval_s: float = MIN_INTERVAL_S) -> None:
        if not api_key:
            raise ValueError("setlist.fm API key is required")
        self.api_key = api_key
        self.min_interval_s = min_interval_s
        self._last_call = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        wait = self.min_interval_s - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        qs = urllib.parse.urlencode(params)
        url = f"{API_ROOT}{path}?{qs}"
        req = urllib.request.Request(url, headers={
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        self._throttle()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise SetlistFmError(f"HTTP {exc.code} from setlist.fm: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise SetlistFmError(f"network error talking to setlist.fm: {exc}") from exc

    def search(self, *, artist: str, date_iso: str) -> list[dict[str, Any]]:
        """Return raw 'setlist' items for an (artist, date) query, newest first.
        *date_iso* is YYYY-MM-DD; the API wants DD-MM-YYYY."""
        y, m, d = date_iso.split("-")
        params = {"artistName": artist, "date": f"{d}-{m}-{y}", "p": "1"}
        data = self._get("/search/setlists", params)
        return list(data.get("setlist", []))


def _songs_from_setlist(raw: dict[str, Any]) -> list[tuple[int | None, str]]:
    out: list[tuple[int | None, str]] = []
    sets = (raw.get("sets") or {}).get("set") or []
    multi = len(sets) > 1 or any((s.get("encore") for s in sets))
    for set_ix, s in enumerate(sets, start=1):
        disc: int | None = set_ix if multi else None
        if s.get("encore"):
            disc = (set_ix if disc is not None else len(sets))
        for song in s.get("song", []):
            name = (song.get("name") or "").strip()
            if not name:
                continue
            cover = song.get("cover")
            if cover and cover.get("name"):
                name = f"{name} ({cover['name']} cover)"
            tape = song.get("tape")
            if tape:
                name = f"[Tape] {name}"
            out.append((disc, name))
    return out


def enrich(client: SetlistFmClient, concert: Concert) -> EnrichedResult | None:
    """Look up *concert* on setlist.fm and return structured data, or None if
    we couldn't identify a single best match. Does not mutate *concert*."""
    if not concert.artist or not concert.date:
        return None
    try:
        matches = client.search(artist=concert.artist, date_iso=concert.date)
    except SetlistFmError:
        return None
    if not matches:
        return None
    # Prefer the match whose venue/city best overlaps what we already have;
    # otherwise take the first (setlist.fm sorts by relevance).
    best = matches[0]
    if concert.venue:
        want = concert.venue.lower()
        for m in matches:
            vname = (((m.get("venue") or {}).get("name")) or "").lower()
            if vname and (vname in want or want in vname):
                best = m
                break

    venue = ((best.get("venue") or {}).get("name"))
    city_obj = ((best.get("venue") or {}).get("city")) or {}
    city = city_obj.get("name")
    region = city_obj.get("stateCode") or city_obj.get("state") or (city_obj.get("country") or {}).get("name")
    songs = _songs_from_setlist(best)
    return EnrichedResult(
        venue=venue,
        city=city,
        region=region,
        songs=songs or None,
        setlist_id=best.get("id"),
        url=best.get("url"),
    )


def merge_enrichment(concert: Concert, result: EnrichedResult, *, overwrite_setlist: bool = False) -> list[str]:
    """Apply *result* to *concert* in place. Local non-empty fields win unless
    *overwrite_setlist* is True and the parsed setlist was empty. Returns a
    list of human-readable notes describing what changed."""
    notes: list[str] = []
    if not concert.venue and result.venue:
        concert.venue = result.venue
        notes.append(f"venue←setlist.fm ({result.venue})")
    if not concert.city and result.city:
        concert.city = result.city
        notes.append(f"city←setlist.fm ({result.city})")
    if not concert.region and result.region:
        concert.region = result.region
        notes.append(f"region←setlist.fm ({result.region})")

    if result.songs:
        have = len(concert.tracks)
        want_audio = len(concert.audio_files)
        if not concert.tracks and want_audio:
            concert.tracks = _finalize_tracks(result.songs)
            notes.append(f"setlist←setlist.fm ({len(concert.tracks)} songs)")
        elif overwrite_setlist and have != want_audio and len(result.songs) == want_audio:
            concert.tracks = _finalize_tracks(result.songs)
            notes.append(f"setlist←setlist.fm (count match: {want_audio})")
        elif have and len(result.songs) == have:
            notes.append("setlist confirmed by setlist.fm")
    if result.url:
        notes.append(f"source: {result.url}")
    return notes
