"""Independent read-only render-size monitor."""

from __future__ import annotations

import os
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .faces import FaceProfile, get_profile
from .notify import Digest


CAPS = {"adopted": 1800, "candidates": 720}


def _sizes(profile: FaceProfile, home: Path) -> dict[str, int]:
    if profile.engine:
        return {
            key: (home / profile.render_files[key]).stat().st_size
            for key in ("adopted", "candidates")
        }
    path = home / profile.render_files["combined"]
    payload = path.read_bytes() if path.is_file() else b""
    sizes = {"adopted": 0, "candidates": 0}
    for line in payload.splitlines(keepends=True):
        key = "candidates" if line.startswith("試用中".encode("utf-8")) else "adopted"
        sizes[key] += len(line)
    return sizes


def inspect(profile: FaceProfile, pgl_home: Path, config: Mapping[str, object], digest: Digest) -> bool:
    try:
        sizes = _sizes(profile, profile.resolve_home(pgl_home, config))
    except Exception as exc:
        digest.emit(f"[WARN] {profile.name}: tripwire cannot read render: {exc}")
        return False
    exceeded = False
    for plane, size in sizes.items():
        cap = CAPS[plane]
        if size > cap:
            exceeded = True
            digest.emit(f"[RED] {profile.name}: {plane} cap exceeded {size}/{cap}B")
        elif size >= cap * 0.8:
            digest.emit(f"[WARN] {profile.name}: {plane} at {size}/{cap}B")
    if exceeded:
        marker = pgl_home / "KILLSWITCH.proposed"
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, f"tripwire proposed by {profile.name}\n".encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, marker)
        os.chmod(marker, 0o600)
    return exceeded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect overlay render caps")
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("--date")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    pgl_home = Path(os.environ.get("PGL_HOME", "~/.persona-growth-loop")).expanduser().resolve()
    config_path = Path(args.config) if args.config else Path(__file__).resolve().parents[1] / "config" / f"growth-{args.face}.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"tripwire config error: {exc}")
        return 1
    run_date = args.date or datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    digest = Digest(pgl_home, run_date)
    exceeded = inspect(get_profile(args.face), pgl_home, config, digest)
    digest.ensure_line()
    return 1 if exceeded else 0


if __name__ == "__main__":
    raise SystemExit(main())
