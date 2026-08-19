# UCD corpus regeneration

The matching guard needs a deterministic review corpus for characters that can
disappear during canonicalization. `growthlane/guard.py` therefore checks in
`_DEFAULT_IGNORABLE_RANGES`, the Unicode 16.0.0
`Default_Ignorable_Code_Point` property (4,174 codepoints), and adds 12 known
blank/nonprinting stragglers. Their union is the 4,186-codepoint
`IGNORABLE_CORPUS`. The checked-in enumeration makes review and tests independent
of whichever Unicode Character Database (UCD) happens to ship with the Python
interpreter running them.

Regenerate when the family deliberately moves to a new Unicode version, or when
there is reason to suspect transcription drift between the published property
and `_DEFAULT_IGNORABLE_RANGES`. Do not regenerate merely because the host Python
uses a different UCD: runtime admission is expected to fail closed in that case.

## Verify the pinned source

Run from the repository root:

```sh
bin/pgl-ucd-corpus verify
```

With no mode, `verify` is the default, so `bin/pgl-ucd-corpus` is equivalent.
The command downloads only this versioned source:

```text
https://www.unicode.org/Public/16.0.0/ucd/DerivedCoreProperties.txt
```

It requires this SHA-256 before parsing:

```text
39d35161f2954497f69e08bdb9e701493f476a3d30222de20028feda36c1dabd
```

Success reports the stragglers and review-corpus total separately, then ends
with:

```text
match: 4174 codepoints in 17 ranges
```

A network failure, checksum mismatch, malformed property range, wrong 4,174
property total, wrong 4,186 review-corpus total, or range mismatch exits
nonzero. A range mismatch prints a unified diff. Treat every mismatch as a
fail-closed finding: do not edit `guard.py` until the source, parser result, and
each changed boundary in the diff are understood and reviewed.

## Verify from an offline copy

Fetch and retain the exact pinned artifact while online, then pass its path to
`--from` later. The local file receives the same checksum validation as a live
download.

```sh
PGL_UCD_FILE=/absolute/path/to/DerivedCoreProperties-16.0.0.txt
python3 - "$PGL_UCD_FILE" <<'PY'
import sys
import urllib.request

url = "https://www.unicode.org/Public/16.0.0/ucd/DerivedCoreProperties.txt"
urllib.request.urlretrieve(url, sys.argv[1])
PY
bin/pgl-ucd-corpus verify --from "$PGL_UCD_FILE"
```

For a machine that is already offline, copy the previously downloaded file to
that path and run only the final command.

## Emit and compare the tuple

`emit` writes the complete regenerated tuple literal to stdout. It never edits
`guard.py`.

```sh
PGL_UCD_FILE=/absolute/path/to/DerivedCoreProperties-16.0.0.txt
PGL_EMIT_FILE=$(mktemp "${TMPDIR:-/tmp}/pgl-ucd-ranges.XXXXXX")
PGL_GUARD_FILE=$(mktemp "${TMPDIR:-/tmp}/pgl-guard-ranges.XXXXXX")
bin/pgl-ucd-corpus emit --from "$PGL_UCD_FILE" > "$PGL_EMIT_FILE"
sed -n '/^_DEFAULT_IGNORABLE_RANGES = (/,/^)/p' growthlane/guard.py > "$PGL_GUARD_FILE"
head -5 "$PGL_EMIT_FILE"
cmp "$PGL_EMIT_FILE" "$PGL_GUARD_FILE" && echo "emit matches guard.py"
rm "$PGL_EMIT_FILE" "$PGL_GUARD_FILE"
```

When investigating drift, preserve the emitted file and use `diff -u` instead
of removing it. Pasting is appropriate only after the mismatch has been
explained and the Unicode-version move has been approved.

## Move the version as one change

The corpus version is a three-way coupling and must move in one PR:

1. Update the script's source URL, SHA-256, and expected property count, then
   use `emit` to produce the candidate ranges. Keep the current review-corpus
   count during this intermediate generation step.
2. Review the range diff and its overlap with the 12 stragglers. Paste the
   approved tuple into `guard.py`, then update the script's expected
   review-corpus count if that union changed.
3. Set `IGNORABLE_CORPUS_UNICODE_VERSION` to that same UCD version.
4. Require the CPython minor whose bundled `unicodedata.unidata_version` is
   exactly equivalent, and update all README badges and requirement rows.

For the current pin, CPython 3.14.x ships UCD 16.0.0. The honest requirement is
therefore **Python 3.14.x (pinned to UCD 16.0.0)**, not “Python 3.14+”. A future
Python minor may bundle a different UCD and is not admitted merely because its
version number is greater.

After any approved move, repeat the live verification, offline verification,
emit comparison, and `make test` before merging.
