"""Startup header + the animated music-note column used during the scan."""
from __future__ import annotations

import random
import time

from rich.console import Console
from rich.progress import ProgressColumn, Task
from rich.text import Text

TAGLINE = "🎵  [bold bright_magenta]TagCleaner[/]  🎸  [bright_cyan]tidy concert metadata in bulk[/]  🎚️"


def render_banner(console: Console) -> None:
    console.print(TAGLINE + "\n")


# Characters that look good scrolling sideways at small widths.
_NOTES = ("♪", "♫", "♬", "♩", "♭", "♮", "♯")
# Rich-style colors chosen to read on both dark and light terminal themes.
_PALETTE = (
    "bright_magenta", "magenta", "medium_orchid", "bright_cyan",
    "cyan", "sky_blue2", "bright_yellow", "yellow", "bright_green",
    "green", "bright_red", "deep_pink3",
)


class AnimatedNotesColumn(ProgressColumn):
    """A Rich progress column that renders a row of music notes scrolling
    leftward with random colors. The content changes on every progress
    refresh, so the user sees continuous activity even when folder parsing
    is stuck on a slow filesystem call.
    """

    def __init__(self, width: int = 20, *, speed_hz: float = 6.0) -> None:
        super().__init__()
        self.width = width
        self.speed_hz = speed_hz
        self._rng = random.Random()
        # A deque of (char, color) tuples, length == width. Rightmost slot is
        # the "head" where new notes appear; each tick we shift left and push
        # a new random cell on.
        self._cells: list[tuple[str, str]] = [(" ", "white")] * width
        self._last_tick = 0.0

    def _advance_if_due(self) -> None:
        now = time.monotonic()
        interval = 1.0 / max(self.speed_hz, 0.1)
        if now - self._last_tick < interval:
            return
        # If we fell behind (e.g., slow I/O), fast-forward multiple steps so
        # the animation keeps pace with wall-clock time instead of lagging.
        steps = max(1, int((now - self._last_tick) / interval))
        for _ in range(min(steps, self.width)):
            if self._rng.random() < 0.55:
                ch = self._rng.choice(_NOTES)
                color = self._rng.choice(_PALETTE)
            else:
                ch, color = " ", "white"
            self._cells = self._cells[1:] + [(ch, color)]
        self._last_tick = now

    def render(self, task: Task) -> Text:
        self._advance_if_due()
        text = Text()
        for ch, color in self._cells:
            text.append(ch, style=color)
        return text
