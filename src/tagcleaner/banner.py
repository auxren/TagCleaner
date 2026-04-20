"""Startup banner — music-themed ASCII art rendered in a pink→cyan gradient."""
from __future__ import annotations

from rich.console import Console
from rich.text import Text

BANNER = r"""
         ⣀                                    ⢀⡄        ⣠⢤⡀⡀
⠈⠓⢤⣀    ⣾⠿⡄           ⢀⣶⣤⣤⣀⡀            ⣸⣷⣀⣀⣀⡠⠤⠿⢧⡧⠓  ⣀⣤⠤⠤⢄⣀⣰⠷⡆
    ⠈⠉⠒⠦⢤⣏⣰⡇      ⢀⡾⠿⣯⣽⣻⡏    ⣠⣀⣀⡤⠤⠴⠒⠒  ⡏⣻⣇⡤⠤⠖⠒⢲⣚⡩⠭⠭⠽⣛⣟⣛⣛⣻⣿⣿⣿⣷⣦⣄
 ⠉⠒⠤⣀  ⣠⣿⠟⠉⠙⠒⠒⢒⣶⡄⡞  ⠐⠒⢒⡟⠒⠚⠉⠉⠁⡿⣦⣀⣠⠤⠤⠒⢾⡿⣁⣯⠤⠔⣒⣫⣭⠤⠐⣒⡫⠭⣤⡞⠒⠚⠉⠉⠉⠙⠚⠛⠿⢿⣿⣦⣷⡀
 ⣀    ⣽⡿⠿⡧⠤⢀⣀⣀⣀⣿⣿⣿⣁⣠⣶⣶⣼⠡⠤⠤⠖⢒⢺⠉⡹⢁⣀⡠⠤⣶⡆⢉⣁⠤⠖⢂⣏⠭⢿⠂⠉⠁                    ⢹⡇⠇
   ⠉⠒⣼⣏⣠⣴⣷⣦⡄      ⡀  ⠈⠛⠛⠁    ⣀⣠⠿⠟⠒⠋⠉⢀⣠⢼⠟⠛⢉⡠⠔⠚⣩⡇⢰⡾
  ⠠⢄⡀⢸⡇⢿⠉⣿⠛⣿⠲⠤⠤⠤⢴⣧⠴⠒⠒⠒⠈⠉⠉⡄  ⣀⡀⠤⠒⠊⠉⣀⣸⠖⠊⠉
     ⠉⠓⠻⢤⣕⣿⡴⠋     ⣾⡟⣧⢀⣀⣠⠤⢄⡞⠛⠉⠁  ⣠⠤⠒⠋⣥⣼
  ⠐⠦⢀⡀⣠⣤⡀⢹⠈⠉⠉⠉⠉⠉⡏⢻⣿⠁    ⢀⣜⣹⡧⠖⠊⠉       ⠙⠁
      ⢿⣛⣣⡾⢤⣀⣀⣀⣤⣴⣅⣴⠧⠄⠒⠚⠛⠛⠋⠁
         ⠉⠉        ⠘⠛⠋  ⠈
"""

_GRADIENT = [
    "bright_magenta",
    "magenta",
    "magenta",
    "purple4",
    "deep_pink3",
    "medium_orchid",
    "blue_violet",
    "medium_purple1",
    "sky_blue2",
    "deep_sky_blue1",
    "cyan1",
]

TAGLINE = "🎵  TagCleaner  🎸  tidy concert metadata in bulk  🎚️"


def render_banner(console: Console) -> None:
    """Print the banner with a per-line color gradient. Safe on no-color TTYs."""
    lines = BANNER.splitlines()[1:]  # drop leading blank
    text = Text()
    for i, line in enumerate(lines):
        color = _GRADIENT[min(i, len(_GRADIENT) - 1)]
        text.append(line + "\n", style=color)
    console.print(text, end="")
    console.print(f"[bold bright_white]{TAGLINE}[/]\n")
