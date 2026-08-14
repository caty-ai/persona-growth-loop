"""Read-only soul-baseline verification and the explicit baseline writer."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Mapping

from .faces import ALPHA_SOUL_SECTION_SPECS, FaceProfile, SoulSectionSpec


class SoulError(RuntimeError):
    pass


_MATCH_COUNT_HINT = (
    "(fix CLAUDE.md so it appears exactly once, or change the code-owned spec — R12a)"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SoulError(f"cannot hash soul file: {path}") from exc
    return digest.hexdigest()


def _read_normalized_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError) as exc:
        raise SoulError(f"cannot read soul file: {path}") from exc


def _newline_delimited_lines(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _extract_digest_and_coverage(
    path: Path, spec: SoulSectionSpec
) -> tuple[str, dict[str, object]]:
    lines = _newline_delimited_lines(_read_normalized_text(path))
    fragments: list[str] = []
    sections: list[dict[str, object]] = []
    for prefix in spec.heading_prefixes:
        matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
        if len(matches) != 1:
            raise SoulError(
                f"soul heading match count must be 1 for {prefix!r}: {len(matches)} "
                f"{_MATCH_COUNT_HINT}"
            )
        start = matches[0]
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if re.match(r"^#{1,3}(?:[ \t]|$)", lines[index]):
                end = index
                break
        section_lines = lines[start:end]
        if not "".join(section_lines[1:]).strip():
            raise SoulError(f"soul section body is empty for {prefix!r}")
        fragment = "".join(section_lines)
        fragments.append(fragment)
        sections.append(
            {
                "prefix": prefix,
                "lines": len(section_lines),
                "bytes": len(fragment.encode("utf-8")),
            }
        )
    for marker in spec.line_markers:
        matches = [line for line in lines if marker in line]
        if len(matches) != 1:
            raise SoulError(
                f"soul line marker match count must be 1 for {marker!r}: {len(matches)} "
                f"{_MATCH_COUNT_HINT}"
            )
        fragments.append(matches[0])
    payload = "\n".join(fragments).encode("utf-8")
    coverage: dict[str, object] = {
        "sections": sections,
        "markers": len(spec.line_markers),
    }
    return hashlib.sha256(payload).hexdigest(), coverage


def _extract_hash(path: Path, spec: SoulSectionSpec) -> str:
    return _extract_digest_and_coverage(path, spec)[0]


def _valid_coverage(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"sections", "markers"}:
        return False
    markers = value.get("markers")
    sections = value.get("sections")
    if (
        not isinstance(markers, int)
        or isinstance(markers, bool)
        or markers < 0
        or not isinstance(sections, list)
        or not sections
    ):
        return False
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"prefix", "lines", "bytes"}:
            return False
        prefix = section.get("prefix")
        line_count = section.get("lines")
        byte_count = section.get("bytes")
        if (
            not isinstance(prefix, str)
            or not prefix
            or not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or line_count < 1
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 1
        ):
            return False
    return True


def _extract_spec(
    profile: FaceProfile,
    path: Path,
    extract: str | None = None,
) -> SoulSectionSpec | None:
    specs = ALPHA_SOUL_SECTION_SPECS if profile.name == "alpha" else ()
    if extract is not None:
        matches = [spec for spec in specs if spec.extract == extract]
        if len(matches) != 1:
            raise SoulError(f"unknown soul extraction method: {extract}")
        spec = matches[0]
        if path != Path(spec.path).expanduser().resolve():
            raise SoulError(f"soul extraction path mismatch for {extract}: {path}")
        return spec
    matches = [spec for spec in specs if path == Path(spec.path).expanduser().resolve()]
    return matches[0] if len(matches) == 1 else None


def overlay_home_pin(profile: FaceProfile, pgl_home: Path) -> Path:
    return pgl_home.expanduser().resolve() / "soul-baseline" / f"{profile.name}.overlay-home"


def read_overlay_home_pin(profile: FaceProfile, pgl_home: Path) -> str | None:
    path = overlay_home_pin(profile, pgl_home)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SoulError(f"invalid overlay-home pin for {profile.name}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"overlay_home"}
        or not isinstance(value.get("overlay_home"), str)
        or not value["overlay_home"]
    ):
        raise SoulError(f"invalid overlay-home pin for {profile.name}")
    return value["overlay_home"]


def write_overlay_home_pin(profile: FaceProfile, pgl_home: Path, overlay_home: str) -> Path:
    path = overlay_home_pin(profile, pgl_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = (
        json.dumps({"overlay_home": overlay_home}, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _validate_soul_path(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    resolved: Path,
) -> None:
    overlay_home = profile.resolve_home(pgl_home, config)
    roots = profile.resolved_soul_roots(config)
    if not any(_inside(resolved, root) for root in roots):
        raise SoulError(f"baseline path is outside declared soul roots: {resolved}")
    # The mutable overlay planes are never valid soul anchors. Luca's declared
    # pack roots are repo-relative, so reject their overlay/growth subtrees
    # explicitly while allowing the code-owned pack soul locations.
    for rel in profile.allowlist:
        target = (overlay_home / rel).resolve()
        if resolved == target:
            raise SoulError(f"baseline path is inside overlay home: {resolved}")
    for rel in profile.expected_dirs:
        mutable_root = (overlay_home / rel).resolve()
        if _inside(resolved, mutable_root):
            raise SoulError(f"baseline path is inside overlay home: {resolved}")


def verify_manifest(
    profile: FaceProfile, pgl_home: Path, config: Mapping[str, object]
) -> list[dict[str, object]]:
    path = profile.baseline_manifest(pgl_home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SoulError(f"missing or invalid soul baseline for {profile.name}") from exc
    if not isinstance(value, dict) or set(value) != {"overlay_home", "files"}:
        raise SoulError(
            f"soul baseline lacks overlay-home pin for {profile.name}; "
            "re-run bin/pgl-baseline"
        )
    recorded_home = value["overlay_home"]
    expected_home = str(profile.resolve_home(pgl_home, config))
    if not isinstance(recorded_home, str) or recorded_home != expected_home:
        raise SoulError(
            f"overlay home mismatch for {profile.name}: recorded={recorded_home!r} "
            f"resolved={expected_home!r}; re-run bin/pgl-baseline only after verifying the intended root"
        )
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise SoulError(f"empty soul baseline for {profile.name}")
    result: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) not in (
            {"path", "sha256"},
            {"path", "sha256", "extract"},
            {"path", "sha256", "extract", "coverage"},
        ):
            raise SoulError(f"invalid soul baseline entry for {profile.name}")
        raw_path, expected = item["path"], item["sha256"]
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            raise SoulError(f"invalid soul baseline entry for {profile.name}")
        resolved = Path(raw_path).expanduser().resolve()
        _validate_soul_path(profile, pgl_home, config, resolved)
        extract = item.get("extract")
        if extract is not None and not isinstance(extract, str):
            raise SoulError(f"invalid soul baseline entry for {profile.name}")
        has_coverage = "coverage" in item
        coverage = item.get("coverage")
        if has_coverage and (extract is None or not _valid_coverage(coverage)):
            raise SoulError(f"invalid soul baseline entry for {profile.name}")
        spec = _extract_spec(profile, resolved, extract) if extract is not None else None
        actual = _extract_hash(resolved, spec) if spec is not None else sha256_file(resolved)
        if actual != expected:
            raise SoulError(f"soul hash mismatch: {resolved}")
        verified: dict[str, object] = {"path": str(resolved), "sha256": expected}
        if has_coverage:
            verified["coverage"] = coverage
        result.append(verified)
    return result


def write_manifest(
    profile: FaceProfile,
    pgl_home: Path,
    config: Mapping[str, object],
    files: list[Path],
) -> Path:
    overlay_home = profile.resolve_home(pgl_home, config)
    if not (overlay_home / ".git").exists():
        raise SoulError(f"overlay home is not a git repository: {overlay_home}")
    missing = [rel for rel in profile.expected_dirs if not (overlay_home / rel).is_dir()]
    if missing:
        raise SoulError(f"overlay home structure missing for {profile.name}: {','.join(missing)}")
    records: list[dict[str, object]] = []
    for supplied in files:
        resolved = supplied.expanduser().resolve()
        _validate_soul_path(profile, pgl_home, config, resolved)
        if not resolved.is_file():
            raise SoulError(f"baseline path is not a file: {resolved}")
        spec = _extract_spec(profile, resolved)
        if profile.name == "alpha" and spec is None:
            raise SoulError(
                f"baseline path is not in the code-owned alpha soul file set: {resolved}"
            )
        record: dict[str, object] = {"path": str(resolved)}
        if spec is not None:
            extracted_hash, coverage = _extract_digest_and_coverage(resolved, spec)
            record["sha256"] = extracted_hash
            record["extract"] = spec.extract
            record["coverage"] = coverage
        else:
            record["sha256"] = sha256_file(resolved)
        records.append(record)
    if not records:
        raise SoulError("at least one soul file is required")
    records.sort(key=lambda item: str(item["path"]))
    path = profile.baseline_manifest(pgl_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    manifest = {"overlay_home": str(overlay_home), "files": records}
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path
