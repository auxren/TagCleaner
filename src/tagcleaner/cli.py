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
import sys
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import __version__
from .banner import ScanDisplay, render_banner
from .drafts import save_drafts, load_drafts
from .history import (
    HISTORY_FILENAME,
    History,
    HistoryEntry,
    TaggingOutcome,
    fingerprint as history_fingerprint,
    load_history,
    save_history,
    should_skip as history_should_skip,
)
from .models import Concert
from .scanner import scan
from .setlistfm import SetlistFmClient, SetlistFmError, enrich, merge_enrichment
from .tagger import Mode, apply_plans, build_plans

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
    p.add_argument("--min-confidence", type=float, default=0.5,
                   help="Skip concerts below this confidence in non-dry-run modes (default: 0.5).")
    p.add_argument("--yes", action="store_true",
                   help="Do not prompt before applying tags.")
    p.add_argument("-v", "--verbose", action="store_true", help="Show full per-track table.")
    p.add_argument("--no-banner", action="store_true",
                   help="Suppress the startup ASCII banner.")
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
) -> tuple[list[Concert], list[HistoryEntry]]:
    """Run scanner.scan() behind a live animated panel so the user can see
    continuous motion even on slow/remote filesystems. The ScanDisplay is
    ticked by Rich's refresh thread, so it keeps animating while the main
    thread is blocked on filesystem I/O.

    Returns ``(fresh, skipped)``: fresh concerts parsed this run, and the
    history entries for folders skipped because they were already tagged
    and their audio contents haven't changed.
    """
    console.print(f"[cyan]🔍 Scanning[/] [bold]{root}[/] ...")
    width = max(48, min((console.size.width or 80) - 8, 78))
    display = ScanDisplay(staff_width=width)
    skipped: list[HistoryEntry] = []

    def _skip(folder: Path, audio: list[Path], info: Path | None) -> bool:
        if rescan_all:
            return False
        fp = history_fingerprint(folder, audio, info)
        entry = history.get(folder)
        if history_should_skip(entry, fp, mode, copy_to):
            skipped.append(entry)  # type: ignore[arg-type]  # guarded by should_skip
            return True
        return False

    with Live(display, console=console, refresh_per_second=12, transient=True):
        fresh = scan(
            root,
            skip=_skip,
            on_folder=display.on_folder,
            on_skip=display.on_skip,
            on_done=lambda c, i, t: display.on_done(c),
        )
    return fresh, skipped


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


def _apply(
    concerts: list[Concert],
    args: argparse.Namespace,
    mode: Mode,
    history: History | None,
) -> int:
    failures = 0
    skipped = 0
    applied = 0
    verb = "tagged" if mode is Mode.IN_PLACE else "copied"
    copy_to_str = str(args.copy_to.resolve()) if args.copy_to else None
    for c in concerts:
        if mode is not Mode.DRY_RUN and c.confidence() < args.min_confidence:
            skipped += 1
            console.print(f"  [yellow]⏭  skip[/] (low conf {c.confidence():.2f}) [dim]{c.folder.name}[/]")
            continue
        if not c.tracks or not c.audio_files:
            skipped += 1
            continue
        if len(c.tracks) != len(c.audio_files) and mode is not Mode.DRY_RUN:
            console.print(f"  [red]⏭  skip[/] (track mismatch) [dim]{c.folder.name}[/]")
            skipped += 1
            continue
        source_root = args.path.resolve()
        plans = build_plans(
            c,
            copy_to_root=args.copy_to.resolve() if args.copy_to else None,
            source_root=source_root,
        )
        results = apply_plans(plans, mode)
        folder_fails = 0
        folder_ok = 0
        for r in results:
            if not r.ok:
                failures += 1
                folder_fails += 1
                console.print(f"    [bold red]❌ FAIL[/] {r.plan.file.name}: {r.error}")
            else:
                applied += 1
                folder_ok += 1
        if folder_fails == 0 and results:
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    mode = _mode(args)

    if not args.no_banner:
        render_banner(console)

    history_path = _resolve_history_path(args)
    history = load_history(history_path) if history_path else History()

    skipped_entries: list[HistoryEntry] = []
    if args.load_drafts:
        concerts = load_drafts(args.load_drafts)
        console.print(f"[bright_black]📂 Loaded {len(concerts)} drafts from {args.load_drafts}[/]")
    else:
        if not args.path.is_dir():
            console.print(f"[bold red]❌ error:[/] not a directory: {args.path}")
            return 2
        concerts, skipped_entries = _scan_with_progress(
            args.path,
            history=history,
            mode=mode,
            copy_to=args.copy_to,
            rescan_all=args.rescan_all,
        )
        for c in concerts:
            history.record_scan(
                c,
                history_fingerprint(c.folder, c.audio_files, c.info_txt),
            )
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
        return 0

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
        return 0

    if not concerts:
        console.print("\n[bright_black]Nothing new to tag — history already covers every folder.[/]")
        _save_history_if_enabled(history, history_path)
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
            return 0

    code = _apply(concerts, args, mode, history)
    _save_history_if_enabled(history, history_path)
    return code


def _save_history_if_enabled(history: History, path: Path | None) -> None:
    if path is None:
        return
    try:
        save_history(history, path)
    except OSError as exc:
        console.print(f"[yellow]⚠️  could not save history: {exc}[/]")


if __name__ == "__main__":
    sys.exit(main())
