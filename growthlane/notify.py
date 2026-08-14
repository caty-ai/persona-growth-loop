"""Append-only private nightly digest with stdout mirroring."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping


class Digest:
    def __init__(self, pgl_home: Path, run_date: str) -> None:
        self.pgl_home = pgl_home
        self.run_date = run_date
        self.count = 0
        self._soul_alerted_faces: set[str] = set()

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

    def mark_soul_alerted(self, face: str) -> bool:
        """Mark a face alerted for this run and report whether it was the first."""

        if face in self._soul_alerted_faces:
            return False
        self._soul_alerted_faces.add(face)
        return True


def send_soul_alert(
    config: Mapping[str, object],
    face: str,
    reason: BaseException | str,
    digest: Digest,
) -> None:
    """Best-effort soul escalation, deduplicated within one digest/run."""

    if not digest.mark_soul_alerted(face):
        return
    configured = config.get("soul_alert_argv")
    if configured is None or configured == []:
        digest.emit(f"[WARN] {face}: soul alert not configured")
        return
    if (
        not isinstance(configured, list)
        or not configured
        or any(not isinstance(item, str) or not item.strip() for item in configured)
    ):
        digest.emit(f"[WARN] {face}: invalid soul_alert_argv ignored")
        return
    argv = [os.path.expanduser(configured[0]), *configured[1:]]
    message = (
        f"[RED] {face}: soul baseline check failed\n"
        f"reason: {reason}\n"
        f"date: {digest.run_date}\n"
        f"再基線はオーナーの明示指示で `bin/pgl-baseline {face} <path>`\n"
    )
    try:
        completed = subprocess.run(
            argv,
            input=message,
            timeout=20,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            reason_text = f"exit {completed.returncode}"
            if detail:
                reason_text += f": {detail}"
            digest.emit(f"[WARN] {face}: soul alert delivery failed: {reason_text}")
    except Exception as exc:
        digest.emit(f"[WARN] {face}: soul alert delivery failed: {exc}")
