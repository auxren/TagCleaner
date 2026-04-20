"""Startup header + the big animated scan panel.

The scan panel is deliberately more than a progress bar: it's a multi-line
`rich.live.Live` renderable so that on slow/remote filesystems the user sees
continuous motion (scrolling music notes) plus a running rolodex of the
artists we just discovered. The animation ticks independently of the main
thread's I/O, so a hung `iterdir` call never makes the UI feel frozen.
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.text import Text

TAGLINE = "🎵  [bold bright_magenta]TagCleaner[/]  🎸  [bright_cyan]tidy concert metadata in bulk[/]  🎚️"


def render_banner(console: Console) -> None:
    console.print(TAGLINE + "\n")


_NOTES = ("♪", "♫", "♬", "♩", "♭", "♮", "♯")
# NB: avoid Supplementary-Plane glyphs like 𝅘𝅥𝅮 (U+1D160) here. Rich measures
# them as 1 cell; many terminals render them as 2 (or 0), and per-frame width
# drift eventually corrupts Live's cursor-up bookkeeping and the panel starts
# duplicating in scrollback instead of redrawing in place.
_DENSITY = 0.42  # probability that a newly-introduced cell is a note vs. empty
_PALETTE = (
    "bright_magenta", "magenta", "medium_orchid", "deep_pink3",
    "bright_cyan", "cyan", "sky_blue2", "steel_blue1",
    "bright_yellow", "yellow", "gold3",
    "bright_green", "green", "spring_green2",
    "bright_red", "orange1",
)
_STAFF_COLORS = ("grey39", "grey42", "grey46")
_EMOJI_ROLODEX = ("🎵", "🎶", "🎸", "🎤", "🎧", "🎺", "🎷", "🎹", "🥁")


@dataclass
class _Finding:
    emoji: str
    artist: str
    album: str
    confidence: float


@dataclass
class ScanDisplay:
    """Rich renderable that combines: a header, a scrolling three-row music
    staff, a 'now parsing' headline, and a rolodex of the most-recently
    discovered concerts. Ticked by Rich's Live refresh thread, so it keeps
    animating while the main thread is blocked on filesystem I/O.
    """

    title: str = "🎵 TagCleaner"
    staff_rows: int = 3
    staff_width: int = 70
    rolodex_size: int = 6
    tick_hz: float = 12.0

    _staff: list[list[tuple[str, str]]] = field(default_factory=list)
    _last_tick: float = 0.0
    _rng: random.Random = field(default_factory=random.Random)
    _current_folder: str = ""
    _current_pulse: int = 0
    _idx: int = 0
    _total: int = 0
    _skipped: int = 0
    _start: float = field(default_factory=time.monotonic)
    _recent: deque[_Finding] = field(default_factory=lambda: deque(maxlen=6))

    def __post_init__(self) -> None:
        self._staff = [
            [(" ", "white") for _ in range(self.staff_width)]
            for _ in range(self.staff_rows)
        ]
        self._recent = deque(maxlen=self.rolodex_size)

    # ---- state updates (called from the main thread) ----

    def on_folder(self, path: Path, idx: int, total: int) -> None:
        self._current_folder = path.name
        self._idx = idx
        self._total = total

    def on_skip(self, path: Path, idx: int, total: int) -> None:
        self._idx = idx
        self._total = total
        self._skipped += 1

    def on_done(self, concert) -> None:
        if not (concert.artist or concert.date):
            return
        emoji = self._rng.choice(_EMOJI_ROLODEX)
        album = concert.album_name() or "(album unknown)"
        self._recent.appendleft(_Finding(
            emoji=emoji,
            artist=concert.artist or "(unknown artist)",
            album=album,
            confidence=concert.confidence(),
        ))

    # ---- animation ----

    def _advance(self) -> None:
        now = time.monotonic()
        interval = 1.0 / max(self.tick_hz, 0.1)
        if now - self._last_tick < interval:
            return
        steps = max(1, min(int((now - self._last_tick) / interval), self.staff_width))
        for _ in range(steps):
            for r, row in enumerate(self._staff):
                if self._rng.random() < _DENSITY:
                    ch = self._rng.choice(_NOTES)
                    color = self._rng.choice(_PALETTE)
                else:
                    ch, color = " ", "white"
                # shift left, push on right
                row.pop(0)
                row.append((ch, color))
            self._current_pulse = (self._current_pulse + 1) % 12
        self._last_tick = now

    # ---- rendering ----

    def __rich__(self) -> RenderableType:
        self._advance()
        body = Group(*self._body())
        return Panel(
            body,
            title=f"[bold bright_white]{self.title}[/]",
            border_style="bright_magenta",
            padding=(0, 2),
        )

    def _body(self) -> Iterable[RenderableType]:
        yield Text("")
        yield self._progress_line()
        yield Text("")
        for i, row in enumerate(self._staff):
            yield self._staff_row(row)
            if i < len(self._staff) - 1:
                yield Text("─" * self.staff_width,
                           style=_STAFF_COLORS[i % len(_STAFF_COLORS)])
        yield Text("")
        yield self._now_parsing_line()
        yield Text("")
        yield Text("recently found", style="bold bright_cyan")
        if not self._recent:
            yield Text("  [dim]listening...[/]", style="dim")
        else:
            for age, finding in enumerate(self._recent):
                yield self._finding_line(finding, age)

    def _progress_line(self) -> Text:
        total = self._total or 1
        frac = min(self._idx / total, 1.0)
        bar_w = self.staff_width - 24
        filled = int(frac * bar_w)
        bar = Text()
        bar.append("▰" * filled, style="bright_magenta")
        bar.append("▱" * (bar_w - filled), style="grey35")
        elapsed = int(time.monotonic() - self._start)
        mins, secs = divmod(elapsed, 60)
        pct = f"{self._idx:>5}/{self._total:<5}"
        timing = f"{mins:02d}:{secs:02d}"
        line = Text()
        line.append("  ")
        line.append_text(bar)
        line.append(f"  {pct} ", style="bold")
        line.append(timing, style="bright_cyan")
        if self._skipped:
            line.append(f"  ⏭ {self._skipped} cached", style="bright_black")
        return line

    def _staff_row(self, row: list[tuple[str, str]]) -> Text:
        text = Text()
        for ch, color in row:
            text.append(ch, style=color)
        return text

    def _now_parsing_line(self) -> Text:
        pulse_styles = ("bold bright_yellow", "bold yellow",
                        "bold bright_magenta", "bold magenta")
        style = pulse_styles[self._current_pulse % len(pulse_styles)]
        line = Text()
        line.append("🎸 now parsing: ", style="bold bright_white")
        folder = self._current_folder or "…"
        if len(folder) > self.staff_width - 20:
            folder = folder[: self.staff_width - 23] + "..."
        line.append(folder, style=style)
        return line

    def _finding_line(self, finding: _Finding, age: int) -> Text:
        # Newest == brightest; older entries fade out through the palette.
        fades = ("bright_white", "white", "grey82", "grey70", "grey58", "grey46")
        fade = fades[min(age, len(fades) - 1)]
        line = Text(f"  {finding.emoji} ")
        line.append(finding.artist, style=f"bold {fade}")
        line.append(" — ", style="grey50")
        line.append(finding.album, style=fade)
        if finding.confidence < 0.75:
            line.append(f"  ({finding.confidence:.2f})", style="yellow")
        return line


def make_post_scan_progress(console: Console) -> Progress:
    """A simpler progress bar for the apply/copy phase after the scan has
    produced its drafts. Not animated — by this point the user has seen the
    full scan panel and just wants a plain tag-writing tick."""
    return Progress(
        TextColumn("[bold cyan]{task.description}[/]"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
