"""Shared launch form for the persona engine CLI.

The CLI script path is validated because it is repository content we execute
directly. `node` intentionally remains a bare PATH lookup with no shape check:
production commonly resolves Homebrew's interpreter through a symlink, so the
strict leaf-shape check we apply to the script would reject the platform
interpreter, and a missing interpreter already fails closed.

The script shape check also stops at the leaf component. The persona CLI lives
inside a git-managed clone whose contents upstream already controls, so a
symlinked parent within that trusted clone is not an escalation over the
existing trust boundary.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


CLI_RELATIVE_PARTS = ("packages", "core", "bin", "persona")
NODE_COMMAND = "node"


class PersonaCliError(Exception):
    pass


def persona_cli_path(clone: Path) -> Path:
    return clone.joinpath(*CLI_RELATIVE_PARTS)


def persona_argv(clone: Path, command: str, install_root: Path) -> tuple[str, ...]:
    """Build the persona CLI argv.

    The install root must be absolute and is physically resolved before we hand
    it to `--dir`. That makes the caller's cwd irrelevant because Node's lexical
    normalization and the kernel's filesystem resolution cannot land in
    different directories.
    """

    if not install_root.is_absolute():
        raise PersonaCliError(f"persona install_root must be absolute: {install_root}")
    if not clone.is_absolute():
        raise PersonaCliError(f"persona clone must be absolute: {clone}")
    install_root = install_root.resolve()

    cli = persona_cli_path(clone)
    try:
        mode = os.lstat(cli).st_mode
    except FileNotFoundError as exc:
        raise PersonaCliError(f"persona CLI missing: {cli}") from exc
    except OSError as exc:
        raise PersonaCliError(f"persona CLI unavailable: {cli}: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise PersonaCliError(f"persona CLI has unsafe shape: {cli}")
    if not stat.S_ISREG(mode):
        raise PersonaCliError(f"persona CLI has unsafe shape: {cli}")

    return (NODE_COMMAND, str(cli), command, "--dir", str(install_root))
