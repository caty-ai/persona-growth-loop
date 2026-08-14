import sys
import tempfile


MACOS_RSYNC_CLIENT_SKIP_REASON = "production sender is the macOS orchestrator; Linux client rsync (samba) emits a --server capability token the dispatcher's fixed allowlist intentionally rejects"


def canonical_temporary_directory() -> tempfile.TemporaryDirectory:
    """Temp dir whose path is already canonical (raw == resolve()).

    On macOS the default temp root lives under /var (a symlink to
    /private/var), which breaks tests that compare raw fixture paths with
    resolve()d production output; /private/tmp is already canonical there.
    On other platforms the default temp root is already canonical.
    """
    if sys.platform == "darwin":
        return tempfile.TemporaryDirectory(dir="/private/tmp")
    return tempfile.TemporaryDirectory()
