"""A record of what the model was asked and what went wrong.

Rejected actions are the most valuable thing a run produces: each one is a case where the prompt,
the board description or the schema failed to convey a rule the engine enforces. Kept as JSON
lines with the board that produced them, they can be replayed against a revised prompt later,
which is the only honest way to tell whether a prompt change actually helped.

Nothing here is on the critical path of a battle, so a journal that cannot be written is reported
once and then ignored rather than interrupting a run that is otherwise fine.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path) if path else None
        self._broken = False
        self._counts: dict[str, int] = {}

        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"Cannot create the journal directory for '{self._path}': {exc}", file=sys.stderr)
                self._broken = True

    @property
    def path(self) -> Path | None:
        return self._path

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def write(self, kind: str, **fields: Any) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1

        if self._path is None or self._broken:
            return

        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **fields}

        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            print(f"Cannot write to the journal '{self._path}': {exc}. Journalling is now off.", file=sys.stderr)
            self._broken = True

    def summary(self) -> str:
        if not self._counts:
            return "nothing journalled"

        parts = ", ".join(f"{count} {kind}" for kind, count in sorted(self._counts.items()))

        return parts + (f" -> {self._path}" if self._path else "")
