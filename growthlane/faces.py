"""Code-owned face profiles and immutable overlay write scopes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


ALPHA_SOUL_ROOTS = ("~/.claude",)
LUCA_SOUL_ROOTS = ("persona-engine/manifest.yml", "persona-engine/catalogs")


@dataclass(frozen=True)
class FaceProfile:
    name: str
    persona_name: str
    engine: bool
    overlay_home: Callable[[Path, Mapping[str, object]], Path]
    allowlist: tuple[str, ...]
    ledger_path: str
    blocklist_path: str
    render_files: Mapping[str, str]
    expected_dirs: tuple[str, ...]
    soul_roots: tuple[str, ...]

    def resolve_home(self, pgl_home: Path, config: Mapping[str, object]) -> Path:
        return self.overlay_home(pgl_home, config).expanduser().resolve()

    def resolve_staging_root(self, config: Mapping[str, object]) -> Path | None:
        if not self.engine:
            return None
        value = config.get("staging_root")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{self.name} config requires non-empty staging_root")
        # Keep the lexical path here so callers can reject a symlink at the
        # configured root before resolving it.
        return Path(value).expanduser().absolute()

    def baseline_manifest(self, pgl_home: Path) -> Path:
        return pgl_home.expanduser().resolve() / "soul-baseline" / f"{self.name}.manifest"

    def resolved_soul_roots(self, config: Mapping[str, object]) -> tuple[Path, ...]:
        if self.name == "alpha":
            return tuple(Path(item).expanduser().resolve() for item in self.soul_roots)
        home = self.resolve_home(Path("."), config)
        return tuple((home / item).resolve() for item in self.soul_roots)


def _alpha_home(pgl_home: Path, _config: Mapping[str, object]) -> Path:
    return pgl_home / "faces" / "alpha"


def _luca_home(_pgl_home: Path, config: Mapping[str, object]) -> Path:
    value = config.get("overlay_home_root")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("luca config requires non-empty overlay_home_root")
    return Path(value)


PROFILES: Mapping[str, FaceProfile] = {
    "alpha": FaceProfile(
        name="alpha",
        persona_name="アルファ",
        engine=False,
        overlay_home=_alpha_home,
        allowlist=("overlay.md", "overlay-ledger.yml", "blocklist.txt"),
        ledger_path="overlay-ledger.yml",
        blocklist_path="blocklist.txt",
        render_files={"combined": "overlay.md"},
        expected_dirs=(),
        soul_roots=ALPHA_SOUL_ROOTS,
    ),
    "luca": FaceProfile(
        name="luca",
        persona_name="ルカ",
        engine=True,
        overlay_home=_luca_home,
        allowlist=(
            "persona-engine/catalogs/overlay/candidates.txt",
            "persona-engine/catalogs/overlay/adopted.txt",
            "growth/overlay-ledger.yml",
            "growth/blocklist.txt",
        ),
        ledger_path="growth/overlay-ledger.yml",
        blocklist_path="growth/blocklist.txt",
        render_files={
            "candidates": "persona-engine/catalogs/overlay/candidates.txt",
            "adopted": "persona-engine/catalogs/overlay/adopted.txt",
        },
        expected_dirs=("persona-engine/catalogs/overlay", "growth"),
        soul_roots=LUCA_SOUL_ROOTS,
    ),
}


def get_profile(face: str) -> FaceProfile:
    try:
        return PROFILES[face]
    except KeyError as exc:
        raise ValueError(f"unknown face: {face}") from exc
