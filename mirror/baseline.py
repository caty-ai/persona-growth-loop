"""Explicit eval-probe baseline recorder."""

import argparse
import os
from pathlib import Path
from typing import Dict, Optional, Sequence

from growthlane.notify import Digest

from .common import (
    REPO,
    MirrorError,
    atomic_json,
    atomic_write,
    ensure_private_dir,
    injected_bytes,
    load_configs,
    manifest_for,
    mirror_lock,
    parse_run_date,
)
from .probes import ProbeError, run_probes


def _write_block(pgl_home: Path, face: str, files: Dict[str, bytes]) -> None:
    root = ensure_private_dir(pgl_home / "mirror" / "probe-baseline" / face / "block")
    file_root = ensure_private_dir(root / "files")
    for name, payload in sorted(files.items()):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise MirrorError(f"unsafe block artifact name: {name}")
        atomic_write(file_root / relative, payload)
    atomic_json(root / "manifest.json", manifest_for(files))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Record the drift-mirror eval baseline")
    parser.add_argument("face", choices=("alpha", "luca"))
    parser.add_argument("--date", help="JST run date (YYYY-MM-DD)")
    parser.add_argument("--config-dir", default=str(REPO / "config"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        run_date = parse_run_date(args.date)
    except MirrorError as exc:
        parser.error(str(exc))
    pgl_home = Path(os.environ.get("PGL_HOME", "~/.persona-growth-loop")).expanduser().resolve()
    digest = Digest(pgl_home, run_date)
    output = pgl_home / "mirror" / "probe-baseline" / f"{args.face}.json"
    with mirror_lock(
        pgl_home, lambda message: digest.emit(f"[RED] {args.face}: {message}")
    ) as acquired:
        if not acquired:
            return 1
        try:
            if (output.is_file() or output.is_symlink()) and not args.force:
                raise MirrorError("probe baseline already exists; use --force to replace it")
            growth, mirror, profile = load_configs(args.face, Path(args.config_dir))
            files, warning = injected_bytes(profile, pgl_home, growth)
            if warning or not files:
                detail = warning or "injected block is empty"
                digest.emit(f"[RED] {args.face}: probe baseline UNAVAILABLE: {detail}")
                return 1
            if not mirror["responder_argv"] or not mirror["scorer_argv"]:
                digest.emit("[WARN] probes skipped: adapters not configured")
                return 0
            result = run_probes(
                files,
                profile.persona_name,
                mirror["responder_argv"],
                mirror["scorer_argv"],
            )
            _write_block(pgl_home, args.face, files)
            record = {
                "recorded_at": run_date,
                "corpus": result["corpus"],
                "corpus_sha256": result["corpus_sha256"],
                "scored_total": result["scored_total"],
                "pushback_retention": result["pushback_retention"],
                "per_probe": result["per_probe"],
            }
            atomic_json(output, record)
            reliability = "reliable" if result["reliable"] else "UNRELIABLE"
            digest.emit(
                f"{args.face}: mirror baseline recorded retention={result['pushback_retention']} "
                f"n={result['scored_total']} {reliability}"
            )
            return 0
        except ProbeError as exc:
            digest.emit(f"[RED] {args.face}: probe baseline failed closed: {exc}")
            return 1
        except Exception as exc:
            digest.emit(f"[RED] {args.face}: mirror baseline failed: {exc}")
            return 1
        finally:
            digest.ensure_line()


if __name__ == "__main__":
    raise SystemExit(main())
