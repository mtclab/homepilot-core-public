"""Console output shared by the `hp` command modules."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table


class CliConsole(Console):
    """A console that never truncates a table when stdout is not a terminal.

    Off a terminal Rich assumes 80 columns and shortens what does not fit:
    `hp agents list | cat` printed "hp-test…" and "connect…". Piped or
    captured output is read by scripts and pasted into issues, so a table is
    printed at its natural width there; on a terminal nothing changes.
    """

    def print(self, *objects: Any, **kwargs: Any) -> None:
        if (
            not self.is_terminal
            and "width" not in kwargs
            and len(objects) == 1
            and isinstance(objects[0], Table)
        ):
            from rich.measure import Measurement

            natural = Measurement.get(self, self.options.update_width(100_000), objects[0])
            if natural.maximum > self.width:
                # `print(width=...)` is clamped to the console's own width, so the
                # table goes through a console as wide as the table, same file.
                wide = Console(file=self.file, width=natural.maximum, no_color=self.no_color)
                wide.print(*objects, **kwargs)
                return
        super().print(*objects, **kwargs)
