"""Shared Unicode admission runtime metadata and drift signaling."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from growthlane.guard import IGNORABLE_CORPUS_UNICODE_VERSION, unicode_admission_drift
from growthlane.notify import Digest


@dataclass(frozen=True)
class UcdRuntimeStatus:
    corpus_version: str
    runtime_version: str
    drifted: bool

    @property
    def direction(self) -> str:
        if self.runtime_version == self.corpus_version:
            return "runtime=corpus"
        runtime = tuple(int(part) for part in self.runtime_version.split("."))
        corpus = tuple(int(part) for part in self.corpus_version.split("."))
        return "runtime>corpus" if runtime > corpus else "runtime<corpus"


def runtime_status() -> UcdRuntimeStatus:
    drift = unicode_admission_drift()
    if drift is not None:
        corpus_version, runtime_version = drift
        return UcdRuntimeStatus(corpus_version, runtime_version, True)
    return UcdRuntimeStatus(
        IGNORABLE_CORPUS_UNICODE_VERSION,
        unicodedata.unidata_version,
        False,
    )


def emit_admission_refusal(digest: Digest, face: str, *, context: str) -> UcdRuntimeStatus | None:
    status = runtime_status()
    if not status.drifted:
        return None
    digest.emit(
        f"[RED] {face}: UCD drift runtime={status.runtime_version} "
        f"corpus={status.corpus_version} direction={status.direction}; {context}"
    )
    return status


def emit_deletion_drift_note(
    digest: Digest, face: str, operation: str
) -> UcdRuntimeStatus | None:
    status = runtime_status()
    if not status.drifted:
        return None
    digest.emit(
        f"[RED] {face}: UCD drift runtime={status.runtime_version} "
        f"corpus={status.corpus_version} — matching may miss variants; "
        f"direction={status.direction}; {operation} continues for deletion direction only"
    )
    return status
