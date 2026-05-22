"""Lightweight per-step progress prefix.

Each step main() instantiates one StepProgress(label, total), then calls
.item() before each iteration to get a "[3/9]" prefix string. .done()
prints a final timing line.

Why not tqdm: run_all.py runs each step as a subprocess and forwards its
stdout. tqdm's carriage-return redraw fights with line-buffered subprocess
output; plain prefixed lines render reliably anywhere.
"""
from __future__ import annotations

import sys
import time


class StepProgress:
    def __init__(self, label: str, total: int, stream=None) -> None:
        self.label = label
        self.total = total
        self.i = 0
        self.t0 = time.monotonic()
        self.stream = stream or sys.stderr
        print(f"\n[{label}] starting — {total} item(s)", file=self.stream)

    def item(self, name: str | None = None) -> str:
        """Bump the counter and return a '[i/N]' prefix for the next log line.
        Optional `name` is appended for convenience: '[3/9] song_x'."""
        self.i += 1
        prefix = f"[{self.i}/{self.total}]"
        if name:
            return f"{prefix} {name}"
        return prefix

    def next(self, name: str | None = None) -> None:
        """Bump counter and print a banner line for the next iteration."""
        self.i += 1
        head = f"[{self.i}/{self.total}]"
        line = f"{head} {name}" if name else head
        print(f"\n--- {line} " + "-" * max(0, 60 - len(line)), file=self.stream)

    def tick(self, msg: str) -> None:
        """Print a status line tied to this step without bumping the counter."""
        print(f"[{self.label}] {msg}", file=self.stream)

    def done(self, ok: int | None = None, failed: int | None = None) -> None:
        dt = time.monotonic() - self.t0
        parts = [f"{self.i}/{self.total} processed"]
        if ok is not None:
            parts.append(f"{ok} ok")
        if failed is not None:
            parts.append(f"{failed} failed")
        parts.append(f"in {dt:.0f}s")
        print(f"[{self.label}] done — " + ", ".join(parts), file=self.stream)
