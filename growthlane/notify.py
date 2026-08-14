"""Append-only private nightly digest with stdout mirroring."""

from __future__ import annotations

import os
from pathlib import Path


class Digest:
    def __init__(self, pgl_home: Path, run_date: str) -> None:
        self.pgl_home = pgl_home
        self.run_date = run_date
        self.count = 0

    @property
    def path(self) -> Path:
        return self.pgl_home / "digest" / f"{self.run_date}.md"

    def emit(self, line: str) -> None:
        clean = " ".join(str(line).splitlines()).strip()
        if not clean:
            clean = "event"
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            os.write(descriptor, (f"- {clean}\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        self.count += 1
        print(clean, flush=True)

    def ensure_line(self) -> None:
        if self.count == 0:
            self.emit("nightly: no changes")
