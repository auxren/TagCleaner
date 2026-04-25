"""TagCleaner CLI entrypoint.

Three stages:
  1. Scan a root directory for concert-like folders and parse metadata.
  2. Emit a drafts.json (optional) and a human-readable preview.
  3. Apply tags — in place, to a copy, or not at all (--dry-run).

`--dry-run` is always safe; it never touches audio files.
`--copy-to DIR` writes tagged copies, leaving the originals pristine.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import __version__
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .banner import ScanDisplay, render_banner
from .drafts import save_drafts, load_drafts
from .history import (
    HISTORY_FILENAME,
    History,
    HistoryEntry,
    TaggingOutcome,
    can_skip_by_mtime as history_can_skip_by_mtime,
    load_history,
    save_history,
    should_skip as history_should_skip,
)
from .lexicon import LEXICON_FILENAME, Lexicon
from .models import Concert
from .scanner import scan
from .setlistfm import SetlistFmClient, SetlistFmError, enrich, merge_enrichment
from .tagger import Mode, apply_plans, build_plans

# Linux filesystems can hand us filenames with non-UTF-8 bytes; Python
# decodes those as lone UTF-16 surrogates via surrogateescape, which then
# crash UTF-8 stdout writes inside rich. Switch the error handler so bad
# chars become U+FFFD instead of raising.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

console = Console()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tagcleaner",
        description="Clean metadata on concert / live-recording audio folders.",
    )
    p.add_argument("path", type=Path, help="Root directory containing concert folders.")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and preview only; do not modify any files.")
    p.add_argument("--copy-to", type=Path, metavar="DIR",
                   help="Copy tagged files to DIR (mirrors source tree). Originals untouched.")
    p.add_argument("--drafts", type=Path, metavar="FILE",
                   help="Write draft JSON to FILE (default: <path>/tagcleaner-drafts.json).")
    p.add_argument("--load-drafts", type=Path, metavar="FILE",
                   help="Skip scanning; load pre-edited drafts from FILE.")
    p.add_argument("--history", type=Path, metavar="FILE",
                   help=f"History file path (default: <path>/{HISTORY_FILENAME}).")
    p.add_argument("--no-history", action="store_true",
                   help="Do not read or write the history file.")
    p.add_argument("--rescan-all", action="store_true",
                   help="Ignore history for this run (force re-parse of every folder).")
    p.add_argument("--exclude", action="append", default=[], metavar="DIR",
                   help="Skip subdirectories with this basename (case-insensitive). "
                        "Repeatable. Useful for staging dirs like 'incomplete', "
                        "'downloads', 'trash'.")
    p.add_argument("--lexicon", type=Path, metavar="FILE",
                   help=f"Artist/venue lexicon path (default: <path>/{LEXICON_FILENAME}).")
    p.add_argument("--no-lexicon", action="store_true",
                   help="Do not build or consult the artist/venue lexicon.")
    p.add_argument("--prompt-unknown", action="store_true",
                   help="Ask for an artist on each concert the parser couldn't resolve. "
                        "Answers feed the lexicon for future scans.")
    p.add_argument("--minimal-tags", action="store_true",
                   help="Write only ARTIST, ALBUMARTIST, ALBUM, and TRACKNUMBER. "
                        "Leave any existing DATE/TITLE/DISC tags untouched.")
    p.add_argument("--min-confidence", type=float, default=0.5,
                   help="Skip concerts below this confidence in non-dry-run modes (default: 0.5).")
    p.add_argument("--track-tolerance", type=int, default=-1, metavar="N",
                   help="Max tracks by which info.txt and audio count may disagree before "
                        "skipping. -1 (default) picks a per-concert tolerance of "
                        "max(2, 15%% of the shorter list); 0 forces strict equality.")
    p.add_argument("--yes", action="store_true",
                   help="Do not prompt before applying tags.")
    p.add_argument("-v", "--verbose", action="store_true", help="Show full per-track table.")
    p.add_argument("--no-banner", action="store_true",
                   help="Suppress the startup ASCII banner.")
    p.add_argument("--plain", action="store_true",
                   help="Use a simple one-line progress bar instead of the animated scan panel. "
                        "Safer on terminals where the animated panel duplicates in scrollback.")
    p.add_argument("--enrich-setlistfm", action="store_true",
                   help="Query setlist.fm to fill missing venue/city/setlist and confirm parsed data.")
    p.add_argument("--setlistfm-key", metavar="KEY",
                   default=os.environ.get("SETLISTFM_API_KEY"),
                   help="setlist.fm API key (or set SETLISTFM_API_KEY env var).")
    p.add_argument("--setlistfm-overwrite", action="store_true",
                   help="Let setlist.fm overwrite the parsed setlist when counts match the audio.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _mode(args: argparse.Namespace) -> Mode:
    if args.dry_run:
        return Mode.DRY_RUN
    if args.copy_to:
        return Mode.COPY_TO
    return Mode.IN_PLACE


def _render_summary(concerts: list[Concert]) -> None:
    table = Table(title=f"{len(concerts)} concert folders", show_lines=False)
    table.add_column("Conf", justify="right")
    table.add_column("Folder", overflow="fold", max_width=44)
    table.add_column("Album", overflow="fold")
    table.add_column("Tracks", justify="right")
    table.add_column("Issues", overflow="fold", max_width=32)
    for c in concerts:
        conf = c.confidence()
        color = "green" if conf >= 0.75 else "yellow" if conf >= 0.5 else "red"
        table.add_row(
            f"[{color}]{conf:.2f}[/]",
            c.folder.name,
            c.album_name() or "[dim](unknown)[/]",
            f"{len(c.tracks)}/{len(c.audio_files)}",
            "; ".join(c.issues) or "[dim]ok[/]",
        )
    console.print(table)


def _render_verbose(concerts: list[Concert]) -> None:
    for c in concerts:
        console.rule(f"[bold]{c.folder.name}[/bold]")
        console.print(f"  Artist: {c.artist}    Date: {c.date}")
        console.print(f"  Album:  {c.album_name()}")
        if c.issues:
            console.print(f"  [yellow]Issues: {', '.join(c.issues)}[/yellow]")
        if not c.tracks:
            continue
        for a, t in zip(c.audio_files, c.tracks):
            disc = f"{t.disc}/{t.disc_total} " if t.disc else ""
            console.print(f"    {disc}{t.number:02d}  {a.name}  →  {t.title}")


def _scan_with_progress(
    root: Path,
    *,
    history: History,
    mode: Mode,
    copy_to: Path | None,
    rescan_all: bool,
    plain: bool = False,
    lexicon: Lexicon | None = None,
    exclude: list[str] | None = None,
) -> tuple[list[tuple[Concert, str, float]], list[HistoryEntry]]:
    """Run scanner.scan() behind a progress UI.

    Returns ``(fresh, skipped)``: ``(concert, fingerprint, folder_mtime)``
    triples for folders parsed this run, and the history entries for
    folders skipped because they were already tagged and either their
    mtime or audio-content fingerprint still matches the stored value.

    When ``plain`` is True we use a simple one-line ``rich.progress.Progress``
    bar. That path is battle-tested across terminals and avoids the width /
    cursor-bookkeeping issues that can trip up the fancy animated panel on
    some ssh/tmux setups.
    """
    console.print(f"[cyan]🔍 Scanning[/] [bold]{root}[/] ...")
    skipped: list[HistoryEntry] = []

    def _pre_skip(folder: Path, mtime: float) -> bool:
        if rescan_all:
            return False
        entry = history.get(folder)
        if history_can_skip_by_mtime(entry, mtime, mode, copy_to):
            skipped.append(entry)  # type: ignore[arg-type]  # guarded by can_skip_by_mtime
            return True
        return False

    def _skip(folder: Path, fp: str) -> bool:
        if rescan_all:
            return False
        entry = history.get(folder)
        if history_should_skip(entry, fp, mode, copy_to):
            skipped.append(entry)  # type: ignore[arg-type]  # guarded by should_skip
            return True
        return False

    excl = exclude or []
    if plain:
        fresh = _scan_plain(root, pre_skip=_pre_skip, skip=_skip, lexicon=lexicon, exclude=excl)
    else:
        fresh = _scan_animated(root, pre_skip=_pre_skip, skip=_skip, lexicon=lexicon, exclude=excl)
    return fresh, skipped


def _scan_animated(root, *, pre_skip, skip, lexicon=None, exclude=()):
    width = max(48, min((console.size.width or 80) - 8, 78))
    display = ScanDisplay(staff_width=width)
    with Live(display, console=console, refresh_per_second=12, transient=True):
        return scan(
            root,
            pre_skip=pre_skip,
            skip=skip,
            on_folder=display.on_folder,
            on_skip=display.on_skip,
            on_done=lambda c, i, t: display.on_done(c),
            lexicon=lexicon,
            exclude=exclude,
        )


def _scan_plain(root, *, pre_skip, skip, lexicon=None, exclude=()):
    """Plain one-line Progress bar. Safer on terminals where the animated
    panel duplicates in scrollback."""
    progress = Progress(
        TextColumn("[cyan]🔍 scanning[/]"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[bright_black]⏭ {task.fields[skipped]} cached[/]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with progress:
        task = progress.add_task("scanning", total=None, skipped=0)
        state = {"skipped": 0}

        def _on_folder(path, idx, total):
            progress.update(task, completed=idx, total=total)

        def _on_skip(path, idx, total):
            state["skipped"] += 1
            progress.update(task, completed=idx, total=total, skipped=state["skipped"])

        return scan(
            root,
            pre_skip=pre_skip,
            skip=skip,
            on_folder=_on_folder,
            on_skip=_on_skip,
            lexicon=lexicon,
            exclude=exclude,
        )


def _enrich_all(concerts: list[Concert], api_key: str, overwrite: bool) -> None:
    try:
        client = SetlistFmClient(api_key)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        return
    hits = 0
    misses = 0
    for c in concerts:
        try:
            result = enrich(client, c)
        except SetlistFmError as exc:
            console.print(f"  [yellow]⚠️  setlist.fm error for {c.folder.name}: {exc}[/]")
            continue
        if not result:
            misses += 1
            continue
        notes = merge_enrichment(c, result, overwrite_setlist=overwrite)
        if notes:
            hits += 1
            console.print(f"  [bright_magenta]✨ enriched[/] [bold]{c.folder.name}[/]: " + "; ".join(notes))
    console.print(f"  [bright_black]setlist.fm: matched {hits}, no-match {misses}[/]")


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _prompt_unknown_artists(
    concerts: list[Concert],
    lexicon: Lexicon | None,
    lexicon_path: Path | None,
) -> None:
    """Ask the user for an artist on each concert that has none.

    Concerts sharing a parent directory are grouped so the user answers
    once per folder-of-shows instead of once per show. Answers go back
    into the concert, into the lexicon (so later concerts in the same
    run canonicalise against them), and into the lexicon file on disk
    after every answer — a Ctrl-C never loses what was already typed.
    """
    if lexicon is None or not sys.stdin.isatty():
        return
    unknowns = [c for c in concerts if not c.artist]
    if not unknowns:
        return

    groups: dict[Path, list[Concert]] = {}
    for c in unknowns:
        groups.setdefault(c.folder.parent, []).append(c)

    console.print(
        f"\n[bold yellow]❓ {len(unknowns)} concert(s) across "
        f"{len(groups)} folder(s) have no artist.[/]"
    )
    console.print(
        "[bright_black]   Type a name to fill it in, empty to skip, "
        "'q' to stop asking.[/]\n"
    )

    stopped = False
    for parent, siblings in groups.items():
        if stopped:
            break
        answer = _prompt_for_group(parent, siblings)
        if answer is None:
            stopped = True
            continue
        if not answer:
            continue
        canonical = lexicon.match_artist(answer) or answer
        lexicon.add_artist(canonical, count=len(siblings))
        for c in siblings:
            c.artist = canonical
            if "artist unknown" in c.issues:
                c.issues.remove("artist unknown")
        console.print(
            f"   [green]→[/] tagged {len(siblings)} concert(s) as "
            f"[bold]{canonical}[/]"
        )
        if lexicon_path is not None:
            try:
                lexicon.save(lexicon_path)
            except OSError as exc:
                console.print(f"   [yellow]⚠️  could not save lexicon: {exc}[/]")


def _prompt_for_group(parent: Path, siblings: list[Concert]) -> str | None:
    """Ask the user for one artist name covering *siblings*.

    Returns the typed answer (possibly empty to skip the group), or
    ``None`` if the user asked to stop.
    """
    if len(siblings) == 1:
        c = siblings[0]
        console.print(f"[bold]{c.folder}[/]")
        details: list[str] = []
        if c.date: details.append(f"date {c.date}")
        if c.venue: details.append(f"venue {c.venue}")
        if c.city: details.append(f"city {c.city}")
        if c.tracks: details.append(f"{len(c.tracks)} tracks")
        if details:
            console.print(f"  [bright_black]{', '.join(details)}[/]")
        for a in c.audio_files[:3]:
            console.print(f"  [bright_black]· {a.name}[/]")
        if len(c.audio_files) > 3:
            console.print(f"  [bright_black]  ... and {len(c.audio_files) - 3} more file(s)[/]")
    else:
        console.print(
            f"[bold]{parent}/[/] "
            f"[bright_black]({len(siblings)} concerts)[/]"
        )
        for c in siblings[:6]:
            sample = c.audio_files[0].name if c.audio_files else ""
            extra = f"  [dim]· {sample}[/]" if sample else ""
            console.print(f"  [bright_black]- {c.folder.name}[/]{extra}")
        if len(siblings) > 6:
            console.print(f"  [bright_black]  ... and {len(siblings) - 6} more[/]")
    try:
        ans = input("artist > ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None
    console.print()
    if ans.lower() in ("q", "quit", "exit"):
        return None
    return ans


def _track_tolerance(track_count: int, audio_count: int, override: int) -> int:
    """Maximum |tracks - files| we'll accept before skipping.

    ``override`` mirrors the ``--track-tolerance`` flag:

    * ``-1`` — auto: ``max(2, ceil(0.15 * min(tracks, files)))``. Covers the
      common tape-trader cases (encore listed but not recorded, a tune-up at
      the start, a one-track splitting/combining difference) without letting
      silently-misaligned massive mismatches through.
    * ``0`` — strict equality, matching the pre-tolerance behaviour.
    * any positive integer — absolute cap, no scaling.
    """
    if override >= 0:
        return override
    shorter = min(track_count, audio_count)
    return max(2, -(-shorter * 15 // 100))  # ceil(0.15 * shorter) without math.ceil


def _apply(
    concerts: list[Concert],
    args: argparse.Namespace,
    mode: Mode,
    history: History | None,
) -> int:
    failures = 0
    skipped = 0
    applied = 0
    album_only = 0
    unchanged = 0
    verb = "tagged" if mode is Mode.IN_PLACE else "copied"
    copy_to_str = str(args.copy_to.resolve()) if args.copy_to else None
    for c in concerts:
        if mode is not Mode.DRY_RUN and c.confidence() < args.min_confidence:
            skipped += 1
            console.print(f"  [yellow]⏭  skip[/] (low conf {c.confidence():.2f}) [dim]{c.folder.name}[/]")
            continue
        if not c.audio_files:
            skipped += 1
            continue
        metadata_only = False
        if not c.tracks:
            # No parsed setlist — fall back to artist/album/date tagging only
            # (nothing to skip to; per-track tags stay as the files had them).
            if mode is Mode.DRY_RUN:
                skipped += 1
                continue
            metadata_only = True
            console.print(
                f"  [magenta]🧩 metadata-only[/] (no setlist parsed) "
                f"[dim]{c.folder.name}[/]"
            )
        elif mode is not Mode.DRY_RUN:
            mismatch = abs(len(c.tracks) - len(c.audio_files))
            if mismatch > 0:
                tolerance = _track_tolerance(
                    len(c.tracks), len(c.audio_files), args.track_tolerance,
                )
                if mismatch > tolerance:
                    console.print(
                        f"  [magenta]🧩 metadata-only[/] "
                        f"(track mismatch {len(c.tracks)}/{len(c.audio_files)}, "
                        f"off by {mismatch}) [dim]{c.folder.name}[/]"
                    )
                    metadata_only = True
                else:
                    console.print(
                        f"  [yellow]⚠️  partial tag[/] "
                        f"({len(c.tracks)} tracks / {len(c.audio_files)} files) "
                        f"[dim]{c.folder.name}[/]"
                    )
        if metadata_only and not (c.artist or c.date):
            # Nothing useful to stamp if we don't even have an artist or date.
            console.print(
                f"  [red]⏭  skip[/] (no artist/date to tag) "
                f"[dim]{c.folder.name}[/]"
            )
            skipped += 1
            continue
        source_root = args.path.resolve()
        plans = build_plans(
            c,
            copy_to_root=args.copy_to.resolve() if args.copy_to else None,
            source_root=source_root,
            metadata_only=metadata_only,
            minimal=args.minimal_tags,
        )
        results = apply_plans(plans, mode)
        folder_fails = 0
        folder_ok = 0
        folder_album_only = 0
        folder_unchanged = 0
        folder_skipped_official = 0
        for r in results:
            if not r.ok:
                failures += 1
                folder_fails += 1
                console.print(f"    [bold red]❌ FAIL[/] {r.plan.file.name}: {r.error}")
                continue
            applied += 1
            folder_ok += 1
            if r.skipped_official:
                folder_skipped_official += 1
                unchanged += 1
            elif r.album_only:
                if r.changed:
                    album_only += 1
                    folder_album_only += 1
                else:
                    unchanged += 1
                    folder_unchanged += 1
        if folder_fails == 0 and results:
            if folder_skipped_official == len(results):
                console.print(
                    f"  [magenta]💿 official release[/] [bold]{c.folder.name}[/] "
                    f"[dim](left untouched)[/]"
                )
            elif folder_unchanged == len(results):
                console.print(
                    f"  [bright_black]✓ unchanged[/] [bold]{c.folder.name}[/] "
                    f"[dim](already tagged)[/]"
                )
            elif folder_album_only + folder_unchanged == len(results):
                console.print(
                    f"  [cyan]📀 album only[/] [bold]{c.folder.name}[/] "
                    f"[dim]({folder_album_only} updated)[/]"
                )
            else:
                console.print(f"  [green]✅ {verb}[/] [bold]{c.folder.name}[/] [dim]({len(results)} tracks)[/]")
        if history is not None and results:
            history.record_tagging(
                c.folder,
                TaggingOutcome(
                    mode=mode.value,
                    applied_at=_utcnow(),
                    applied=folder_ok,
                    failed=folder_fails,
                    copy_to=copy_to_str,
                ),
            )
    console.print(
        f"\n[bold bright_white]🎉 Done.[/] "
        f"[green]applied[/]={applied}  "
        f"[cyan]album-only[/]={album_only}  "
        f"[bright_black]unchanged[/]={unchanged}  "
        f"[yellow]skipped[/]={skipped}  "
        f"[red]failed[/]={failures}  "
        f"[cyan]mode[/]={mode.value}"
    )
    return 1 if failures else 0


def _utcnow() -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def _resolve_history_path(args: argparse.Namespace) -> Path | None:
    if args.no_history:
        return None
    if args.history:
        return args.history
    if args.path.is_dir():
        return args.path / HISTORY_FILENAME
    return None


def _resolve_lexicon_path(args: argparse.Namespace) -> Path | None:
    if args.no_lexicon:
        return None
    if args.lexicon:
        return args.lexicon
    if args.path.is_dir():
        return args.path / LEXICON_FILENAME
    return None


def _build_lexicon(history: History, path: Path | None) -> Lexicon | None:
    """Rebuild the lexicon from history entries, persist it if possible."""
    if path is None:
        return None
    lex = Lexicon.from_history(history)
    try:
        lex.save(path)
    except OSError as exc:
        console.print(f"[yellow]⚠️  could not save lexicon: {exc}[/]")
    return lex


# Trailing "(24bit-192kHz)" / "[24B-44.1kHz]" fidelity tags on Qobuz/HDTracks
# folders. Stripping them exposes the bare "Artist - Album" underneath.
_BITRATE_TAIL = re.compile(r"\s*[\(\[](\d{1,2}\s*bit|\d{1,2}B)[-\s]?\d", re.IGNORECASE)


def _extract_release_artist(folder_name: str) -> str | None:
    """Extract the artist from a release-folder name, or None to skip.

    Handles the common shapes in a studio-release library:
      * ``Artist`` (bare)                      → ``Artist``
      * ``Artist - Album (1993) [24B-192kHz]`` → ``Artist``
      * ``Artist - 1993 - Album (24bit-192)``  → ``Artist``
      * ``_test`` / ``.hidden`` / ``(V/A) ...`` → None
      * dotted shorthand like ``Smith.Joe.1972.Album.Src.abcd`` → None (ambiguous)
    """
    if not folder_name or folder_name[0] in "_.":
        return None
    if folder_name[0] in "([":
        return None
    if " - " in folder_name:
        head = folder_name.split(" - ", 1)[0].strip()
        return head or None
    # "Smith.Joe.1972.Album.Src.abcd" — too many fields to disambiguate.
    if folder_name.count(".") >= 3 and " " not in folder_name.split(".")[0]:
        return None
    cleaned = _BITRATE_TAIL.split(folder_name)[0].strip()
    return cleaned or None


def _lexicon_command(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="tagcleaner lexicon",
        description="Manage the artist/venue lexicon.",
    )
    sub = p.add_subparsers(dest="action", required=True)

    imp = sub.add_parser(
        "import",
        help="Seed the lexicon from a directory of releases (one folder per album).",
    )
    imp.add_argument("dir", type=Path,
                     help="Directory whose top-level folders name an artist or 'Artist - Album'.")
    imp.add_argument("--lexicon", type=Path, required=True, metavar="FILE",
                     help="Lexicon file to merge into (created if missing).")
    imp.add_argument("--min-count", type=int, default=2, metavar="N",
                     help="Floor each added count at N so singletons clear the match "
                          "threshold (default: 2). Use 1 to preserve true counts.")
    imp.add_argument("--dry-run", action="store_true",
                     help="Report what would change without writing.")

    args = p.parse_args(argv)
    if args.action == "import":
        return _lexicon_import(args)
    return 2


def _lexicon_import(args: argparse.Namespace) -> int:
    from collections import Counter

    if not args.dir.is_dir():
        console.print(f"[bold red]❌ not a directory:[/] {args.dir}")
        return 2

    counter: Counter[str] = Counter()
    total = 0
    skipped = 0
    for entry in args.dir.iterdir():
        if not entry.is_dir():
            continue
        total += 1
        artist = _extract_release_artist(entry.name)
        if artist is None:
            skipped += 1
            continue
        counter[artist] += 1
    # Compilation bucket — not an artist the lexicon should confirm for.
    counter.pop("Various Artists", None)

    console.print(
        f"[cyan]📂 {args.dir}[/]: {total} folder(s), "
        f"{len(counter)} unique artists, {skipped} skipped."
    )

    lex = Lexicon.load(args.lexicon)
    before = set(lex.artists)
    new = sum(1 for a in counter if a not in before)
    console.print(
        f"[cyan]📚 lexicon[/]: {len(lex.artists)} existing artist(s); "
        f"{new} new, {len(counter) - new} already present."
    )

    if args.dry_run:
        console.print("[bold cyan]🧪 Dry run — no changes written.[/]")
        return 0

    added = 0
    bumped = 0
    for artist, count in counter.items():
        lex.add_artist(artist, count=max(count, args.min_count))
        if artist in before:
            bumped += 1
        else:
            added += 1

    try:
        lex.save(args.lexicon)
    except OSError as exc:
        console.print(f"[bold red]❌ could not save lexicon:[/] {exc}")
        return 1
    console.print(
        f"[green]✅ imported[/] — {added} new, {bumped} bumped. "
        f"Lexicon now holds {len(lex.artists)} artist(s)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] == "lexicon":
        return _lexicon_command(raw[1:])
    args = _parse_args(raw)
    mode = _mode(args)

    if not args.no_banner:
        render_banner(console)

    history_path = _resolve_history_path(args)
    history = load_history(history_path) if history_path else History()
    lexicon_path = _resolve_lexicon_path(args)
    lexicon = _build_lexicon(history, lexicon_path)
    if lexicon is not None and (lexicon.artists or lexicon.venues):
        console.print(
            f"[bright_black]📚 Lexicon: {len(lexicon.artists)} artists, "
            f"{len(lexicon.venues)} venues[/]"
        )

    skipped_entries: list[HistoryEntry] = []
    if args.load_drafts:
        concerts = load_drafts(args.load_drafts)
        console.print(f"[bright_black]📂 Loaded {len(concerts)} drafts from {args.load_drafts}[/]")
    else:
        if not args.path.is_dir():
            console.print(f"[bold red]❌ error:[/] not a directory: {args.path}")
            return 2
        fresh, skipped_entries = _scan_with_progress(
            args.path,
            history=history,
            mode=mode,
            copy_to=args.copy_to,
            rescan_all=args.rescan_all,
            plain=args.plain,
            lexicon=lexicon,
            exclude=args.exclude,
        )
        concerts = [c for c, _fp, _mt in fresh]
        for concert, fp, mtime in fresh:
            history.record_scan(concert, fp, mtime)
        if skipped_entries:
            console.print(
                f"[green]   found[/] [bold]{len(concerts)}[/] fresh, "
                f"[cyan]⏭ skipped[/] [bold]{len(skipped_entries)}[/] already-tagged\n"
            )
        else:
            console.print(f"[green]   found[/] [bold]{len(concerts)}[/] concert folder(s)\n")

    if not concerts and not skipped_entries:
        console.print("[yellow]🤷 No concert folders found.[/]")
        _save_history_if_enabled(history, history_path)
        _save_lexicon_if_enabled(lexicon, lexicon_path)
        return 0

    if args.prompt_unknown:
        _prompt_unknown_artists(concerts, lexicon, lexicon_path)

    if args.enrich_setlistfm:
        if not args.setlistfm_key:
            console.print("[bold red]❌ --enrich-setlistfm requires --setlistfm-key or SETLISTFM_API_KEY.[/]")
            return 2
        console.print("[magenta]🌐 Enriching from setlist.fm[/]")
        _enrich_all(concerts, args.setlistfm_key, args.setlistfm_overwrite)
        console.print()

    if args.verbose:
        _render_verbose(concerts)
    else:
        _render_summary(concerts)

    if skipped_entries:
        console.print(
            f"[bright_black]   (plus {len(skipped_entries)} folder(s) already tagged — "
            f"use --rescan-all to re-parse)[/]"
        )

    drafts_path = args.drafts or (args.path / "tagcleaner-drafts.json" if args.path.is_dir() else None)
    if drafts_path and concerts:
        try:
            save_drafts(concerts, drafts_path)
            console.print(f"[bright_black]💾 Drafts written to {drafts_path}[/]")
        except OSError as exc:
            console.print(f"[yellow]⚠️  could not save drafts: {exc}[/]")

    if mode is Mode.DRY_RUN:
        console.print("\n[bold cyan]🧪 Dry run — no files modified.[/]")
        _save_history_if_enabled(history, history_path)
        _save_lexicon_if_enabled(lexicon, lexicon_path)
        return 0

    if not concerts:
        console.print("\n[bright_black]Nothing new to tag — history already covers every folder.[/]")
        _save_history_if_enabled(history, history_path)
        _save_lexicon_if_enabled(lexicon, lexicon_path)
        return 0

    if not args.yes:
        action = (
            "🏷️  write tags in place" if mode is Mode.IN_PLACE
            else f"📋 copy + tag into [bold]{args.copy_to}[/]"
        )
        console.print()
        if not _confirm(f"Proceed to {action}?"):
            console.print("[yellow]✋ Aborted.[/]")
            _save_history_if_enabled(history, history_path)
            _save_lexicon_if_enabled(lexicon, lexicon_path)
            return 0

    code = _apply(concerts, args, mode, history)
    _save_history_if_enabled(history, history_path)
    _save_lexicon_if_enabled(lexicon, lexicon_path)
    return code


def _save_history_if_enabled(history: History, path: Path | None) -> None:
    if path is None:
        return
    try:
        save_history(history, path)
    except OSError as exc:
        console.print(f"[yellow]⚠️  could not save history: {exc}[/]")


def _save_lexicon_if_enabled(lexicon: Lexicon | None, path: Path | None) -> None:
    if path is None or lexicon is None:
        return
    try:
        lexicon.save(path)
    except OSError as exc:
        console.print(f"[yellow]⚠️  could not save lexicon: {exc}[/]")


if __name__ == "__main__":
    sys.exit(main())
