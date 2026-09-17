"""Headless manual confirmation: commands appended to a file are executed by the engine.

`./cmd.sh b 1` appends "b 1" to <state>/commands; the running service picks it up within
`poll_s`. This is how a launchd-managed process (no stdin) receives human confirmations.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from solana_sniper.telemetry.logging import get_logger

log = get_logger(__name__)

Handler = Callable[[str], Awaitable[None]]


class FileCommandSource:
    def __init__(self, path: Path, *, poll_s: float = 0.5) -> None:
        self._path = path
        self._poll = poll_s
        self._offset = 0
        self.processed = 0

    def read_new_lines(self) -> list[str]:
        try:
            size = self._path.stat().st_size
        except OSError:
            return []
        if size < self._offset:  # truncated/rotated by an operator
            self._offset = 0
        if size == self._offset:
            return []
        with self._path.open("rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
        # only consume complete lines; a partially written line waits for the next poll
        last_newline = chunk.rfind(b"\n")
        if last_newline < 0:
            return []
        consumed = chunk[: last_newline + 1]
        self._offset += len(consumed)
        return [
            line.strip()
            for line in consumed.decode("utf-8", "replace").splitlines()
            if line.strip()
        ]

    async def run(self, handler: Handler) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._path.touch(exist_ok=True)
        # commands written before this process started are stale: skip them
        with contextlib.suppress(OSError):
            self._offset = self._path.stat().st_size
        while True:
            for line in self.read_new_lines():
                self.processed += 1
                log.info("file_command", command=line)
                await handler(line)
            await asyncio.sleep(self._poll)
