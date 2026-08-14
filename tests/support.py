import sys
import tempfile


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
