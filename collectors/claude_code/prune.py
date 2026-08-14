"""Deterministic Tier L retention pruning."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path


_OBS_FILE = re.compile(r"(?:usage-)?(\d{4}-\d{2}-\d{2})\.jsonl\Z")


def prune(obs_root: str | Path, run_date: str | date) -> list[Path]:
    """Delete observation files more than 30 days older than ``run_date``."""

    anchor = date.fromisoformat(run_date) if isinstance(run_date, str) else run_date
    obslog_root = Path(obs_root).expanduser() / "obslog"
    removed: list[Path] = []
    if not obslog_root.is_dir():
        return removed

    # overlay-contract §10: observation and safe-direction pruning continue
    # while KILLSWITCH exists. There is intentionally no killswitch gate here.
    for path in sorted(obslog_root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        match = _OBS_FILE.fullmatch(path.name)
        if match is None:
            continue
        try:
            bucket_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if (anchor - bucket_date).days > 30:
            path.unlink()
            removed.append(path)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune Tier L observation logs")
    parser.add_argument("--obs-root", required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    for removed in prune(args.obs_root, args.date):
        print(f"PRUNED {removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
